import subprocess


def cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


def ps_line(pid, ppid, comm, args, stat="S"):
    return f"{pid:>6} {ppid:>6} {stat} {comm} {args}"


def pane_line(session, pane_id, pane_pid, command, path):
    return "\t".join([session, pane_id, str(pane_pid), command, path])


def _matches(args, prefix):
    """A rule for a bare command name also matches that command called by absolute path (lsof → /usr/sbin/lsof)."""
    head = args[:len(prefix)]
    if head and prefix and "/" not in prefix[0]:
        head = [head[0].rsplit("/", 1)[-1], *head[1:]]
    return tuple(head) == prefix


class UnmatchedCommand(AssertionError):
    """A strict FakeRunner got a command that no rule covers."""


class FakeRunner:
    """Records calls; answers with the last registered rule whose prefix matches.

    Unmatched commands answer rc 0 / empty output, unless strict=True: then they raise UnmatchedCommand,
    so a test cannot silently depend on an unregistered command.
    """

    def __init__(self, strict=False):
        self.calls = []
        self.rules = []
        self.strict = strict

    def on(self, prefix, result):
        self.rules.append((tuple(prefix), result))
        return self

    def __call__(self, args, *, env=None, cwd=None, timeout=None, stdin=None):
        args = list(args)
        self.calls.append({"args": args, "env": env, "cwd": cwd, "timeout": timeout, "stdin": stdin})
        for prefix, res in reversed(self.rules):
            if _matches(args, prefix):
                return res(args) if callable(res) else res
        if self.strict:
            raise UnmatchedCommand(f"no rule for {args!r}")
        return cp()

    def sent_keys(self):
        """Args of every tmux send-keys call, after the `send-keys` word (["-t", target, keys...])."""
        out = []
        for c in self.calls:
            a = c["args"]
            if a and a[0].rsplit("/", 1)[-1] == "tmux" and "send-keys" in a:
                out.append(a[a.index("send-keys") + 1:])
        return out

    def called(self, prefix):
        prefix = tuple(prefix)
        return [c for c in self.calls if _matches(c["args"], prefix)]
