"""Shared text helpers and key constants: display width (CJK takes two cells), pad/truncate, wrapping, pane capture cleanup. Depends on no other ui module."""
import unicodedata

ENTER = ("\n", "\r", "KEY_ENTER")
BACKSPACE = ("\x7f", "\b", "KEY_BACKSPACE", "^?", "^H")
MOVES = {"KEY_UP": -1, "k": -1, "KEY_DOWN": 1, "j": 1}
QUOTE_LINES = 3


def _width(ch):
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def text_width(text):
    return sum(_width(ch) for ch in text)


def pad(text, width):
    """Pad with spaces or truncate by display width (CJK takes two cells), never cutting a character in half."""
    out, used = [], 0
    for ch in text:
        w = _width(ch)
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out) + " " * max(0, width - used)


def _wrap(text, width, max_lines=QUOTE_LINES):
    if width <= 0:
        return []
    lines, cur, used = [], [], 0
    for ch in text:
        w = _width(ch)
        if used + w > width:
            lines.append("".join(cur))
            cur, used = [], 0
        cur.append(ch)
        used += w
    if cur:
        lines.append("".join(cur))
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and text_width(last) + 1 > width:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


def _printable(ch):
    return unicodedata.category(ch) not in ("Cc", "Cf", "Cs")


def clean_capture(raw):
    """Text from capture-pane -p → lines: strip control characters and trailing whitespace, turn tabs into spaces, drop trailing blank lines."""
    lines = ["".join(ch for ch in line.replace("\t", "    ") if _printable(ch)).rstrip()
             for line in (raw or "").split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return lines
