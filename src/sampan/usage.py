"""5h／週額度: Claude's come from the status-line JSON (sampan statusline records them), Codex's from its newest rollout.
Never reads account credentials and never goes online."""
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .fsutil import PRIVATE_FILE, atomic_write, private_dir

CODEX_TAIL = 256 * 1024  # bytes of the newest rollout read from its end
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


def usage_path(cfg, profile):
    return cfg.state_dir / "usage" / f"{profile}.json"


def _window(used, resets_at):
    """A Window from a percent and an epoch-seconds reset time; None unless both are numbers."""
    if isinstance(used, bool) or isinstance(resets_at, bool):
        return None
    if not isinstance(used, (int, float)) or not isinstance(resets_at, (int, float)):
        return None
    return Window(float(used), datetime.fromtimestamp(resets_at, timezone.utc))


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


def _load_window(raw):
    return _window(raw.get("used"), raw.get("resets_at")) if isinstance(raw, dict) else None


def claude_usage(cfg, profile):
    """The last reading sampan statusline recorded for this profile; None when there is none or it can't be read."""
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
    return Usage(five, week, at) if five or week else None


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


def usage_text(u, now):
    """「5h 42%・週 18%（17:20）」; the time is the reading's (month/day too when not today, local time).
    A window past its resets_at is left out (its number no longer holds); nothing left → ""."""
    if u is None:
        return ""
    parts = [f"{label} {w.used:.0f}%" for label, w in (("5h", u.five_hour), ("週", u.week))
             if w is not None and w.resets_at > now]
    if not parts:
        return ""
    at = u.at.astimezone()
    when = at.strftime("%H:%M") if at.date() == now.astimezone().date() else at.strftime("%m/%d %H:%M")
    return f"{'・'.join(parts)}（{when}）"


# ---------- sampan statusline ----------

def _err(msg):
    try:
        print(msg, file=sys.stderr, flush=True)
    except Exception:
        pass


def _run_wrapped(out, wrapped, stdin, run_wrapped):
    """Run the user's own status-line command on the same stdin; its stdout goes out as it is, its exit code is ours."""
    try:
        r = run_wrapped(wrapped, input=stdin, capture_output=True)
    except OSError as e:
        _err(f"sampan statusline：無法執行 {wrapped[0]}（{e}）")
        return 127
    if r.stdout:
        out(r.stdout.decode(errors="replace").removesuffix("\n"))
    if r.stderr:
        try:
            sys.stderr.buffer.write(r.stderr)
            sys.stderr.flush()
        except Exception:
            pass
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


def cmd_statusline(cfg, out, wrapped, *, stdin, env, run_wrapped=subprocess.run, now=None):
    """Claude Code's status-line command. Records the 額度 (every error swallowed: cfg None = the config did not
    load, record nothing), then runs `wrapped` on the same stdin and passes its output and exit code through; with
    no wrapped command, prints its own one line. Never raises and never prints a sampan error on stdout."""
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
            return _run_wrapped(out, wrapped, stdin, run_wrapped)
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
