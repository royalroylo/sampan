import contextlib
import os
import time
from pathlib import Path

from sampan.config import load_config
from sampan.status import State, StatusRow

TOML = """
version = 1

[marketplaces]
claude-plugins-official = "anthropics/claude-plugins-official"
my-plugins = "owner/my-plugins"

[profiles.alice]
config_dir = "~/.claude"
default = true
label = "alice@example.com"
plugins = ["tools@my-plugins", "superpowers@claude-plugins-official"]

[profiles.work]
config_dir = "~/.claude-profiles/work"
label = "work@example.com"
plugins = ["tools@my-plugins"]
"""


def make_env(tmp: Path, toml: str = TOML):
    home = tmp / "home"
    (home / ".claude" / "projects").mkdir(parents=True, exist_ok=True)
    (home / ".claude-profiles" / "work").mkdir(parents=True, exist_ok=True)
    path = tmp / "config.toml"
    path.write_text(toml)
    return load_config(path, home=home, state_dir=tmp / "state")


def km_source(r):
    """known_marketplaces.json source: owner/repo -> github, URL -> git, None -> local directory."""
    if not r:
        return {"source": "directory", "path": "/x"}
    if "://" in r or r.startswith("git@"):
        return {"source": "git", "url": r}
    return {"source": "github", "repo": r}


# POSIX TZ strings for the zones the tests use, so they do not depend on tzdata being installed (minimal Linux images have none)
POSIX_TZ = {"Asia/Tokyo": "JST-9", "Asia/Taipei": "CST-8", "America/New_York": "EST5EDT,M3.2.0,M11.1.0"}


@contextlib.contextmanager
def local_tz(name):
    """Run with the machine's local timezone set to `name` (e.g. "Asia/Tokyo"), then restore it."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = POSIX_TZ.get(name, name)
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def status_row(name, state=State.RUNNING, *, agent="claude", profile="alice", agent_status="idle", tokens=None,
               sid="x", last=None, detail="", pane=None):
    """A StatusRow for the ui tests; the one place that knows the row's field order."""
    return StatusRow(name, agent, profile, sid, state, detail, agent_status, last, tokens, pane)
