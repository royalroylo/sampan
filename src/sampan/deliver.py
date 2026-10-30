"""Deliver a dispatch to its target session. Actions layer: this module presses keys.

Claude: the text goes through a tmux buffer loaded from the outbox file (never through a shell or send-keys), and only
into an empty prompt. Codex: `codex queue --thread` takes the text as one argv element, no shell.
"""
import os
import subprocess

from .codex_daemon import CODEX_NOT_FOUND, first_line
from .launch import codex_env
from .screens import claude_input_state, claude_prompt_text

PASTE_SETTLE_S = 0.5  # let the agent take the bracketed paste before Enter
VERIFY_S = 0.3  # after Enter: how often the box is looked at again
VERIFY_LOOKS = 10  # how many looks before giving up (about 3 seconds)
NUDGE_AFTER = 5  # still in the box after this many looks: press Enter once more (never paste again)
PASTE_MARK = "[Pasted text"  # how Claude shows a long or multi-line paste on the prompt line
OURS_MIN = 8  # at least this many characters of our first line must match the prompt line
REASON_STUCK = "按了兩次 Enter，訊息還留在輸入框"
CODEX_QUEUE_TIMEOUT_S = 30
REASON_DRAFT = "對方輸入框有未送出的字"
REASON_BLOCKED = "對方畫面停在選單或提示"
REASON_COPY_MODE = "對方畫面在捲動模式（tmux copy mode）"
REASON_QUEUE_TIMEOUT = "可能已送出，請確認（codex queue 逾時）"


class Unsure(str):
    """A delivery reason meaning the text may already have reached the target: the caller must never send it again
    (a plain str reason means nothing was sent)."""


class Pasted(Unsure):
    """A to_claude reason meaning the text is already in the target's prompt box but Enter failed."""


def source_line(from_name, from_profile, did):
    """from_name None = the terminal; a name without a profile (a label such as 「未登記的 session（claude）」) as is."""
    who = "終端機" if from_name is None else f"{from_name}（{from_profile}）" if from_profile else from_name
    return f"〔sampan 轉送｜來自 {who}｜派工 {did}｜直接回覆文字即可，sampan 會帶回〕"


def compose(from_name, from_profile, did, message):
    return source_line(from_name, from_profile, did) + "\n" + message


def to_claude(tmux, pane_id, outbox, did, sleep, raw=False):
    """Paste the outbox file into an empty prompt and press Enter, then look again until the box no longer holds our
    text (Enter can be lost while the paste settles): one more Enter if it is still there after NUDGE_AFTER looks,
    never a second paste. None on success; else the reason, as a plain str when nothing was pasted, or as Pasted
    when the text is in the box but Enter failed or did not send it.
    raw: the text is a / command typed as is; Claude shows its completion menu under it (a "blocked" screen), so the
    prompt line alone tells whether it is still in the box."""
    state = claude_input_state(tmux.capture(pane_id, escapes=True))
    if state == "draft":
        return REASON_DRAFT
    if state != "empty":
        return REASON_BLOCKED
    mode = tmux.pane_in_mode(pane_id)  # copy mode: the screen still shows the box, but keys would go to tmux
    if mode is not False:
        return REASON_COPY_MODE if mode else REASON_BLOCKED
    name = f"sampan-{did}"
    r = tmux.load_buffer(name, outbox)  # one at a time: a failed load must not be followed by a paste
    if r.returncode != 0:
        return first_line(r.stderr) or f"tmux 結束碼 {r.returncode}"
    r = tmux.paste_buffer(name, pane_id)
    if r.returncode != 0:
        tmux.delete_buffer(name)  # best effort: -d deletes it only after a paste
        return first_line(r.stderr) or f"tmux 結束碼 {r.returncode}"
    sleep(PASTE_SETTLE_S)
    r = tmux.send_keys(pane_id, "Enter")
    if r.returncode != 0:
        return Pasted(first_line(r.stderr) or f"tmux 結束碼 {r.returncode}")  # the text is already in the box
    first = _first_line(outbox)
    for look in range(1, VERIFY_LOOKS + 1):
        sleep(VERIFY_S)
        if not _ours_pending(tmux.capture(pane_id, escapes=True), first, raw):
            # gone from the prompt line: sent. Also when the screen cannot be read or shows something else (a
            # dialog, Claude's suggestion, someone's draft): never press a key on what is not our paste; if it was
            # not sent after all, the reply check fails it later (找不到回覆), still at most once
            return None
        if look == NUDGE_AFTER:
            if tmux.pane_in_mode(pane_id) is not False:  # scrolled meanwhile: Enter would go to tmux
                return Pasted(REASON_COPY_MODE)
            r = tmux.send_keys(pane_id, "Enter")
            if r.returncode != 0:
                return Pasted(first_line(r.stderr) or f"tmux 結束碼 {r.returncode}")
    return Pasted(REASON_STUCK)


def _first_line(outbox):
    try:
        return outbox.read_text(encoding="utf-8").split("\n", 1)[0].strip()
    except OSError:
        return ""


def _ours_pending(screen, first, raw=False):
    """The input box holds typed (not suggested) text and its prompt line is our paste: Claude's paste marker, or the
    start of our first line (as much of it as the line shows, at least OURS_MIN characters). raw: a typed / command
    with its menu open ("blocked") counts too; an "empty" box (Claude suggesting the command again) never does."""
    if claude_input_state(screen) not in (("draft", "blocked") if raw else ("draft",)):
        return False
    text = (claude_prompt_text(screen) or "").strip()
    if text.startswith(PASTE_MARK):
        return True
    n = min(len(first), len(text))
    return n >= min(OURS_MIN, len(first)) and n > 0 and text[:n] == first[:n]


def to_codex(cfg, run, thread_id, text):
    """Queue the text on a Codex thread. None on success, else a one-line reason; Unsure when `codex queue` timed out
    (the daemon may have taken the message: it is never queued again)."""
    extra = codex_env(cfg)
    env = {**os.environ, **extra} if extra else None
    r = run(["codex", "queue", "--thread", thread_id, "--message", text], env=env,
            timeout=CODEX_QUEUE_TIMEOUT_S, stdin=subprocess.DEVNULL)
    if r.returncode == 0:
        return None
    if r.returncode == 124:
        return Unsure(REASON_QUEUE_TIMEOUT)
    err = first_line(r.stderr)
    if r.returncode == 127 and (not err or err.startswith("[Errno 2]")):
        return CODEX_NOT_FOUND
    return err or first_line(r.stdout) or f"結束碼 {r.returncode}"
