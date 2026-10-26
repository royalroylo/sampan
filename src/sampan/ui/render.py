"""Screens (pure functions): turn state, info card and messages into lines of [(text, style), …]. Never touches curses."""
from .. import update as update_mod
from ..prompts import prompt_choices, prompt_lines
from .cards import Keys, StateLine, Wrap, agent_style, message_style, state_style
from .codex_others import ListNote, OwnCols
from .dirpick import tilde
from .state import list_state
from .text import _wrap, pad, text_width, wrap_items

WIDE = 100  # at this width and above the info card goes on the right, below it goes under the list
LIST_COLS = (6, 10, 7)  # render's default column widths (other than the name); the session list uses list_cols (one more column, profile)
PROFILE_MAX = 14  # maximum width of the profile column (longer ones are truncated)
NAME_GAP = 2  # how many cells the name column keeps beyond the longest name; the other columns follow right after, the extra width stays on the right
PREVIEW_MIN = 5  # minimum number of lines left for the preview in a narrow layout (when the list is long)


LABELS = ("狀態", "agent", "目錄", "對話", "最後活動", "context", "tmux")


def preview_lines(lines, width, height):
    """Preview area: the last height lines, each padded/truncated to width by display width (CJK never cut in half)."""
    if width <= 0 or height <= 0:
        return []
    return [pad(x, width) for x in lines[-height:]]


def _cell(c):
    return c if isinstance(c, tuple) else (c, "")


def _cell_text(c):
    """The text of a cell; a list cell is several (text, style) runs (the dim folder path of a search hit)."""
    return "".join(t for t, _s in c) if isinstance(c, list) else _cell(c)[0]


def _fit(segs, width):
    """A run of (text, style) truncated/padded by display width to exactly width."""
    out, used = [], 0
    for text, style in segs:
        if used >= width:
            break
        piece = pad(text, width - used).rstrip(" ") if text_width(text) > width - used else text
        out.append((piece, style))
        used += text_width(piece)
    return out + ([(" " * (width - used), "")] if used < width else [])


def _cells(cells, cols, width, cap=None):
    """One list row → [(text, style), …]: the first column uses the remaining width (at most cap cells when cap is given), the other columns follow cols, extra width stays on the right.
    A cell may be a string or (string, style); the first one may also be a list of (string, style) runs."""
    runs = cells[0] if isinstance(cells[0], list) else None
    cells = [(_cell_text(runs), "") if runs else _cell(cells[0])] + [_cell(c) for c in cells[1:]]
    first = width - 1 - sum(cols) - len(cols)
    if first < 4:
        return _fit([(" " + " ".join(t for t, _s in cells), "")], width)
    if cap is not None:
        first = max(4, min(first, cap))
    segs = [(" ", "")] + _fit(runs, first) if runs else [(" " + pad(cells[0][0], first), cells[0][1])]
    segs += [(" " + pad(t, w), s) for (t, s), w in zip(cells[1:], cols)]
    return _fit(segs, width)


def _list_rows(left, cursor, cols, width, height, empty, name_w=None):
    if height <= 0:
        return []
    if not left:
        return [[(pad(" " + (empty or ""), width), "")]] + [[(pad("", width), "")]] * (height - 1)
    top = max(0, min(cursor - height // 2, len(left) - height))
    # with other columns present, the name column extends only NAME_GAP cells past the longest name and the other columns follow right after it (not pushed to the far right)
    # name_w (the widest name cell of the whole list) keeps the columns still while the list is filtered
    plain = [c for c in left if not isinstance(c, (ListNote, OwnCols))]
    widest = name_w if name_w is not None else max((text_width(_cell_text(c[0])) for c in plain), default=0)
    cap = widest + NAME_GAP if cols and left else None
    rows = []
    for i, cells in enumerate(left[top:top + height]):
        if isinstance(cells, ListNote):  # display only: a section title or a note, never highlighted
            rows.append(_fit([(" " + cells, cells.style)], width))
            continue
        segs = (_cells(cells, cells.cols, width, min(cells.cap, max(4, width // 2))) if isinstance(cells, OwnCols)
                else _cells(cells, cols, width, cap))
        if top + i == cursor:
            segs = [(t, f"sel+{s}" if s else "sel") for t, s in segs]
        rows.append(segs)
    return rows + [[(pad("", width), "")]] * (height - len(rows))


def _card_line(i, line, width):
    """One info card line → [(text, style), …]: name bold, separator grey, column names dim, state coloured, available keys cyan."""
    text = pad(line, width)
    if i == 0:
        return [(text, "head")]
    if line.startswith("─"):
        return [(text, "dim")]
    if line.startswith("↷"):
        return [(text, "warn")]
    if line == "最後一句話":
        return [(text, "label")]
    for name in LABELS:
        label = pad(name, 9)
        if line.startswith(label):
            value = line[len(label):]
            style = state_style(line.state) if isinstance(line, StateLine) else ""
            return _fit([(label, "label"), (value, style)], width)
    return [(text, "")]


def _card_rows(card, width):
    out = []
    for line in card:
        if isinstance(line, Keys):
            out += [[(pad(x, width), "key")] for x in wrap_items(line, width)]
        elif isinstance(line, tuple):  # a line that is already styled
            out.append(_fit(list(line), width))
        elif isinstance(line, Wrap):
            out += [[(pad(x, width), "")] for x in _wrap(line, width)]
        else:
            out.append(_card_line(len(out), line, width))
    return out


def _panel(crows, preview, width, height):
    """Content on the right (or underneath): info card / header; with a preview, a separator line follows and the remaining lines hold the last lines of the preview."""
    rows = crows[:max(0, height)]
    rest = height - len(rows)
    if preview is not None and rest >= 2:
        rows += [[(pad("─" * width, width), "dim")]] + [[(x, "")] for x in preview_lines(preview, width, rest - 1)]
    return rows


def screen_title(updater=None):
    """Title of the list screen: sampan plus the running version (e.g. sampan 0.6.3), so it is obvious at a glance whether it is an old process that has not been restarted."""
    ver = updater.current if updater is not None else update_mod.current_version()
    return f"sampan {ver}" if ver and ver != "unknown" else "sampan"


def _wrap_message(text, width):
    """[(line, style)] for one message: wrapped to the screen width, continuation lines indented by two cells, same style."""
    style = message_style(text)
    first = _wrap(text, width - 1, max_lines=1 << 30) or [""]
    out = [(first[0], style)]
    rest = text[len(first[0]):].lstrip(" ")
    out += [("  " + piece, style) for piece in _wrap(rest, width - 3, max_lines=1 << 30)]
    return out


def _fit_messages(groups, n, width):
    """The last n wrapped lines; the newest message keeps its head (cut with … if too long), older ones keep their tail."""
    shown = []
    for i, g in enumerate(reversed(groups)):
        space = n - len(shown)
        if space <= 0:
            break
        if len(g) <= space:
            shown = g + shown
        elif i == 0:
            g = g[:space]
            text, style = g[-1]
            while text and text_width(text) + 1 > width - 1:
                text = text[:-1]
            shown = g[:-1] + [(text + "…", style)]
        else:
            shown = g[-space:] + shown
    return shown


def render(*, title, left, cursor, card, preview=None, messages=(), prompt=None, help="", width, height,
           cols=LIST_COLS, empty=None, notice=None, extra=(), name_w=None, accounts=(), dispatch=None, solo=False):
    """Turn one screen into [(text, style), …] lines that fit within height lines, each with display width at most width (styles: title / sel / dim / prompt / "").
    At width >= WIDE the info card sits to the right of the list, otherwise under it; when too small, lines are dropped from the top, keeping the message area and key help to the end.
    When preview (lines already through clean_capture) is not None, card is a compact header, followed by a separator line and the last lines of the pane screen.
    notice (e.g. the new-version hint) is a fixed line above the key help, kept to the end like the key help.
    extra ([(text, style)], e.g. the question and options of an agent startup prompt) goes under the message area and is also kept to the end.
    name_w, when given, is the name column width to cap at instead of the widest name in left (a filtered list keeps the full list's columns).
    accounts (the main list's one line per profile, a str or (text, style)) sit between the list area and the message area, kept to the end like notice.
    dispatch (the dispatch summary line, or None) follows the accounts.
    solo: the list takes the whole width (no info card), for screens whose rows say everything themselves."""
    if width <= 0 or height <= 0:
        return []
    messages = list(messages)
    title_line = [(pad(f"═══ {title} " + "═" * width, width), "title")]
    help_line = [(pad(" " + help, width), "dim")]
    prompt_lines = [[(pad(" " + text, width), style)] for text, style in extra]
    prompt_lines += [[(pad(" " + prompt, width), "prompt")]] if prompt is not None else []
    prompt_lines += [[(pad(" " + notice, width), "attn")]] if notice else []
    account_lines = [[(pad(" " + a, width), "muted")] if isinstance(a, str) else [(pad(" " + a[0], width), a[1])]
                     for a in accounts]
    account_lines += [[(pad(" " + dispatch, width), "attn")]] if dispatch else []
    room = height - 3 - len(prompt_lines) - len(account_lines)  # title, separator line, key help
    groups = [_wrap_message(m, width) for m in messages]
    wrapped = [piece for g in groups for piece in g]
    n_msg = min(len(wrapped), max(1, height // 3)) if wrapped else 0
    n_msg = max(0, min(n_msg, room - 1)) if room > 1 else min(n_msg, max(0, room))
    body_h = max(0, room - n_msg)
    shown = _fit_messages(groups, n_msg, width) if n_msg else []
    msg_lines = [[(pad(" " + m, width), style)] for m, style in shown]
    if width >= WIDE and not solo:
        cw = max(36, min(64, width // 2))
        lw = width - cw - 3
        lrows = _list_rows(left, cursor, cols, lw, body_h, empty, name_w)
        crows = _panel(_card_rows(card, cw), preview, cw, body_h)
        crows += [[(pad("", cw), "")]] * (body_h - len(crows))
        body = [segs + [(" │ ", "dim")] + c for segs, c in zip(lrows, crows)]
        sep = "─" * (lw + 1) + "┴" + "─" * (width - lw - 2)
    else:
        crows = _card_rows([x for x in card if x != ""], width)  # drop blank lines when stacked underneath
        avail = body_h - 1  # separator line between the list and the info card
        need = max(1, len(left))
        want = len(crows) + (1 + PREVIEW_MIN if preview is not None else 0)
        list_h = min(need, max(avail - want, (avail + 1) // 2)) if avail > 0 else body_h
        card_h = (min(len(crows), avail - list_h) if preview is None else avail - list_h) if avail > 0 else 0
        body = _list_rows(left, cursor, cols, width, body_h - card_h - (1 if card_h else 0), empty, name_w)
        if card_h:
            shown = _panel(crows, preview, width, card_h)
            shown += [[(pad("", width), "")]] * (card_h - len(shown))
            body += [[("─" * width, "dim")]] + shown
        sep = "─" * width
    lines = [title_line] + body + [[(sep, "dim")]] + account_lines + msg_lines + prompt_lines + [help_line]
    return lines[-height:]


DIR_HELP = ("↑↓ 移動　→/Enter 進入　← 上一層　空白 選這個目錄　+ 新建資料夾　/ 直接打路徑　Tab 最近用過　. 顯示隱藏"
            "　Esc 取消")


def render_dirpick(dp, width, height, *, title="選工作目錄"):
    """Directory chooser screen (pure function, dp is a dirpick.DirPicker): the title line is the current directory; subdirectories on the left, on the right the "recently used" ones and the sessions
    already in the directory under the cursor. Below WIDE columns only the left side exists (Tab switches to recently used, which then fills the left, and the session hint goes in the message area)."""
    home = dp.home
    head = tilde(dp.cwd or "", home)
    head = head if head.endswith("/") else head + "/"
    on_list = dp.focus == "list" or not dp.recent
    wide = width >= WIDE
    note = dp.note()
    messages = [dp.message] if dp.message else []
    if wide or on_list:
        left = [(("● " if i == dp.cursor else "  ") + e + ("" if e == ".." else "/"),) for i, e in enumerate(dp.entries)]
        cursor = dp.cursor if on_list else -1
    else:
        left = [("  " + tilde(d, home),) for d in dp.recent]
        cursor, head = dp.rcursor, "最近用過"
    card = []
    if wide:
        room = max(1, height - 6 - (1 if dp.prompt is not None else 0) - len(messages) - (3 if note else 0))
        top = max(0, dp.rcursor - room + 1) if not on_list else 0
        card = ["最近用過"]
        for i, d in enumerate(dp.recent[top:top + room], top):
            text = "  " + tilde(d, home)
            card.append(((text, "sel"),) if not on_list and i == dp.rcursor else text)
        if not dp.recent:
            card.append("  （還沒有）")
        if note:
            card += ["", Wrap(note)]
    elif note:
        messages.append(note)
    return render(title=f"{title}　{head}", left=left, cursor=cursor, card=card, messages=messages, prompt=dp.prompt,
                  help=DIR_HELP, width=width, height=height, cols=(), empty="（沒有子目錄）")


def prompt_frame(name, agent, p, cursor):
    """How an agent startup prompt looks in the message area (pure function): the question (and extra text), the options (the one under the cursor is preceded by ●)."""
    head, *detail = prompt_lines(name, agent, p)
    lines = [(head, "attn")] + [(line, "muted") for line in detail]
    for k, (label, _value) in enumerate(prompt_choices(p)):
        lines.append((("  ● " if k == cursor else "    ") + label, "sel" if k == cursor else ""))
    return lines


def frame_text(frame):
    return ["".join(text for text, _style in segs) for segs in frame]


def _profile_cell(r):
    return "-" if r.agent == "codex" else (r.profile or "-")


def list_empty(rows, query, kind="全部"):
    """Message for an empty session list: no sessions at all, none under the state filter kind, or no name matching
    the search (within that filter)."""
    if not rows:
        return "目前沒有任何 session（n 開新的）"
    if kind != "全部":
        return f"{kind}的 session 裡沒有名稱含「{query}」的" if query else f"沒有{kind}的 session"
    return f"沒有名稱含「{query}」的 session" if query else "目前沒有任何 session（n 開新的）"


def list_cols(rows):
    """Widths of the session list columns other than the name: agent, profile (the longest profile, at most PROFILE_MAX cells), state, claude status."""
    pw = max([text_width(_profile_cell(r)) for r in rows] + [1])
    return (6, min(PROFILE_MAX, pw), 10, 7)


def _list_cells(r, marked, current, num=None, total=0, *, depth=0, prefix=""):
    """One session list row. With num, the row starts with its 1-based position in the unfiltered list, right-aligned to the width of total
    (num "" keeps the space blank). depth indents the name two cells per folder level; prefix (a search hit's
    "folder path · ") goes dim before the name."""
    mark = "●" if r.name in marked else " "
    here = " ←" if r.name == current else ""
    lead = f"{num:>{len(str(total))}} " if num is not None else ""
    head = f"{lead}{mark} {'  ' * depth}"
    name = [(head, ""), (prefix, "dim"), (f"{r.name}{here}", "")] if prefix else f"{head}{r.name}{here}"
    return (name, r.agent, _profile_cell(r),
            (list_state(r), state_style(r.state)), (r.agent_status, agent_style(r.agent_status)))


def folder_cells(item, marked, total=0):
    """One folder row (ui.tree.FolderItem): no number, the mark, the indent, ▸ (collapsed) or ▾, the name and how many
    sessions are under it; a collapsed one also says how many of those are busy. No agent / profile / state columns."""
    mark = "●" if item.path in marked else " "
    lead = " " * (len(str(total)) + 1) if total else ""
    busy = f"　{item.busy} 忙" if item.collapsed and item.busy else ""
    text = f"{lead}{mark} {'  ' * item.depth}{'▸' if item.collapsed else '▾'} {item.path.rpartition('/')[2]} ({item.total}){busy}"
    return (text, "", "", "", "")


def _restore_cells(c, box):
    mark = "x" if c.row.tmux in box.marked else (" " if not c.skip else "-")
    return (f"[{mark}] {c.row.tmux}", c.row.agent, c.row.profile or "-")


def _adopt_cells(c, box):
    mark = "x" if c.key in box.marked else (" " if not c.skip else "-")
    return (f"[{mark}] {c.where}", c.agent, c.ago)


PLUGIN_STYLE = {"最新": "ok", "有新版": "warn", "不一致": "err", "未裝": "muted"}
PLUGIN_COLS = (5, 6)  # plugin list: installed/total, state
TOGGLE_COLS = (12, 22)  # profile toggles: installed version, note


def plugin_cells(row, num, total):
    """One plugin list row: number (right-aligned to the width of total) and name, installed/total, coloured state."""
    return (f"{num:>{len(str(total))}} {row.name}", f"{len(row.installed)}/{row.total}",
            (row.state, PLUGIN_STYLE[row.state]))


def toggle_cells(t, c):
    """One profile row of layer 2 (t is a ProfileToggles, c a ProfileChoice): box and profile, installed version, note."""
    note = t.note(c)
    return (f"{t.mark(c)} {c.profile}", (c.installed or "—")[:12],
            (note or "", "attn" if note and note.startswith("←") else "muted"))


MSTYLE = {"一致": "ok", "沒用到": "muted", "來源不同": "err", "清單外": "warn"}
MARKET_COLS = (5, 8)  # marketplace list: registered/total, state


def market_cells(row, num, total):
    """One marketplace list row (a MarketRow): number and name, registered/total, coloured state."""
    return (f"{num:>{len(str(total))}} {row.name}", f"{len(row.registered)}/{row.total}",
            (row.state, MSTYLE[row.state]))


def tab_title(base, tab):
    """base + the tab switch, the active one wrapped in ▸…◂: "… ［▸plugin◂｜來源］" (tab is "plugin" or "market")."""
    words = [("plugin", "plugin"), ("market", "來源")]
    return f"{base} ［" + "｜".join(f"▸{w}◂" if k == tab else w for k, w in words) + "］"
