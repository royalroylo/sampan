import os
import stat
from pathlib import Path


def write_target(path):
    """The file a write to path really changes: a symlink (even a dangling one) is followed, the link stays."""
    path = Path(path)
    return path.resolve() if path.is_symlink() else path


PRIVATE_FILE = 0o600  # files under the state dir: only the user may read or write them
PRIVATE_DIR = 0o700


def try_chmod(path_or_fd, mode):
    """chmod (fchmod for an int fd), best-effort: a directory the user does not own or a filesystem that refuses
    chmod must never fail the write itself."""
    try:
        if isinstance(path_or_fd, int):
            os.fchmod(path_or_fd, mode)
        else:
            os.chmod(path_or_fd, mode)
    except OSError:
        pass


def private_dir(path):
    """mkdir -p path (errors raise) and make it PRIVATE_DIR, also when it already existed with looser bits (best-effort)."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    try_chmod(path, PRIVATE_DIR)
    return path


def private_file(path):
    """Set path to PRIVATE_FILE (fixes a file an older version left with looser bits); best-effort."""
    try_chmod(path, PRIVATE_FILE)


def atomic_write(path, text, *, mode=None, new_mode=None):
    """Write text atomically at write_target(path); the temp file never lingers.
    mode: the file always ends with this mode. Otherwise an existing file keeps its mode, and a new file gets new_mode
    (None: the umask default). The temp file is created 0600, so the text is never readable by others in between."""
    target = write_target(path)
    old = stat.S_IMODE(target.stat().st_mode) if target.exists() else None
    final = mode if mode is not None else (old if old is not None else new_mode)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE if final is not None else 0o666)
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        if final is not None:
            try_chmod(tmp, final)
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
