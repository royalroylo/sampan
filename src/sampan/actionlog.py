"""actions.log under the state dir: every message a plugin / marketplace / restart / forget / switch action printed,
one line each with the time and the action, so an earlier action's messages can be read back (the screen keeps only
the last one). Credentials are masked (scrub); a log that can't be written never fails the action."""
import fcntl
import os
import re
from datetime import datetime

from .config import redact
from .fsutil import PRIVATE_FILE, private_dir, try_chmod

LOG_NAME = "actions.log"
MAX_BYTES = 1 << 20  # past this the log becomes actions.log.1 (one old copy is kept)
# besides user:token@ in URLs (config.redact): GitHub tokens, NAME_TOKEN=… style assignments, Authorization headers
_SECRETS = (
    (re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"), "***"),
    (re.compile(r"\b([A-Za-z0-9_]*(?:TOKEN|SECRET|PASSWORD|PASSWD|API_KEY)[A-Za-z0-9_]*=)\S+", re.I), r"\1***"),
    (re.compile(r"(Authorization:\s*(?:\w+\s+)?)\S+", re.I), r"\1***"),
)


def log_path(cfg):
    return cfg.state_dir / LOG_NAME


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def scrub(text):
    """text with credentials masked: config.redact plus the _SECRETS patterns."""
    text = redact(text)
    for pattern, repl in _SECRETS:
        text = pattern.sub(repl, text)
    return text


def append(cfg, label, lines, *, now=_now):
    if getattr(cfg, "state_dir", None) is None:  # a stand-in cfg (tests) has nowhere to log
        return
    path = log_path(cfg)
    try:
        private_dir(path.parent)
        # one lock for the size check, the rotation and the append: two sampan processes must not rotate twice
        with os.fdopen(os.open(path.with_name(LOG_NAME + ".lock"), os.O_WRONLY | os.O_CREAT, PRIVATE_FILE)) as lk:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
            if path.exists() and path.stat().st_size >= MAX_BYTES:
                os.replace(path, path.with_name(LOG_NAME + ".1"))
            with os.fdopen(os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, PRIVATE_FILE), "a",
                           encoding="utf-8") as fh:
                try_chmod(fh.fileno(), PRIVATE_FILE)
                stamp, tag = now(), scrub(label)
                fh.writelines(f"{stamp} [{tag}] {scrub(line)}\n" for line in lines)
    except OSError:
        pass


def tee(cfg, label, out, *, now=_now):
    """out that also appends each non-empty line to actions.log under label. An out that already logs is returned
    as it is, so an action run inside another one is logged once, under the outer label."""
    if getattr(out, "actionlog", False):
        return out

    def logged(line=""):
        out(line)
        lines = [s for s in str(line).split("\n") if s.strip()]
        if lines:
            append(cfg, label, lines, now=now)
    logged.actionlog = True
    return logged
