"""Codex asks the user before a tool runs only when its config.toml says so: `approval_mode = "prompt"` in
`[mcp_servers.sampan.tools.<tool>]`. sampan keeps those tables for its acting tools (ACTING_TOOLS) when
`[mcp] confirm = true` and removes them when it is false. With confirm it also writes `approval_mode = "approve"` for
the mail tools (INBOX_TOOLS: a doorbelled agent takes and answers its mail without a prompt); confirm off leaves those
alone, and `sampan mcp uninstall` removes them. Each tool is judged on its own (WANTED), so a config written by an
older sampan, without the tables of the newer tools, only lacks those (mismatched); the tools it has keep working.
The file is edited as text, so every line that is not one of those tables (comments, order, blank lines) stays byte
for byte; it is backed up first and checked with tomllib.
Only the unquoted header form is recognised (what Codex and sampan write), not `[mcp_servers."sampan".tools."send"]`."""
import stat
import tomllib
from pathlib import Path

from . import fsutil
from .mcp_server import ACTING_TOOLS

PROMPT = "prompt"
APPROVE = "approve"
INBOX_TOOLS = ("inbox_take", "reply")
WANTED = {**{t: PROMPT for t in ACTING_TOOLS}, **{t: APPROVE for t in INBOX_TOOLS}}  # with [mcp] confirm on
SAMPAN_TOOLS = tuple(WANTED)
NOT_TOML = "Codex config.toml 不是合法的 TOML："
NOT_TABLES = ('Codex config.toml 的 sampan 設定不是用 [mcp_servers.sampan.tools.<工具>] 表寫的，sampan 改不了；'
              '請手動設 approval_mode = "prompt"')


def section(tool: str) -> str:
    return "[mcp_servers.sampan.tools." + tool + "]"


def strip_sections(text: str, tools=SAMPAN_TOOLS) -> str:
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


def with_prompt(text: str) -> str:
    """text with sampan's tables as WANTED at the end (one per tool: "prompt" for the acting tools, "approve" for the
    mail tools; any earlier sampan table removed), in the file's own line ending (\r\n when it has any)."""
    nl = "\r\n" if "\r\n" in text else "\n"
    out = strip_sections(text)
    if out and not out.endswith("\n"):
        out += nl
    for tool, mode in WANTED.items():
        out += f'{nl}{section(tool)}{nl}approval_mode = "{mode}"{nl}'
    return out


def _sampan(data):
    servers = data.get("mcp_servers")
    entry = servers.get("sampan") if isinstance(servers, dict) else None
    return entry if isinstance(entry, dict) else None


def _mode(entry, tool):
    tools = entry.get("tools")
    table = tools.get(tool) if isinstance(tools, dict) else None
    return table.get("approval_mode") if isinstance(table, dict) else None


def mismatched(text: str, confirm: bool, *, uninstall=False) -> list:
    """The tools whose table `sampan mcp install` (or, with uninstall, `uninstall`) would change, in WANTED order.
    confirm: every tool not set as WANTED. Off: the acting tools set to "prompt" (an "approve" there is the user's);
    uninstall also the mail tools set to "approve". Empty when sampan is not in Codex or the TOML is broken."""
    try:
        entry = _sampan(tomllib.loads(text))
    except tomllib.TOMLDecodeError:
        return []
    if entry is None:
        return []
    if confirm:
        return [t for t, mode in WANTED.items() if _mode(entry, t) != mode]
    wrong = [t for t in ACTING_TOOLS if _mode(entry, t) == PROMPT]
    return wrong + ([t for t in INBOX_TOOLS if _mode(entry, t) == APPROVE] if uninstall else [])


def absent(text: str) -> list:
    """sampan's tools with no table at all in text (an older sampan wrote none for them); empty when sampan is not in
    Codex or the TOML is broken."""
    try:
        entry = _sampan(tomllib.loads(text))
    except tomllib.TOMLDecodeError:
        return []
    return [] if entry is None else [t for t in SAMPAN_TOOLS if _mode(entry, t) is None]


def needs_fix(text: str, confirm: bool, *, uninstall=False) -> bool:
    """Whether `sampan mcp install` (uninstall) would change the tables (mismatched is not empty)."""
    return bool(mismatched(text, confirm, uninstall=uninstall))


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


def apply(path: Path, confirm: bool, *, now, dry_run=False, uninstall=False) -> str:
    """Make the tables match confirm (uninstall: also drop the mail tools' "approve" tables). Returns "不用改", "會更新"
    (dry_run) or "已更新（備份：<name>）"; raises ValueError (nothing written) when the file or the result is not
    valid TOML. With confirm, a file without `[mcp_servers.sampan]` (or no file) is left alone: tables alone would
    give Codex a server with no command."""
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
    wrong = mismatched(text, confirm, uninstall=uninstall)
    if not wrong:  # already right, wherever the tables are: rewriting would only move them
        return "不用改"
    if confirm:
        new = with_prompt(text)
    else:  # every acting table goes once one asks (as before); a mail table only when it is wrong itself
        new = strip_sections(text, (ACTING_TOOLS if set(wrong) & set(ACTING_TOOLS) else ())
                             + tuple(t for t in INBOX_TOOLS if t in wrong))
    try:
        tomllib.loads(new)
        ok = not needs_fix(new, confirm, uninstall=uninstall)
    except tomllib.TOMLDecodeError:
        ok = False
    if not ok:  # sampan's entry is written in a form strip/with_prompt do not see (inline or quoted tables)
        raise ValueError(NOT_TABLES)
    if new == text:
        return "不用改"
    if dry_run:
        return "會更新"
    mode = stat.S_IMODE(path.stat().st_mode)
    backup = fsutil.backup(path, raw, mode, now)
    fsutil.atomic_write(path, new, encoding="utf-8")  # keeps the mode; \r\n is written unchanged on POSIX
    return f"已更新（備份：{backup.name}）"
