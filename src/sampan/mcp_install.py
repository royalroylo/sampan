"""`sampan mcp install` / `uninstall`: register `sampan mcp serve` with each enabled Claude profile and with Codex,
using the agents' own `mcp add` / `mcp remove` commands (so their config formats stay theirs)."""
import json
import os
import shlex
import tomllib
from datetime import datetime
from pathlib import Path

from . import actionlog, codex_approval
from .config import redact
from .launch import codex_env

NAME = "sampan"


def _targets(cfg):
    """(label, agent, env, profile | None) for every enabled Claude profile and for Codex when enabled."""
    out = [(p.name, "claude", p.env(os.environ), p) for p in cfg.profiles.values()]
    if cfg.codex.enabled:
        env = {**os.environ, **codex_env(cfg)}
        if not codex_env(cfg):  # the configured home is ~/.codex: an ambient CODEX_HOME would point codex elsewhere
            env.pop("CODEX_HOME", None)
        out.append((cfg.codex.profile or "codex", "codex", env, None))
    return out


def _claude_entry(profile):
    """Whether the profile's .claude.json has the user-scope sampan entry; None when that cannot be told (missing,
    unreadable or not the expected shape)."""
    try:
        data = json.loads(Path(profile.claude_json).read_text())
    except (OSError, ValueError):
        return None
    servers = data.get("mcpServers", {}) if isinstance(data, dict) else None
    return NAME in servers if isinstance(servers, dict) else None


def _installed(run, agent, env, profile=None):
    """A Claude profile is read from its .claude.json: running `claude` only to look renews the login, and from a shell
    that cannot open the keychain it writes .credentials.json (10/30). `claude mcp get` only when the file cannot tell."""
    if agent == "claude" and profile is not None:
        known = _claude_entry(profile)
        if known is not None:
            return known
    return run([agent, "mcp", "get", NAME], env=env).returncode == 0


def _add_argv(agent, sampan_path):
    serve = ["--", sampan_path, "mcp", "serve"]
    return ["claude", "mcp", "add", "--scope", "user", NAME, *serve] if agent == "claude" else ["codex", "mcp", "add", NAME, *serve]


def _remove_argv(agent):
    return ["claude", "mcp", "remove", "--scope", "user", NAME] if agent == "claude" else ["codex", "mcp", "remove", NAME]


def _codex_approval(cfg, out, confirm, dry_run, *, adding=False):
    """Codex's approval_mode tables for sampan's acting tools (codex_approval); 0, or 1 on an error."""
    label = "Codex config.toml"
    if dry_run and adding and confirm:  # codex has not written its sampan entry yet, so apply would find nothing
        out(f"{label}：（試跑）會設成每次詢問")
        return 0
    try:
        msg = codex_approval.apply(Path(cfg.codex.home) / "config.toml", confirm, now=datetime.now(), dry_run=dry_run)
    except ValueError as e:
        out(f"❌ {e}")
        return 1
    except OSError as e:
        out(f"❌ {label}：{e}")
        return 1
    out(f"{label}：{'（試跑）' if dry_run else ''}{msg}")
    return 0


def _apply(cfg, run, out, build, *, want_installed, dry_run, done, skip, codex_confirm, codex_first, profiles=()):
    """codex_confirm / codex_first: for Codex, the approval tables are set to codex_confirm before `codex mcp …`
    (codex_first) or after it, also when that is skipped. profiles: only these labels (empty = all); an unknown one
    refuses before anything runs."""
    targets = _targets(cfg)
    labels = [t[0] for t in targets]
    unknown = [n for n in dict.fromkeys(profiles) if n not in labels]
    if unknown:
        out(f"❌ 沒有這個使用中的 profile：{', '.join(unknown)}（可用：{', '.join(labels)}）")
        return 2
    if profiles:
        targets = [t for t in targets if t[0] in profiles]
    out = actionlog.tee(cfg, "mcp", out)
    rc = 0
    for name, agent, env, profile in targets:
        need = _installed(run, agent, env, profile) == want_installed
        if agent == "codex" and codex_first:
            rc |= _codex_approval(cfg, out, codex_confirm, dry_run)
        ok = True
        if not need:
            out(f"{name}：{skip}")
        elif dry_run:
            out(f"{name}：（試跑）會執行 {shlex.join(build(agent))}")
        else:
            argv = build(agent)
            r = run(argv, env=env)
            ok = r.returncode == 0
            if ok:
                out(f"✅ {name}：{done}")
            else:
                rc = 1
                out(f"❌ {name}：{shlex.join(argv)} 失敗：{redact((r.stderr or r.stdout).strip())[:300]}")
        if agent == "codex" and not codex_first and ok:
            rc |= _codex_approval(cfg, out, codex_confirm, dry_run, adding=need)
    return rc


def install(cfg, run, out, *, sampan_path, dry_run=False, profiles=()):
    if Path(sampan_path).name != "sampan":  # e.g. `python -m sampan`: the agents would be told to start something that does not exist
        out(f"❌ 找不到 sampan 執行檔（{sampan_path}），沒裝")
        return 2
    return _apply(cfg, run, out, lambda a: _add_argv(a, sampan_path), want_installed=False,
                  dry_run=dry_run, done="已裝 sampan MCP（重開該 profile 的 session 才會出現工具）",
                  skip="已裝，略過", codex_confirm=cfg.mcp_confirm, codex_first=False, profiles=profiles)


def uninstall(cfg, run, out, *, dry_run=False, profiles=()):
    return _apply(cfg, run, out, _remove_argv, want_installed=True, dry_run=dry_run,
                  done="已移除 sampan MCP", skip="沒裝，略過", codex_confirm=False, codex_first=True, profiles=profiles)


def _claude_has(profile):
    return _claude_entry(profile) is True


def _codex_has(cfg):
    try:
        data = tomllib.loads((Path(cfg.codex.home) / "config.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    servers = data.get("mcp_servers")
    return isinstance(servers, dict) and NAME in servers


def status(cfg):
    """profile name -> whether the user-scope sampan MCP entry is there. Reads the config files directly: `claude mcp get`
    would start the server to health-check it, too slow for doctor."""
    out = {p.name: _claude_has(p) for p in cfg.profiles.values()}
    if cfg.codex.enabled:
        out[cfg.codex.profile or "codex"] = _codex_has(cfg)
    return out
