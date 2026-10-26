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


INJECTED_TAG = re.compile(r"\s*<[a-z][a-z0-9_-]*/?>")  # <user-prompt-submit-hook>, <ide_opened_file>, <artifact-content-…/>… are added by Claude itself
NOT_TYPED = ("<command-", "<local-command", "<bash-", "<task-notification", "<system-reminder", "Caveat:",
             "[Request interrupted")


def last_user_text(path):
    """The last message the user typed in a transcript (slash commands, tool results and injected text left out);
    "" when none can be read."""
    try:
        return _last_user_text(path)
    except OSError:
        return ""


def _user_content(rec, join=False):
    """Text of a record the user typed (str, or a list of text parts: the first part alone, or all joined when join);
    None for anything else: not a user record, meta/skill injections, compaction summaries, tool results, injected text."""
    if not isinstance(rec, dict) or rec.get("type") != "user" or rec.get("isMeta"):
        return None
    if rec.get("isCompactSummary") or rec.get("isVisibleInTranscriptOnly"):  # the compaction summary is not typed text
        return None
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, list):
        texts = [c.get("text") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        ok = texts and len(texts) == len(content) and all(isinstance(t, str) for t in texts)
        content = ("".join(texts) if join else texts[0]) if ok else None
    if (isinstance(content, str) and content.strip() and not content.lstrip().startswith(NOT_TYPED)
            and not INJECTED_TAG.match(content)):
        return content
    return None


@cache_by_stat
def _last_user_text(path):
    for line in reverse_lines(path):
        if '"user"' not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        content = _user_content(rec)
        if content is not None:
            return content
    return ""


@dataclass(frozen=True)
class Reply:
    """found: the sent message is in the transcript; done: text is the reply (an assistant text exists after it)."""
    found: bool
    done: bool
    text: str


def _assistant_text(rec):
    if not isinstance(rec, dict) or rec.get("type") != "assistant" or rec.get("isSidechain"):
        return ""
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return content.strip() and content or ""
    if not isinstance(content, list):
        return ""
    texts = [c.get("text") for c in content if isinstance(c, dict) and c.get("type") == "text"
             and isinstance(c.get("text"), str)]
    return "".join(texts) if "".join(texts).strip() else ""  # a record with only thinking / tool_use has no text


def reply_to(path, sent):
    """The reply to the last user record whose text equals sent: the last assistant text after it and before the next
    message the user typed. Skill injections, compaction summaries, tool results and thinking-only records are never
    the reply, and never the bound message. Reply(False, False, "") when sent is not in the transcript (/clear, a new file)."""
    try:
        return _reply_to(path, sent)
    except OSError:
        return Reply(False, False, "")


def _reply_to(path, sent):
    after = []  # records after the candidate, newest first
    for line in reverse_lines(path):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if _user_content(rec, join=True) == sent:
            window = []
            for later in reversed(after):  # oldest first, up to the next typed message
                if _user_content(later, join=True) is not None:
                    break
                window.append(later)
            texts = [t for t in map(_assistant_text, window) if t]
            return Reply(True, True, texts[-1]) if texts else Reply(True, False, "")
        after.append(rec)
    return Reply(False, False, "")


def transcript_carry(profiles, old, new, session_id):
    """What continuing session_id under profile new (instead of old) needs: (transcript to move, None),
    (None, None) when new already sees it (a shared projects/, or an earlier switch that moved it but was interrupted),
    or (None, reason). There is only ever one copy: a second transcript of the same conversation anywhere in new's or
    old's projects is a reason to stop, never something to overwrite."""
    profiles = list(profiles)
    by_name = {p.name: p for p in profiles}
    path = find_transcript(profiles, session_id, prefer=new)
    if path is None:
        return None, f"找不到逐字稿 {session_id[:8]}，不改開新對話"
    real = os.path.realpath(path)
    owners = transcript_owners(profiles, path)
    copies = [h for name in (old, new) for h in Path(by_name[name].projects_dir).glob(f"*/{session_id}.jsonl")
              if os.path.realpath(h) != real]
    if copies:
        return None, f"同一個對話有兩份逐字稿（{path}、{copies[0]}）；請先決定留哪一份"
    if new in owners:
        return None, None
    if old not in owners:
        return None, f"逐字稿在 profile {owners[0] if owners else path.parent}，不是 {old}"
    dest_dir = Path(by_name[new].projects_dir) / path.parent.name
    side = path.with_suffix("")
    if (dest_dir / side.name).exists():
        return None, f"{new} 已經有這個對話的附屬資料夾（{dest_dir / side.name}）；請先決定留哪一份"
    return path, None


def move_transcript(path, projects_dir):
    """Move a transcript and its sidecar directory (<id>/: subagents, tool results) into projects_dir under the same
    project folder; returns the new path. Renames only (same filesystem), so each piece moves whole; a failure puts
    back what already moved and raises the original error."""
    dest_dir = Path(projects_dir) / path.parent.name
    dest_dir.mkdir(parents=True, exist_ok=True)
    side = path.with_suffix("")
    moved = []
    try:
        for src in [path] + ([side] if side.is_dir() else []):
            target = dest_dir / src.name
            os.rename(src, target)
            moved.append((src, target))
    except OSError:
        for src, target in reversed(moved):  # keep the conversation in one place
            try:
                os.rename(target, src)
            except OSError:
                pass
        raise
    return dest_dir / path.name
