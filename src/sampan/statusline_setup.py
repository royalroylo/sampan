"""sampan statusline install / uninstall: wrap (or unwrap) the statusLine command in each Claude profile's settings.json."""
import json
import shlex
import shutil
from pathlib import Path

from . import actionlog, fsutil
from .clock import now_stamp


def _settings(profile):
    return Path(profile.config_dir) / "settings.json"


def _read(profile):
    """(data, None) for a readable settings.json (or a missing one: {}); (None, reason) when it can't be trusted."""
    path = _settings(profile)
    if not path.exists():
        return {}, None
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        return None, f"❌ {profile.name}：{path} 不是合法的 JSON，沒改"
    sl = data.get("statusLine")
    if sl is not None and not (isinstance(sl, dict) and isinstance(sl.get("command"), str)):
        return None, f"❌ {profile.name}：{path} 的 statusLine 格式不認得，沒改"
    return data, None


def _command(data):
    sl = data.get("statusLine")
    return sl["command"] if isinstance(sl, dict) else None


def _tokens(cmd):
    """shlex tokens when cmd already goes through `<…/sampan> statusline`, else None."""
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return None
    if len(toks) >= 2 and Path(toks[0]).name == "sampan" and toks[1] == "statusline":
        return toks
    return None


def _first_token_end(cmd):
    """Index just past the first shell word of cmd (quotes and backslashes respected)."""
    i, quote = 0, None
    while i < len(cmd):
        c = cmd[i]
        if quote:
            if c == quote:
                quote = None
            elif c == "\\" and quote == '"':
                i += 1
        elif c in "'\"":
            quote = c
        elif c == "\\":
            i += 1
        elif c.isspace():
            break
        i += 1
    return min(i, len(cmd))


def _install_command(cmd, sampan_path):
    toks = _tokens(cmd) if cmd else None
    if toks is not None:
        if toks[0] == str(sampan_path):
            return cmd
        cmd = cmd.lstrip()
        return shlex.quote(str(sampan_path)) + cmd[_first_token_end(cmd):]  # an older install location: only the path changes
    if not cmd:
        return f"{shlex.quote(str(sampan_path))} statusline"
    return f"{shlex.quote(str(sampan_path))} statusline -- sh -c {shlex.quote(cmd)}"


_NOT_OURS = object()


def _uninstall_command(cmd):
    """The original command, None when the statusLine key should go, _NOT_OURS when sampan is not in it."""
    toks = _tokens(cmd) if cmd else None
    if toks is None:
        return _NOT_OURS
    rest = toks[2:]
    if not rest:
        return None
    if rest[0] != "--":
        return _NOT_OURS
    rest = rest[1:]
    if not rest:
        return None
    if len(rest) == 3 and rest[:2] == ["sh", "-c"]:
        return rest[2]
    return shlex.join(rest)


def planned(cfg, profile, sampan_path):
    """(current command or None, command after install or None)."""
    data, _why = _read(profile)
    cmd = _command(data or {})
    return cmd, _install_command(cmd, sampan_path)


def _write(profile, data, new):
    """Set (str) or delete (None) the statusLine command; backup first; only that key changes. -> error text or None."""
    path = _settings(profile)
    data = dict(data)
    if new is None:
        data.pop("statusLine", None)
    else:
        old = data.get("statusLine") if isinstance(data.get("statusLine"), dict) else {}
        data["statusLine"] = {"type": "command", "command": new,
                              **{k: v for k, v in old.items() if k not in ("type", "command")}}
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    try:
        if json.loads(text) != data:
            return f"❌ {profile.name}：寫回的內容驗證不過，沒改"
        if path.exists():
            shutil.copy2(path, path.with_name(f"settings.json.bak-{now_stamp()}"))
        fsutil.atomic_write(path, text)
    except (OSError, ValueError) as e:
        return f"❌ {profile.name}：寫不進 {path}（{e}）"
    return None


def _run(cfg, out, ask, profiles, *, yes, compute, same_note, label):
    out = actionlog.tee(cfg, label, out)
    rc, todo = 0, []
    for p in profiles:
        if not Path(p.config_dir).is_dir():
            out(f"❌ {p.name}：{p.config_dir} 不存在，略過")
            rc = 1
            continue
        data, why = _read(p)
        if why:
            out(why)
            rc = 1
            continue
        cur = _command(data)
        new = compute(cur)
        if new == cur:
            out(f"  {p.name}：{same_note}")
            continue
        out(f"  {p.name}：{cur or '（沒有）'} → {new or '（移除）'}")
        todo.append((p, data, new))
    if not todo:
        out("沒有要變更的")
        return rc
    if not yes:
        try:
            answer = ask("確定？(y/N): ")
        except EOFError:
            answer = ""
        if (answer or "").strip().lower() != "y":
            out("已取消")
            return rc
    for p, data, new in todo:
        err = _write(p, data, new)
        if err:
            out(err)
            rc = 1
        else:
            out(f"✔ {p.name}：已更新 {_settings(p)}")
    return rc


def install(cfg, out, ask, profiles, sampan_path, *, yes=False):
    if Path(sampan_path).name != "sampan":  # e.g. run as `python -m sampan`: a status line pointing there would break
        actionlog.tee(cfg, "statusline install", out)(f"❌ 找不到 sampan 執行檔（{sampan_path}），沒改")
        return 1
    return _run(cfg, out, ask, profiles, yes=yes, label="statusline install", same_note="已經接上，不用改",
                compute=lambda cur: _install_command(cur, sampan_path))


def uninstall(cfg, out, ask, profiles, *, yes=False):
    def compute(cur):
        r = _uninstall_command(cur)
        return cur if r is _NOT_OURS else r
    return _run(cfg, out, ask, profiles, yes=yes, label="statusline uninstall", same_note="沒有接上，不用改",
                compute=compute)
