"""launchd schedule: sampan save --quiet every hour on the hour. The schedule only saves; it must never call restore/new/move."""
import os
import plistlib
from pathlib import Path

from .fsutil import atomic_write, private_dir

LABEL = "io.github.royalroylo.sampan-save"
# Every hour on the hour. StartInterval is not used: its timer pauses during sleep, so a laptop with the lid closed might not save for hours;
# a run that StartCalendarInterval missed is made up once on wake.
SCHEDULE = {"Minute": 0}


def plist_path(home, label=LABEL):
    return Path(home) / "Library" / "LaunchAgents" / f"{label}.plist"


def expected_program(sampan_path):
    return [str(sampan_path), "save", "--quiet"]


def default_path_env(home):
    # /usr/sbin: lsof (used to find Codex's rollout) lives there
    return ":".join([str(Path(home) / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
                     "/usr/sbin", "/sbin"])


ENV_KEYS = ("SAMPAN_CONFIG", "SAMPAN_STATE_DIR")


def plist_text(sampan_path, state_dir, path_env, env=None):
    """env: SAMPAN_CONFIG/SAMPAN_STATE_DIR in effect when the schedule is installed, so the hourly save uses the same
    config and registry."""
    state_dir = Path(state_dir)
    variables = {"PATH": path_env, "LANG": "en_US.UTF-8", "LC_CTYPE": "en_US.UTF-8"}
    variables.update({k: str(v) for k, v in (env or {}).items() if k in ENV_KEYS})
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": expected_program(sampan_path),
        "EnvironmentVariables": variables,
        "StartCalendarInterval": SCHEDULE,
        "RunAtLoad": False,
        "StandardOutPath": str(state_dir / "launchd.out.log"),
        "StandardErrorPath": str(state_dir / "launchd.err.log"),
    }, sort_keys=False).decode()


def _domain(uid):
    return f"gui/{os.getuid() if uid is None else uid}"


def check(home, sampan_path, run=None, *, uid=None, env=None):
    """("ok"|"missing"|"unreadable"|"wrong"|"env"|"schedule"|"not_loaded", ProgramArguments or None).

    "schedule": the plist is from an older sampan (StartInterval instead of the hourly StartCalendarInterval).

    With env (the SAMPAN_CONFIG/SAMPAN_STATE_DIR that should be in the plist), a plist carrying other values is "env".
    With run, a correct plist is also checked with `launchctl print` (read-only): a plist that never got
    bootstrapped (or was booted out) is "not_loaded"."""
    path = plist_path(home)
    if not path.exists():
        return "missing", None
    try:
        data = plistlib.loads(path.read_bytes())
        program = data.get("ProgramArguments")
    except (OSError, ValueError, plistlib.InvalidFileException, AttributeError):
        return "unreadable", None
    if program != expected_program(sampan_path):
        return "wrong", program
    variables = data.get("EnvironmentVariables")
    variables = variables if isinstance(variables, dict) else {}
    if env is not None and {k: variables.get(k) for k in ENV_KEYS} != {k: env.get(k) for k in ENV_KEYS}:
        return "env", program
    if data.get("StartCalendarInterval") != SCHEDULE or "StartInterval" in data:
        return "schedule", program
    if run is not None and run(["launchctl", "print", f"{_domain(uid)}/{LABEL}"]).returncode != 0:
        return "not_loaded", program
    return "ok", program


def write_plist(sampan_path, state_dir, home, env=None):
    private_dir(state_dir)
    return atomic_write(plist_path(home), plist_text(sampan_path, state_dir, default_path_env(home), env))


def load(run, home, *, uid=None):
    """(Re)load the written plist; True when launchctl bootstrap succeeded."""
    run(["launchctl", "bootout", f"{_domain(uid)}/{LABEL}"])  # fails if it was never loaded; harmless
    return run(["launchctl", "bootstrap", _domain(uid), str(plist_path(home))]).returncode == 0


def install(run, sampan_path, state_dir, home, *, uid=None, env=None):
    """Write the plist and (re)load it, True when launchctl bootstrap succeeded. Raises OSError if the write fails."""
    write_plist(sampan_path, state_dir, home, env)
    return load(run, home, uid=uid)
