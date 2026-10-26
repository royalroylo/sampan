"""Codex asks the user before a tool runs only when its config.toml says so: `approval_mode = "prompt"` in
`[mcp_servers.sampan.tools.<tool>]`. sampan keeps those tables for its acting tools (ACTING_TOOLS) when
`[mcp] confirm = true` and removes them when it is false. The file is edited as text, so every line that is not one of
those tables (comments, order, blank lines) stays byte for byte; it is backed up first and checked with tomllib.
Only the unquoted header form is recognised (what Codex and sampan write), not `[mcp_servers."sampan".tools."send"]`."""
import os
import stat
import tomllib
from pathlib import Path

from . import fsutil
from .mcp_server import ACTING_TOOLS

PROMPT = "prompt"
NOT_TOML = "Codex config.toml 不是合法的 TOML："
NOT_TABLES = ('Codex config.toml 的 sampan 設定不是用 [mcp_servers.sampan.tools.<工具>] 表寫的，sampan 改不了；'
              '請手動設 approval_mode = "prompt"')


def section(tool: str) -> str:
    return "[mcp_servers.sampan.tools." + tool + "]"


def strip_sections(text: str, tools=ACTING_TOOLS) -> str:
    """text without sampan's tables: each runs from its header line to the next table header. The blank line just
    before the header goes too; blank and comment lines just before the next header stay (they belong to it)."""
    heads = {section(t) for t in tools}
    out, tail, inside = [], [], False
    for line in _lines(text):
        s = line.strip()
        if s.startswith("["):
            if inside:
                out.extend(tail)
                tail, inside = [], False
            if s in heads:
                if out and out[-1].strip() == "":
                    out.pop()
                inside = True
                continue
        elif inside:
            if s == "" or s.startswith("#"):
                tail.append(line)
            else:
                tail = []  # a key of sampan's table: what came before it was part of the table
            continue
        out.append(line)
    return "".join(out + tail)


def _lines(text):
    """text split after each \n only (str.splitlines also splits at \x0c, \u2028…); each line keeps its \r\n or \n."""
    parts = text.split("\n")
    return [p + "\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def with_prompt(text: str, tools=ACTING_TOOLS) -> str:
    """text with one `approval_mode = "prompt"` table per tool at the end (any earlier sampan table removed), in the
    file's own line ending (\r\n when it has any)."""
    nl = "\r\n" if "\r\n" in text else "\n"
    out = strip_sections(text, tools)
    if out and not out.endswith("\n"):
        out += nl
    for tool in tools:
        out += f'{nl}{section(tool)}{nl}approval_mode = "{PROMPT}"{nl}'
    return out


def _sampan(data):
    servers = data.get("mcp_servers")
    entry = servers.get("sampan") if isinstance(servers, dict) else None
    return entry if isinstance(entry, dict) else None


def _mode(entry, tool):
    tools = entry.get("tools")
    table = tools.get(tool) if isinstance(tools, dict) else None
    return table.get("approval_mode") if isinstance(table, dict) else None


def needs_fix(text: str, confirm: bool) -> bool:
    """Whether `sampan mcp install` would change the tables. False when sampan is not in Codex or the TOML is broken."""
    try:
        entry = _sampan(tomllib.loads(text))
    except tomllib.TOMLDecodeError:
        return False
    if entry is None:
        return False
    if confirm:
        return any(_mode(entry, t) != PROMPT for t in ACTING_TOOLS)
    return any(_mode(entry, t) == PROMPT for t in ACTING_TOOLS)


def tool_prompts(path, tool) -> bool:
    """Whether Codex (config at path) asks the user before sampan's `tool`. Missing, unreadable or broken → False."""
    try:
        entry = _sampan(tomllib.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False
    return entry is not None and _mode(entry, tool) == PROMPT


def _parse(text):
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{NOT_TOML}{e}") from None


def _backup(path, raw, mode, now):
    """Write the bytes raw next to path as config.toml.bak-sampan-<time> with mode; never overwrites an earlier backup."""
    base = f"{path.name}.bak-sampan-{now:%Y%m%d-%H%M%S}"
    for n in range(100):
        target = path.with_name(base if n == 0 else f"{base}-{n}")
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, fsutil.PRIVATE_FILE)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(raw)
            os.chmod(target, mode)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return target
    raise OSError(f"{path.parent} 裡的 sampan 備份檔太多")


def apply(path: Path, confirm: bool, *, now, dry_run=False) -> str:
    """Make the tables match confirm. Returns "不用改", "會更新" (dry_run) or "已更新（備份：<name>）"; raises ValueError
    (nothing written) when the file or the result is not valid TOML. With confirm, a file without
    `[mcp_servers.sampan]` (or no file) is left alone: tables alone would give Codex a server with no command."""
    path = Path(path)
    try:
        raw = path.read_bytes()  # bytes: read_text would turn \r\n into \n and rewrite every line
    except FileNotFoundError:
        return "不用改"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"{NOT_TOML}{e}") from None
    data = _parse(text)
    if confirm and _sampan(data) is None:
        return "不用改"
    new = with_prompt(text) if confirm else strip_sections(text)
    try:
        tomllib.loads(new)
        ok = not needs_fix(new, confirm)
    except tomllib.TOMLDecodeError:
        ok = False
    if not ok:  # sampan's entry is written in a form strip/with_prompt do not see (inline or quoted tables)
        raise ValueError(NOT_TABLES)
    if new == text:
        return "不用改"
    if dry_run:
        return "會更新"
    mode = stat.S_IMODE(path.stat().st_mode)
    backup = _backup(path, raw, mode, now)
    fsutil.atomic_write(path, new, encoding="utf-8")  # keeps the mode; \r\n is written unchanged on POSIX
    return f"已更新（備份：{backup.name}）"
