import subprocess


def run(args, *, env=None, cwd=None, timeout=None, stdin=None):
    """Run a command and capture text output; a missing binary returns rc 127, a timeout (seconds) rc 124."""
    try:
        return subprocess.run(list(args), env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                              stdin=stdin)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(args), 124, "", "timeout")
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(list(args), 127, "", str(e))


def no_stdin(run):
    """run, with stdin=DEVNULL unless the call passes its own: a child that inherits our stdin would read what is
    meant for us (the curses ui's keystrokes, the MCP server's JSON-RPC stream)."""
    def wrapped(args, **kw):
        if kw.get("stdin") is None:
            kw["stdin"] = subprocess.DEVNULL
        return run(args, **kw)
    return wrapped
