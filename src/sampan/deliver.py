"""Deliver a dispatch to its target session. Actions layer: this module presses keys.

Claude: the text goes through a tmux buffer loaded from the outbox file (never through a shell or send-keys), and only
into an empty prompt. Codex: `codex queue --thread` takes the text as one argv element, no shell.
"""
import os
import subprocess

from .codex_daemon import CODEX_NOT_FOUND, first_line
from .launch import codex_env
from .screens import claude_input_state

PASTE_SETTLE_S = 0.5  # let the agent take the bracketed paste before Enter
CODEX_QUEUE_TIMEOUT_S = 30
REASON_DRAFT = "對方輸入框有未送出的字"
REASON_BLOCKED = "對方畫面停在選單或提示"


class Pasted(str):
    """A to_claude reason meaning the text is already in the target's prompt box but Enter failed: the caller must
    not paste it again (a plain str reason means nothing was pasted)."""


def source_line(from_name, from_profile, did):
    who = "終端機" if from_name is None else f"{from_name}（{from_profile}）"
    return f"〔sampan 轉送｜來自 {who}｜派工 {did}〕"


def compose(from_name, from_profile, did, message):
    return source_line(from_name, from_profile, did) + "\n" + message


def to_claude(tmux, pane_id, outbox, did, sleep):
    """Paste the outbox file into an empty prompt and press Enter. None on success; else the reason, as a plain str when
    nothing was pasted, or as Pasted when the text is in the box but Enter failed."""
    state = claude_input_state(tmux.capture(pane_id, escapes=True))
    if state == "draft":
        return REASON_DRAFT
    if state != "empty":
        return REASON_BLOCKED
    name = f"sampan-{did}"
    for step in (lambda: tmux.load_buffer(name, outbox), lambda: tmux.paste_buffer(name, pane_id)):
        r = step()  # one at a time: a failed load must not be followed by a paste
        if r.returncode != 0:
            return first_line(r.stderr) or f"tmux 結束碼 {r.returncode}"
    sleep(PASTE_SETTLE_S)
    r = tmux.send_keys(pane_id, "Enter")
    if r.returncode != 0:
        return Pasted(first_line(r.stderr) or f"tmux 結束碼 {r.returncode}")  # the text is already in the box
    return None


def to_codex(cfg, run, thread_id, text):
    """Queue the text on a Codex thread. None on success, else a one-line reason."""
    extra = codex_env(cfg)
    env = {**os.environ, **extra} if extra else None
    r = run(["codex", "queue", "--thread", thread_id, "--message", text], env=env,
            timeout=CODEX_QUEUE_TIMEOUT_S, stdin=subprocess.DEVNULL)
    if r.returncode == 0:
        return None
    err = first_line(r.stderr)
    if r.returncode == 127 and (not err or err.startswith("[Errno 2]")):
        return CODEX_NOT_FOUND
    return err or first_line(r.stdout) or f"結束碼 {r.returncode}"
