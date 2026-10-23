"""Structural guard: the test run can never reach the real tmux / claude / codex / ...

Installed once by tests/__init__.py (so it is active for any way of running the tests). It wraps the
real call points -- subprocess.run / Popen (which also covers call / check_* and sampan.runner.run,
since those go through them) and os.exec* -- and raises RealCommandBlocked for:

  * tmux, claude, codex, lsof, launchctl, uv, osascript, sampan: always;
  * git: only network / mutating subcommands (ls-remote, fetch, clone, push, pull).
    Read-only local git (e.g. `git ls-files` in test_scrub) stays allowed.

Shell strings (shell=True, or the argument after `sh -c` / `bash -c` / `zsh -c` ...) are split into simple commands on
; & | ( ) and backticks, and every word is checked against the blocked names, so `sh -c 'cd x && tmux kill-server'`
is blocked wherever tmux appears. git is checked per simple command (its subcommand), so `sh -c 'git ls-remote ...'`
is blocked too. Limits of this word-level parse (it is not a shell):
  * newlines split commands only inside an argv `-c` argument; a shell=True string is first split on whitespace and
    re-joined with spaces, so there a newline does not end a command and a git subcommand on the next line is not
    seen as one (blocked names such as tmux are still caught anywhere);
  * quotes are dropped, so a quoted value with spaces after a git option (`git -C 'my repo' fetch`) shifts the words
    and the subcommand is misread.

The executable is matched on its basename, so /usr/sbin/lsof is caught like lsof.

RealCommandBlocked derives from BaseException on purpose: sampan code wraps subprocess calls in
`except OSError` / `except Exception`, and a block must not be turned into a quiet "command failed".
Every block is also recorded in `blocked_calls`; at interpreter exit a non-empty record forces exit
status 1, even if something still swallowed the exception (e.g. a background thread).
A test that deliberately provokes a block clears the record afterwards (blocked_calls.clear()).

Opting out (should be unnecessary; use FakeRunner instead):

    with allow_real_commands("uv"):
        ...
"""
import atexit
import contextlib
import os
import re
import subprocess
import sys

BLOCKED = frozenset({"tmux", "claude", "codex", "lsof", "launchctl", "uv", "osascript", "sampan"})
BLOCKED_GIT_SUBCOMMANDS = frozenset({"ls-remote", "fetch", "clone", "push", "pull"})

SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh"})
_WORD = re.compile(r"[^\s;&|()<>`$'\"]+")  # command-string words, splitting on shell separators and quotes
_SEPARATORS = re.compile(r"[;&|()`\n]")  # what ends one simple command in a shell string

blocked_calls = []
_allowed = set()


class RealCommandBlocked(BaseException):
    """A test tried to run a real external command that the test suite must never touch."""


def _git_subcommand(args):
    it = iter(args[1:])
    for a in it:
        if a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            next(it, None)
        elif not a.startswith("-"):
            return a
    return None


def _argv(args):
    if isinstance(args, (str, bytes, os.PathLike)):
        args = os.fsdecode(args).split()
    return [os.fsdecode(a) for a in args]


def _shell_text(argv, is_string):
    """The command string a shell would run: a string command (shell=True), or the argument after -c of sh/bash/zsh/dash; None otherwise."""
    if is_string:
        return " ".join(argv)
    if os.path.basename(argv[0]) in SHELLS and any(a.startswith("-") and "c" in a[1:] and not a.startswith("--") for a in argv[1:]):
        return " ".join(argv[1:])
    return None


def _shell_commands(text):
    """The simple commands of a shell string, each as its list of words (basename of the first one), split on ; & | newline ( ) `."""
    for part in _SEPARATORS.split(text):
        words = _WORD.findall(part)
        if words:
            yield [os.path.basename(words[0]), *words[1:]]


def _blocked_in_shell(text):
    """The first forbidden word in a shell string: any word naming a blocked command, or a network / mutating git subcommand."""
    for words in _shell_commands(text):
        for i, w in enumerate(words):
            name = os.path.basename(w)
            if name in BLOCKED and name not in _allowed:
                return name
            if name == "git" and "git" not in _allowed and _git_subcommand(words[i:]) in BLOCKED_GIT_SUBCOMMANDS:
                return f"git {_git_subcommand(words[i:])}"
    return None


def check(args):
    """Raise RealCommandBlocked if `args` (argv list or command string) is a forbidden command, also when hidden in `sh -c '...'` or a shell string."""
    argv = _argv(args)
    if not argv:
        return
    name = os.path.basename(argv[0])
    if name in _allowed:
        return
    text = _shell_text(argv, isinstance(args, (str, bytes, os.PathLike)))
    word = _blocked_in_shell(text) if text is not None else None
    if word is not None:
        blocked_calls.append(argv)
        raise RealCommandBlocked(f"tests must not run the real command {word!r} through a shell: {argv!r}")
    if name in BLOCKED or (name == "git" and _git_subcommand(argv) in BLOCKED_GIT_SUBCOMMANDS):
        blocked_calls.append(argv)
        raise RealCommandBlocked(
            f"tests must not run the real command {argv!r}; use FakeRunner (or, if truly needed, "
            f"tests.guard.allow_real_commands({name!r}))")


@contextlib.contextmanager
def allow_real_commands(*names):
    """Let the named executables (basename) through the guard inside the with-block."""
    added = set(names) - _allowed
    _allowed.update(added)
    try:
        yield
    finally:
        _allowed.difference_update(added)


_real = {"run": subprocess.run, "Popen": subprocess.Popen}


class _GuardedPopen(_real["Popen"]):
    def __init__(self, args, *a, **kw):
        check(args)
        super().__init__(args, *a, **kw)


def _guarded_run(args, *a, **kw):
    check(args)
    return _real["run"](args, *a, **kw)


def _guarded_exec(real):
    def guarded(file, *a, **kw):
        check([file])
        return real(file, *a, **kw)
    guarded.__name__ = real.__name__
    return guarded


def install():
    if getattr(subprocess.run, "_sampan_guard", False):
        return
    _guarded_run._sampan_guard = True
    subprocess.run = _guarded_run
    subprocess.Popen = _GuardedPopen
    for name in ("execv", "execve", "execvp", "execvpe"):
        setattr(os, name, _guarded_exec(getattr(os, name)))


@atexit.register
def _fail_if_anything_was_blocked():
    if blocked_calls:
        print(f"tests.guard: real command(s) were blocked during the run: {blocked_calls}", file=sys.stderr, flush=True)
        os._exit(1)
