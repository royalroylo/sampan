import os
import stat
from pathlib import Path


def write_target(path):
    """The file a write to path really changes: a symlink (even a dangling one) is followed, the link stays."""
    path = Path(path)
    return path.resolve() if path.is_symlink() else path


def atomic_write(path, text):
    """Write text atomically at write_target(path); an existing file keeps its mode; the temp file never lingers."""
    target = write_target(path)
    mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def reverse_lines(path, chunk=1 << 16):
    """A text file's lines, last line first, reading backwards chunk by chunk (only as far as the caller iterates).
    Lines are split on bytes before decoding, so a multibyte character split by a chunk edge stays whole."""
    with open(path, "rb") as fh:
        pos = fh.seek(0, os.SEEK_END)
        rest = b""
        first = True
        while pos > 0:
            step = min(chunk, pos)
            pos -= step
            fh.seek(pos)
            parts = (fh.read(step) + rest).split(b"\n")
            rest = parts.pop(0)
            if first and parts and parts[-1] == b"":
                parts.pop()  # trailing newline
            first = False
            for raw in reversed(parts):
                yield raw.decode(errors="replace")
        if rest:
            yield rest.decode(errors="replace")


def cache_by_stat(fn):
    """Memoise fn(path) on (path, size, mtime): transcripts are re-read only when they change."""
    memo = {}

    def wrapper(path):
        st = os.stat(path)
        key = (str(path), st.st_size, st.st_mtime_ns)
        if key not in memo:
            if len(memo) > 512:
                memo.clear()
            memo[key] = fn(path)
        return memo[key]
    wrapper.__wrapped__ = fn
    return wrapper
