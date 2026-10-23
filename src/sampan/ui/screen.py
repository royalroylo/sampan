"""curses adapter layer: CursesScreen provides pick / checklist / choose_dir / ask / out to cmd_ui, and handles keys, redrawing and preview capture."""
import os
import time
from datetime import datetime, timezone

from ..prompts import SKIP, prompt_choices
from ..status import status_rows
from ..tmux import TMUX
from .actions import cmd_ui, dir_context
from .cards import _quote_of, adopt_card, card_lines, gather_cards, preview_header, restore_card
from .dirpick import DirPicker
from .render import (
    LIST_COLS,
    _adopt_cells,
    _list_cells,
    _restore_cells,
    list_cols,
    list_empty,
    prompt_frame,
    render,
    render_dirpick,
    screen_title,
)
from .state import Checklist, Picker, _row_key
from .text import BACKSPACE, ENTER, MOVES, clean_capture, text_width

KEYS_HELP = "↑↓ 移動　1-9 跳列　/ 搜尋　空白 勾選　Enter 接上　s 停掉　R 接回　A 接手　n 開新　w 存檔　q 離開"
RESTORE_HELP = "↑↓ 移動　空白 勾選　Enter 確認接回　q 取消"
ADOPT_HELP = "↑↓ 移動　空白 勾選　Enter 確認接手　q 取消"
PROMPT_HELP = "↑↓ 選擇　Enter 送出　Esc 先不管"
TICK_MS = 1000  # while the list waits for a key, re-capture the preview this often
REFRESH_S = 5  # while the list waits for a key, re-read the session list (state, info cards) this often
NO_SCREEN = "（看不到畫面）"
SELF_SCREEN = "（sampan 自己就在這個 pane）"


def _now():
    return datetime.now(timezone.utc)


class CursesScreen:
    """One curses screen: pick / restore_checklist / adopt_checklist swap the left-hand content, ask asks in the message area, out redraws immediately.
    scr only uses getmaxyx, erase, addstr, refresh, get_wch (tests use a fake object)."""

    def __init__(self, scr, cfg=None, run=None, now_fn=_now, *, clock=time.monotonic, rows_fn=status_rows,
                 own_pane=None, updater=None):
        self.scr, self.cfg, self.run, self.now_fn = scr, cfg, run, now_fn
        self.updater = updater  # update.Checker: the list screen shows a new-version hint, and re-checks in the idle time while waiting for a key once the cache has expired
        self.clock, self.rows_fn, self.own_pane = clock, rows_fn, own_pane
        self.messages = []
        self.warning = None  # one-line explanation when tmux is unreadable (the hint line of the list screen)
        self.prompt = None
        self.extra = []  # question under the message area (agent startup prompt)
        self.title = screen_title(updater)
        self.view = dict(title=self.title, left=[], cursor=0, card=[], preview=None, help=KEYS_HELP, cols=LIST_COLS,
                         empty=None, notice=None)
        self.last_name = None  # session the cursor was last on: after an action returns to the list the cursor stays there
        try:
            import curses
            curses.curs_set(0)
        except Exception:  # noqa: BLE001  no real curses (tests) or the terminal does not support it
            pass

    def _attrs(self):
        """Style name → curses attribute; on a terminal without colours only bold / reverse / dim are used."""
        if getattr(self, "_attr_map", None) is not None:
            return self._attr_map
        import curses
        m = {"title": curses.A_BOLD, "head": curses.A_BOLD, "sel": curses.A_REVERSE, "dim": curses.A_DIM,
             "prompt": curses.A_BOLD, "muted": curses.A_DIM, "label": curses.A_DIM, "key": 0,
             "ok": 0, "warn": 0, "err": 0, "info": 0, "attn": 0}
        try:
            if curses.has_colors():
                curses.start_color()
                try:
                    curses.use_default_colors()
                    bg = -1
                except curses.error:
                    bg = curses.COLOR_BLACK
                for i, (name, fg, extra) in enumerate([
                        ("ok", curses.COLOR_GREEN, 0), ("warn", curses.COLOR_YELLOW, 0),
                        ("err", curses.COLOR_RED, curses.A_BOLD), ("info", curses.COLOR_CYAN, 0),
                        ("attn", curses.COLOR_MAGENTA, curses.A_BOLD), ("key", curses.COLOR_CYAN, 0),
                        ("title", curses.COLOR_CYAN, curses.A_BOLD), ("head", curses.COLOR_CYAN, curses.A_BOLD),
                        ("prompt", curses.COLOR_YELLOW, curses.A_BOLD)], start=1):
                    curses.init_pair(i, fg, bg)
                    m[name] = curses.color_pair(i) | extra
        except curses.error:
            pass
        self._attr_map = m
        return m

    def draw(self):
        h, w = self.scr.getmaxyx()
        self._paint(render(**self.view, messages=self.messages, prompt=self.prompt, extra=self.extra, width=w - 1,
                           height=h))

    def _paint(self, frame):
        """Draw a frame such as render()'s (style name → curses attribute; a combination like "sel+ok" stacks the attributes)."""
        import curses
        base = self._attrs()

        def attr(style):
            a = 0
            for part in (style or "").split("+"):
                a |= base.get(part, 0)
            return a
        self.scr.erase()
        for y, segs in enumerate(frame):
            x = 0
            for text, style in segs:
                try:
                    self.scr.addstr(y, x, text, attr(style))
                except curses.error:
                    pass
                x += text_width(text)
        self.scr.refresh()

    def key(self):
        """The next key; returns None when a timeout was set and expired without a key."""
        import curses
        try:
            ch = self.scr.get_wch()
        except curses.error:
            return None
        if isinstance(ch, int):
            try:
                return curses.keyname(ch).decode()
            except (curses.error, ValueError):
                return str(ch)
        return ch

    def out(self, line=""):
        self.messages += str(line).split("\n")
        del self.messages[:-200]
        self.draw()

    def ask(self, prompt):
        """Ask in the message area; Enter submits, Backspace deletes a character, Esc cancels (returns None). Ctrl-C raises KeyboardInterrupt as usual."""
        buf = ""
        try:
            while True:
                self.prompt = f"{prompt}{buf}_"
                self.draw()
                k = self.key()
                if k is None:
                    continue
                if k in ENTER:
                    break
                if k == "\x1b":
                    return None
                if k in BACKSPACE:
                    buf = buf[:-1]
                elif len(k) == 1 and k.isprintable():
                    buf += k
        finally:
            self.prompt = None
        self.messages.append(f"{prompt}{buf}")
        self.draw()
        return buf

    def prompt_choice(self, name, agent, p):
        """The agent stopped at a startup menu (called by prompts.Relay): list the question and options under the message area, return the option number, ATTACH or SKIP.
        The cursor defaults to "go in and handle it myself" (the agent's option is not preselected); a number key picks that number directly; Esc = leave it for now. Waiting for a key does not time out, and
        the preview and list are not refreshed meanwhile (pick is not running)."""
        choices = prompt_choices(p)
        cur, saved, choice = 0, self.view, None
        self._timeout(-1)
        try:
            while choice is None:
                self.view = dict(saved, help=PROMPT_HELP, preview=None, notice=None)
                self.extra = prompt_frame(name, agent, p, cur)
                self.draw()
                k = self.key()
                if k is None:
                    continue
                if k in MOVES:
                    cur = max(0, min(len(choices) - 1, cur + MOVES[k]))
                elif k in ENTER:
                    choice = choices[cur][1]
                elif k == "\x1b":
                    choice = SKIP
                elif len(k) == 1 and k.isdigit() and 1 <= int(k) <= len(p.options):
                    choice = int(k) - 1
        finally:
            self.extra = []
            self.view = saved
        self.out(f"  → {name}：{next(label for label, value in choices if value == choice)}")
        return choice

    def _timeout(self, ms):
        set_timeout = getattr(self.scr, "timeout", None)
        if set_timeout is not None:
            set_timeout(ms)

    def capture(self, r, current=None):
        """The pane screen of the row under the cursor (read-only: capture-pane, no keys sent); [NO_SCREEN] when it cannot be captured.
        When the agent pane is unknown, capture that session's active pane; sampan's own pane is never captured (the screen would nest infinitely)."""
        pane = r.pane
        if (pane and pane == self.own_pane) or (not pane and current and r.name == current):
            return [SELF_SCREEN]
        target = pane or f"={r.name}:"
        try:
            res = self.run([*TMUX, "capture-pane", "-p", "-t", target])
        except OSError:
            return [NO_SCREEN]
        return clean_capture(res.stdout) if res.returncode == 0 else [NO_SCREEN]

    def _reload(self, rows, picker):
        """Re-read the list (every REFRESH_S seconds while waiting for a key): replace rows in place (what cmd_ui holds is new too), the cursor stays on the same name (else the first row), checkmarks, search and digit buffer are kept."""
        try:
            new = self.rows_fn(self.cfg, self.run)
        except Exception:  # noqa: BLE001  a failed background refresh must not crash the screen: keep the old list
            return False
        self.warning = getattr(new, "warning", None)
        rows[:] = new
        picker.reload(rows)  # keeps the search, the digit buffer and the cursor session
        return True

    def pick(self, rows, current):
        """Arrow-key menu. While waiting for a key, re-capture the pane screen of the row under the cursor (a row with a tmux session) every TICK_MS and re-read the list every REFRESH_S seconds."""
        def load():
            return (gather_cards(self.cfg, self.run, rows) if self.cfg is not None else {}), self.now_fn(), {}
        cards, now, built = load()
        self.warning = getattr(rows, "warning", None)
        names = [r.name for r in rows]
        picker = Picker(rows, self.last_name if self.last_name in names else current, clock=self.clock)
        shot, tick, refreshed = (None, None), False, self.clock()
        self._timeout(TICK_MS)
        try:
            while True:
                card, preview = [], None
                if picker.visible:
                    r = picker.visible[picker.cursor]
                    name = self.last_name = r.name
                    info = cards.get(name)
                    if (info or {}).get("panes", 0) > 0 and self.run is not None:
                        if tick or shot[0] != name:
                            shot = (name, self.capture(r, current))
                        preview = shot[1]
                        card = preview_header(self.cfg, r, info)
                    else:
                        if name not in built:
                            built[name] = card_lines(self.cfg, r, info, now)
                        card = built[name]
                # row numbers and the name column width follow the unfiltered list
                cells = {r.name: _list_cells(r, picker.marked, current, i, len(rows)) for i, r in enumerate(rows, 1)}
                left = [cells[r.name] for r in picker.visible]
                name_w = max((text_width(c[0]) for c in cells.values()), default=None)
                self.view = dict(title=self.title, left=left, cursor=picker.cursor, card=card, preview=preview,
                                 help=KEYS_HELP, cols=list_cols(rows), empty=list_empty(rows, picker.query),
                                 name_w=name_w,
                                 notice=self.warning or (self.updater.hint() if self.updater is not None else None))
                self.prompt = picker.prompt
                self.draw()
                k = self.key()
                tick = k is None
                if tick:
                    if self.updater is not None:
                        self.updater.tick()
                    if self.cfg is not None and self.clock() - refreshed >= REFRESH_S:
                        refreshed = self.clock()
                        if self._reload(rows, picker):
                            cards, now, built = load()
                    continue
                action = picker.key(k)
                if action is not None:
                    if action[0] != "quit":
                        self.messages = []  # a new action: the message area holds only this action's result
                    return action
        except KeyboardInterrupt:
            return ("quit", [])
        finally:
            self.prompt = None
            self.view = dict(self.view, preview=None, notice=None)
            self._timeout(-1)

    def checklist(self, cands, *, title, help, cells, ident, card, cols):
        box = Checklist(cands, ident)
        saved, self.messages = self.view, []
        built = {}
        try:
            while True:
                c = cands[box.cursor] if cands else None
                if c is not None and box.cursor not in built:
                    built[box.cursor] = card(c)
                self.view = dict(title=title, left=[cells(x, box) for x in cands], cursor=box.cursor,
                                 card=built.get(box.cursor, []), preview=None, help=help, cols=cols, empty=None,
                                 notice=None)
                self.draw()
                k = self.key()
                if k is None:
                    continue
                action = box.key(k)
                if action is not None:
                    return action[1]
        except KeyboardInterrupt:
            return None
        finally:
            self.view = saved

    def choose_dir(self, start, *, home, recent=(), sessions=None, allow_missing=False, title="選工作目錄"):
        """Choose a directory (dirpick.DirPicker): returns the chosen path; Esc / Ctrl-C returns None."""
        dp = DirPicker(start, home=home, recent=recent, sessions=sessions, allow_missing=allow_missing)
        try:
            while True:
                h, w = self.scr.getmaxyx()
                self._paint(render_dirpick(dp, w - 1, h, title=title))
                k = self.key()
                if k is None:
                    continue
                res = dp.key(k)
                if res is not None:
                    return res[1]
        except KeyboardInterrupt:
            return None
        finally:
            self.draw()

    def restore_checklist(self, cands):
        now = self.now_fn()

        def card(c):
            quote = _quote_of(self.cfg, c.row.agent, c.row.profile, c.row.session_id) if self.cfg else ""
            return restore_card(self.cfg, c, now, quote)
        return self.checklist(cands, title="sampan restore：勾選要接回的", help=RESTORE_HELP, cells=_restore_cells,
                              ident=_row_key, card=card, cols=(6, 10))

    def adopt_checklist(self, cands):
        return self.checklist(cands, title="sampan adopt：勾選要接手的（tmux 外的 claude／codex）", help=ADOPT_HELP,
                              cells=_adopt_cells, ident=lambda c: c.key, card=adopt_card, cols=(6, 10))


def _curses_prepare():
    import locale

    locale.setlocale(locale.LC_ALL, "")
    os.environ.setdefault("ESCDELAY", "25")  # do not wait a second after Esc


def curses_adopt_checklist(cands):
    """The checklist when `sampan adopt` runs on its own (opens and closes curses itself)."""
    import curses

    _curses_prepare()
    try:
        return curses.wrapper(lambda scr: CursesScreen(scr).adopt_checklist(cands))
    except KeyboardInterrupt:
        return None


def run_outside(scr, args):
    """Temporarily hand the terminal to another program (tmux attach): curses steps out first, and when that program ends (detach) we return to the original screen."""
    import curses
    import subprocess
    curses.def_prog_mode()
    curses.endwin()
    try:
        return subprocess.call(args)
    finally:
        curses.reset_prog_mode()
        scr.clear()
        scr.refresh()


def curses_ui(cfg, run, out, *, current_session=None, own_pane=None, exec_fn=os.execvp, wrapper=None,
              run_outside=run_outside, updater=None, **kw):
    """The whole ui opens curses only once. When attaching to a session outside tmux, curses steps out temporarily and tmux attach runs in the foreground; after detach we are back at the list.
    On exit the last contents of the message area are printed on the terminal."""
    pending, box = [], {}

    def main(scr):
        screen = box["screen"] = CursesScreen(scr, cfg, run, rows_fn=kw.get("rows_fn", status_rows), own_pane=own_pane,
                                              updater=updater)
        opts = dict(picker_fn=screen.pick, restore_picker_fn=screen.restore_checklist, prompt_fn=screen.prompt_choice,
                    adopt_picker_fn=screen.adopt_checklist, exec_fn=lambda f, args: pending.append((f, args)),
                    attach_fn=lambda args: run_outside(scr, args), loop=True,
                    choose_dir_fn=lambda start, *, allow_missing: screen.choose_dir(
                        start, home=str(cfg.home), allow_missing=allow_missing,
                        title="選搬過去的目錄" if allow_missing else "選工作目錄", **dir_context(cfg)))
        opts.update(kw)
        return cmd_ui(cfg, run, screen.out, screen.ask, current_session=current_session, own_pane=own_pane,
                      updater=updater, **opts)

    if wrapper is None:
        import curses
        _curses_prepare()
        wrapper = curses.wrapper
    try:
        rc = wrapper(main)
    except KeyboardInterrupt:
        rc = 0
    screen = box.get("screen")
    for line in (screen.messages if screen else []):
        out(line)
    if pending:
        exec_fn(*pending[0])
    return rc
