"""Apply the plugin screen's plan: run `claude plugin ...` per profile, then write config.toml once, only for the profiles that succeeded."""
import os
import shlex
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import config_edit
from .clock import now_stamp
from .config import strip_credentials, valid_source
from .fsutil import atomic_write
from .plugin_view import _mkt, newest, snapshot
from .plugins import read_state
from .status import State


@dataclass(frozen=True)
class Applied:
    changed: list  # profiles where a command ran and succeeded (they need a restart)
    failed: dict  # profile -> error message
    backup: Path | None


def _edit(text, pid, ch, current):
    """Text with ch's list edit applied (unchanged when the list already matches); None when the edit can't be done."""
    if ch.listed == (pid in current):
        return text
    return (config_edit.add_plugin if ch.listed else config_edit.remove_plugin)(text, ch.profile, pid)


def _expected(lists, pid, changes):
    out = {n: list(v) for n, v in lists.items()}
    for ch in changes:
        cur = out.get(ch.profile, [])
        if ch.listed and pid not in cur:
            out[ch.profile] = cur + [pid]
        elif not ch.listed:
            out[ch.profile] = [x for x in cur if x != pid]
    return out


def _build(text, pid, changes, mkt, source):
    """Original text + the list edits of `changes` (+ the marketplace entry when source); None when it fails the check."""
    lists = config_edit.plugin_lists(text)
    if lists is None:
        return None
    new = text
    if source:
        new = config_edit.add_marketplace(new, mkt, source)
    for ch in changes:
        if new is None or ch.profile not in lists:
            return None
        new = _edit(new, pid, ch, lists[ch.profile])
    if new is None or config_edit.plugin_lists(new) != _expected(lists, pid, changes):
        return None
    if source and tomllib.loads(new).get("marketplaces", {}).get(mkt) != source:
        return None
    return new


def _write_config(cfg, original, final, stamp):
    """Back up the file as it is on disk (mode kept), then write final atomically; the backup path, or None if unchanged."""
    if final == original:
        return None
    backup = cfg.path.with_name(f"{cfg.path.name}.bak-{stamp()}")
    shutil.copy2(cfg.path, backup)
    atomic_write(cfg.path, final)
    return backup


def _commands(pid, ch, source):
    cmds = []
    if ch.add_marketplace:
        cmds.append(["claude", "plugin", "marketplace", "add", source])
    if ch.install is True:
        cmds.append(["claude", "plugin", "install", pid, "--scope", "user"])
    elif ch.install is False:
        cmds.append(["claude", "plugin", "uninstall", pid, "--scope", "user"])
    return cmds


def apply_changes(cfg, run, out, pid, changes, *, new_source=None, stamp=now_stamp):
    mkt, short = _mkt(pid), pid.split("@", 1)[0]
    text = cfg.path.read_text()  # read now: the file may have been edited since the screen opened
    source = new_source or cfg.marketplaces.get(mkt)
    if (new_source or any(ch.add_marketplace for ch in changes)) and not (
            source and valid_source(source) and strip_credentials(source) == source):
        out(f"❌ {mkt} 沒有可用的來源，什麼都沒做")
        return Applied([], {}, None)
    if _build(text, pid, changes, mkt, new_source) is None:
        out(f"❌ 設定檔無法自動修改（{cfg.path}），什麼都沒做；請手動改後跑 sampan plugins sync")
        return Applied([], {}, None)

    changed, failed, ok = [], {}, []
    for ch in changes:
        prof = cfg.profiles[ch.profile]
        cmds = _commands(pid, ch, source)
        env = prof.env(os.environ)
        err = None
        for c in cmds:
            r = run(c, env=env, cwd=str(cfg.home))
            if r.returncode != 0:
                err = f"{shlex.join(c)} 失敗：{(r.stderr or r.stdout).strip()[:300]}"
                break
        if err:
            failed[ch.profile] = err
            out(f"❌ {ch.profile}：{err}")
            continue
        ok.append(ch)
        if ch.install is True:
            version = read_state(prof).installed.get(pid, "?")
            out(f"✔ {ch.profile}：已安裝 {short} {version[:12]}")
        elif ch.install is False:
            out(f"✔ {ch.profile}：已移除 {short}")
        else:
            out(f"✔ {ch.profile}：清單已更新")
        if cmds:
            changed.append(ch.profile)

    backup = None
    if ok:
        final = _build(text, pid, ok, mkt, new_source)
        if final is None:
            out(f"❌ 設定檔無法自動修改（{cfg.path}），指令已執行；請手動改後跑 sampan plugins sync")
        else:
            backup = _write_config(cfg, text, final, stamp)
            if backup:
                out(f"✔ 設定檔已更新（備份 {backup.name}）")
    if failed:
        out(f"⚠ {', '.join(failed)} 沒有改（設定檔也沒動那幾個 profile）")
    return Applied(changed, failed, backup)


def plan_update(snap, pids=None):
    """Update-only commands per profile: `marketplace update` for the marketplaces of installed plugins, then `plugin update` per plugin.

    Never installs, uninstalls or reads the config lists. Profiles with nothing to do are left out."""
    plan = {}
    for name, st in snap.states.items():
        if name in snap.missing:
            continue
        installed = sorted(p for p in st.installed if pids is None or p in pids)
        mkts = sorted({_mkt(p) for p in installed} & set(st.marketplaces))
        cmds = [["claude", "plugin", "marketplace", "update", m] for m in mkts]
        cmds += [["claude", "plugin", "update", p, "--scope", "user"] for p in installed]
        if installed:
            plan[name] = cmds
    return plan


def update_plugins(cfg, run, out, pids=None):
    """Run plan_update; report only versions that really changed. Returns the profiles with a change."""
    plan = plan_update(snapshot(cfg), pids)
    changed = []
    for name, cmds in plan.items():
        prof = cfg.profiles[name]
        before = read_state(prof).installed
        env = prof.env(os.environ)
        for c in cmds:
            r = run(c, env=env, cwd=str(cfg.home))
            if r.returncode != 0:
                out(f"❌ {name}：{shlex.join(c)} 失敗：{(r.stderr or r.stdout).strip()[:300]}")
        after = read_state(prof).installed
        diffs = [(p, before[p], after[p]) for p in sorted(after) if p in before and before[p] != after[p]]
        for p, b, a in diffs:
            out(f"  {name}：{p.split('@', 1)[0]} {b[:12]} → {a[:12]}")
        if diffs:
            changed.append(name)
    if not changed:
        out("沒有變動")
    return changed


def check_latest(cfg, run, out, snap):
    """Refresh each profile's marketplace clones (network), then read the newest version of every installed plugin."""
    result = {}
    for name, st in snap.states.items():
        if name in snap.missing:
            continue
        prof = cfg.profiles[name]
        env = prof.env(os.environ)
        for m in sorted({_mkt(p) for p in st.installed} & set(st.marketplaces)):
            out(f"▶ {name}：更新 {m} 來源…")
            c = ["claude", "plugin", "marketplace", "update", m]
            r = run(c, env=env, cwd=str(cfg.home))
            if r.returncode != 0:
                out(f"⚠ {name}：{shlex.join(c)} 失敗：{(r.stderr or r.stdout).strip()[:300]}")
        for pid in sorted(st.installed):
            result[(name, pid)] = newest(prof, _mkt(pid), pid.split("@", 1)[0], run)
    return result


def running_counts(rows, profiles):
    """Running Claude sessions per profile (only counts above zero)."""
    counts = {}
    for r in rows:
        if r.agent == "claude" and r.state is State.RUNNING and r.profile in profiles:
            counts[r.profile] = counts.get(r.profile, 0) + 1
    return counts


def restart_hint(counts):
    if not counts:
        return None
    return "要生效需重開 " + "、".join(f"{p} 的 session（{n} 個）" for p, n in counts.items()) \
        + "：按 R 立刻重開，其他鍵稍後"


def restart_profiles(cfg, run, out, ask, profiles, *, own_pane=None, on_prompt=None, stop_fn=None, restore_fn=None):
    """Stop idle sessions of the profiles and restore exactly those; busy / own-pane ones are listed once. Returns the worst rc."""
    from . import restore, session_run  # lazy: session_run imports a lot
    stop_fn = stop_fn or session_run.cmd_stop
    restore_fn = restore_fn or restore.cmd_restore
    worst, left_all = 0, []
    for p in profiles:
        s, left = [], []
        worst = max(worst, stop_fn(cfg, run, out, profile=p, own_pane=own_pane, stopped=s, left=left, timeout=90))
        if s:
            worst = max(worst, restore_fn(cfg, run, out, ask, names=tuple(s), confirm=False, on_prompt=on_prompt))
        left_all += left
    if left_all:
        out("⚠ 這些還在跑舊版，閒下來後再重開：" + "、".join(sorted(set(left_all))))
    return worst
