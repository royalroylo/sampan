"""`sampan mcp install` / `uninstall`: register `sampan mcp serve` with each enabled Claude profile and with Codex,
using the agents' own `mcp add` / `mcp remove` commands (so their config formats stay theirs).
With the user's consent (`--allow-inbox`, or y at the question) install also adds ALLOW to each Claude profile's
settings.json `permissions.allow`, so a doorbelled agent takes and answers its mail without a prompt; uninstall
removes them."""
import json
import os
import shlex
import stat
import tomllib
from datetime import datetime
from pathlib import Path

from . import actionlog, codex_approval, fsutil
from .config import redact
from .launch import codex_env

NAME = "sampan"
ALLOW = ("mcp__sampan__inbox_take", "mcp__sampan__reply")
ALLOW_NAMES = "inbox_take、reply"


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


def _settings(profile_dir):
    return Path(profile_dir) / "settings.json"


def _read_settings(path):
    """(raw bytes or None when missing, data) of a settings.json whose permissions.allow sampan can edit; raises
    ValueError (the reason, naming the file) for broken JSON or a shape it does not know."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, {}
    except OSError as e:
        raise ValueError(f"{path} 讀不了（{e}），沒改") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise ValueError(f"{path} 不是合法的 JSON，沒改") from None
    perms = data.get("permissions", {}) if isinstance(data, dict) else None
    allow = perms.get("allow", []) if isinstance(perms, dict) else None
    if not isinstance(allow, list):
        raise ValueError(f"{path} 的 permissions.allow 格式不認得，沒改")
    return raw, data


def _write_settings(path, raw, data, now):
    """Back up the current bytes (when there is a file) as settings.json.bak-sampan-<time>, then write data
    atomically through a symlink, keeping the mode. Returns the backup's name or None."""
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if json.loads(text) != data:
        raise ValueError(f"{path} 寫回的內容驗證不過，沒改")
    backup = None
    if raw is not None:
        target = fsutil.write_target(path)
        backup = fsutil.backup(target, raw, stat.S_IMODE(target.stat().st_mode), now).name
    fsutil.atomic_write(path, text, encoding="utf-8")  # not the locale's: ensure_ascii=False keeps the user's text
    return backup


def allow_status(profile_dir) -> bool:
    """Whether the profile's settings.json allows both ALLOW tools (unreadable or broken → False)."""
    try:
        _raw, data = _read_settings(_settings(profile_dir))
    except ValueError:
        return False
    allow = data.get("permissions", {}).get("allow", [])
    return all(a in allow for a in ALLOW)


def allow_inbox(profile_dir, *, now, dry_run=False) -> str:
    """Add the missing ALLOW entries at the end of permissions.allow; every other key and entry, and their order,
    stay. A missing file is created as {"permissions": {"allow": [...]}}. Raises ValueError (nothing written) for a
    file it cannot edit. Nothing to add: no write, no backup."""
    path = _settings(profile_dir)
    raw, data = _read_settings(path)
    allow = data.get("permissions", {}).get("allow", [])
    missing = [a for a in ALLOW if a not in allow]
    if not missing:
        return "已允許，不用改"
    if dry_run:
        return f"（試跑）會允許 {ALLOW_NAMES}"
    data.setdefault("permissions", {}).setdefault("allow", []).extend(missing)
    backup = _write_settings(path, raw, data, now)
    if backup is None:
        return f"已建立，允許 {ALLOW_NAMES}"
    return f"已允許 {ALLOW_NAMES}（備份：{backup}）"


def remove_inbox_allow(profile_dir, *, now, dry_run=False) -> str:
    """Remove exactly the ALLOW strings from permissions.allow (everything else, the emptied list included, stays).
    Raises ValueError (nothing written) for a file it cannot edit. Nothing to remove: no write, no backup."""
    path = _settings(profile_dir)
    raw, data = _read_settings(path)
    allow = data.get("permissions", {}).get("allow", [])
    if not any(a in ALLOW for a in allow):
        return "沒有允許，不用改"
    if dry_run:
        return f"（試跑）會移除 {ALLOW_NAMES} 的允許"
    data["permissions"]["allow"] = [a for a in allow if a not in ALLOW]
    backup = _write_settings(path, raw, data, now)
    return f"已移除 {ALLOW_NAMES} 的允許（備份：{backup}）"


def _inbox_allow(out, name, profile, dry_run, *, adding):
    """allow_inbox / remove_inbox_allow for one Claude profile, said on one line; 0, or 1 when it was refused.
    Removing says nothing when there is nothing to remove (uninstall runs it for every profile)."""
    fn = allow_inbox if adding else remove_inbox_allow
    try:
        msg = fn(profile.config_dir, now=datetime.now(), dry_run=dry_run)
    except (ValueError, OSError) as e:
        out(f"❌ {name}：{e}")
        return 1
    if adding or msg != "沒有允許，不用改":
        out(f"{name}：settings.json{'' if msg.startswith('（') else ' '}{msg}")
    return 0


def _codex_approval(cfg, out, confirm, dry_run, *, adding=False, uninstall=False):
    """Codex's approval_mode tables for sampan's tools (codex_approval); 0, or 1 on an error."""
    label = "Codex config.toml"
    if dry_run and adding and confirm:  # codex has not written its sampan entry yet, so apply would find nothing
        out(f"{label}：（試跑）會設成每次詢問")
        return 0
    try:
        msg = codex_approval.apply(Path(cfg.codex.home) / "config.toml", confirm, now=datetime.now(), dry_run=dry_run,
                                   uninstall=uninstall)
    except ValueError as e:
        out(f"❌ {e}")
        return 1
    except OSError as e:
        out(f"❌ {label}：{e}")
        return 1
    out(f"{label}：{'（試跑）' if dry_run else ''}{msg}")
    return 0


def _apply(cfg, run, out, build, *, want_installed, dry_run, done, skip, codex_confirm, codex_first, profiles=(),
           inbox=None):
    """codex_confirm / codex_first: for Codex, the approval tables are set to codex_confirm before `codex mcp …`
    (codex_first, which is uninstall) or after it, also when that is skipped. profiles: only these labels (empty =
    all); an unknown one refuses before anything runs. inbox: "add" / "remove" ALLOW in each Claude profile's
    settings.json once its own step went well (None: leave settings.json alone)."""
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
            rc |= _codex_approval(cfg, out, codex_confirm, dry_run, uninstall=True)
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
        if agent == "claude" and inbox and (ok or inbox == "remove"):  # uninstall removes them whatever claude said
            rc |= _inbox_allow(out, name, profile, dry_run, adding=inbox == "add")
    return rc


def install(cfg, run, out, *, sampan_path, dry_run=False, profiles=(), allow_inbox=False):
    """allow_inbox: the user agreed to ALLOW (asked by the cli, or --allow-inbox)."""
    if Path(sampan_path).name != "sampan":  # e.g. `python -m sampan`: the agents would be told to start something that does not exist
        out(f"❌ 找不到 sampan 執行檔（{sampan_path}），沒裝")
        return 2
    return _apply(cfg, run, out, lambda a: _add_argv(a, sampan_path), want_installed=False,
                  dry_run=dry_run, done="已裝 sampan MCP（重開該 profile 的 session 才會出現工具）",
                  skip="已裝，略過", codex_confirm=cfg.mcp_confirm, codex_first=False, profiles=profiles,
                  inbox="add" if allow_inbox else None)


def uninstall(cfg, run, out, *, dry_run=False, profiles=()):
    return _apply(cfg, run, out, _remove_argv, want_installed=True, dry_run=dry_run,
                  done="已移除 sampan MCP", skip="沒裝，略過", codex_confirm=False, codex_first=True, profiles=profiles,
                  inbox="remove")


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
