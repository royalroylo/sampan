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
from .plugin_view import _mkt
from .plugins import read_state


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
