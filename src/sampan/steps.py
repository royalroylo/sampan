"""Standard steps after a session's agent is (re)started: the launch line, and the Remote Control reset.

When Claude resumes a conversation it reattaches the old Remote Control bridge under the old name; only Disconnect +
`/remote-control <name>` gives the session the right name. So on every launch of a profile with Remote Control on,
sampan disconnects and reconnects under the alias, without judging whether the name already is right. It presses only
text+Enter, Up, Enter and Escape, and Enter on the menu only with the cursor verified on Disconnect; anything it does
not recognise → Escape and a reason."""
import re
from dataclasses import dataclass

from .launch import launch_line, remote_control_on, send_line
from .registry import display_name
from .screens import ANSI_RE, claude_input_state

RC_ACTIVE = "/remote-control is active"
RC_DISCONNECTED = "Remote Control disconnected."
RC_DISCONNECT = "Disconnect this session"
RC_MENU_FOOT = "Enter to select · Esc to continue"
RC_TITLE = "Remote Control"
RC_COMMAND = "/remote-control"
RC_WAIT = 20  # seconds for the bridge to come up (at boot, and after reconnecting)
MENU_WAIT = 5  # seconds for the menu to show, and for the disconnect to be confirmed
MOVE_WAIT = 2  # seconds for the cursor to move after Up
MAX_UPS = 3
POLL = 0.5  # waits are counted in polls of this many seconds (sleep is injected), not wall clock
MENU_LINES = 12  # how far above the footer the menu's title may be
FOOT_SLACK = 2  # non-blank lines allowed under the footer
SEGMENT_RE = re.compile(r"\s{2,}")  # "Show QR code   Scan with your phone…": an option's text is its first segment


@dataclass(frozen=True)
class RcMenu:
    options: list[str]
    cursor: int  # index of the ❯ option, -1 when no option has it


def _plain(screen):
    return [ANSI_RE.sub("", ln).replace("\xa0", " ").rstrip() for ln in screen.rstrip("\n").split("\n")]


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def rc_menu(screen):
    """The Remote Control menu at the bottom of the screen (its title and RC_MENU_FOOT), or None for any other screen."""
    lines = _plain(screen)
    foot = next((i for i in range(len(lines) - 1, -1, -1) if RC_MENU_FOOT in lines[i]), None)
    if foot is None or sum(1 for ln in lines[foot + 1:] if ln.strip()) > FOOT_SLACK:
        return None
    title = next((i for i in range(foot - 1, max(foot - MENU_LINES, -1), -1) if lines[i].strip() == RC_TITLE), None)
    if title is None:
        return None
    options, cursor = [], -1
    for ln in lines[title + 1:foot]:
        text = ln.strip()
        if text.startswith("❯"):
            cursor = len(options)
            text = text[1:].strip()
        elif not text or _indent(ln) <= _indent(lines[title]):
            continue  # the description and URL lines sit at the title's indent; options are indented further
        options.append(SEGMENT_RE.split(text, 1)[0])
    return RcMenu(options, cursor) if options else None


def rc_wanted(cfg, row, profile=None):
    """Whether a launch of this row gets the Remote Control reset: a Claude row whose profile has Remote Control on."""
    p = cfg.profiles.get(profile or row.profile)
    return row.agent == "claude" and p is not None and remote_control_on(p)


def _wait(tmux, pane, pred, seconds, sleep):
    """Capture until pred(screen) holds, polling every POLL for at most `seconds`; the last screen that held, or None."""
    polls = int(seconds / POLL)
    for i in range(polls + 1):
        screen = tmux.capture(pane, escapes=True)  # escapes: claude_input_state needs them; the rest uses _plain
        if pred(screen):
            return screen
        if i < polls:
            sleep(POLL)
    return None


def _last(lines, pred):
    return next((i for i in range(len(lines) - 1, -1, -1) if pred(lines[i])), None)


def _disconnected(screen):
    """RC_DISCONNECTED after the last `❯ /remote-control`, with no reconnect after it (an earlier reset in a resumed
    transcript has one)."""
    lines = _plain(screen)
    cmd = _last(lines, lambda ln: ln.strip() == f"❯ {RC_COMMAND}")
    if cmd is None:
        return False
    rest = lines[cmd + 1:]
    return (any(RC_DISCONNECTED in ln for ln in rest)
            and not any(RC_ACTIVE in ln or ln.strip().startswith(f"❯ {RC_COMMAND} ") for ln in rest))


def _reconnected(screen, alias):
    """RC_ACTIVE after the last `❯ /remote-control <alias>` line (the alias may wrap onto the next lines), and that line
    after our disconnect (the last `❯ /remote-control` and RC_DISCONNECTED): a resumed transcript may hold an earlier
    reset under the same alias."""
    lines = _plain(screen)
    cmd = _last(lines, lambda ln: ln.strip().startswith(f"❯ {RC_COMMAND} "))
    marks = (_last(lines, lambda ln: ln.strip() == f"❯ {RC_COMMAND}"), _last(lines, lambda ln: RC_DISCONNECTED in ln))
    ours = max((i for i in marks if i is not None), default=-1)
    if cmd is None or cmd < ours:
        return False
    active = next((i for i in range(cmd + 1, len(lines)) if RC_ACTIVE in lines[i]), None)
    if active is None:
        return False
    typed = " ".join(lines[cmd:active]).strip()[len(f"❯ {RC_COMMAND} "):]
    return "".join(typed.split()) == "".join(alias.split())


def rc_reset(tmux, pane, alias, *, sleep, wait=RC_WAIT):
    """Disconnect Remote Control and reconnect it as `alias`. None when done, else why it was not (nothing pressed
    blindly: a step that does not see what it expects presses Escape if a menu may be open, and stops)."""
    screen = _wait(tmux, pane, lambda s: any(RC_ACTIVE in ln for ln in _plain(s)), wait, sleep)
    if screen is None:
        return "沒看到 Remote Control 連上"
    state = claude_input_state(screen)
    if state == "draft":
        return "輸入框有字"
    if state != "empty":
        return "找不到空的輸入框"
    tmux.send_keys(pane, RC_COMMAND, "Enter")
    try:
        reason = _to_disconnect(tmux, pane, sleep)
    except BaseException:  # a tmux error, or Ctrl-C
        _escape(tmux, pane)  # the menu may be open: leave the pane as it was, best effort
        raise
    if reason is not None:
        return reason
    if _wait(tmux, pane, _disconnected, MENU_WAIT, sleep) is None:
        return "沒看到斷開"
    state = claude_input_state(tmux.capture(pane, escapes=True))  # the box on the current screen, just before typing
    if state != "empty":
        if state == "blocked":
            tmux.send_keys(pane, "Escape")  # a menu or dialog may be open
        return "重新連線前輸入框不是空的"
    tmux.send_keys(pane, _tmux_literal(f"{RC_COMMAND} {alias}"), "Enter")
    if _wait(tmux, pane, lambda s: _reconnected(s, alias), wait, sleep) is None:
        return "重新連線沒確認"
    return None


def _escape(tmux, pane):
    try:
        tmux.send_keys(pane, "Escape")
    except Exception:
        pass


def _tmux_literal(text):
    """tmux takes an argument ending in `;` as a command separator; `\\;` at the end is a literal `;`."""
    return text[:-1] + "\\;" if text.endswith(";") else text


def _to_disconnect(tmux, pane, sleep):
    """Steps 3-4: wait for the menu, move the cursor to Disconnect and press Enter. None once Enter is pressed, else
    the reason (Escape already sent)."""
    screen = _wait(tmux, pane, lambda s: rc_menu(s) is not None, MENU_WAIT, sleep)
    if screen is None:
        tmux.send_keys(pane, "Escape")
        return "選單沒出現"
    menu = rc_menu(screen)
    ups = 0
    # a redraw slower than MOVE_WAIT may let a second Up queue behind the first, and the Enter then lands on another
    # option: harmless (it only closes the menu), and step 5 reports it
    while menu is not None and not _on_disconnect(menu) and ups < MAX_UPS:
        tmux.send_keys(pane, "Up")
        ups += 1
        before = menu.cursor
        screen = _wait(tmux, pane, lambda s: (m := rc_menu(s)) is None or m.cursor != before, MOVE_WAIT, sleep)
        menu = rc_menu(screen if screen is not None else tmux.capture(pane, escapes=True))
    if menu is None or not _on_disconnect(menu):
        tmux.send_keys(pane, "Escape")
        return "游標沒到 Disconnect"
    tmux.send_keys(pane, "Enter")
    return None


def _on_disconnect(menu):
    return 0 <= menu.cursor < len(menu.options) and menu.options[menu.cursor] == RC_DISCONNECT


def launch_cmd(cfg, row, *, profile=None, cwd=None):
    """The line `launch` sends (also what dry runs print): resume the row's conversation, named by its alias."""
    return launch_line(cfg, row.agent, profile or row.profile, resume=row.session_id, cwd=cwd,
                       name=display_name(row))


def launch(cfg, tmux, target, row, *, profile=None, cwd=None, sleep, clear=True):
    """Clear the pane's input line and start the row's agent on its conversation, named by its alias (also the Remote
    Control name when the profile has it on). Returns the line sent; dry runs are the caller's business.
    clear=False: a pane sampan has just created has no input to clear."""
    line = launch_cmd(cfg, row, profile=profile, cwd=cwd)
    if clear:
        tmux.send_keys(target, "C-u")
    send_line(tmux, target, row.agent, line, sleep)
    return line


def rc_step(cfg, tmux, row, pane, out, *, sleep, profile=None):
    """The Remote Control reset after a launch, reported in one line. Does nothing (and prints nothing) for a profile
    without Remote Control; never raises and never changes the operation's outcome."""
    if not rc_wanted(cfg, row, profile):
        return
    alias = display_name(row)
    try:
        reason = rc_reset(tmux, pane, alias, sleep=sleep)
    except Exception as e:  # a tmux failure mid-way is a reason like any other
        reason = str(e) or type(e).__name__
    if reason is None:
        out(f"  · {row.tmux} Remote Control 名稱已校正為「{alias}」")
    else:
        out(f"  ⚠ {row.tmux}：RC 校正沒做：{reason}")
