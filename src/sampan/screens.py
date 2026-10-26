"""Everything sampan knows about what Claude Code and Codex draw on their screens.

Markers and parsers live here and nowhere else, so a new agent release means editing one file.
Verified on Claude Code 2.1.287 and codex-cli 0.147.0 (fixtures: tests/fixtures/codex_*.txt, tests/test_prompts.py).

Codex menus are lines "› N. text" above "Press enter to continue" / "Press enter to confirm or esc to go back".
Claude's trust prompt is not numbered: the cursor "❯" starts on "No, exit".
"""
import re
from dataclasses import dataclass

# Codex: activity
WORKING_RE = re.compile(r"\(\d+[hms]\b.*esc to interrupt\)")  # "• Working (3s • esc to interrupt)"
MODAL_MARKERS = ("Press enter to", "Would you like to")  # approval, trust, update and other menus
COMPOSER_PREFIX = "› "  # the input box (placeholder, or what the user half-typed)
FOOTER_RE = re.compile(r"^\s+\S.* · \S")  # "  codex-model medium · /Users/u/proj"
# Codex: startup prompts
CODEX_TRUST_PROMPT = "Do you trust the contents of this directory"
# Claude: startup prompt
CLAUDE_TRUST_OPTION = "trust this folder"  # "Yes, I trust this folder"
CODEX_TRUST_YES = "Yes"  # "1. Yes, continue"
CLAUDE_WORKSPACE = "Accessing workspace:"
CLAUDE_SAFETY = "Quick safety check"


def codex_activity(screen):
    """"working", "idle" or "unknown" (anything not confidently recognised, including approval prompts)."""
    tail = [line.rstrip() for line in screen.splitlines() if line.strip()][-12:]
    if not tail:
        return "unknown"
    if any(marker in line for line in tail[-6:] for marker in MODAL_MARKERS):
        return "unknown"
    if any(WORKING_RE.search(line) for line in tail):
        return "working"
    if FOOTER_RE.match(tail[-1]) and any(line.startswith(COMPOSER_PREFIX) for line in tail[-8:-1]):
        return "idle"
    return "unknown"


def _codex_trust_path(screen):
    """Path shown by the last "Do you trust the contents of this directory?" prompt on screen (wrapped lines joined)."""
    lines = screen.splitlines()
    for i in reversed(range(len(lines))):
        _, sep, rest = lines[i].partition("You are in ")
        if not sep:
            continue
        parts = [rest.strip()]
        for nxt in lines[i + 1:]:
            if "Do you trust" in nxt:
                return "".join(parts) or None
            parts.append(nxt.strip())
        return None
    return None


def _claude_trust_path(screen):
    """Directory shown on Claude's trust prompt; None when no prompt is on screen.

    Claude wraps long paths itself (the next line is indented) and tmux -J cannot rejoin them, so take the lines
    between "Accessing workspace:" and "Quick safety check", strip each line's edges (indent, padding) and join them
    with nothing. A space inside a line is part of the path and is kept."""
    lines = screen.splitlines()
    starts = [i for i, l in enumerate(lines) if l.strip() == CLAUDE_WORKSPACE]
    if not starts:
        return None
    body = []
    for l in lines[starts[-1] + 1:]:
        if l.strip().startswith(CLAUDE_SAFETY) or (body and not l.strip()):
            break
        if l.strip():  # the blank line under the title
            body.append(l)
    return "".join(l.strip() for l in body) or None


def trust_path(screen, agent):
    """The directory a trust prompt on this screen asks about (agent "claude" or "codex"); None when there is none."""
    return _codex_trust_path(screen) if agent == "codex" else _claude_trust_path(screen)


# Startup menus (Codex update/trust/hooks review, Claude trust)
CODEX_END = ("Press enter to",)
CLAUDE_END = ("Enter to confirm",)
CODEX_OPT = re.compile(r"^[›> ]\s*(\d+)\.\s+(.+)$")
CLAUDE_CUR = "❯"
NUMBERED = re.compile(r"^\d+\.\s+")
GROUP_MARKS = (">", "•", "✨", "⚠", "■")  # symbols that start each Codex block: a new block even without a blank line
HEADLINES = ("Update available", "need review")
QUESTION_END = re.compile(r"\?(?=\s|$)")  # a ? inside a URL does not count
BLOCK_LINES = 12  # how many lines above the options to look at (anything higher is old screen content)
DETAIL_LINES = 3
CROP = 200


@dataclass(frozen=True)
class Prompt:
    agent: str
    question: str
    options: tuple = ()  # empty = options not recognised (the user can only go in and handle it, or leave it)
    current: int | None = None  # option the cursor is on
    detail: tuple = ()  # extra text under the question (trusted directory, update notes…)
    numbers: tuple = ()  # numbers shown on the Codex screen
    trust_path: str | None = None  # directory shown by the trust prompt (wrapped lines rejoined; spaces inside a line kept)

    def keys_for(self, index):
        """Keys to send to pick option number index; each group is one send-keys."""
        if self.agent == "codex":
            return [[self.numbers[index]]]
        here = self.current or 0
        move = "Down" if index > here else "Up"
        return [[move]] * abs(index - here) + [["Enter"]]

    def same(self, other):
        return other is not None and (self.question, self.options, self.detail) == (
            other.question, other.options, other.detail)


def _bottom_marker(raw, markers):
    """The menu's last line (e.g. Press enter to continue): look only at the last non-blank line of the screen; if anything follows it (input box, footer)
    the menu is already past and only remains in the scrollback, so None."""
    nb = [i for i, line in enumerate(raw) if line.strip()]
    return nb[-1] if nb and any(m in raw[nb[-1]] for m in markers) else None


def _block(raw, top, stop):
    """The lines above the options (top to bottom, blank lines kept as ""); stop at the previous menu or at a line in stop."""
    lines, n = [], 0
    for i in range(top - 1, -1, -1):
        line = raw[i]
        if line.strip():
            if any(m in line for m in stop) or CODEX_OPT.match(line):
                break
            n += 1
            if n > BLOCK_LINES:
                break
        lines.insert(0, line)
    return lines


def _groups(lines, marks=()):
    groups, cur = [], []
    for line in lines:
        s = line.strip()
        if not s or (marks and s.startswith(marks)):
            if cur:
                groups.append(cur)
            cur = [s] if s else []
            continue
        cur.append(s)
    if cur:
        groups.append(cur)
    return groups


def _crop(text):
    return text if len(text) <= CROP else text[:CROP - 1] + "…"


def _question(groups):
    """(question, extra): the sentence with a question mark in the last block; failing that a title line such as "Update available"; failing that the first line of the block nearest the options."""
    for gi in range(len(groups) - 1, -1, -1):
        text = " ".join(groups[gi])
        m = QUESTION_END.search(text)
        if m:
            return text[:m.end()], [l for g in groups[gi + 1:] for l in g]
    flat = [l for g in groups for l in g]
    for li in range(len(flat) - 1, -1, -1):
        if any(h in flat[li] for h in HEADLINES):
            return flat[li], flat[li + 1:]
    if not groups:
        return "", []
    start = len(flat) - len(groups[-1])
    return flat[start], flat[start + 1:]


def _unknown(agent, raw, end):
    tail = [l.strip() for l in raw[:end] if l.strip()][-5:]
    return Prompt(agent, "", (), None, tuple(_crop(l) for l in tail))


def _codex_options(raw, end):
    """(options top to bottom [(number, text, is_cursor)], line number of the first option); ([], None) when the options are incomplete (do not reach 1.)."""
    opts, cont = [], []
    i = end - 1
    while i >= 0:
        line = raw[i]
        if not line.strip():
            if opts or cont:
                break
            i -= 1
            continue
        m = CODEX_OPT.match(line)
        if m and (not opts or int(m.group(1)) == opts[-1][0] - 1):
            opts.append((int(m.group(1)), " ".join([m.group(2).strip(), *cont]), line.startswith("›")))
            cont = []
            if opts[-1][0] == 1:
                return opts[::-1], i
            i -= 1
            continue
        if len(cont) < 2 and line.startswith("   "):  # a wrapped option (indented deeper than the number)
            cont.insert(0, line.strip())
            i -= 1
            continue
        break
    return [], None


def _parse_codex(raw):
    end = _bottom_marker(raw, CODEX_END)
    if end is None:
        return None
    opts, top = _codex_options(raw, end)
    if not opts:
        return _unknown("codex", raw, end)
    block = _block(raw, top, CODEX_END)
    shown = trust_path("\n".join(block), "codex")
    if shown:  # "You are in <directory>" (may wrap) is not part of the question
        s = max(k for k, l in enumerate(block) if "You are in " in l)
        e = next((k for k in range(s, len(block)) if "Do you trust" in block[k]), len(block))
        block = block[:s] + block[e:]
    question, detail = _question(_groups(block, GROUP_MARKS))
    trust = None
    if shown and "Do you trust" in question:
        trust, detail = shown, [f"目錄：{shown}"]
    current = next((k for k, o in enumerate(opts) if o[2]), None)
    return Prompt("codex", _crop(question), tuple(o[1] for o in opts), current,
                  tuple(_crop(l) for l in detail[:DETAIL_LINES]), tuple(str(o[0]) for o in opts), trust)


def _indent(line):
    return len(line) - len(line.lstrip(" "))


def _claude_options(raw, end):
    """(option texts, cursor position, line number of the first option); options is empty when nothing is recognised."""
    run = []
    for i in range(end - 1, -1, -1):
        if not raw[i].strip():
            if run:
                break
            continue
        run.insert(0, i)
    cur = [i for i in run if raw[i].lstrip().startswith(CLAUDE_CUR)]
    if len(cur) != 1:
        return (), None, None
    here = cur[0]
    col = _indent(raw[here]) + len(CLAUDE_CUR)
    col += _indent(raw[here][col:])
    pos = run.index(here)
    lo = pos
    while lo > 0 and _indent(raw[run[lo - 1]]) == col:
        lo -= 1
    hi = pos
    while hi + 1 < len(run) and _indent(raw[run[hi + 1]]) == col:
        hi += 1
    picked = run[lo:hi + 1]
    texts = []
    for i in picked:
        t = raw[i].strip()
        if t.startswith(CLAUDE_CUR):
            t = t[len(CLAUDE_CUR):].strip()
        texts.append(NUMBERED.sub("", t))
    return tuple(texts), picked.index(here), picked[0]


def _claude_trust(block):
    """(directory of the trust prompt, the other lines without the title and directory): between "Accessing workspace:" and "Quick safety check" (the directory may wrap)."""
    starts = [k for k, l in enumerate(block) if l.strip() == "Accessing workspace:"]
    if not starts:
        return None, block
    s = starts[-1]
    path, k = [], s + 1
    while k < len(block):
        t = block[k].strip()
        if t.startswith("Quick safety check") or (path and not t):
            break
        if t:
            path.append(t)
        k += 1
    return ("".join(path) or None), block[:s] + block[k:]


def _parse_claude(raw):
    end = _bottom_marker(raw, CLAUDE_END)
    if end is None:
        return None
    options, current, top = _claude_options(raw, end)
    if not options:
        return _unknown("claude", raw, end)
    shown, block = _claude_trust(_block(raw, top, CLAUDE_END))
    question, detail = _question(_groups(block))
    trust = None
    if shown:
        trust, detail = shown, [f"目錄：{shown}"]
    return Prompt("claude", _crop(question), options, current, tuple(_crop(l) for l in detail[:DETAIL_LINES]),
                  (), trust)


def parse_prompt(screen, agent):
    """The menu at the bottom of the screen; None when there is no menu (idle, working, or an old menu left only in the scrollback above).
    When the menu's last line is visible but the options cannot be read, returns a Prompt with empty options (the user can only go in and handle it, or leave it)."""
    raw = [line.rstrip() for line in (screen or "").splitlines()]
    return _parse_codex(raw) if agent == "codex" else _parse_claude(raw)


def _claude_at_trust(screen):
    """Claude trust prompt at the bottom of the screen: the menu ends in "Enter to confirm", or (older screen, no
    footer) the "trust this folder" option is within the last two lines. A prompt under later output is neither."""
    p = parse_prompt(screen, "claude")
    if p is not None:
        return p.trust_path is not None or any(CLAUDE_TRUST_OPTION in o for o in p.options)
    return any(CLAUDE_TRUST_OPTION in l for l in [l for l in screen.splitlines() if l.strip()][-2:])


def bottom_trust_path(screen):
    """Directory on a Claude trust prompt that is the last thing on the screen; None otherwise (no prompt, one left
    in the scrollback above other output, or one not drawn completely yet)."""
    if not _claude_at_trust(screen):
        return None
    lines = [l for l in screen.splitlines() if l.strip()]
    starts = [i for i, l in enumerate(lines) if l.strip() == CLAUDE_WORKSPACE]
    if not starts or not any(CLAUDE_TRUST_OPTION in l for l in lines[starts[-1]:]):
        return None
    return _claude_trust_path(screen)


def at_trust_prompt(screen, agent):
    """Is this agent waiting at a trust prompt (bottom of the screen only)? Used where nothing may be pressed."""
    if agent == "claude":
        return _claude_at_trust(screen)
    p = parse_prompt(screen, "codex")
    return p is not None and (p.trust_path is not None or CODEX_TRUST_PROMPT in p.question)


# ---------- Claude: is the input box safe to paste into ----------

RULE_RE = re.compile(r"^─{10,}$")  # the horizontal rules above and below the input box
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PROMPT_PREFIX = "❯\xa0"  # Claude draws the prompt as ❯ + NBSP
BOX_BOTTOM_LINES = 5  # non-blank lines allowed under the box (status line, hints); more means it scrolled away


def claude_input_state(screen):
    """"empty" only for an input box whose prompt is empty or shows a dim placeholder; "draft" when the user typed
    something; "blocked" for everything else (menus, trust and permission prompts, no recognisable box).
    A whitelist: a screen this does not recognise is never "empty"."""
    lines = screen.rstrip("\n").split("\n")
    plain = [ANSI_RE.sub("", ln).rstrip(" ") for ln in lines]
    rules = [i for i, ln in enumerate(plain) if RULE_RE.match(ln)]
    if len(rules) < 2:
        return "blocked"
    top, bottom = rules[-2], rules[-1]
    if sum(1 for ln in plain[bottom + 1:] if ln.strip()) > BOX_BOTTOM_LINES:
        return "blocked"
    prompt = next((i for i in range(bottom - 1, top, -1) if plain[i].startswith("❯")), None)
    if prompt is None or not plain[prompt].startswith(PROMPT_PREFIX):
        return "blocked"
    text = plain[prompt][len(PROMPT_PREFIX):].strip()
    if re.match(r"\d+\.\s", text) or text.startswith("/"):
        return "blocked"  # a numbered option of a dialog, or a slash-command menu
    if any(plain[i].strip() for i in range(top + 1, bottom) if i != prompt):
        return "draft"  # another line inside the box (e.g. a draft that starts with a blank line) is not empty
    if not text:
        return "empty"
    after = lines[prompt].split(PROMPT_PREFIX, 1)[1] if PROMPT_PREFIX in lines[prompt] else ""
    if not after.startswith("\x1b[2m"):
        return "draft"  # normal-intensity text: the user typed it
    # a dim run that ends (reset or normal intensity) and is followed by more text is a placeholder plus a draft
    tail = re.split(r"\x1b\[(?:0|22)m", after[len("\x1b[2m"):], maxsplit=1)
    return "empty" if len(tail) == 1 or not ANSI_RE.sub("", tail[1]).strip() else "draft"
