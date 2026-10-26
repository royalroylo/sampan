"""The key that jumps out of a sampan session ([ui] detach_key, Ctrl+Q by default), bound in the running tmux server.

tmux key bindings are global to the server, so the binding is conditional: in a session sampan marked with the
session option @sampan it detaches (attached from outside tmux) or switches back to the previous session (sampan
switched over from its own pane); anywhere else the key is passed on to the program. ~/.tmux.conf is never touched,
and a binding of the same key that sampan did not write is never overwritten."""
import re

from .fsutil import atomic_write
from .tmux import TMUX

DEFAULT = "C-q"
MARK = "@sampan"
KEY_RE = re.compile(r"^(?:[CMS]-)*[A-Za-z0-9]+$")  # C-q, M-q, F12, C-M-x …: safe inside the binding's command strings
LAST = "detach-key"  # state file: the key sampan bound last, so a changed key frees the old one


def binding(key):
    """tmux arguments (after TMUX) of sampan's binding for key."""
    return ["bind-key", "-n", key, "if-shell", "-F", f"#{{{MARK}}}",
            "if-shell -F '#{client_last_session}' 'switch-client -l' 'detach-client'", f"send-keys {key}"]


def _current(run, key):
    """The root-table binding of key as tmux lists it, or "" when there is none (or tmux is not running)."""
    r = run([*TMUX, "list-keys", "-T", "root", key])
    return r.stdout.strip() if r.returncode == 0 else ""


def _ours(text):
    return MARK in text


def _taken(key):
    return f"tmux 已經把 {key} 綁成別的功能（不是 sampan 綁的），沒有覆蓋；要用請改 [ui] detach_key 或拿掉那個綁定"


def conflict(cfg, run):
    """Read only (for doctor): why the key cannot be bound, or None."""
    key = cfg.detach_key
    if not key:
        return None
    current = _current(run, key)
    return _taken(key) if current and not _ours(current) else None


def _last_path(cfg):
    return cfg.state_dir / LAST


def _read_last(cfg):
    try:
        return _last_path(cfg).read_text().strip() or DEFAULT
    except OSError:
        return DEFAULT


def ensure(cfg, run, names=()):
    """Bind cfg.detach_key and mark the sessions in names; with no key, remove a binding sampan wrote. Returns a
    warning for the user, or None. Never raises on a tmux failure (the attach goes on either way); with no tmux
    server running there is nothing to bind or mark yet, so it does nothing."""
    if run([*TMUX, "list-sessions", "-F", "#{session_name}"]).returncode != 0:
        return None
    key = cfg.detach_key
    for old in dict.fromkeys((_read_last(cfg), DEFAULT)):
        if old != key and _ours(_current(run, old)):
            run([*TMUX, "unbind-key", "-n", old])  # the key was changed or turned off: free the one sampan bound
    if not key:
        return None
    current = _current(run, key)
    if current and not _ours(current):
        return _taken(key)
    r = run([*TMUX, *binding(key)])
    if r.returncode != 0:
        return f"沒辦法綁 {key}：{(r.stderr or '').strip() or '未知原因'}"
    try:
        atomic_write(_last_path(cfg), key + "\n", mode=0o600)
    except OSError:
        pass
    for name in names:
        run([*TMUX, "set-option", "-t", f"={name}:", MARK, "1"])  # set-option takes a pane target: "=name:"
    return None
