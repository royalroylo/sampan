import subprocess


def run(args, *, env=None, cwd=None):
    """Run a command and capture text output; a missing binary returns rc 127."""
    try:
        return subprocess.run(list(args), env=env, cwd=cwd, capture_output=True, text=True)
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(list(args), 127, "", str(e))
