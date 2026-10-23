import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .fsutil import cache_by_stat, reverse_lines
from .procs import is_uuid

PANE_RE = re.compile(r"(%\d+)$")
USAGE_KEYS = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


@dataclass(frozen=True)
class LiveClaude:
    pid: int
    profile: str
    session_id: str
    cwd: str
    status: str
    pane_id: object  # str | None


def _session_files(profile):
    d = Path(profile.config_dir) / "sessions"
    if not d.is_dir():
        return []
    return sorted(f for f in d.glob("*.json") if f.stem.isdigit())


def read_session_files(profiles, alive):
    """Session files of live pids. Unrecognised or inconsistent files are skipped with a warning."""
    live, warnings = [], []
    for prof in profiles:
        for f in _session_files(prof):
            file_pid = int(f.stem)
            if file_pid not in alive:
                continue
            try:
                j = json.loads(f.read_text())
                pid, sid, cwd = int(j["pid"]), j["sessionId"], str(j["cwd"])
                if not is_uuid(sid):
                    raise ValueError(f"sessionId {sid!r}")
            except (OSError, ValueError, KeyError, TypeError) as e:
                warnings.append(f"{f}：格式無法辨識（{type(e).__name__}），略過")
                continue
            if pid != file_pid:
                warnings.append(f"{f}：檔內 pid {pid} 與檔名不一致，略過")
                continue
            m = PANE_RE.search(str(j.get("tmux") or ""))
            live.append(LiveClaude(pid, prof.name, sid, cwd, str(j.get("status") or "unknown"),
                                   m.group(1) if m else None))
    return live, warnings


def stale_session_files(profiles, alive):
    return [f for prof in profiles for f in _session_files(prof) if int(f.stem) not in alive]


def find_transcript(profiles, session_id, prefer=None):
    """Transcript of a session: the preferred profile first, then the others in order. None for an id that is not a
    UUID (it is used in a glob)."""
    if not is_uuid(session_id):
        return None
    profiles = list(profiles)
    ordered = [p for p in profiles if p.name == prefer] + [p for p in profiles if p.name != prefer]
    for prof in ordered:
        hits = sorted(prof.projects_dir.glob(f"*/{session_id}.jsonl"))
        if hits:
            return hits[0]
    return None


def transcript_owners(profiles, path):
    """Names of the profiles whose projects dir holds path, compared by realpath (a projects/ shared through a
    symlink counts for every profile that links it)."""
    real = Path(os.path.realpath(path))
    return [p.name for p in profiles if Path(os.path.realpath(p.projects_dir)) in real.parents]


def transcript_problem(profiles, profile, session_id):
    """Why session_id cannot be resumed as a conversation of profile (None = its transcript is in that profile)."""
    profiles = list(profiles)
    path = find_transcript(profiles, session_id, prefer=profile)
    if path is None:
        return f"找不到逐字稿 {session_id[:8]}，不改開新對話"
    owners = transcript_owners(profiles, path)
    if profile not in owners:
        return f"逐字稿在 profile {owners[0] if owners else path.parent}，不是 {profile}"
    return None


@cache_by_stat
def transcript_stats(path):
    """(last user/assistant timestamp, context tokens of the last usage); read from the end, stops once both are known."""
    last_ts, tokens = None, None
    for line in reverse_lines(path):
        if last_ts is not None and tokens is not None:
            break
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if last_ts is None and rec.get("type") in ("user", "assistant") and rec.get("timestamp"):
            last_ts = rec["timestamp"]
        msg = rec.get("message")
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if tokens is None and isinstance(usage, dict):
            tokens = sum(int(usage.get(k) or 0) for k in USAGE_KEYS)
    return last_ts, tokens


INJECTED_TAG = re.compile(r"\s*<[a-z][a-z0-9_-]*>")  # <user-prompt-submit-hook>, <ide_opened_file>… are added by Claude itself
NOT_TYPED = ("<command-", "<local-command", "<bash-", "<task-notification", "<system-reminder", "Caveat:",
             "[Request interrupted")


def last_user_text(path):
    """The last message the user typed in a transcript (slash commands, tool results and injected text left out);
    "" when none can be read."""
    try:
        return _last_user_text(path)
    except OSError:
        return ""


@cache_by_stat
def _last_user_text(path):
    for line in reverse_lines(path):
        if '"user"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("type") != "user" or rec.get("isMeta"):
            continue
        msg = rec.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, list):
            texts = [c.get("text") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            content = texts[0] if texts and len(texts) == len(content) else None
        if (isinstance(content, str) and content.strip() and not content.lstrip().startswith(NOT_TYPED)
                and not INJECTED_TAG.match(content)):
            return content
    return ""


def transcript_carry(profiles, old, new, session_id):
    """What switching session_id from profile old to profile new needs: (transcript to move, None), (None, None) when new
    already sees it (a shared projects/), or (None, reason) when it cannot be done. Never two copies: a transcript the new
    profile already has under the same id is a reason, not something to overwrite."""
    profiles = list(profiles)
    by_name = {p.name: p for p in profiles}
    path = find_transcript(profiles, session_id, prefer=old)
    if path is None:
        return None, f"找不到逐字稿 {session_id[:8]}，不改開新對話"
    if new in transcript_owners(profiles, path):
        return None, None
    dest = Path(by_name[new].projects_dir) / path.parent.name / path.name
    if dest.exists():
        return None, f"{new} 已經有同一個對話的逐字稿（{dest}），和 {path} 是兩份；請先決定留哪一份"
    return path, None


def move_transcript(path, projects_dir):
    """Move a transcript and its sidecar directory (<id>/: subagents, tool results) into projects_dir under the same
    project folder; returns the new path. A rename on the same filesystem, so it is never half done."""
    import shutil

    dest_dir = Path(projects_dir) / path.parent.name
    dest_dir.mkdir(parents=True, exist_ok=True)
    side = path.with_suffix("")
    moved = []
    try:
        for src in [path] + ([side] if side.is_dir() else []):
            target = dest_dir / src.name
            shutil.move(str(src), str(target))
            moved.append((src, target))
    except OSError:
        for src, target in reversed(moved):  # put back what already moved, so the conversation stays in one place
            shutil.move(str(target), str(src))
        raise
    return dest_dir / path.name
