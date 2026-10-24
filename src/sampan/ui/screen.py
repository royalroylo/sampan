"""curses adapter layer: CursesScreen provides pick / checklist / choose_dir / ask / out to cmd_ui, and handles keys, redrawing and preview capture."""
import os
import time
from datetime import datetime, timezone

from ..marketplace_view import build_market_rows, market_card, market_choices
from ..plugin_apply import check_latest
from ..plugin_view import NO_SOURCE, _mkt, build_rows, catalog, profile_choices, snapshot
from ..plugin_view import card_lines as plugin_card
from ..prompts import SKIP, prompt_choices
from ..status import status_rows
from ..tmux import TMUX
from .actions import cmd_ui, dir_context
from .cards import Wrap, _quote_of, adopt_card, card_lines, gather_cards, preview_header, restore_card
from .dirpick import DirPicker
from .plugin_actions import (
    add_flow,
    disabled_lines,
    market_add_flow,
    market_save_flow,
    market_toggles,
    market_update_flow,
    reload_config,
    restart_offer,
    save_flow,
    update_flow,
)
from .plugin_state import CatalogPicker, PluginPicker, ProfileToggles
from .render import (
    LIST_COLS,
    MARKET_COLS,
    PLUGIN_COLS,
    TOGGLE_COLS,
    _adopt_cells,
    _list_cells,
    _restore_cells,
    list_cols,
    list_empty,
    market_cells,
    plugin_cells,
    prompt_frame,
    render,
    render_dirpick,
    screen_title,
    tab_title,
    toggle_cells,
)
from .state import Checklist, Picker, _row_key
from .text import BACKSPACE, ENTER, MOVES, clean_capture, text_width

KEYS_HELP = "↑↓ 移動　1-9 跳列　/ 搜尋　空白 勾選　Enter 接上　s 停掉　R 接回　A 接手　n 開新　w 存檔　p plugin　q 離開"
RESTORE_HELP = "↑↓ 移動　空白 勾選　Enter 確認接回　q 取消"
ADOPT_HELP = "↑↓ 移動　空白 勾選　Enter 確認接手　q 取消"
PLUGINS_HELP = "↑↓ 移動　1-9 跳列　/ 搜尋　Enter 選 profile　u 更新這個　U 全部更新　a 新增　r 檢查新版　q 返回　Tab 切到來源"
MARKETS_HELP = "↑↓ 移動　1-9 跳列　/ 搜尋　Enter 選 profile　u 更新這個　U 全部更新　a 新增來源　Tab 切到 plugin　q 返回"
TOGGLE_HELP = "↑↓ 移動　空白 勾選／取消　s 儲存　Esc 返回"
CATALOG_HELP = "↑↓ 移動　/ 搜尋　Enter 選這個　i 自己輸入　q 返回"
PLUGINS_TITLE = "sampan · plugin 管理"
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

    def _getkey(self, prompt):
        """Show prompt in the message area and return the next key (timeouts are ignored)."""
        try:
            while True:
                self.prompt = prompt
                self.draw()
                k = self.key()
                if k is not None:
                    return k
        finally:
            self.prompt = None

    def plugins(self, *, ask, own_pane=None, on_prompt=None):
        """The plugin screen (p): layer 1 lists the plugins (or, after Tab, the marketplaces), Enter opens layer 2
        (per-profile toggles). q returns 0. Waits for keys without timeouts; the config is re-read after every action."""
        own_pane = own_pane if own_pane is not None else self.own_pane
        cfg = reload_config(self.cfg, self.out)
        latest, checked_at, tab = None, None, "plugin"
        snap = snapshot(cfg)
        rows, mrows = build_rows(cfg, snap, latest), build_market_rows(cfg, snap)
        pickers = {"plugin": PluginPicker(rows, clock=self.clock), "market": PluginPicker(mrows, clock=self.clock)}
        saved = self.view
        self._timeout(-1)

        def offer(changed):
            if changed:
                restart_offer(cfg, self.run, self.out, ask, changed, self._getkey, own_pane=own_pane,
                              on_prompt=on_prompt, rows_fn=self.rows_fn)
        try:
            while True:
                picker = pickers[tab]
                title = tab_title(PLUGINS_TITLE, tab) + (f"　上次檢查 {checked_at}" if checked_at else "")
                here = picker.visible[picker.cursor] if picker.visible else None
                if tab == "plugin":
                    cells = {r.name: plugin_cells(r, i, len(rows)) for i, r in enumerate(rows, 1)}
                    empty = "目前沒有任何 plugin（a 新增）" if not picker.query else f"沒有名稱含「{picker.query}」的 plugin"
                    card, help_, cols = plugin_card(cfg, here) if here else [], PLUGINS_HELP, PLUGIN_COLS
                else:
                    cells = {r.name: market_cells(r, i, len(mrows)) for i, r in enumerate(mrows, 1)}
                    empty = "目前沒有任何來源（a 新增來源）" if not picker.query else f"沒有名稱含「{picker.query}」的來源"
                    card, help_, cols = market_card(cfg, snap, here) if here else [], MARKETS_HELP, MARKET_COLS
                self.view = dict(title=title, left=[cells[r.name] for r in picker.visible], cursor=picker.cursor,
                                 card=card, preview=None, help=help_, cols=cols, empty=empty, notice=None,
                                 name_w=max((text_width(c[0]) for c in cells.values()), default=None))
                self.prompt = picker.prompt
                self.draw()
                k = self.key()
                if k is None:
                    continue
                action = picker.key(k)
                if action is None:
                    continue
                kind, names = action
                self.prompt = None
                if kind == "back":
                    return 0
                if kind == "tab":
                    tab = "market" if tab == "plugin" else "plugin"
                    continue
                self.messages = []  # a new action: the message area holds only this action's result
                goto = None
                if tab == "market":
                    goto = self._market_action(cfg, ask, kind, names, mrows, offer)
                elif kind == "open":
                    r = next(x for x in rows if x.name == names[0])
                    offer(self._plugin_toggles(cfg, ask, r.pid, r.source, latest))
                elif kind == "update":
                    r = next(x for x in rows if x.name == names[0])
                    offer(update_flow(cfg, self.run, self.out, ask, [r.pid]))
                elif kind == "update_all":
                    offer(update_flow(cfg, self.run, self.out, ask, None))
                elif kind == "check":
                    latest = check_latest(cfg, self.run, self.out, snapshot(cfg))
                    checked_at = self.now_fn().astimezone().strftime("%H:%M")
                elif kind == "add":
                    picked = add_flow(cfg, self.out, ask, snapshot(cfg), self._choose_catalog)
                    if picked:
                        pid, new_source = picked
                        offer(self._plugin_toggles(cfg, ask, pid, new_source or cfg.marketplaces.get(_mkt(pid)),
                                                   latest, new_source=new_source))
                        goto = pid
                cfg = reload_config(cfg, self.out)
                snap = snapshot(cfg)
                rows, mrows = build_rows(cfg, snap, latest), build_market_rows(cfg, snap)
                pickers["plugin"].reload(rows)  # the cursor stays on the same row
                pickers["market"].reload(mrows)
                if tab == "plugin":
                    target = next((r.name for r in rows if r.pid == goto), None)
                elif goto is not None:  # a new source: the row whose config source is the one typed
                    target = next((r.name for r in mrows if r.source == goto), None)
                else:
                    target = None
                if target is not None and picker.query is None:
                    picker._goto(target)
        finally:
            self.prompt = None
            self.view = saved

    def _market_action(self, cfg, ask, kind, names, mrows, offer):
        """One action of the 來源 tab. Returns the source just added (to move the cursor to it), else None."""
        if kind == "open":
            r = next(x for x in mrows if x.name == names[0])
            offer(self._market_toggles(cfg, ask, r.name, r.source))
        elif kind == "update":
            market_update_flow(cfg, self.run, self.out, ask, [names[0]])
        elif kind == "update_all":
            market_update_flow(cfg, self.run, self.out, ask, None)
        elif kind == "add":
            src = market_add_flow(self.out, ask)
            if src:
                offer(self._market_toggles(cfg, ask, None, src))
                return src
        return None  # "check" (r) belongs to the plugin tab

    def _market_toggles(self, cfg, ask, name, source):
        """Layer 2 of one marketplace (name None: a new source, nothing checked; source None: outside the config).
        s previews and saves. Returns the profiles that need a restart (a removal that uninstalled plugins)."""
        snap = snapshot(cfg)
        choices = market_choices(cfg, snap, name, source)
        t = market_toggles(choices)
        if name is None:
            card = [f"來源 {source}", "名稱 （加入後才知道名稱）"]
        else:
            n = sum(1 for e in catalog(cfg, snap) if _mkt(e.pid) == name)
            card = [f"來源 {source or '（不在設定檔）'}", f"目錄有 {n} 個 plugin"]
        card += disabled_lines(choices)
        while True:
            self.view = dict(title=f"{PLUGINS_TITLE} › {name or source}", left=[toggle_cells(t, c) for c in choices],
                             cursor=t.cursor, card=card, preview=None, help=TOGGLE_HELP, cols=TOGGLE_COLS,
                             empty="（沒有任何 profile）", notice=None, name_w=None)
            self.prompt = t.prompt
            self.draw()
            k = self.key()
            if k is None:
                continue
            action = t.key(k)
            self.prompt = t.prompt
            if action is None:
                continue
            self.prompt = None
            if action[0] == "back":
                return []
            return market_save_flow(cfg, self.run, self.out, ask, name, choices, t.checked, source=source,
                                    new=name is None)

    def _plugin_toggles(self, cfg, ask, pid, source, latest, *, new_source=None):
        """Layer 2: one box per profile; s previews and saves. Returns the profiles that changed (empty when nothing was saved)."""
        choices = profile_choices(cfg, snapshot(cfg), pid, source)
        t = ProfileToggles(choices)  # boxes start at the install state
        newest = next((lt.version for p in cfg.profiles if (lt := (latest or {}).get((p, pid))) and lt.version), None)
        card = [f"來源 {source or NO_SOURCE}", f"最新 {newest or '未檢查'}"] + disabled_lines(choices)
        while True:
            self.view = dict(title=f"{PLUGINS_TITLE} › {pid}", left=[toggle_cells(t, c) for c in choices],
                             cursor=t.cursor, card=card, preview=None, help=TOGGLE_HELP, cols=TOGGLE_COLS,
                             empty="（沒有任何 profile）", notice=None, name_w=None)
            self.prompt = t.prompt
            self.draw()
            k = self.key()
            if k is None:
                continue
            action = t.key(k)
            self.prompt = t.prompt
            if action is None:
                continue
            self.prompt = None
            if action[0] == "back":
                return []
            return save_flow(cfg, self.run, self.out, ask, pid, choices, t.checked, new_source=new_source)

    def _choose_catalog(self, entries):
        """choose_fn for add_flow: the catalog picker. Returns the plugin id, "" (type it in) or None (cancelled)."""
        picker = CatalogPicker(entries, clock=self.clock)
        while True:
            here = picker.visible[picker.cursor].entry if picker.visible else None
            card = [here.pid, "─" * 30, f"來源 {here.source or NO_SOURCE}"] + (
                ["", Wrap(here.description)] if here.description else []) if here else []
            empty = (f"沒有名稱含「{picker.query}」的 plugin" if picker.query
                     else "沒有已下載的 marketplace 目錄（i 自己輸入）")
            self.view = dict(title=f"{PLUGINS_TITLE} › 新增", left=[(r.entry.pid,) for r in picker.visible],
                             cursor=picker.cursor, card=card, preview=None, help=CATALOG_HELP, cols=(), empty=empty,
                             notice=None, name_w=None)
            self.prompt = picker.prompt
            self.draw()
            k = self.key()
            if k is None:
                continue
            action = picker.key(k)
            if action is None:
                continue
            self.prompt = None
            kind, names = action
            if kind == "back":
                return None
            if kind == "type":
                return ""
            return names[0]


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
                    adopt_picker_fn=screen.adopt_checklist, plugins_fn=screen.plugins, exec_fn=lambda f, args: pending.append((f, args)),
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
