"""The Claude Code versions sampan's screen reading has been checked against.

sampan reads a few things off Claude Code's screen (the input box, the trust and startup menus, the Remote Control menu),
and a release can change the words. A version listed here has fixtures in tests/fixtures or a live run behind it; any other
version may still work, `sampan doctor` only says it has not been looked at. Add a version when a run on it has been
checked (and the fixtures updated when the screen changed)."""
import re
from collections import Counter

VERIFIED = {
    "claude": (
        "2.1.287",  # the fixtures under tests/fixtures (input box, trust prompt, Remote Control menu)
        "2.1.311",  # live runs in a private tmux: input box, Remote Control reset, rename
        "2.1.330",  # the Remote Control menu with the "Esc to close" footer (tests/fixtures/rc/menu_v330_*), rename
    ),
}

_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


def parse(version):
    """(major, minor, patch) of "2.1.311"; None for anything else ("2.1.311-beta", "", "unknown")."""
    text = (version or "").strip()
    if not _VERSION_RE.fullmatch(text):
        return None
    return tuple(int(p) for p in text.split("."))


def newest(agent):
    return max(VERIFIED[agent], key=parse)


def judge(agent, version):
    """"verified" (listed), "newer" (above the newest listed), "older" (below it, not listed) or "unknown" (not a
    plain N.N.N version)."""
    key = parse(version)
    if key is None:
        return "unknown"
    if version.strip() in VERIFIED[agent]:
        return "verified"
    return "newer" if key > parse(newest(agent)) else "older"


def count_versions(versions):
    """{version: how many} of the non-empty entries, in version order (unparseable ones last)."""
    counts = Counter(v.strip() for v in versions if v and v.strip())
    return dict(sorted(counts.items(), key=lambda kv: (parse(kv[0]) is None, parse(kv[0]) or (), kv[0])))
