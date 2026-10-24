import subprocess


def run(args, *, env=None, cwd=None, timeout=None):
    """Run a command and capture text output; a missing binary returns rc 127, a timeout (seconds) rc 124."""
    try:
        return subprocess.run(list(args), env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(args), 124, "", "timeout")
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(list(args), 127, "", str(e))
