"""5h／週額度: Claude's come from the status-line JSON (sampan statusline records them), Codex's from its newest rollout.
Never reads account credentials and never goes online."""
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .fsutil import PRIVATE_FILE, atomic_write, private_dir

CODEX_TAIL = 256 * 1024  # bytes of the newest rollout read from its end
STALE_S = 300  # a reading older than this gets its time shown
WARN_AT = 80  # percent: ⚠ from here, ⛔ at 100
CODEX_WINDOWS = {300: "five_hour", 10080: "week"}  # window_minutes → field (R3: never by primary/secondary position)


@dataclass(frozen=True)
class Window:
    used: float  # percent
    resets_at: datetime


@dataclass(frozen=True)
class Usage:
    five_hour: Window | None
    week: Window | None
    at: datetime  # when the numbers were read
    reached: bool = False  # the account hit a limit (Codex daemon: rateLimitReachedType set or ordinaryUsageAllowed false)


def usage_path(cfg, profile):
    return cfg.state_dir / "usage" / f"{profile}.json"


def _window(used, resets_at):
    """A Window from a percent and an epoch-seconds reset time; None unless both are numbers and the time is one a
    datetime can hold (external data: a huge, NaN or millisecond value is no window, never an exception)."""
    if isinstance(used, bool) or isinstance(resets_at, bool):
        return None
    if not isinstance(used, (int, float)) or not isinstance(resets_at, (int, float)):
        return None
    try:
        return Window(float(used), datetime.fromtimestamp(resets_at, timezone.utc))
    except (OverflowError, ValueError, OSError):
        return None


def _dump_window(raw):
    """{"used", "resets_at"} from a status-line window ({"used_percentage", "resets_at"}); None when unusable."""
    if not isinstance(raw, dict) or _window(raw.get("used_percentage"), raw.get("resets_at")) is None:
        return None
    return {"used": raw["used_percentage"], "resets_at": raw["resets_at"]}


def record_claude(cfg, profile, data, now):
    """Write the status-line JSON's rate_limits to usage/<profile>.json (dir 700, file 600, atomic).
    No rate_limits (not a subscriber, or before the first response) or no usable window: nothing is written, so an
    older reading is kept."""
    limits = data.get("rate_limits") if isinstance(data, dict) else None
    if not isinstance(limits, dict):
        return
    five, week = _dump_window(limits.get("five_hour")), _dump_window(limits.get("seven_day"))
    if five is None and week is None:
        return
    path = usage_path(cfg, profile)
    private_dir(path.parent)
    text = json.dumps({"at": now.isoformat(timespec="seconds"), "five_hour": five, "week": week})
    atomic_write(path, text + "\n", mode=PRIVATE_FILE)


def record_codex(cfg, profile, u):
    """Save a Codex reading (from the daemon) to usage/<profile>.json: same shape as record_claude's plus "reached"
    (dir 700, file 600, atomic). Windows are stored as {"used", "resets_at" (epoch seconds)}."""
    def dump(w):
        return None if w is None else {"used": w.used, "resets_at": w.resets_at.timestamp()}
    path = usage_path(cfg, profile)
    private_dir(path.parent)
    text = json.dumps({"at": u.at.isoformat(timespec="seconds"), "five_hour": dump(u.five_hour),
                       "week": dump(u.week), "reached": bool(u.reached)})
    atomic_write(path, text + "\n", mode=PRIVATE_FILE)


def move_usage(cfg, old, new):
    """Follow a profile rename (new) or forget (new None) with sampan's own usage/<old>.json; returns an error or None."""
    path = usage_path(cfg, old)
    try:
        if new is None:
            path.unlink(missing_ok=True)
        elif path.exists():
            os.replace(path, usage_path(cfg, new))
    except OSError as e:
        return str(e)
    return None


def _load_window(raw):
    return _window(raw.get("used"), raw.get("resets_at")) if isinstance(raw, dict) else None


def saved_usage(cfg, profile):
    """The last reading sampan saved for this profile (statusline for Claude, the daemon for Codex); None when there
    is none or it can't be read."""
    try:
        saved = json.loads(usage_path(cfg, profile).read_text())
        at = datetime.fromisoformat(saved["at"])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if not isinstance(saved, dict):
        return None
    if at.tzinfo is None:
        at = at.astimezone()
    five, week = _load_window(saved.get("five_hour")), _load_window(saved.get("week"))
    reached = saved.get("reached") is True
    return Usage(five, week, at, reached) if five or week or reached else None


claude_usage = saved_usage  # older name; doctor and others still use it


def _newest_rollout(codex_home):
    best = None
    sessions = Path(codex_home) / "sessions"
    if not sessions.is_dir():
        return None
    for f in sessions.glob("**/rollout-*.jsonl"):
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, f)
    return best[1] if best else None


def _codex_record(line):
    """Usage from one rollout line carrying rate_limits; None for any other line, and (R3) for a record whose
    primary and secondary are both null (e.g. limit_id "premium")."""
    if '"rate_limits"' not in line:
        return None
    try:
        rec = json.loads(line)
        limits = rec["payload"]["rate_limits"]
        at = datetime.fromisoformat(rec["timestamp"].replace("Z", "+00:00"))
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    if not isinstance(limits, dict):
        return None
    found = {}
    for key in ("primary", "secondary"):
        w = limits.get(key)
        if not isinstance(w, dict):
            continue
        field = CODEX_WINDOWS.get(w.get("window_minutes"))
        win = _window(w.get("used_percent"), w.get("resets_at"))
        if field and win and field not in found:
            found[field] = win
    if not found:
        return None
    return Usage(found.get("five_hour"), found.get("week"), at if at.tzinfo else at.replace(tzinfo=timezone.utc))


def codex_usage(profile):
    """The last usable rate_limits record in the newest rollout (by mtime) under the profile's sessions/, reading
    only its last 256 KB; None when there is none."""
    path = _newest_rollout(profile.config_dir)
    if path is None:
        return None
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - CODEX_TAIL))
            lines = fh.read().decode(errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        u = _codex_record(line)
        if u is not None:
            return u
    return None


def codex_profile_usage(cfg, p):
    """A Codex profile's usage: the saved daemon reading or the newest rollout's, whichever is newer."""
    saved, rollout = saved_usage(cfg, p.name), codex_usage(p)
    if saved is None or rollout is None:
        return saved or rollout
    return saved if saved.at >= rollout.at else rollout


REACHED_S = timedelta(hours=5)  # a reached flag with no window to expire it is trusted at most one 5h window


def _live(u, now):
    """[(label, Window)] of the windows not yet past their reset."""
    return [(label, w) for label, w in (("5h", u.five_hour), ("週", u.week)) if w is not None and w.resets_at > now]


def _levels(u, now):
    """([(label, window, level)], reached-without-window) with level 2 = used up, 1 = warning, 0 = fine.
    reached with no window at 100%: the fullest window counts as used up. A stored reached flag is stale (ignored)
    once any stored window is past its reset, and with no window at all it is honoured only for REACHED_S."""
    live = _live(u, now)
    stored = [w for w in (u.five_hour, u.week) if w is not None]
    reached = u.reached and all(w.resets_at > now for w in stored) and (stored or now - u.at <= REACHED_S)
    levels = [2 if w.used >= 100 else 1 if w.used >= WARN_AT else 0 for _l, w in live]
    if reached and live and 2 not in levels:
        levels[max(range(len(live)), key=lambda i: live[i][1].used)] = 2
    return [(label, w, lv) for (label, w), lv in zip(live, levels)], bool(reached and not stored)


def usage_alert(u, now):
    """"err" (a window used up, or the limit reached), "warn" (a window at 80% or more) or "". Windows past their
    reset do not count."""
    if u is None:
        return ""
    levels, bare = _levels(u, now)
    top = max([lv for _l, _w, lv in levels] + [2 if bare else 0])
    return ("", "warn", "err")[top]


def _clock(t, now):
    t = t.astimezone()
    return t.strftime("%H:%M") if t.date() == now.astimezone().date() else t.strftime("%m/%d %H:%M")


def usage_text(u, now):
    """「5h 42%・週 18%」; from 80% 「⚠ 週 85%（10/27 09:10 重置）・5h 40%」, used up 「⛔ 5h 已用完（08:28 重置）・週 73%」
    (worst window first, then 5h, 週; times local, with MM/DD when not today). The reading's time 「（HH:MM）」
    is appended only when it is older than STALE_S. A window past its resets_at is left out; nothing left → ""."""
    if u is None:
        return ""
    levels, bare = _levels(u, now)
    if bare:
        return "⛔ 已用完"
    if not levels:
        return ""
    parts = []
    for label, w, lv in sorted(levels, key=lambda x: -x[2]):  # stable: 5h before 週 at equal level
        if lv == 2:
            parts.append(f"{label} 已用完（{_clock(w.resets_at, now)} 重置）")
        elif lv == 1:
            parts.append(f"{label} {w.used:.0f}%（{_clock(w.resets_at, now)} 重置）")
        else:
            parts.append(f"{label} {w.used:.0f}%")
    top = max(lv for _l, _w, lv in levels)
    text = ("", "⚠ ", "⛔ ")[top] + "・".join(parts)
    if (now - u.at).total_seconds() > STALE_S:
        text += f"（{_clock(u.at, now)}）"
    return text


# ---------- sampan statusline ----------

def _err(msg):
    try:
        print(msg, file=sys.stderr, flush=True)
    except Exception:
        pass


def _pass_through(data, sink):
    if data:
        sink.write(data)
        sink.flush()


def _run_wrapped(wrapped, stdin, run_wrapped, stdout, stderr):
    """Run the user's own status-line command on the same stdin; its stdout and stderr go out byte for byte, its
    exit code is ours."""
    try:
        r = run_wrapped(wrapped, input=stdin, capture_output=True)
    except OSError as e:
        _err(f"sampan statusline：無法執行 {wrapped[0]}（{e}）")
        return 127
    _pass_through(r.stdout, stdout if stdout is not None else sys.stdout.buffer)
    try:
        _pass_through(r.stderr, stderr if stderr is not None else sys.stderr.buffer)
    except Exception:
        pass  # a lost stderr must not change the status line or its exit code
    return r.returncode


def _branch(run_wrapped, cwd):
    try:
        r = run_wrapped(["git", "-C", cwd, "branch", "--show-current"], capture_output=True, timeout=1)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout.decode(errors="replace").strip() if r.returncode == 0 else ""


def _own_line(cfg, profile, data, run_wrapped, now):
    """模型 · 目錄（分支）· context N% · 額度; parts that are not known are left out."""
    model = data.get("model") if isinstance(data.get("model"), dict) else {}
    workspace = data.get("workspace") if isinstance(data.get("workspace"), dict) else {}
    ctx = data.get("context_window") if isinstance(data.get("context_window"), dict) else {}
    parts = []
    if isinstance(model.get("display_name"), str):
        parts.append(model["display_name"])
    cwd = workspace.get("current_dir") or data.get("cwd")
    if isinstance(cwd, str) and cwd:
        home = str(cfg.home) if cfg is not None else str(Path.home())
        shown = "~" + cwd[len(home):] if cwd == home or cwd.startswith(home + "/") else cwd
        branch = _branch(run_wrapped, cwd)
        parts.append(f"{shown}（{branch}）" if branch else shown)
    used = ctx.get("used_percentage")
    if isinstance(used, (int, float)) and not isinstance(used, bool):
        parts.append(f"context {used:.0f}%")
    text = usage_text(claude_usage(cfg, profile), now) if cfg is not None and profile else ""
    if text:
        parts.append(text)
    line = parts[0] if parts else ""
    for part in parts[1:]:
        line += ("· " if line.endswith("）") else " · ") + part  # 「目錄（分支）· context」: no space after a full-width bracket
    return line


def cmd_statusline(cfg, out, wrapped, *, stdin, env, run_wrapped=subprocess.run, now=None, stdout=None, stderr=None):
    """Claude Code's status-line command. Records the 額度 (every error swallowed: cfg None = the config did not
    load, record nothing), then runs `wrapped` on the same stdin and passes its output (byte-exact, to the binary
    stdout/stderr; default sys.stdout.buffer / sys.stderr.buffer) and exit code through; with no wrapped command,
    prints its own one line through out. Never raises and never prints a sampan error on stdout."""
    profile, data = None, {}
    now = now or datetime.now(timezone.utc)
    try:
        parsed = json.loads(stdin.decode())
        data = parsed if isinstance(parsed, dict) else {}
        if cfg is not None:
            from .observe import profile_for_dir
            profile = profile_for_dir(cfg, env.get("CLAUDE_CONFIG_DIR"))
            if profile is not None:
                record_claude(cfg, profile, data, now)
    except Exception:
        pass
    if wrapped:
        try:
            return _run_wrapped(wrapped, stdin, run_wrapped, stdout, stderr)
        except Exception as e:
            _err(f"sampan statusline：{e}")
            return 1
    try:
        line = _own_line(cfg, profile, data, run_wrapped, now)
        if line:
            out(line)
    except Exception:
        pass
    return 0
