"""Optional Claude Code SessionStart hook: reports which conversation each claude process is in."""
import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from .clock import now_stamp
from .fsutil import PRIVATE_FILE, atomic_write, private_dir, try_chmod, write_target

HOOK_MARK = "sampan hook session-start"
_SUFFIX = " hook session-start"
# when the path contains spaces the command gets quoted ('…/sampan' hook session-start); it must still be recognised as ours
_OURS_RE = re.compile(r"sampan['\"]? hook session-start\s*$")


@dataclass(frozen=True)
class HookRecord:
    pid: int
    session_id: str
    cwd: str
    pane: object  # str | None
    config_dir: str
    source: str
    at: str


def _claude_ancestor(procs, start_pid):
    seen = set()
    cur = procs.get(start_pid)
    while cur is not None and cur.pid not in seen:
        if cur.comm == "claude" and not cur.zombie:
            return cur.pid
        seen.add(cur.pid)
        cur = procs.get(cur.ppid)
    return None


def run_session_start(stdin, env, *, state_dir, procs_fn, now, start_pid=None):
    """Write state_dir/hooks/<claude pid>.json. Never raises and never prints (stdout becomes model context)."""
    try:
        data = json.loads(stdin.read())
        if not isinstance(data, dict) or not isinstance(data.get("session_id"), str) or not data["session_id"]:
            return
        pid = _claude_ancestor(procs_fn(), os.getpid() if start_pid is None else start_pid)
        if pid is None:
            return
        rec = {"pid": pid, "session_id": data["session_id"], "cwd": data.get("cwd", ""),
               "pane": env.get("TMUX_PANE"), "config_dir": env.get("CLAUDE_CONFIG_DIR", ""),
               "source": data.get("source", ""), "at": now()}
        private_dir(state_dir)
        d = private_dir(Path(state_dir) / "hooks")
        for f in d.glob("*.json"):
            try_chmod(f, PRIVATE_FILE)  # records an older version left readable by others
        atomic_write(d / f"{pid}.json", json.dumps(rec, ensure_ascii=False), mode=PRIVATE_FILE)
    except BaseException:
        pass


def read_hook_records(state_dir, alive):
    d = Path(state_dir) / "hooks"
    if not d.is_dir():
        return {}
    records = {}
    for f in d.glob("*.json"):
        if not f.stem.isdigit():
            continue
        pid = int(f.stem)
        if pid not in alive:
            try:
                f.unlink()
            except OSError:
                pass
            continue
        try:
            raw = json.loads(f.read_text())
            records[pid] = HookRecord(pid, raw["session_id"], raw.get("cwd", ""), raw.get("pane"),
                                      raw.get("config_dir", ""), raw.get("source", ""), raw.get("at", ""))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return records


def _is_ours(command):
    return isinstance(command, str) and (HOOK_MARK in command or bool(_OURS_RE.search(command)))


def _settings_path(profile):
    return Path(profile.config_dir) / "settings.json"


def _load(path):
    """(data, ok): a missing file is an empty dict; unreadable or non-object JSON is not ok."""
    if not path.exists():
        return {}, True
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}, False
    return (data, True) if isinstance(data, dict) else ({}, False)


def _groups(data):
    hooks = data.get("hooks")
    groups = hooks.get("SessionStart") if isinstance(hooks, dict) else None
    return groups if isinstance(groups, list) else []


def _our_commands(data):
    for g in _groups(data):
        for h in g.get("hooks", []) if isinstance(g, dict) and isinstance(g.get("hooks"), list) else []:
            if isinstance(h, dict) and _is_ours(h.get("command")):
                yield h["command"]


def _command_exists(cmd):
    head = cmd.split(_SUFFIX)[0] if _SUFFIX in cmd else cmd
    try:
        parts = shlex.split(head)
    except ValueError:
        parts = []
    return bool(parts) and Path(parts[0]).exists()


def hook_status(profile):
    data, ok = _load(_settings_path(profile))
    commands = list(_our_commands(data)) if ok else []
    if not commands:
        return "absent"
    if any(_command_exists(cmd) for cmd in commands):
        return "installed"
    return "stale"


def _write(path, data):
    """Back up the current file, then write atomically (through a symlink, keeping the mode)."""
    target = write_target(path)
    if target.exists():
        target.with_name(f"settings.json.bak-sampan-{now_stamp()}").write_bytes(target.read_bytes())
    atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def _try_write(path, data):
    try:
        _write(path, data)
    except OSError:
        return False
    return True


def install_hook(profile, sampan_path):
    path = _settings_path(profile)
    data, ok = _load(path)
    if not ok:
        return False
    commands = list(_our_commands(data))
    if commands:
        return True
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict) or not isinstance(hooks.setdefault("SessionStart", []), list):
        return False
    hooks["SessionStart"].append(
        {"hooks": [{"type": "command", "command": f"{shlex.quote(str(sampan_path))}{_SUFFIX}"}]})
    return _try_write(path, data)


def _strip(data, match):
    """Remove the hook entries whose command satisfies match from data (in memory), dropping emptied containers."""
    kept = []
    for g in _groups(data):
        if isinstance(g, dict) and isinstance(g.get("hooks"), list):
            inner = [h for h in g["hooks"] if not (isinstance(h, dict) and match(h.get("command")))]
            if not inner:
                continue
            g = {**g, "hooks": inner}
        kept.append(g)
    if kept:
        data["hooks"]["SessionStart"] = kept
    else:
        del data["hooks"]["SessionStart"]
        if not data["hooks"]:
            del data["hooks"]


def _drop(path, data, match):
    _strip(data, match)
    return _try_write(path, data)


def uninstall_hook(profile):
    path = _settings_path(profile)
    data, ok = _load(path)
    if not ok or not any(_our_commands(data)):
        return False
    return _drop(path, data, _is_ours)
