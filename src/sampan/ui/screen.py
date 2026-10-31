"""curses adapter layer: CursesScreen provides pick / checklist / choose_dir / ask / out to cmd_ui, and handles keys, redrawing and preview capture."""
import json
import os
import tempfile
import time
from datetime import datetime, timezone

from .. import codex_daemon, dispatch
from .. import folders as F
from ..fsutil import PRIVATE_FILE, atomic_write, private_dir
from ..lights import SEEN_DWELL_S, light, load_seen, mark_seen, replied_at
from ..marketplace_view import build_market_rows, market_card, market_choices
from ..plugin_apply import check_latest
from ..plugin_view import NO_SOURCE, _mkt, build_rows, catalog, profile_choices, snapshot
from ..plugin_view import card_lines as plugin_card
from ..prompts import SKIP, prompt_choices
from ..status import State, status_rows
from ..tmux import TMUX, Tmux
from .actions import cancel_flow, cmd_ui, dir_context
from .cards import Wrap, _quote_of, adopt_card, card_lines, folder_card, gather_cards, preview_header, restore_card
from .codex_others import GO_DESKTOP, OTHER_KEY, ListNote, OwnCols, block_left, other_card, other_cells, other_rows
from .daemon_watch import apply_codex_status
from .dirpick import DirPicker
from .dispatch_watch import LIST_HELP as DISPATCH_HELP
from .dispatch_watch import LIST_TITLE as DISPATCH_TITLE
from .dispatch_watch import DispatchPicker, dispatch_cells, dispatch_cols, dispatch_summary
from .folder_actions import save_collapsed
from .plugin_actions import (
    add_flow,
    disabled_lines,
    enter_source,
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
from .profile_actions import TITLE as PROFILES_TITLE
from .profile_actions import account_lines, load_rows, profile_action, profile_cells, profile_cols, profile_usage
from .profile_actions import card_lines as profile_card
from .render import (
    LIST_COLS,
    MARKET_COLS,
    PLUGIN_COLS,
    TOGGLE_COLS,
    _adopt_cells,
    _cell_text,
    _list_cells,
    _restore_cells,
    folder_cells,
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
from .state import Checklist, Picker, ProfilePicker, _row_key
from .text import BACKSPACE, ENTER, MOVES, clean_capture, text_width
from .tree import FolderItem, folder_key, folder_of

# one line at 120 columns: digits (row numbers are on every row), the arrows, w (save now), Enter (on every row's
# card), R and A still work, they are only left out here; g / f are short here, their folder card spells them out
KEYS_HELP = "/ 找　Tab 篩選　! 等人　v 預覽　* 全選　空白 勾選　s 停　S 重開　g 包　f 搬　n 新　D 派工　t 送　p 外掛　o 帳號　q 退"
RESTORE_HELP = "↑↓ 移動　空白 勾選　Enter 確認接回　q 取消"
ADOPT_HELP = "↑↓ 移動　空白 勾選　Enter 確認接手　q 取消"
PLUGINS_HELP = "↑↓ 移動　1-9 跳列　/ 搜尋　Enter 選 profile　u 更新這個　U 全部更新　a 新增　r 檢查新版　q 返回　Tab 切到來源"
MARKETS_HELP = "↑↓ 移動　1-9 跳列　/ 搜尋　Enter 選 profile　u 更新這個　U 全部更新　a 新增來源　Tab 切到 plugin　q 返回"
TOGGLE_HELP = "↑↓ 移動　空白 勾選／取消　s 儲存　Esc 返回"
CATALOG_HELP = "↑↓ 移動　/ 搜尋　Enter 選這個　i 自己輸入　q 返回"
PLUGINS_TITLE = "sampan · plugin 管理"
PROFILES_HELP = "↑↓ 移動　r 改名　d 停用／恢復　f forget　i 裝狀態列　a 新增　q 返回"
PROMPT_HELP = "↑↓ 選擇　Enter 送出　Esc 先不管"
FOLDER_PICK_HELP = "↑↓ 移動　Enter 選這個　Esc 取消"
FOLDER_PICK_TITLE = "sampan · 搬到哪個資料夾"
TICK_MS = 1000  # while the list waits for a key, re-capture the preview this often
REFRESH_S = 5  # while the list waits for a key, re-read the session list (state, info cards) this often
USAGE_S = 60  # the account lines' 額度 are re-read at most this often (and whenever the list is entered again)
UI_FILE = "ui.json"  # the screen's own settings in the state dir: {"preview": true|false}
NO_SCREEN = "（看不到畫面）"
SELF_SCREEN = "（sampan 自己就在這個 pane）"


def _now():
    return datetime.now(timezone.utc)


def load_preview_on(state_dir):
    """Whether the list shows the preview (v): ui.json's "preview"; on when the file is missing or unreadable."""
    try:
        data = json.loads((state_dir / UI_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    on = data.get("preview") if isinstance(data, dict) else None
    return on if isinstance(on, bool) else True


def save_preview_on(state_dir, on):
    """Keep v's choice in ui.json (other keys there are kept). Never raises: an unsaved choice lasts this run only."""
    try:
        private_dir(state_dir)
        try:
            data = json.loads((state_dir / UI_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data = dict(data, preview=on) if isinstance(data, dict) else {"preview": on}
        atomic_write(state_dir / UI_FILE, json.dumps(data, ensure_ascii=False), mode=PRIVATE_FILE, encoding="utf-8")
    except (OSError, ValueError, TypeError):
        pass


class Dwell:
    """How long the preview has stayed on one row: due once it has been shown for SEEN_DWELL_S without a break;
    once per stay (moving away and back starts over)."""

    def __init__(self):
        self.name, self.since, self.done = None, None, False

    def see(self, name, now):
        """The row whose preview this frame shows (None: no preview)."""
        if name != self.name:
            self.name, self.since, self.done = name, now, False

    def due(self, name, now):
        """True the first time name has been looked at long enough (name: the row under the cursor now)."""
        if name is None or name != self.name or self.done or now - self.since < SEEN_DWELL_S:
            return False
        self.done = True
        return True


def tmp_roots():
    """System temp directories whose Codex threads the「Codex 其他對話」block leaves out (by path component)."""
    raw = [tempfile.gettempdir(), os.environ.get("TMPDIR")]  # the paths as given next to their realpaths
    roots = ["/tmp", "/private/tmp"] + [x for r in raw if r for x in (r, os.path.realpath(r))]
    return tuple(dict.fromkeys(roots))


class CursesScreen:
    """One curses screen: pick / restore_checklist / adopt_checklist swap the left-hand content, ask asks in the message area, out redraws immediately.
    scr only uses getmaxyx, erase, addstr, refresh, get_wch (tests use a fake object)."""

    def __init__(self, scr, cfg=None, run=None, now_fn=_now, *, clock=time.monotonic, rows_fn=status_rows,
                 own_pane=None, updater=None, usage_fn=None, interactive=None, sampan_path=None, watcher=None,
                 dispatch_watcher=None, dispatches_load=dispatch.load, cancel_fn=None):
        self.scr, self.cfg, self.run, self.now_fn = scr, cfg, run, now_fn
        # watcher: daemon_watch.DaemonWatcher (curses_ui starts / stops it); its latest snapshot colours the Codex rows
        self.watcher, self._snap = watcher, None
        # dispatch_watcher: dispatch_watch.DispatchWatcher; its items feed the summary line (dispatches_load reads the D list)
        self.dispatch_watcher, self.dispatches_load, self.cancel_fn = dispatch_watcher, dispatches_load, cancel_fn
        self._tmp_roots = tmp_roots()
        # usage_fn(cfg, profile) -> Usage | None for the account lines and profile cards (default: the files on disk);
        # interactive(args, env) runs a login with the terminal (curses steps out); None = say what to run instead
        self.usage_fn, self.interactive, self.sampan_path = usage_fn, interactive, sampan_path
        self._usage = None  # (clock time, {profile: Usage}) behind the account lines; None = read again
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
        self.last_filter = 0  # the state filter, kept the same way across actions (not across runs)
        self._folders = None  # (stat of folders.json or None, Tree or None, error or None): read again only when it changes
        self._folders_said = None  # the folders.json error last shown: the same one is not shown again
        self.preview_on = load_preview_on(cfg.state_dir) if cfg is not None else True  # v
        self._looks = (None, {}, {})  # (tmux attach times, seen marks, collected replies) behind the lights
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
        self._apply_snap(rows)
        self._read_looks(rows)
        picker.reload(rows, lights=self._lights(rows))  # keeps the search, the digit buffer and the cursor session
        return True

    def _read_looks(self, rows):
        """Read what the lights need besides the rows (with the list, not every tick): tmux attach times, seen.json
        and the collected dispatches. The first run with no seen.json marks every running session seen now, so it does
        not light up everything nobody has attached to as 待看. Unreadable sources count as nothing seen."""
        if self.cfg is None:
            return
        state_dir = self.cfg.state_dir
        try:
            attach = Tmux(self.run).attach_times() if self.run is not None else None
        except Exception:  # noqa: BLE001  the lights are a hint: a failed read must not crash the screen
            attach = None
        if not (state_dir / "seen.json").exists():
            now = self.now_fn()
            for r in rows:
                if r.state is State.RUNNING:
                    mark_seen(state_dir, r.name, now)
        try:
            replied = replied_at(self.dispatches_load(state_dir))
        except dispatch.DispatchError:
            replied = {}
        self._looks = (attach, load_seen(state_dir), replied)

    def _lights(self, rows):
        """{name: lights label or None} from rows and the last _read_looks."""
        attach, seen, replied = self._looks
        out = {}
        for r in rows:
            attached, last = (attach or {}).get(r.name, (0, None))
            out[r.name] = light(r, attached=attached, last_attached=last, seen=seen.get(r.name),
                                replied=replied.get(r.name))
        return out

    def _mark_seen(self, name):
        """The preview stayed SEEN_DWELL_S on name: record it (seen.json and the lights at once)."""
        now = self.now_fn()
        mark_seen(self.cfg.state_dir, name, now)
        self._looks[1][name] = now

    def _apply_snap(self, rows):
        """Give rows the watcher's latest Codex thread states. Returns True when the snapshot is a new one since the
        last call: then the 額度 cache is dropped too (the watcher has just saved a fresh reading)."""
        snap = self.watcher.latest if self.watcher is not None else None
        apply_codex_status(rows, snap)
        if snap is self._snap:
            return False
        self._snap = snap
        self._usage = None
        return True

    def _others(self, rows, now):
        """(OtherRows, how many more, daemon down) for the「Codex 其他對話」block. Nothing without a watcher or an
        enabled Codex profile, or before the watcher's first read; down when its latest read failed and the list
        has a Codex row (any state)."""
        w = self.watcher
        if w is None or self.cfg is None or not self.cfg.codex_profiles or not getattr(w, "polled", True):
            return [], 0, False
        if w.latest is None:  # nagging about the daemon only makes sense when a Codex session is listed
            return [], 0, any(r.agent == "codex" for r in rows)
        found, extra = other_rows(w.latest, rows, now, tmp_roots=self._tmp_roots)
        return found, extra, False

    def _start_note(self, rows):
        """(starting, failed reason) of the watcher's one auto-start of the Codex daemon; (False, None) otherwise.
        A missing codex command is only worth showing when the list has a Codex row (like the daemon-down line)."""
        w = self.watcher
        if w is None or self.cfg is None or not self.cfg.codex_profiles:
            return False, None
        state = getattr(w, "start_state", None)
        down = getattr(w, "latest", None) is None  # a daemon that is up hides both notes (the block is shown instead)
        failed = state == "failed" and down
        reason = (getattr(w, "start_error", None) or "未知原因") if failed else None
        if reason == codex_daemon.CODEX_NOT_FOUND and not any(r.agent == "codex" for r in rows):
            reason = None
        return state == "starting" and down, reason

    def _load_folders(self):
        """(Tree, error) of folders.json: an empty Tree when there is none, (None, message) when it cannot be trusted.
        Read again only when the file's stat changed (the list is redrawn every second)."""
        if self.cfg is None:
            return None, None
        path = self.cfg.state_dir / F.FOLDERS_FILE
        try:
            st = path.stat()
            stamp = (st.st_mtime_ns, st.st_size, st.st_ino)
        except OSError:
            stamp = None
        if self._folders is None or self._folders[0] != stamp:
            try:
                self._folders = (stamp, F.load(self.cfg.state_dir), None)
            except F.FoldersError as e:
                self._folders = (stamp, None, str(e))
        return self._folders[1], self._folders[2]

    def _folders_note(self, err):
        """The notice for a folders.json error the first time it is seen (a fixed file resets it); None otherwise."""
        if err is None or err == self._folders_said:
            self._folders_said = err
            return None
        self._folders_said = err
        return f"{err}，先照原樣列出"

    @staticmethod
    def _picker_tree(tree, rows):
        """The tree for the Picker (it resolves against the rows it has, so a session added by a refresh lands in its
        folder). None when there is no folder at all (no folders.json, or a broken one): the flat list then keeps
        rows' order exactly as before folders existed; g / f still work, their actions read folders.json themselves."""
        return tree if tree is not None and F.resolve(tree, [r.name for r in rows]).folders else None

    def _left(self, picker, rows, current, now, extra, down):
        """The list lines (tmux rows, then the block) and the cursor's index among them. Row numbers and the name
        column width follow the list as it is without a search (folder rows and sessions hidden in collapsed folders
        get no number); block lines are never numbered or landed on. While searching each hit inside a folder shows
        its folder path, dim, before the name."""
        total = len(rows)
        base = picker.base_items()
        nums = {}
        for _, it in base:
            if not isinstance(it, FolderItem):
                nums[it.name] = len(nums) + 1

        def cells(depth, it):
            if isinstance(it, FolderItem):
                return folder_cells(it, picker.marked_folders, total)
            where = folder_of(picker.tree, it.name) if picker.query is not None else ""
            return _list_cells(it, picker.marked, current, nums.get(it.name, ""), total, depth=depth,
                               prefix=f"{where} · " if where else "", light=picker.lights.get(it.name))
        left = [cells(d, it) for d, it in picker.items]
        full = [cells(d, it) for d, it in base] if picker.query is not None else left
        name_w = max((text_width(_cell_text(c[0])) for c in full + left if len(c) > 1),  # folder rows span the row
                     default=None)
        lead = " " * (len(str(len(rows))) + 3 if rows else 2)  # under the number and the mark of a tmux row
        starting, failed = self._start_note(rows)
        block = [] if picker.filter else block_left(  # 「Codex 其他對話」 only under 全部
            [OwnCols(other_cells(o, now, lead)) for o in picker.visible[picker.n_rows:]], extra,
            down=down, starting=starting, failed=failed)
        if block and not left:
            left = [ListNote(list_empty(rows, picker.query, picker.filter_name), "")]
        cursor = len(left) - picker.n_rows + picker.cursor + (1 if picker.on_other() else 0)
        return left + block, cursor, name_w

    def _account_lines(self, rows):
        """The account lines under the list. Usage (a Codex rollout scan, Claude record files) is cached for USAGE_S
        seconds; the running counts follow rows on every draw."""
        if self.cfg is None:
            return []
        if self._usage is None or self.clock() - self._usage[0] >= USAGE_S:
            fn = self.usage_fn or profile_usage
            self._usage = (self.clock(), {n: fn(self.cfg, p) for n, p in self.cfg.all_profiles.items()
                                          if not p.disabled})
        cache = self._usage[1]
        return account_lines(self.cfg, rows, self.now_fn(), usage_fn=lambda cfg, p: cache.get(p.name))

    def _dispatch_line(self):
        w = self.dispatch_watcher
        return dispatch_summary(w.items) if w is not None else None

    def dispatches(self, *, ask, **_kw):
        """The dispatch list (D), newest first; x cancels the queued one under the cursor after asking, q returns 0.
        Re-read every TICK_MS while waiting for a key (the watcher thread advances them)."""
        def load():
            try:
                return sorted(self.dispatches_load(self.cfg.state_dir), key=lambda d: d.created, reverse=True)
            except dispatch.DispatchError as e:
                self.out(f"❌ {e}")
                return []
        picker = DispatchPicker(load())
        if not picker.items:
            self.out("沒有派工")
            return 0
        saved = self.view
        self._timeout(TICK_MS)
        try:
            while True:
                self.view = dict(title=DISPATCH_TITLE, left=[dispatch_cells(d) for d in picker.items],
                                 cursor=picker.cursor, card=[], preview=None, solo=True,
                                 help=DISPATCH_HELP, cols=dispatch_cols(picker.items), empty="沒有派工", notice=None,
                                 name_w=None)
                self.draw()
                k = self.key()
                if k is None:
                    picker.reload(load())
                    continue
                action = picker.key(k)
                if action is None:
                    continue
                if action[0] == "back":
                    return 0
                self.messages = []
                cancel_flow(self.cfg, self.out, ask, picker.here, cancel_fn=self.cancel_fn)
                picker.reload(load())
        except KeyboardInterrupt:
            return 0
        finally:
            self.view = saved
            self._timeout(-1)

    def pick(self, rows, current):
        """Arrow-key menu. While waiting for a key, re-capture the pane screen of the row under the cursor (a row with a tmux session) every TICK_MS and re-read the list every REFRESH_S seconds.
        A preview that stays on one row for SEEN_DWELL_S marks that session seen; v hides / shows the preview."""
        def load():
            return (gather_cards(self.cfg, self.run, rows) if self.cfg is not None else {}), self.now_fn(), {}
        cards, now, built = load()
        self.warning = getattr(rows, "warning", None)
        self._usage = None  # back on the list (after an action): read the 額度 again
        self._apply_snap(rows)
        others, extra, down = self._others(rows, now)
        raw, err = self._load_folders()  # None (broken file): the flat list, said once
        fnote = self._folders_note(err)
        tree = self._picker_tree(raw, rows)
        names = [r.name for r in rows] + [o.name for o in others]
        if tree is not None:
            names += [folder_key(p) for p in F.resolve(tree, [r.name for r in rows]).folders]
        self._read_looks(rows)
        picker = Picker(rows, self.last_name if self.last_name in names else current, clock=self.clock,
                        others=others, tree=tree, own_pane=self.own_pane, filter=self.last_filter,
                        lights=self._lights(rows))
        shot, tick, refreshed, dwell = (None, None), False, self.clock(), Dwell()
        self._timeout(TICK_MS)
        try:
            while True:
                if self._apply_snap(rows):
                    built = {}  # the info cards show agent_status (and the block's cards its state)
                others, extra, down = self._others(rows, now)
                if others != picker.others:
                    picker.reload(rows, others)
                under = picker.visible[picker.cursor].name if picker.visible else None
                if self.preview_on and dwell.due(under, self.clock()):
                    self._mark_seen(under)
                picker.set_lights(self._lights(rows))  # the snapshot or a seen mark may have changed them
                card, preview = [], None
                if picker.visible:
                    r = picker.visible[picker.cursor]
                    name = self.last_name = r.name
                    info = cards.get(name)
                    if isinstance(r, FolderItem):  # no preview: the folder's own card
                        card = folder_card(r, rows, picker.tree)
                    elif picker.on_other():  # read only: its card (and rollout context) is built only when selected
                        if name not in built:
                            built[name] = other_card(r, now, home=self.cfg.home if self.cfg is not None else None)
                        card = built[name]
                    elif self.preview_on and (info or {}).get("panes", 0) > 0 and self.run is not None:
                        if tick or shot[0] != name:
                            shot = (name, self.capture(r, current))
                        preview = shot[1]
                        card = preview_header(self.cfg, r, info)
                    else:
                        if name not in built:
                            built[name] = card_lines(self.cfg, r, info, now)
                        card = built[name]
                looked = preview is not None and preview not in ([NO_SCREEN], [SELF_SCREEN])  # a real screen only
                dwell.see(picker.visible[picker.cursor].name if looked else None, self.clock())
                left, cursor, name_w = self._left(picker, rows, current, now, extra, down)
                title = self.title if not picker.filter else f"{self.title} › {picker.filter_name}（{picker.filter_count}）"
                self.view = dict(title=title, left=left, cursor=cursor, card=card, preview=preview,
                                 help=KEYS_HELP, cols=list_cols(rows),
                                 empty=list_empty(rows, picker.query, picker.filter_name),
                                 name_w=name_w, accounts=self._account_lines(rows), dispatch=self._dispatch_line(),
                                 notice=self.warning or fnote or (self.updater.hint() if self.updater is not None else None))
                picker.width = self.scr.getmaxyx()[1] - 2  # draw()'s width less the prompt row's leading space
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
                        new_raw, err = self._load_folders()
                        if new_raw is not raw:  # changed on disk (another sampan, sampan folder …)
                            raw = new_raw
                            fnote = (self._folders_note(err) or fnote) if err else self._folders_note(None)
                            picker.set_tree(self._picker_tree(raw, rows))
                    continue
                action = picker.key(k)
                self.last_filter = picker.filter
                if action is not None and action[0] == "preview":
                    self.preview_on = not self.preview_on
                    if self.cfg is not None:
                        save_preview_on(self.cfg.state_dir, self.preview_on)
                    shot = (None, None)  # shown again: capture now, not the screen from before
                    continue
                if action is not None and action[0] == "collapse":  # memory only: saved here, the list stays as it is
                    save_collapsed(self.cfg, action[1][0], action[1][1] == "1")
                    raw = self._load_folders()[0]  # our own write: not a change to apply
                    continue
                if action is not None and action[0] == "restart!":  # the plan the user just confirmed
                    action = ("restart!", [picker.restart_plan])
                if action is not None and (action[0] == "desktop"
                                           or any(str(n).startswith(OTHER_KEY) for n in action[1])):
                    self.messages = [GO_DESKTOP]  # a「Codex 其他對話」row never becomes an action
                    continue
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

    def choose_folder(self, labels):
        """f's destination list: returns the index of the row picked with Enter, None on Esc / q / Ctrl-C."""
        saved, cur = self.view, 0
        self._timeout(-1)
        try:
            while True:
                self.view = dict(title=FOLDER_PICK_TITLE, left=[(x,) for x in labels], cursor=cur, card=[],
                                 preview=None, help=FOLDER_PICK_HELP, cols=(), empty=None, notice=None, name_w=None)
                self.draw()
                k = self.key()
                if k is None:
                    continue
                if k in MOVES:
                    cur = max(0, min(len(labels) - 1, cur + MOVES[k]))
                elif k in ENTER:
                    return cur if labels else None
                elif k in ("\x1b", "q"):
                    return None
        except KeyboardInterrupt:
            return None
        finally:
            self.view = saved

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

        def show(picker, prompt=True):
            """Draw layer 1 of the current tab (the list, its card and help) with picker's prompt (prompt=False: none)."""
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
            self.prompt = picker.prompt if prompt else None
            self.draw()
        try:
            while True:
                picker = pickers[tab]
                show(picker)
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
                goto, changed = None, []
                if tab == "market":
                    goto, changed = self._market_action(cfg, ask, kind, names, mrows)
                elif kind == "open":
                    r = next(x for x in rows if x.name == names[0])
                    changed = self._plugin_toggles(cfg, ask, r.pid, r.source, latest,
                                                   new_source=enter_source(cfg, r.pid, r.source))
                elif kind == "update":
                    r = next(x for x in rows if x.name == names[0])
                    changed = update_flow(cfg, self.run, self.out, ask, [r.pid])
                elif kind == "update_all":
                    changed = update_flow(cfg, self.run, self.out, ask, None)
                elif kind == "check":
                    latest = check_latest(cfg, self.run, self.out, snapshot(cfg))
                    checked_at = self.now_fn().astimezone().strftime("%H:%M")
                elif kind == "add":
                    picked = add_flow(cfg, self.out, ask, snapshot(cfg), self._choose_catalog)
                    if picked:
                        pid, new_source = picked
                        changed = self._plugin_toggles(cfg, ask, pid, new_source or cfg.marketplaces.get(_mkt(pid)),
                                                       latest, new_source=new_source)
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
                if changed:  # ask on the list as it is now, not on the frame of before the save
                    show(picker, prompt=False)
                    offer(changed)
        finally:
            self.prompt = None
            self.view = saved

    def profiles(self, *, ask, **_kw):
        """The profile (帳號) screen (o): the profiles on the left (disabled ones dim), the card of the one under the
        cursor on the right; r/d/f/i/a act, the config and the list are re-read after every action. q returns 0.
        The main list goes on with the config as it is afterwards (self.cfg)."""
        cfg = reload_config(self.cfg, self.out)
        usage_fn = self.usage_fn or profile_usage
        prows = load_rows(cfg, self.run, self.out, self.rows_fn)
        picker = ProfilePicker(prows, clock=self.clock)
        saved, cards = self.view, {}
        self._timeout(-1)
        try:
            while True:
                here = picker.visible[picker.cursor] if picker.visible else None
                if here is not None and here.name not in cards:
                    cards[here.name] = profile_card(cfg, here, self.now_fn(), usage_fn)
                self.view = dict(title=PROFILES_TITLE, left=[profile_cells(r) for r in picker.visible],
                                 cursor=picker.cursor, card=cards.get(here.name, []) if here else [], preview=None,
                                 help=PROFILES_HELP, cols=profile_cols(picker.visible), empty="目前沒有任何 profile（a 新增）",
                                 notice=None, name_w=None)
                self.draw()
                k = self.key()
                if k is None:
                    continue
                action = picker.key(k)
                if action is None:
                    continue
                kind, names = action
                if kind == "back":
                    return 0
                self.messages = []  # a new action: the message area holds only this action's result
                goto = profile_action(cfg, self.run, self.out, ask, kind, names[0] if names else None,
                                      interactive=self.interactive, login_outside=False,
                                      sampan_path=self.sampan_path)
                cfg = reload_config(cfg, self.out)
                prows, cards = load_rows(cfg, self.run, self.out, self.rows_fn), {}
                picker.reload(prows)  # the cursor stays on the same profile
                if goto is not None:
                    picker._goto(goto)
        finally:
            self.cfg = cfg
            self.prompt = None
            self.view = saved

    def _market_action(self, cfg, ask, kind, names, mrows):
        """One action of the 來源 tab. Returns (the source just added, to move the cursor to it, or None;
        the profiles that need a restart)."""
        if kind == "open":
            r = next(x for x in mrows if x.name == names[0])
            return None, self._market_toggles(cfg, ask, r.name, r.source)
        elif kind == "update":
            market_update_flow(cfg, self.run, self.out, ask, [names[0]])
        elif kind == "update_all":
            market_update_flow(cfg, self.run, self.out, ask, None)
        elif kind == "add":
            src = market_add_flow(self.out, ask)
            if src:
                return src, self._market_toggles(cfg, ask, None, src)
        return None, []  # "check" (r) belongs to the plugin tab

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


def run_outside(scr, args, env=None):
    """Temporarily hand the terminal to another program (tmux attach, a login): curses steps out first, and when that program ends (detach) we return to the original screen."""
    import curses
    import subprocess
    curses.def_prog_mode()
    curses.endwin()
    try:
        return subprocess.call(args, env=env)
    finally:
        curses.reset_prog_mode()
        scr.clear()
        scr.refresh()


def curses_ui(cfg, run, out, *, current_session=None, own_pane=None, exec_fn=os.execvp, wrapper=None,
              run_outside=run_outside, updater=None, watcher=None, dispatch_watcher=None, **kw):
    """The whole ui opens curses only once. When attaching to a session outside tmux, curses steps out temporarily and tmux attach runs in the foreground; after detach we are back at the list.
    On exit the last contents of the message area are printed on the terminal.
    watcher (daemon_watch.DaemonWatcher, None = none) and dispatch_watcher (dispatch_watch.DispatchWatcher) are started here
    and stopped when the ui ends, however it ends."""
    pending, box = [], {}

    def ui_rows(c, r):  # the daemon is read by the watcher thread only: never on the ui thread
        return status_rows(c, r, snapshot_fn=lambda *a, **k: watcher.latest if watcher is not None else None)
    rows_fn = kw.get("rows_fn", ui_rows)

    def main(scr):
        screen = box["screen"] = CursesScreen(scr, cfg, run, rows_fn=rows_fn, own_pane=own_pane,
                                              updater=updater, interactive=lambda args, env: run_outside(scr, args, env),
                                              watcher=watcher, dispatch_watcher=dispatch_watcher)

        def set_cfg(c):
            screen.cfg = c
            if dispatch_watcher is not None:
                dispatch_watcher.cfg = c
            if watcher is not None:
                watcher.cfg = c  # renamed / added Codex profiles: the next poll saves under the new names
        opts = dict(picker_fn=screen.pick, restore_picker_fn=screen.restore_checklist, prompt_fn=screen.prompt_choice,
                    adopt_picker_fn=screen.adopt_checklist, plugins_fn=screen.plugins, profiles_fn=screen.profiles,
                    dispatches_fn=screen.dispatches, choose_folder_fn=screen.choose_folder,
                    set_cfg_fn=set_cfg,
                    exec_fn=lambda f, args: pending.append((f, args)),
                    attach_fn=lambda args: run_outside(scr, args), loop=True,
                    choose_dir_fn=lambda start, *, allow_missing: screen.choose_dir(
                        start, home=str(cfg.home), allow_missing=allow_missing,
                        title="選搬過去的目錄" if allow_missing else "選工作目錄", **dir_context(screen.cfg)))
        opts.update(rows_fn=rows_fn)
        opts.update(kw)
        return cmd_ui(cfg, run, screen.out, screen.ask, current_session=current_session, own_pane=own_pane,
                      updater=updater, **opts)

    if wrapper is None:
        import curses
        _curses_prepare()
        wrapper = curses.wrapper
    if watcher is not None:
        watcher.start()
    if dispatch_watcher is not None:
        dispatch_watcher.start()
    try:
        rc = wrapper(main)
    except KeyboardInterrupt:
        rc = 0
    finally:
        if watcher is not None:
            watcher.stop()
        if dispatch_watcher is not None:
            dispatch_watcher.stop(notice=out)  # curses is closed: let a delivery in flight finish before exec / exit
    screen = box.get("screen")
    for line in (screen.messages if screen else []):
        out(line)
    if pending:
        exec_fn(*pending[0])
    return rc
