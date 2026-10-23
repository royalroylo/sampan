"""Whether a newer sampan exists, and the update when the user presses U in the ui.

Latest version = the highest vX.Y.Z tag in the source repo (git ls-remote, 10 s timeout); the result is cached in
<state_dir>/update-check.json and the network is not touched again for 6 hours. If it cannot be determined (offline, no permission,
timeout) it counts as unknown and nothing is shown. A dev version (0.4.6.dev3+gabc) counts as 0.4.6, i.e. not older than v0.4.6;
unparseable version numbers are not announced.

Update: first uv tool upgrade sampan and confirm the version changed with a fresh child process sampan --version; if it did not,
uv tool install --reinstall git+REPO@v<latest> (pinned to the latest tag found; unpinned if none was found). All child processes go through
injectable run/stream, so tests never touch the network or a real installation.
U in the ui and sampan update re-check now (force, skipping the cache); with force, do_update reinstalls without trying upgrade first.
The hourly sampan save does not check.
"""
import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as metadata_version
from pathlib import Path

from .fsutil import PRIVATE_FILE, atomic_write, private_dir

REPO = "https://github.com/royalroylo/sampan"
PACKAGE = "sampan"
TTL = timedelta(hours=6)
TIMEOUT = 10
CACHE = "update-check.json"
UPGRADE = ["uv", "tool", "upgrade", PACKAGE]


def reinstall_cmd(tag=None):
    """uv tool install --reinstall from the source repo, pinned to vTAG when the tag is known (else unpinned)."""
    return ["uv", "tool", "install", "--reinstall", f"git+{REPO}" + (f"@v{tag}" if tag else "")]


_TAG = re.compile(r"refs/tags/v(\d+)\.(\d+)\.(\d+)$")
_BASE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:$|[.+\-]|[a-z])")


def _now():
    return datetime.now(timezone.utc)


def current_version():
    """Generated from the git tag at install time (hatch-vcs), e.g. 0.2.0, 0.2.1.dev3+g79b7d82."""
    try:
        return metadata_version(PACKAGE)
    except PackageNotFoundError:
        return "unknown"


def _run(args, *, env=None):
    """run with a timeout: timeout → rc 124, program not found → rc 127. stdin is not the terminal (git will not stop to ask for credentials)."""
    try:
        return subprocess.run(list(args), env=env, capture_output=True, text=True, timeout=TIMEOUT,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(args), 124, "", "timeout")
    except OSError as e:
        return subprocess.CompletedProcess(list(args), 127, "", str(e))


def _stream(args, on_line):
    """Run args, handing stdout/stderr to on_line line by line; returns the exit code (127 if the program is not found)."""
    try:
        p = subprocess.Popen(list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                             text=True, bufsize=1)
    except OSError as e:
        on_line(str(e))
        return 127
    with p:
        for line in p.stdout:
            on_line(line.rstrip("\n"))
    return p.wait()


def base_version(v):
    """(major, minor, patch) of a version; a dev version uses its base. Unparseable, or 0.0.0+unknown (no git info) → None."""
    if not v or "unknown" in v:
        return None
    m = _BASE.match(v.strip())
    return tuple(int(x) for x in m.groups()) if m else None


_CLEAN = re.compile(r"v?(\d+\.\d+\.\d+)$")


def clean_release(v):
    """"0.6.1" for a clean release version (no dev or local part), else None: the tag to pin a reinstall to when the latest is unknown."""
    m = _CLEAN.match((v or "").strip())
    return m.group(1) if m else None


def latest_tag(run=_run):
    """The highest vX.Y.Z in the repo (without the v, e.g. "0.4.6"); None on any failure."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        r = run(["git", "ls-remote", "--tags", "--refs", REPO], env=env)
    except Exception:  # noqa: BLE001  a failed version check must not affect anything
        return None
    if r.returncode != 0:
        return None
    found = []
    for line in (r.stdout or "").splitlines():
        m = _TAG.search(line.strip())
        if m:
            found.append(tuple(int(x) for x in m.groups()))
    return ".".join(map(str, max(found))) if found else None


def newer(current, latest):
    """True only if latest is newer than current; False if either is unparseable (nothing is shown)."""
    cur, new = base_version(current), base_version(latest)
    return cur is not None and new is not None and new > cur


# ── cache ──


def read_cache(state_dir):
    """(latest or None, checked_at); file missing or broken → None."""
    try:
        data = json.loads((Path(state_dir) / CACHE).read_text())
        at = datetime.fromisoformat(data["checked_at"])
        latest = data["latest"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if at.tzinfo is None or not (latest is None or isinstance(latest, str)):
        return None
    return latest, at


def write_cache(state_dir, latest, at):
    path = Path(state_dir) / CACHE
    try:
        private_dir(path.parent)
        atomic_write(path, json.dumps({"checked_at": at.isoformat(), "latest": latest}) + "\n", mode=PRIVATE_FILE)
    except OSError:
        pass


def fresh(at, now):
    return at is not None and timedelta(0) <= now - at < TTL


def check(state_dir, *, run=_run, now=_now, force=False):
    """(latest or None, checked_at): use the cache while it is fresh (within 6 hours), otherwise query the network and write the cache back (null is recorded too if nothing was found).
    force=True: skip the cache and query now (U in the ui, sampan update)."""
    t = now()
    cached = None if force else read_cache(state_dir)
    if cached is not None and fresh(cached[1], t):
        return cached
    latest = latest_tag(run)
    write_cache(state_dir, latest, t)
    return latest, t


def _thread(fn):
    threading.Thread(target=fn, daemon=True, name="sampan-update-check").start()


class Checker:
    """For the ui: start() checks in the background (the list appears first), tick() re-checks while waiting for a key if the cache has expired; hint() is the line to show."""

    def __init__(self, current, state_dir, *, run=_run, now=_now, spawn=_thread):
        self.current, self.state_dir, self.run, self.now, self._spawn = current, state_dir, run, now, spawn
        self.latest, self.checked_at = None, None
        self._busy = False

    def load_cache(self):
        """Read the cache only (however old, no network): for --plain."""
        cached = read_cache(self.state_dir)
        if cached is not None:
            self.latest, self.checked_at = cached

    def refresh(self, force=False):
        self.latest, self.checked_at = check(self.state_dir, run=self.run, now=self.now, force=force)

    def _bg(self):
        try:
            self.refresh()
        except Exception:  # noqa: BLE001  a failed background check is treated as unknown
            pass
        finally:
            self._busy = False

    def start(self):
        if self._busy:
            return
        self._busy = True
        self._spawn(self._bg)

    def tick(self):
        if not self._busy and not fresh(self.checked_at, self.now()):
            self.start()

    def available(self):
        """Return the newer version if there is one (e.g. "0.4.6"), otherwise None."""
        return self.latest if newer(self.current, self.latest) else None

    def hint(self):
        new = self.available()
        return f"有新版 v{new}（目前 {self.current}），按 U 更新" if new else None


# ── update ──


def installed_version(run=_run):
    """Version reported by a fresh child process sampan --version; unreadable → None."""
    try:
        r = run(["sampan", "--version"])
    except Exception:  # noqa: BLE001
        return None
    parts = (r.stdout or "").split()
    return parts[1] if r.returncode == 0 and len(parts) >= 2 and parts[0] == "sampan" else None


def do_update(out, *, current, stream=_stream, run=_run, which=shutil.which, latest=None, force=False,
              reopen=True):
    """Update sampan; returns whether it succeeded (the caller restarts sampan on success). Output goes line by line to out.
    Without force, success = the version really changed.
    latest: the version to pin (e.g. "0.6.1"); if not given, look up the highest tag in the repo now, and reinstall unpinned if that fails.
    force=True: skip uv tool upgrade and reinstall right away; returns True when the reinstall exits 0 and the installed version is
    readable, even when that version equals current.
    reopen=False (sampan update): the success line does not say sampan reopens."""
    if which("uv") is None:
        out(f"❌ 找不到 uv；請在終端機手動更新：{' '.join(UPGRADE)}（不行再 {' '.join(reinstall_cmd(latest))}）")
        return False
    tag = latest
    if force:
        if tag is None:
            tag = latest_tag(run)
        cmd = reinstall_cmd(tag)
        out(f"▶ {' '.join(cmd)}")
        rc = stream(cmd, lambda line: out(f"  {line}"))
        after = installed_version(run)
        if rc == 0 and after:
            out(f"✔ 已重新安裝 {after}" + ("，重新開啟 sampan…" if reopen else ""))
            return True
        out(f"❌ 重新安裝沒有成功（exit {rc}，版本 {after or '讀不到'}）；可以在終端機手動執行：{' '.join(cmd)}")
        return False
    for step in ("upgrade", "reinstall"):
        if step == "reinstall" and tag is None:
            tag = latest_tag(run)
        cmd = UPGRADE if step == "upgrade" else reinstall_cmd(tag)
        out(f"▶ {' '.join(cmd)}")
        rc = stream(cmd, lambda line: out(f"  {line}"))
        after = installed_version(run)
        if after and after != current:
            out(f"✔ 已更新到 {after}" + ("，重新開啟 sampan…" if reopen else ""))
            return True
        out(f"⚠ 執行後版本仍是 {after or '（讀不到）'}" + (f"（exit {rc}）" if rc else ""))
    out(f"❌ 更新沒有成功，仍是 {current}；可以在終端機手動執行：{' '.join(reinstall_cmd(tag))}")
    return False
