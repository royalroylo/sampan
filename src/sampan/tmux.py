from dataclasses import dataclass

FMT = "#{session_name}\t#{pane_id}\t#{pane_pid}\t#{pane_current_command}\t#{pane_current_path}"
SHELLS = frozenset({"zsh", "bash", "sh", "fish", "-zsh"})
TMUX = ("tmux", "-u")  # -u: without LANG (launchd) CJK session names would otherwise turn into ____


@dataclass(frozen=True)
class Pane:
    session: str
    pane_id: str
    pane_pid: int
    command: str
    path: str


class TmuxUnreadable(Exception):
    """tmux is there but the pane list is unreadable (permissions, socket, version mismatch, timeout…): this must not be taken as "no sessions"."""


def _no_server(stderr):
    """tmux says "no server": the normal state after a reboot, all sessions really are gone (not a read failure)."""
    s = stderr.lower()
    return ("no server running" in s or "failed to connect to server" in s
            or ("error connecting to" in s and ("no such file or directory" in s or "connection refused" in s)))


class Tmux:
    def __init__(self, run):
        self.run = run
        self.unreadable_reason = None  # why the last panes() returned None (first line of stderr)

    def _tmux(self, *args):
        return self.run([*TMUX, *args])

    def server_running(self):
        return self._tmux("list-sessions").returncode == 0

    def server_id(self):
        r = self._tmux("list-sessions", "-F", "#{pid}-#{start_time}")
        lines = r.stdout.split()
        return lines[0] if r.returncode == 0 and lines else None

    def panes(self):
        r = self._tmux("list-panes", "-a", "-F", FMT)
        if r.returncode != 0:
            if _no_server(r.stderr or ""):
                return []  # no server running (e.g. just booted): there really are no panes
            lines = (r.stderr or "").strip().splitlines()
            self.unreadable_reason = lines[0] if lines else f"tmux 結束碼 {r.returncode}"
            return None  # unreadable: the caller must not treat this as "no panes"
        panes = []
        for line in r.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) != 5 or not parts[2].isdigit():
                continue
            panes.append(Pane(parts[0], parts[1], int(parts[2]), parts[3], parts[4]))
        return panes

    def require_panes(self):
        """panes(), but raises TmuxUnreadable when it cannot be read (the message is a one-liner meant for the user)."""
        panes = self.panes()
        if panes is None:
            raise TmuxUnreadable(unreadable_message(self.unreadable_reason))
        return panes

    def has_session(self, name):
        return self._tmux("has-session", "-t", f"={name}").returncode == 0

    def new_session(self, name, cwd):
        r = self._tmux("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", name, "-c", cwd)
        pane = r.stdout.strip()
        return pane if r.returncode == 0 and pane else None

    def rename_session(self, old, new):
        return self._tmux("rename-session", "-t", f"={old}", new).returncode == 0

    def kill_session(self, name):
        return self._tmux("kill-session", "-t", f"={name}").returncode == 0

    def send_keys(self, target, *keys):
        return self._tmux("send-keys", "-t", target, *keys)

    def capture(self, target, lines=30):
        r = self._tmux("capture-pane", "-p", "-t", target, "-S", f"-{lines}")
        return r.stdout if r.returncode == 0 else ""


def unreadable_message(reason):
    return f"讀不到 tmux（{reason}），狀態無法判斷"
