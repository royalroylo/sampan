"""Display-width helpers (CJK takes two cells). A leaf module: imports nothing from sampan."""
import unicodedata


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
