import json
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .config import redact, same_source, strip_credentials


@dataclass
class ProfilePluginState:
    installed: dict
    project_scoped: list
    enabled: set
    marketplaces: dict
    extra_marketplaces: set
    scoped: list = field(default_factory=list)  # [(plugin id, scope, projectPath)], installs other than user scope
    commits: dict = field(default_factory=dict)  # plugin id -> gitCommitSha of the user-scope entry
    market_info: dict = field(default_factory=dict)  # marketplace name -> {"updated": lastUpdated or None, "auto": autoUpdate or None}


def _json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def source_of(src):
    """The source in known_marketplaces.json → GitHub owner/repo, a git URL, or None (a local directory or another source sampan does not manage)."""
    if not isinstance(src, dict):
        return None
    if src.get("source") == "github":
        return src.get("repo")
    if src.get("source") == "git":
        return strip_credentials(src.get("url"))
    return None


def read_state(profile):
    pd = Path(profile.config_dir) / "plugins"
    installed, project_scoped, scoped, commits = {}, [], [], {}
    for pid, entries in ((_json(pd / "installed_plugins.json") or {}).get("plugins") or {}).items():
        for e in entries:
            if e.get("scope") == "user":
                installed[pid] = str(e.get("version") or "?")
                if e.get("gitCommitSha"):
                    commits[pid] = str(e["gitCommitSha"])
            else:
                where = f" {e['projectPath']}" if e.get("projectPath") else ""
                project_scoped.append(f"{pid}（{e.get('scope')}{where}）")
                scoped.append((pid, e.get("scope"), e.get("projectPath")))
    marketplaces, market_info = {}, {}
    for name, v in (_json(pd / "known_marketplaces.json") or {}).items():
        v = v if isinstance(v, dict) else {}
        marketplaces[name] = source_of(v.get("source") or {})
        upd, auto = v.get("lastUpdated"), v.get("autoUpdate")
        market_info[name] = {"updated": upd if isinstance(upd, str) else None, "auto": auto if isinstance(auto, bool) else None}
    settings = _json(Path(profile.config_dir) / "settings.json") or {}
    enabled = {k for k, v in (settings.get("enabledPlugins") or {}).items() if v}
    extra = set((settings.get("extraKnownMarketplaces") or {}).keys())
    return ProfilePluginState(installed, project_scoped, enabled, marketplaces, extra, scoped, commits, market_info)


def missing_dir_message(profile):
    return f"⚠ {profile.name}：{profile.config_dir} 不存在，略過（不會建立）"


def status_lines(cfg):
    profiles = list(cfg.profiles.values())
    missing = {p.name for p in profiles if not Path(p.config_dir).is_dir()}
    states = {p.name: read_state(p) for p in profiles}
    ids = sorted(set().union(*(set(p.plugins) for p in profiles), *(set(s.installed) for s in states.values())))
    lines = ["plugin".ljust(46) + "".join(p.name.ljust(16) for p in profiles)]
    for pid in ids:
        cells, versions = [], set()
        for p in profiles:
            v, listed = states[p.name].installed.get(pid), pid in p.plugins
            if p.name in missing:
                cells.append("目錄不存在")
            elif v and listed:
                cells.append(v[:14])
                versions.add(v)
            elif v:
                cells.append(f"{v[:12]} +")
            else:
                cells.append("✗ 未裝" if listed else "—")
        mark = "  ← 版本不一致" if len(versions) > 1 else ""
        lines.append(pid.ljust(46) + "".join(c.ljust(16) for c in cells) + mark)
    lines.append("（+ = 裝了但不在清單；✗ = 清單有但沒裝）")
    for p in profiles:
        st = states[p.name]
        if p.name in missing:
            lines.append(missing_dir_message(p))
            continue
        for x in st.project_scoped:
            lines.append(f"⚠ {p.name}：project scope 安裝 {x}")
    return lines


def cmd_plugins_status(cfg, out):
    for line in status_lines(cfg):
        out(line)
    return 0


def plan_sync(cfg, profile, state):
    cmds, errors, warnings = [], [], []
    blocked = set()
    for m in sorted({pid.split("@", 1)[1] for pid in profile.plugins}):
        want = cfg.marketplaces[m]
        if m not in state.marketplaces:
            cmds.append(["claude", "plugin", "marketplace", "add", want])
        elif not same_source(state.marketplaces[m], want):
            errors.append(f"marketplace {m} 的來源是 {state.marketplaces[m] or '本機或不支援的來源'}，清單要求 {want}；"
                          f"不自動改（見 sampan doctor），這個 marketplace 的 plugin 這次略過")
            blocked.add(m)
            continue
        cmds.append(["claude", "plugin", "marketplace", "update", m])
    for pid in profile.plugins:
        if pid.split("@", 1)[1] in blocked:
            continue
        verb = "update" if pid in state.installed else "install"
        cmds.append(["claude", "plugin", verb, pid, "--scope", "user"])
    for m in sorted(set(state.marketplaces) - set(cfg.marketplaces)):
        warnings.append(f"清單外的 marketplace：{m}")
    for pid in sorted(state.enabled - set(profile.plugins)):
        warnings.append(f"清單外但已啟用：{pid}")
    if state.scoped:
        warnings.append(f"另有 {len(state.scoped)} 筆 user 以外的安裝（sampan doctor 有說明；local 的可用 --prune 移除）")
    return cmds, errors, warnings


def plan_prune(cfg, profile, state):
    """Plugins/marketplaces outside the list and duplicate installs outside user scope → ([(command, cwd or None)], [notes on what was skipped]).

    Project scope is never touched: the one at ~ is ~/.claude/settings.json (the default profile's user settings), and
    removing it would disable the default profile's plugins too; the one in a repo is written to the git-tracked .claude/settings.json."""
    cmds, skipped, home = [], [], []
    for pid in sorted(set(state.installed) - set(profile.plugins)):
        cmds.append((["claude", "plugin", "uninstall", pid, "--scope", "user"], None))
    for pid, scope, path in state.scoped:
        if scope == "local":
            if path and Path(path).is_dir():
                cmds.append((["claude", "plugin", "uninstall", pid, "--scope", "local"], path))
            else:
                skipped.append(f"{pid} 的 local 安裝在 {path}，目錄不存在，略過")
        elif path and Path(path) == cfg.home:
            if not profile.default:
                home.append(pid)
        else:
            skipped.append(f"{pid}：{scope} scope 由 {path}/.claude/settings.json 啟用（進 git），不自動改")
    if home:
        skipped.append(f"{len(home)} 個 plugin 的 ~ project scope 來自 ~/.claude/settings.json（預設 profile 的 user 設定），不動；"
                       f"成因是有 {profile.name} 的 session 工作目錄是 ~（見 sampan doctor）")
    for m in sorted(set(state.marketplaces) - set(cfg.marketplaces)):
        cmds.append((["claude", "plugin", "marketplace", "remove", m], None))
    return cmds, skipped


def cmd_sync(cfg, run, out, *, profile=None, dry_run=False, prune=False):
    if profile is not None and profile not in cfg.profiles:
        out(f"❌ 未知的 profile：{profile}")
        return 2
    targets = [cfg.profiles[profile]] if profile else list(cfg.profiles.values())
    failures, changed = [], False
    for p in targets:
        out(f"== {p.name}")
        if not Path(p.config_dir).is_dir():
            out(missing_dir_message(p))
            continue
        before = read_state(p)
        cmds, errors, warnings = plan_sync(cfg, p, before)
        failures += [f"{p.name}：{e}" for e in errors]
        for w in warnings:
            out(f"  ⚠ {w}")
        env = p.env(os.environ)
        for c in cmds:
            if dry_run:
                out(f"  [dry-run] {shlex.join(c)}")
                continue
            r = run(c, env=env, cwd=str(cfg.home))
            if r.returncode != 0:
                failures.append(f"{p.name}：{shlex.join(c)} 失敗：{redact((r.stderr or r.stdout).strip())[:300]}")
        if not dry_run:
            after = read_state(p).installed
            diffs = [(pid, before.installed.get(pid), after.get(pid)) for pid in p.plugins]
            diffs = [(pid, b, a) for pid, b, a in diffs if b != a]
            for pid, b, a in diffs:
                out(f"  {pid}：{b or '未裝'} → {a or '未裝'}")
            if not diffs:
                out("  沒有變動")
            changed = changed or bool(diffs)
        if prune:
            pcmds, skipped = plan_prune(cfg, p, before if dry_run else read_state(p))
            for s in skipped:
                out(f"  ⚠ {s}")
            for c, cwd in pcmds:
                where = f"（在 {cwd}）" if cwd else ""
                if dry_run:
                    out(f"  [dry-run] {shlex.join(c)}{where}")
                    continue
                r = run(c, env=env, cwd=cwd or str(cfg.home))
                if r.returncode != 0:
                    failures.append(f"{p.name}：{shlex.join(c)} 失敗：{redact((r.stderr or r.stdout).strip())[:300]}")
                else:
                    out(f"  移除：{shlex.join(c[2:])}{where}")
                    changed = True
    if changed:
        out("要生效需整個重開該 profile 的 session：sampan stop --profile <p> && sampan restore"
            "（不要用 /reload-plugins）")
    for f in failures:
        out(f"  ❌ {f}")
    return 1 if failures else 0
