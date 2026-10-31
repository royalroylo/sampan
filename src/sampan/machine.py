"""This machine's identity: a stable id and a display name in machine.toml, next to the config file."""
import json
import socket
import tomllib
from dataclasses import dataclass
from pathlib import Path

from . import fsutil, ids


@dataclass(frozen=True)
class Machine:
    id: str
    name: str


def path(cfg_path):
    return Path(cfg_path).parent / "machine.toml"


def ref(machine_id, session_id):
    return f"{machine_id}:{session_id}"


def _default_name(hostname):
    try:
        name = hostname()
    except Exception:
        name = ""
    return name.removesuffix(".local") or "unknown"


def _write(p, m):
    fsutil.atomic_write(p, f"id = {json.dumps(m.id)}\nname = {json.dumps(m.name)}\n", mode=fsutil.PRIVATE_FILE,
                        encoding="utf-8")


def load_or_create(cfg_path, *, hostname=socket.gethostname):
    """Read machine.toml, creating it (new id, hostname as name) when absent. Never raises: an unreadable or
    unwritable file gives Machine("", name), an unknown id."""
    p = path(cfg_path)
    fallback = Machine("", _default_name(hostname))
    try:
        if p.exists():
            data = tomllib.loads(p.read_text(encoding="utf-8"))
            mid, name = data.get("id"), data.get("name")
            if isinstance(mid, str) and mid:
                return Machine(mid, name if isinstance(name, str) and name else fallback.name)
            return fallback
        m = Machine(ids.new_id(), fallback.name)
        _write(p, m)
        return m
    except Exception:
        return fallback


def rename(cfg_path, name):
    """Change the display name, keeping the id. Returns the new Machine, or an error string."""
    if not name or any(ord(c) < 32 or ord(c) == 127 for c in name):
        return "機器名稱不可為空，也不可含控制字元"
    cur = load_or_create(cfg_path)
    if not cur.id:
        return "讀不到或寫不了 machine.toml，沒有改名"
    m = Machine(cur.id, name)
    try:
        _write(path(cfg_path), m)
    except OSError as e:
        return f"寫不了 machine.toml：{e}"
    return m
