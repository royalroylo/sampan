import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .clock import parse_iso
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
    version: str = ""  # the Claude Code version this process runs ("" when the file has none)


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
                                   m.group(1) if m else None, str(j.get("version") or "")))
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
    """found: the sent message is in the transcript; done: text is the reply (an assistant text exists after it);
    closed: a later message was typed after it, so no more reply text can come;
    aborted: (Codex) the turn was interrupted."""
    found: bool
    done: bool
    text: str
    closed: bool = False
    aborted: bool = False  # Codex only: the turn ended with turn_aborted


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


PASTED_RE = re.compile(r'\n\n<pasted_content id="([^"<>]+)">\n(.*)</pasted_content id="\1">\n', re.S)


def _bound_text(content):
    """What a user record says, for binding to a sent text: a long paste is stored wrapped in exactly one
    pasted_content block (ids match), which is removed; trailing newlines never count."""
    m = PASTED_RE.fullmatch(content)
    return (m.group(2) if m else content).rstrip("\n")


def reply_to(path, sent, start=None):
    """The reply to the last user record whose text equals sent: the last assistant text after it and before the next
    message the user typed. Skill injections, compaction summaries, tool results and thinking-only records are never
    the reply, and never the bound message. Reply(False, False, "") when sent is not in the transcript (/clear, a new file).
    start: only records from that byte offset on (the size of the transcript when this delivery was pasted, so the
    same text pasted earlier, e.g. before a resend, is never bound)."""
    try:
        return _reply_to(path, sent, start or 0)
    except OSError:
        return Reply(False, False, "")


def _window_reply(after):
    """Reply for a bound record, given the records after it (newest first): the last assistant text before the next
    message the user typed."""
    window, closed = [], False
    for later in reversed(after):  # oldest first, up to the next typed message
        if _user_content(later, join=True) is not None:
            closed = True
            break
        window.append(later)
    texts = [t for t in map(_assistant_text, window) if t]
    return Reply(True, True, texts[-1], closed) if texts else Reply(True, False, "", closed)


def _reply_to(path, sent, start):
    after = []  # records after the candidate, newest first
    for line in reverse_lines(path, start=start):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        content = _user_content(rec, join=True)
        if content is not None and _bound_text(content) == sent.rstrip("\n"):
            return _window_reply(after)
        after.append(rec)
    return Reply(False, False, "")


INBOX_TAKE = "mcp__sampan__inbox_take"
INTERRUPTED = "[Request interrupted"  # Claude's record of an Esc (or a denied tool call)
DENIED = ("doesn't want to proceed", "was rejected")  # the user said no to the call; other errors (even about
# permissions: a classifier denial, "permission denied" from a command) leave the turn going


def _denied(part):
    """A tool_result that says the call was not allowed (the user said no to the permission prompt)."""
    if not part.get("is_error"):
        return False
    content = part.get("content")
    if isinstance(content, list):
        content = " ".join(c.get("text", "") for c in content if isinstance(c, dict) and isinstance(c.get("text"), str))
    return isinstance(content, str) and any(m in content for m in DENIED)


@dataclass(frozen=True)
class TurnInfo:
    """The turn after a marker (up to the next typed message or the end of the file): found: a user record holds the
    marker; ended: a typed message came after it, or the turn's last step was an assistant text with no tool call
    waiting for its result, or a tool call that was denied or interrupted (any other tool result last is mid-turn);
    last_text: the last assistant text of that turn;
    take_seen: inbox_take was called in it."""
    found: bool
    ended: bool = False
    last_text: str = ""
    take_seen: bool = False


def _user_text_anywhere(rec):
    """Every text of a user record, typed or a tool result (where a 收據 shows up); "" for any other record and for
    compaction summaries (which may quote a marker)."""
    if not isinstance(rec, dict) or rec.get("type") != "user":
        return ""
    if rec.get("isCompactSummary") or rec.get("isVisibleInTranscriptOnly"):
        return ""
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    out = []

    def collect(c):
        if isinstance(c, str):
            out.append(c)
        elif isinstance(c, list):
            for part in c:
                if isinstance(part, dict):
                    collect(part.get("text") if part.get("type") == "text" else part.get("content"))
    collect(content)
    return "\n".join(out)


def _forward_records(path, start):
    with open(path, "rb") as fh:
        fh.seek(start)
        for raw in fh:
            try:
                rec = json.loads(raw.decode(errors="replace"))
            except ValueError:
                continue
            if isinstance(rec, dict):
                yield rec


def after_marker(path, marker, start=None):
    """TurnInfo of the turn after the last user record that contains marker (a doorbell code, or 收據 <id> in an
    inbox_take tool result). start: a byte offset to read from (a cursor); None reads back from the end of the file
    only as far as the marker."""
    try:
        return _after_marker(path, marker, start)
    except OSError:
        return TurnInfo(False)


def _after_marker(path, marker, start):
    after = None  # records after the marker, oldest first
    if start is None:
        later = []
        for line in reverse_lines(path):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            if marker in _user_text_anywhere(rec):
                after = list(reversed(later))
                break
            later.append(rec)
    else:
        for rec in _forward_records(path, start):
            if marker in _user_text_anywhere(rec):
                after = []
            elif after is not None:
                after.append(rec)
    if after is None:
        return TurnInfo(False)
    last_text, pending, take_seen, last = "", set(), False, None  # last: what the turn did last
    for rec in after:
        if _user_content(rec, join=True) is not None:
            return TurnInfo(True, True, last_text, take_seen)  # a typed message: that turn is over, however it went
        msg = rec.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if rec.get("type") == "user":
            if _user_text_anywhere(rec).lstrip().startswith(INTERRUPTED):
                last = "stopped"
            for part in content if isinstance(content, list) else ():
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    pending.discard(part.get("tool_use_id"))
                    last = "stopped" if _denied(part) else "result"  # a plain error goes on like any result
            continue
        if rec.get("type") != "assistant" or rec.get("isSidechain"):
            continue
        for part in content if isinstance(content, list) else [{"type": "text", "text": content}]:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "tool_use":
                pending.add(part.get("id"))
                take_seen = take_seen or part.get("name") == INBOX_TAKE
                last = "tool"
            elif part.get("type") == "text" and isinstance(part.get("text"), str) and part["text"].strip():
                last_text, last = part["text"], "text"
    ended = last == "stopped" or (last == "text" and not pending)
    return TurnInfo(True, ended, last_text, take_seen)


def command_reply(path, sent, after):
    """The reply to a direct send (sent: one line starting with /, typed as is so Claude runs it): bound to the last
    record at or after the datetime after that is either a user record whose text is sent, or a user record or a
    `system`/`local_command` record that holds <command-name>{first word of sent}</command-name> (a skill, /model,
    /compact; /context writes only the system one). The reply text follows reply_to's rules."""
    try:
        return _command_reply(path, sent, after)
    except OSError:
        return Reply(False, False, "")


def _command_record(rec, sent, mark, after):
    if rec.get("type") == "system" and rec.get("subtype") == "local_command":
        text = rec.get("content")
    elif rec.get("type") == "user" and not (rec.get("isCompactSummary") or rec.get("isVisibleInTranscriptOnly")):
        msg = rec.get("message")
        text = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(text, list):
            text = "".join(c.get("text") for c in text if isinstance(c, dict) and isinstance(c.get("text"), str))
    else:
        return False
    if not isinstance(text, str) or not (text.strip() == sent or mark in text):
        return False
    ts = parse_iso(rec.get("timestamp"))
    return ts is not None and ts >= after


def _command_reply(path, sent, after):
    sent = sent.strip()
    mark = f"<command-name>{sent.split()[0] if sent else ''}</command-name>"
    later = []  # records after the candidate, newest first
    for line in reverse_lines(path):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if _command_record(rec, sent, mark, after):
            return _window_reply(later)
        later.append(rec)
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
