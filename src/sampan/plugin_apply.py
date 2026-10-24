"""Apply the plugin screen's plan: run `claude plugin ...` per profile, then write config.toml once, only for the profiles that succeeded.

Plugins (apply_changes) and marketplaces (apply_market) share the same rules: env/cwd per profile, one result line per
profile, and one config write through _write_config."""
import os
import shlex
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import actionlog, config_edit
from .clock import now_stamp
from .config import SOURCE_RULE, redact, same_source, strip_credentials, usable_source, valid_source
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


def _loadable(text):
    """load_config's rule that an edit can break: every listed plugin's marketplace is in [marketplaces]."""
    lists, markets = config_edit.plugin_lists(text), _markets(text)
    return lists is not None and markets is not None and all(
        _mkt(x) in markets for pids in lists.values() for x in pids)


def _err(c, r):
    """The line for a failed command; scheme://user:token@ in its output is masked."""
    return f"{shlex.join(c)} 失敗：{redact((r.stderr or r.stdout).strip())[:300]}"


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
    return new if _loadable(new) else None


def _write_config(cfg, out, original, final, stamp, rebuild):
    """Write final once, after a backup of the file as it is on disk (mode kept). Returns the backup path, or None.

    The file is re-read first: if it changed since `original` was read, final = rebuild(fresh text), so a hand edit made
    while the commands ran survives. A None final, or an OSError on backup or write, is reported and nothing is written."""
    try:
        fresh = cfg.path.read_text()
        if fresh != original:
            original, final = fresh, rebuild(fresh)
        if final is None:
            out(f"❌ 設定檔無法自動修改（{cfg.path}），指令已執行；請手動改後跑 sampan plugins sync")
            return None
        if final == original:
            return None
        backup = cfg.path.with_name(f"{cfg.path.name}.bak-{stamp()}")
        shutil.copy2(cfg.path, backup)
        try:
            atomic_write(cfg.path, final)
        except OSError:
            backup.unlink(missing_ok=True)  # a backup only of a write that happened
            raise
    except OSError as e:
        out(f"❌ 設定檔寫入失敗（{e}），指令已執行；請手動改後跑 sampan plugins sync")
        return None
    out(f"✔ 設定檔已更新（備份 {backup.name}）")
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
    out = actionlog.tee(cfg, f"plugin 存檔 {pid}", out)
    mkt, short = _mkt(pid), pid.split("@", 1)[0]
    text = cfg.path.read_text()  # read now: the file may have been edited since the screen opened
    source = new_source or cfg.marketplaces.get(mkt)
    if (new_source or any(ch.add_marketplace for ch in changes)) and not usable_source(source):
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
                err = _err(c, r)
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
        def rebuild(t):
            return _build(t, pid, ok, mkt, new_source)
        backup = _write_config(cfg, out, text, rebuild(text), stamp, rebuild)
    if failed:
        out(f"⚠ {', '.join(failed)} 沒有改（設定檔也沒動那幾個 profile）")
    return Applied(changed, failed, backup)


def refuse_credentials(source):
    """The refusal line for a marketplace source sampan must not pass to claude or write, or None when it is fine."""
    if strip_credentials(source) != source:
        return "❌ 網址裡不要放帳號或 token，認證交給 git 的 credential helper"
    if not valid_source(source):
        return f"❌ 來源要寫成 {SOURCE_RULE}"
    return None


def _ask(ask, prompt):
    try:
        raw = ask(prompt)
    except (EOFError, KeyboardInterrupt):  # end of input or Ctrl-C both count as no
        return None
    return None if raw is None else raw.strip().lower()


def _markets(text):
    try:
        return tomllib.loads(text).get("marketplaces", {})
    except tomllib.TOMLDecodeError:
        return None


def _market_edit(text, name, source):
    """text with [marketplaces] name = source (source None: the line removed); unchanged when already so; None when
    it can't be done safely (parse error, the name points elsewhere, or the edit didn't give the expected table)."""
    before = _markets(text)
    if before is None:
        return None
    if source is None:
        if name not in before:
            return text
        new = config_edit.remove_marketplace(text, name)
    else:
        if name in before:
            return text if same_source(before[name], source) else None
        new = config_edit.add_marketplace(text, name, source)
    after = None if new is None else _markets(new)
    want = {k: v for k, v in before.items() if k != name} | ({} if source is None else {name: source})
    return new if after == want and config_edit.plugin_lists(new) == config_edit.plugin_lists(text) \
        and _loadable(new) else None


def apply_market(cfg, run, out, ask, name, changes, *, source, stamp=now_stamp):
    """Add or remove one marketplace per profile (`claude plugin marketplace add/remove`), then update [marketplaces] once.

    name is None when adding a new source: it is learned from the first profile that added it. source is the source
    to add (None for a marketplace outside the config; such a one can only be removed)."""
    out = actionlog.tee(cfg, f"來源 {name or source}", out)
    text = cfg.path.read_text()
    if any(ch.add for ch in changes):
        refusal = refuse_credentials(source)
        if refusal:
            out(refusal)
            return Applied([], {}, None)

    changed, failed, added = [], {}, []
    lost = False  # an add worked but its name could not be found
    for ch in changes:
        prof = cfg.profiles[ch.profile]
        c = ["claude", "plugin", "marketplace", "add", source] if ch.add else ["claude", "plugin", "marketplace", "remove", name]
        r = run(c, env=prof.env(os.environ), cwd=str(cfg.home))
        if r.returncode != 0:
            failed[ch.profile] = _err(c, r)
            out(f"❌ {ch.profile}：{failed[ch.profile]}")
            continue
        changed.append(ch.profile)
        if not ch.add:
            out(f"✔ {ch.profile}：已移除 {name}")
            continue
        added.append(ch.profile)
        if name is None and not lost:
            name = next((n for n, s in read_state(prof).marketplaces.items() if same_source(s, source)), None)
            if name is None:
                lost = True
                out("❌ 加入後找不到這個來源的名稱，設定檔沒改")
        out(f"✔ {ch.profile}：已加入 {name or source}")

    target = False  # False: no config edit; else the [marketplaces] value to set (None removes the line)
    listed = _markets(text) or {}
    if added and name and not lost:
        theirs = listed.get(name)
        if theirs is None:
            target = source
        elif not same_source(theirs, source):
            out(f"❌ {name} 在設定檔已經指向 {theirs}，設定檔沒改（{'、'.join(added)} 已加入的保留，"
                f"可用 claude plugin marketplace remove {name} 移除）")
    elif name in listed and any(not ch.add and ch.profile in changed for ch in changes) \
            and not any(name in read_state(p).marketplaces for p in cfg.profiles.values()):
        users = [p for p, pids in (config_edit.plugin_lists(text) or {}).items() if any(_mkt(x) == name for x in pids)]
        if users:  # e.g. a profile whose directory is missing: dropping the line would break the config
            out(f"設定檔 [marketplaces] 保留 {name}：{'、'.join(users)} 的清單還有用到它的 plugin")
        elif _ask(ask, f"也從設定檔 [marketplaces] 拿掉 {name}？(y/N): ") == "y":
            target = None

    backup = None
    if target is not False:
        def rebuild(t):
            return _market_edit(t, name, target)
        backup = _write_config(cfg, out, text, rebuild(text), stamp, rebuild)
    if failed:
        out(f"⚠ {', '.join(failed)} 沒有改（設定檔也沒動那幾個 profile）")
    return Applied(changed, failed, backup)


def update_markets(cfg, run, out, names=None):
    """`claude plugin marketplace update <name>` in each profile that has the marketplace (names None: all of them)."""
    out = actionlog.tee(cfg, "來源更新", out)
    for prof in cfg.profiles.values():
        env = prof.env(os.environ)
        for m in sorted(read_state(prof).marketplaces):
            if names is not None and m not in names:
                continue
            c = ["claude", "plugin", "marketplace", "update", m]
            r = run(c, env=env, cwd=str(cfg.home))
            if r.returncode != 0:
                out(f"❌ {prof.name}：{_err(c, r)}")
            else:
                out(f"✔ {prof.name}：{m} 已更新")


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
    out = actionlog.tee(cfg, "plugin 更新", out)
    plan = plan_update(snapshot(cfg), pids)
    changed = []
    for name, cmds in plan.items():
        prof = cfg.profiles[name]
        before = read_state(prof).installed
        env = prof.env(os.environ)
        for c in cmds:
            r = run(c, env=env, cwd=str(cfg.home))
            if r.returncode != 0:
                out(f"❌ {name}：{_err(c, r)}")
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
    out = actionlog.tee(cfg, "檢查新版", out)
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
                out(f"⚠ {name}：{_err(c, r)}")
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
    out = actionlog.tee(cfg, "重開", out)
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
