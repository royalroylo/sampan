"""The main list's keys (KEY_GROUPS): the ? page (help_frame, pure) and the bottom help line (FOOTER) both come from it.
An entry is (key, what it does) or (key, what it does, its short name on the bottom line)."""
from ..textwidth import ljust, text_width

KEY_GROUPS = [
    ("移動與選取", [
        ("↑ ↓", "移動游標"),
        ("1–9", "跳到第幾列（10 以上 1 秒內打兩位數）"),
        ("/", "搜尋名稱或別名（Esc 取消）", "找"),
        ("Tab／Shift+Tab", "依狀態篩選：全部、等人、執行中…", "篩選"),
        ("!", "直接看「等人」，再按回到「全部」", "等人"),
        ("空白", "勾選／取消這一列（勾好幾列一起做）", "勾選"),
        ("*", "全選；都勾了再按一次全部取消", "全選"),
        ("v", "顯示／隱藏右邊的即時預覽"),
    ]),
    ("session", [
        ("Enter", "進到這個 session（已停止的先接回）"),
        ("s", "停掉（agent 正常結束，對話保留）", "停"),
        ("S", "重開：停掉再接回同一段對話，不進去", "重開"),
        ("t", "傳訊息給它（勾了多列就一起送）", "送"),
        ("n", "開一個新的 session", "新"),
        ("R", "一次接回好幾個已停止的"),
        ("A", "接手 tmux 外的 claude／codex"),
        ("e", "改名（tmux 與登記表一起改）"),
        ("L", "設別名（下次啟動套用到 RC 名稱）"),
        ("P", "換 profile 接回同一段（只限 Claude）"),
        ("m", "換工作目錄，接回同一段對話"),
        ("r／a", "標成 retired／改回 active"),
        ("X", "從登記表移除（已停止或 retired 的）"),
    ]),
    ("資料夾", [
        ("g", "把游標或勾選的包成新資料夾", "包"),
        ("f", "搬進某個資料夾", "搬"),
        ("←", "跳到所在的資料夾列"),
        ("Enter（資料夾）", "收合／展開"),
        ("e／X（資料夾）", "改名／刪除資料夾（session 不動）"),
    ]),
    ("其他畫面與工具", [
        ("D", "派工清單（取消排隊中的）", "派工"),
        ("p", "plugin 與 marketplace 管理", "外掛"),
        ("o", "帳號：額度、改名、停用、新增", "帳號"),
        ("w", "立刻存一次登記表"),
        ("U", "更新 sampan（有新版時）"),
        ("?", "這一頁", "說明"),
        ("q", "離開 sampan", "退"),
    ]),
]

def _footer():
    """The bottom line: ? first, then the keys with a short name in the table's order. Tab／Shift+Tab shows as Tab."""
    items = [(k.split("／")[0], e[2]) for _g, keys in KEY_GROUPS for e in keys if len(e) == 3 for k in [e[0]]]
    items.sort(key=lambda kv: kv[0] != "?")
    return "　".join(f"{k} {name}" for k, name in items)


FOOTER = _footer()
TITLE = "sampan 按鍵說明"
FOOT = "按任意鍵回到清單"


def _lines():
    key_w = max(text_width(e[0]) for _g, keys in KEY_GROUPS for e in keys)
    out = []
    for group, keys in KEY_GROUPS:
        if out:
            out.append([])
        out.append([(group, "head")])
        out += [[("  " + ljust(e[0], key_w) + "  ", "key"), (e[1], "")] for e in keys]
    return out


def _cut(segs, width):
    """segs shortened to width display columns."""
    out, used = [], 0
    for text, style in segs:
        room = width - used
        if room <= 0:
            break
        while text_width(text) > room:
            text = text[:-1]
        out.append((text, style))
        used += text_width(text)
    return out


def help_frame(width, height):
    """The ? page as a frame for CursesScreen._paint: title, the groups (in two columns when one does not fit the
    height and the width allows), and the way back on the last row. A screen too small shows what fits."""
    body = _lines()
    room = max(1, height - 3)
    col_w = max(text_width("".join(t for t, _s in line)) for line in body if line) + 4
    columns = [body]
    if len(body) > room and width >= 2 * col_w:
        breaks = [i for i, line in enumerate(body) if not line]  # split at a group break, columns as even as can be
        split = min(breaks, key=lambda i: max(i, len(body) - i - 1))
        columns = [body[:split], body[split + 1:]]
    rows = []
    for i in range(max(len(c) for c in columns)):
        segs = []
        for n, col in enumerate(columns):
            line = col[i] if i < len(col) else []
            if n < len(columns) - 1:
                used = text_width("".join(t for t, _s in line))
                line = line + [(" " * (col_w - used), "")]
            segs += line
        rows.append(_cut(segs, width - 1))
    frame = [[(TITLE, "title")], []] + rows[:room]
    while len(frame) < height - 1:
        frame.append([])
    frame.append(_cut([(FOOT if len(rows) <= room else f"{FOOT}（視窗太小，沒有全部列出）", "dim")], width - 1))
    return frame[:height]
