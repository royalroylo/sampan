"""`sampan mcp install` / `uninstall`: register `sampan mcp serve` with each enabled Claude profile and with Codex,
using the agents' own `mcp add` / `mcp remove` commands (so their config formats stay theirs)."""
import json
import os
import shlex
import tomllib
from pathlib import Path

from . import actionlog
from .config import redact
from .launch import codex_env

NAME = "sampan"


def _targets(cfg):
    """(label, agent, env) for every enabled Claude profile and for Codex when enabled."""
    out = [(p.name, "claude", p.env(os.environ)) for p in cfg.profiles.values()]
    if cfg.codex.enabled:
        out.append((cfg.codex.profile or "codex", "codex", {**os.environ, **codex_env(cfg)}))
    return out


def _installed(run, agent, env):
    return run([agent, "mcp", "get", NAME], env=env).returncode == 0


def _add_argv(agent, sampan_path):
    serve = ["--", sampan_path, "mcp", "serve"]
    return ["claude", "mcp", "add", "--scope", "user", NAME, *serve] if agent == "claude" else ["codex", "mcp", "add", NAME, *serve]


def _remove_argv(agent):
    return ["claude", "mcp", "remove", "--scope", "user", NAME] if agent == "claude" else ["codex", "mcp", "remove", NAME]


def _apply(cfg, run, out, build, *, want_installed, dry_run, done, skip):
    out = actionlog.tee(cfg, "mcp", out)
    rc = 0
    for name, agent, env in _targets(cfg):
        if _installed(run, agent, env) != want_installed:
            out(f"{name}：{skip}")
            continue
        argv = build(agent)
        if dry_run:
            out(f"{name}：（試跑）會執行 {shlex.join(argv)}")
            continue
        r = run(argv, env=env)
        if r.returncode == 0:
            out(f"✅ {name}：{done}")
        else:
            rc = 1
            out(f"❌ {name}：{shlex.join(argv)} 失敗：{redact((r.stderr or r.stdout).strip())[:300]}")
    return rc


def install(cfg, run, out, *, sampan_path, dry_run=False):
    if Path(sampan_path).name != "sampan":  # e.g. `python -m sampan`: the agents would be told to start something that does not exist
        out(f"❌ 找不到 sampan 執行檔（{sampan_path}），沒裝")
        return 2
    return _apply(cfg, run, out, lambda a: _add_argv(a, sampan_path), want_installed=False,
                  dry_run=dry_run, done="已裝 sampan MCP（重開該 profile 的 session 才會出現工具）",
                  skip="已裝，略過")


def uninstall(cfg, run, out, *, dry_run=False):
    return _apply(cfg, run, out, _remove_argv, want_installed=True, dry_run=dry_run,
                  done="已移除 sampan MCP", skip="沒裝，略過")


def _claude_has(profile):
    try:
        data = json.loads(Path(profile.claude_json).read_text())
    except (OSError, ValueError):
        return False
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    return isinstance(servers, dict) and NAME in servers


def _codex_has(cfg):
    try:
        data = tomllib.loads((Path(cfg.codex.home) / "config.toml").read_text())
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
