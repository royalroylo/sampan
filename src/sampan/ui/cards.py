"""Info card: per-row data (gather_cards), card contents (card_lines, restore_card, adopt_card, preview_header) and the style decisions. Pure data, never touches curses."""
from .. import claude_state, codex_state
from ..adopt import _ago, _short
from ..registry import RegistryError, load_rows
from ..status import State, _fmt_time, _fmt_tokens
from ..tmux import TMUX, Tmux
from .state import show_state
from .text import pad


class Wrap(str):
    """A line of the info card that wraps (at most QUOTE_LINES lines, ending with … if it does not fit); other lines are truncated when they do not fit."""


def _label(name, value):
    return f"{pad(name, 9)}{value}"


class Keys(Wrap):
    """The "keys available for this row" line of the info card (used for colouring)."""


class StateLine(str):
    """The "state" line of the info card; it carries the State so colouring need not parse the display text."""

    def __new__(cls, text, state):
        line = super().__new__(cls, text)
        line.state = state
        return line


_STATE_STYLE = {
    State.RUNNING: "ok",
    State.STOPPED: "muted", State.RETIRED: "muted", State.NO_AGENT: "muted",
    State.AWAIT_TRUST: "warn", State.UNREGISTERED: "warn",
    State.AGENT_MISMATCH: "err", State.PROFILE_MISMATCH: "err", State.MULTI: "err", State.UNKNOWN: "err",
}


def state_style(state):
    """Colour of the state text: running green; stopped / retired / no agent grey; waiting for trust yellow; mismatch or several agents red."""
    return _STATE_STYLE.get(state, "")


def agent_style(status):
    """claude/codex status: idle green, busy/working yellow, waiting magenta (waiting for you), others grey."""
    return {"idle": "ok", "busy": "warn", "working": "warn", "waiting": "attn"}.get(status, "muted")


def message_style(line):
    s = line.lstrip()
    for prefix, style in (("✔", "ok"), ("·", "ok"), ("⚠", "warn"), ("⌛", "warn"), ("⏸", "warn"), ("❌", "err"),
                          ("▶", "info"), ("…", "muted"), ("↷", "muted")):
        if s.startswith(prefix):
            return style
    return ""


def _quote_of(cfg, agent, profile, sid):
    """The last user message of the conversation (whitespace collapsed to one space); "" when unreadable."""
    if not sid:
        return ""
    try:
        if agent == "codex":
            path = codex_state.find_rollout(cfg.codex.home, sid)
            text = codex_state.last_user_text(path) if path is not None else ""
        elif agent == "claude":
            path = claude_state.find_transcript(cfg.profiles.values(), sid, prefer=profile)
            text = claude_state.last_user_text(path) if path is not None else ""
        else:
            text = ""
    except OSError:
        text = ""
    return " ".join(text.split())[:300]


def gather_cards(cfg, run, rows):
    """Data each info card needs that status_rows lacks: {name: {cwd, windows, panes, quote}}. Read once each time the list is refreshed."""
    try:
        reg = {r.tmux: r for r in load_rows(cfg.registry_path)}
    except RegistryError:
        reg = {}
    panes = Tmux(run).panes() or []  # unreadable: treated as no panes, the info card merely lacks the tmux line; state and keys are handled by status_rows' "cannot tell" ("無法判斷")
    r = run([*TMUX, "list-windows", "-a", "-F", "#{session_name}"])
    windows = r.stdout.split("\n") if r.returncode == 0 else []
    cards = {}
    for row in rows:
        name = row.name
        mine = [p for p in panes if p.session == name]
        cwd = reg[name].cwd if name in reg else (mine[0].path if mine else "")
        cards[name] = {"cwd": cwd, "windows": windows.count(name), "panes": len(mine),
                       "quote": _quote_of(cfg, row.agent, row.profile, row.sid)}
    return cards


def row_keys(row, info):
    """The keys available for this row (last line of the info card)."""
    state, live = row.state, (info or {}).get("panes", 0) > 0
    if state is State.RETIRED:
        keys = (["Enter 接上"] if live else []) + ["a activate"]
    elif state is State.STOPPED:
        keys = ["Enter 接回", "e 改名", "r retire"]
    elif state is State.RUNNING:
        keys = ["Enter 接上", "s 停掉", "m 搬", "e 改名", "r retire"]
    else:
        keys = ["Enter 接上"] if live else []
    return "　".join(keys)


def _activity_line(last, now):
    return _label("最後活動", f"{_fmt_time(last)}（{_ago(last, now)}）")


def card_lines(cfg, row, info, now):
    info = info or {}
    state = show_state(row)
    if row.agent_status not in ("-", "", None):
        state += f"（{row.agent_status}）"
    lines = [row.name, "─" * 200, StateLine(_label("狀態", state), row.state)]
    if row.agent == "codex":
        lines.append(_label("agent", "codex"))
    elif row.agent not in ("-", ""):
        lines.append(_label("agent", f"{row.agent} ・ {row.profile or '-'}"))
    if info.get("cwd"):
        lines.append(_label("目錄", _short(cfg, info["cwd"])))
    if row.sid:
        lines.append(_label("對話", row.sid[:8]))
    if row.last:
        lines.append(_activity_line(row.last, now))
    if row.tokens is not None:
        lines.append(_label("context", _fmt_tokens(row.tokens)))
    if info.get("panes"):
        lines.append(_label("tmux", f"{info.get('windows') or 1} 個視窗、{info['panes']} 個 pane"))
    if info.get("quote"):
        lines += ["", "最後一句話", Wrap(f"「{info['quote']}」")]
    keys = row_keys(row, info)
    if keys:
        lines += ["", Keys(keys)]
    return lines


def restore_card(cfg, c, now, quote=""):
    r = c.row
    lines = [r.tmux, "─" * 200, StateLine(_label("狀態", "已停止"), State.STOPPED),
             _label("agent", "codex" if r.agent == "codex" else f"{r.agent} ・ {r.profile or '-'}")]
    lines.append(_label("目錄", _short(cfg, r.cwd) if cfg is not None else r.cwd))
    lines.append(_label("對話", r.session_id[:8]))
    if c.last:
        lines.append(_activity_line(c.last, now))
    if c.tokens is not None:
        lines.append(_label("context", _fmt_tokens(c.tokens)))
    if quote:
        lines += ["", "最後一句話", Wrap(f"「{quote}」")]
    if c.skip:
        lines += ["", Wrap(f"↷ 不能接回：{c.skip}")]
    return lines


def adopt_card(c):
    lines = [c.where, "─" * 200,
             _label("agent", "codex" if c.agent == "codex" else f"{c.agent} ・ {c.profile or '-'}"),
             _label("目錄", c.where)]
    if c.session_id:
        lines.append(_label("對話", c.session_id[:8]))
    if c.last:
        lines.append(_label("最後活動", f"{_fmt_time(c.last)}（{c.ago}）"))
    if c.quote:
        lines += ["", "最後一句話", Wrap(c.quote)]
    if c.skip:
        lines += ["", Wrap(f"↷ 不能接手：{c.skip}")]
    return lines


def preview_header(cfg, row, info):
    """For a row with a tmux session: the right side holds only a compact header (state, agent, profile, context, directory, available keys), the rest goes to the preview."""
    info = info or {}
    state = show_state(row)
    if row.agent_status not in ("-", "", None):
        state += f"（{row.agent_status}）"
    parts = [state]
    if row.agent not in ("-", ""):
        parts.append(row.agent)
        if row.agent != "codex" and row.profile not in ("-", "", None):
            parts.append(row.profile)
    if row.tokens is not None:
        parts.append(f"context {_fmt_tokens(row.tokens)}")
    rest = "・".join(parts[1:])
    lines = [f"{row.name}（預覽）", "─" * 200,
             ((parts[0], state_style(row.state)), ("・" + rest if rest else "", ""))]
    if info.get("cwd"):
        lines.append(_short(cfg, info["cwd"]))
    keys = row_keys(row, info)
    if keys:
        lines.append(Keys(keys))
    return lines
