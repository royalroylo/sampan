"""Build the claude/codex launch commands, plus `sampan claude` / `sampan codex`."""
import os
import shlex
import sys

from .procs import is_uuid


def disabled_problem(cfg, name):
    """The refusal for opening a new session under a disabled Claude profile, None otherwise."""
    p = cfg.all_profiles.get(name)
    if p is not None and p.agent == "claude" and p.disabled:
        return f"❌ profile {name} 已停用（sampan profile enable {name}）"
    return None


def claude_argv(profile, *, resume=None, session_id=None, extra=()):
    if resume and session_id:
        raise ValueError("resume 與 session_id 不可同時給")
    argv = ["claude"]
    if resume:
        argv += ["--resume", resume]
    if session_id:
        argv += ["--session-id", session_id]
    return argv + list(extra)


def codex_argv(*, resume=None, extra=()):
    return ["codex", *(["resume", resume] if resume else []), *extra]


def shell_command(argv, *, env_set, env_unset=(), cwd=None):
    """One-line command sent to the pane; paths and arguments go through shlex quote (safe for spaces and CJK)."""
    parts = []
    if env_unset or env_set:
        parts = ["env", *[a for name in env_unset for a in ("-u", name)],
                 *[f"{k}={v}" for k, v in env_set.items()]]
    line = shlex.join(parts + list(argv))
    return f"cd {shlex.quote(cwd)} && {line}" if cwd else line


def _codex_env(cfg):
    """CODEX_HOME to set whenever the configured home is not ~/.codex. Only ~/.codex is compared: the shell in a
    tmux pane may not have the caller's CODEX_HOME."""
    if cfg.codex.home == cfg.home / ".codex":
        return {}
    return {"CODEX_HOME": str(cfg.codex.home)}


def launch_line(cfg, agent, profile_name, *, resume=None, session_id=None, cwd=None):
    if resume is not None and not is_uuid(resume):
        # an empty or broken id makes claude/codex start a new conversation or show a picker: reattaching must never turn into a new conversation
        raise ValueError(f"對話 id {resume!r} 不是 UUID，不能接回")
    if agent == "codex":
        # when resuming from another directory Codex asks whether to use the recorded directory or the current one; an explicit -C avoids the question
        extra = ("-C", cwd) if cwd else ()
        return shell_command(codex_argv(resume=resume, extra=extra), env_set=_codex_env(cfg), cwd=cwd)
    profile = cfg.profiles[profile_name]
    argv = claude_argv(profile, resume=resume, session_id=session_id)
    if profile.default:
        return shell_command(argv, env_set={}, env_unset=("CLAUDE_CONFIG_DIR",), cwd=cwd)
    return shell_command(argv, env_set={"CLAUDE_CONFIG_DIR": str(profile.config_dir)}, cwd=cwd)


def send_line(tmux, target, agent, line, sleep):
    """Type a line into a pane and press Enter. Codex swallows an Enter sent in the same send-keys as the text,
    so for Codex the Enter goes separately about a second later."""
    if agent == "codex":
        tmux.send_keys(target, line)
        sleep(1)
        tmux.send_keys(target, "Enter")
    else:
        tmux.send_keys(target, line, "Enter")


def cmd_claude(cfg, run, out, ask, profile_name, extra, *, execvpe=os.execvpe, isatty=sys.stdin.isatty):
    if profile_name is None:
        if not isatty():
            out("❌ 沒有指定 profile，而且不是互動終端機；用 sampan claude <profile>"
                f"（可用：{', '.join(cfg.profiles)}）")
            return 2
        names = list(cfg.profiles)
        out("Available Claude profiles:")
        out("")
        for i, n in enumerate(names, 1):
            out(f"  {i}) {n}  — {cfg.profiles[n].label}")
        out("")
        try:
            raw = (ask(f"Select [1-{len(names)}]: ") or "").strip()
        except EOFError:
            out("")
            out("已取消")
            return 1
        if not (raw.isdigit() and 1 <= int(raw) <= len(names)):
            out("❌ Invalid selection.")
            return 1
        profile_name = names[int(raw) - 1]
    if disabled_problem(cfg, profile_name):
        out(disabled_problem(cfg, profile_name))
        return 2
    if profile_name not in cfg.profiles:
        out(f"❌ 沒有這個 profile：{profile_name}（可用：{', '.join(cfg.profiles)}）")
        return 2
    profile = cfg.profiles[profile_name]
    out(f"▶ Using profile: {profile_name}")
    out("")
    argv = claude_argv(profile, extra=extra)
    execvpe(argv[0], argv, profile.env(os.environ))
    return 0


def cmd_codex(cfg, extra, *, out=print, execvpe=os.execvpe):
    if not cfg.codex.enabled:
        out("❌ Codex 已關閉（沒有使用中的 Codex profile）")
        return 2
    argv = codex_argv(extra=extra)
    env = dict(os.environ)
    env.update(_codex_env(cfg))
    execvpe(argv[0], argv, env)
    return 0
