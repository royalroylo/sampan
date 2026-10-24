"""Text-level edits of config.toml: every byte outside the edited spot stays unchanged."""
import json
import re
import tomllib

NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _s(value):
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _k(name):
    return name if NAME_RE.match(name) else _s(name)


_MARKETPLACES_RE = re.compile(r"^[ \t]*\[[ \t]*marketplaces[ \t]*\][ \t]*(#.*)?$", re.M)


def _profile_header(name):
    q = re.escape(name)
    return re.compile(rf"""^[ \t]*\[[ \t]*profiles[ \t]*\.[ \t]*(?:{q}|"{q}"|'{q}')[ \t]*\][ \t]*(#.*)?$""", re.M)


def _line_end(text, pos):
    nl = text.find("\n", pos)
    return len(text) if nl == -1 else nl + 1


def _array_close(text, open_i):
    """Index of the ']' closing the array opened at open_i, and of the last significant char before it."""
    i, depth, last = open_i + 1, 1, open_i
    while i < len(text):
        c = text[i]
        if c == "#":
            i = _line_end(text, i)
            continue
        if c in "\"'":
            j = i + 1
            while j < len(text) and text[j] != c:
                j += 2 if c == '"' and text[j] == "\\" else 1
            last, i = j, j + 1
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return i, last
        if not c.isspace():
            last = i
        i += 1
    return None, None


def _append_plugins(text, name, pids):
    m = _profile_header(name).search(text)
    if m is None:
        return None
    start = _line_end(text, m.end())
    end = next((start + mm.start() for mm in re.finditer(r"^[ \t]*\[", text[start:], re.M)), len(text))
    pm = re.compile(r"^[ \t]*plugins[ \t]*=[ \t]*", re.M).search(text, start, end)
    if pm is None:
        prefix = text[:start] if text[:start].endswith("\n") else text[:start] + "\n"
        return prefix + f"plugins = [{', '.join(_s(p) for p in pids)}]\n" + text[start:]
    open_i = pm.end()
    if text[open_i:open_i + 1] != "[":
        return None
    close_i, last = _array_close(text, open_i)
    if close_i is None:
        return None
    if last == open_i:
        return text[:open_i + 1] + ", ".join(_s(p) for p in pids) + text[open_i + 1:]
    comma = "" if text[last] == "," else ","
    nl = text.rfind("\n", last, close_i)
    if nl != -1:
        # keep the array's own style: a trailing comma only if the last item already had one
        items = "".join(f"\n  {_s(p)}," for p in pids)
        if comma:
            items = items[:-1]
        return text[:last + 1] + comma + text[last + 1:nl] + items + text[nl:]
    return text[:last + 1] + comma + " " + ", ".join(_s(p) for p in pids) + text[last + 1:]


def _add_marketplaces(text, new):
    entries = "".join(f"{_k(n)} = {_s(r)}\n" for n, r in new.items())
    m = _MARKETPLACES_RE.search(text)
    if m is None:
        return text.rstrip("\n") + "\n\n[marketplaces]\n" + entries
    pos = _line_end(text, m.end())
    prefix = text[:pos] if text[:pos].endswith("\n") else text[:pos] + "\n"
    return prefix + entries + text[pos:]


def add_plugin(text, profile, pid):
    """Append pid to [profiles.<profile>] plugins; None when that can't be done safely."""
    return _append_plugins(text, profile, [pid])


def add_marketplace(text, name, source):
    return _add_marketplaces(text, {name: source})


def _plugins_array(text, profile):
    """(open_i, close_i) of the plugins array of a profile, or None."""
    m = _profile_header(profile).search(text)
    if m is None:
        return None
    start = _line_end(text, m.end())
    end = next((start + mm.start() for mm in re.finditer(r"^[ \t]*\[", text[start:], re.M)), len(text))
    pm = re.compile(r"^[ \t]*plugins[ \t]*=[ \t]*", re.M).search(text, start, end)
    if pm is None or text[pm.end():pm.end() + 1] != "[":
        return None
    close_i, _last = _array_close(text, pm.end())
    return None if close_i is None else (pm.end(), close_i)


def _string_items(text, open_i, close_i):
    """[(start, end, value)] of the string elements; None if anything else is in the array."""
    items, i = [], open_i + 1
    while i < close_i:
        c = text[i]
        if c == "#":
            i = _line_end(text, i)
        elif c.isspace() or c == ",":
            i += 1
        elif c in "\"'":
            j = i + 1
            while j < close_i and text[j] != c:
                j += 2 if c == '"' and text[j] == "\\" else 1
            if j >= close_i:
                return None
            try:
                value = tomllib.loads(f"v = {text[i:j + 1]}")["v"]
            except tomllib.TOMLDecodeError:
                return None
            items.append((i, j + 1, value))
            i = j + 1
        else:
            return None
    return items


_LINE_TAIL = re.compile(r"[ \t]*,?[ \t]*(#[^\n]*)?(\n|$)")


def remove_plugin(text, profile, pid):
    """Remove pid from [profiles.<profile>] plugins; None when the section, array or element is missing."""
    span = _plugins_array(text, profile)
    if span is None:
        return None
    open_i, close_i = span
    items = _string_items(text, open_i, close_i)
    if items is None:
        return None
    idx = next((n for n, it in enumerate(items) if it[2] == pid), None)
    if idx is None:
        return None
    start, end, _v = items[idx]
    own = _comma_after(text, end, close_i)
    own = own if own is not None and own < _next_start(items, idx, close_i) else None
    prev = None
    if idx > 0 and own is None and idx == len(items) - 1:
        prev = _comma_after(text, items[idx - 1][1], start)  # last item: its predecessor's comma goes too
    ls = text.rfind("\n", 0, start) + 1
    tail = _LINE_TAIL.match(text, end)
    if not text[ls:start].strip() and tail and tail.end() <= close_i and tail.group(0).endswith("\n"):
        out = text[:ls] + text[tail.end():]
        return out if prev is None else out[:prev] + out[prev + 1:]
    lo, hi = start, end
    if own is not None:
        hi = own + 1
    if own is not None or prev is not None or len(items) == 1:
        while lo > open_i + 1 and text[lo - 1] in " \t":
            lo -= 1
    if own is not None and idx < len(items) - 1:
        lo = start
        while hi < close_i and text[hi] in " \t":
            hi += 1
    elif len(items) == 1:
        while hi < close_i and text[hi] in " \t":
            hi += 1
    elif prev is None and own is None:
        lo = start
    out = text[:lo] + text[hi:]
    return out if prev is None else out[:prev] + out[prev + 1:]


def _comma_after(text, pos, limit):
    """Index of the comma following pos (whitespace and comments skipped), or None."""
    i = pos
    while i < limit:
        if text[i] == "#":
            i = _line_end(text, i)
        elif text[i].isspace():
            i += 1
        else:
            return i if text[i] == "," else None
    return None


def _next_start(items, idx, close_i):
    return items[idx + 1][0] if idx + 1 < len(items) else close_i


def remove_marketplace(text, name):
    """Drop the `name = ...` line from the [marketplaces] table; None when it isn't there."""
    m = _MARKETPLACES_RE.search(text)
    if m is None:
        return None
    start = _line_end(text, m.end())
    end = next((start + mm.start() for mm in re.finditer(r"^[ \t]*\[", text[start:], re.M)), len(text))
    q = re.escape(name)
    km = re.compile(rf"""^[ \t]*(?:{q}|"{q}"|'{q}')[ \t]*=[^\n]*(?:\n|$)""", re.M).search(text, start, end)
    if km is None:
        return None
    return text[:km.start()] + text[km.end():]


def plugin_lists(text):
    """profile -> plugins ([] when the key is missing), or None on a parse error."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    return {n: list(p.get("plugins", [])) for n, p in data.get("profiles", {}).items()}
