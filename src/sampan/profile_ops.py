"""sampan profile list / rename, and the one safe way profile actions write config.toml (edit_config)."""
import os
import shutil
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from . import actionlog, registry
from .clock import now_iso, now_stamp
from .config import ConfigError, load_config
from .config_edit import NAME_RE, _k, _s, append_section, remove_table, rename_profile
from .fsutil import atomic_write
from .init import NAME_RULE, _disp, _email
from .observe import PROC_ROOT, live_by_pane
from .procs import list_procs
from .status import State, status_rows
from .tmux import Tmux, TmuxUnreadable

UNKNOWN_ACCOUNT = "（讀不到）"


@dataclass(frozen=True)
class ProfileRow:
    name: str
    agent: str  # "claude" | "codex"
    config_dir: Path
    label: str
    disabled: bool
    default: bool
    running: int  # sessions running under it now
    plugins: int | None  # None for Codex
    account: str


# ---------- writing the config ----------

def _convert_codex(cfg, text):
    """text with the implicit Codex profile written out as [profiles.<name>] and any [codex] table removed;
    None when that can't be done. Unchanged when there is no implicit Codex profile ([codex] enabled = false
    means no Codex profile at all, so it is left as it is)."""
    if not cfg.implicit_codex:
        return text, None
    p = next(p for p in cfg.all_profiles.values() if p.agent == "codex")
    disp = _disp(p.config_dir, cfg.home)
    text = append_section(text, f"[profiles.{_k(p.name)}]\nagent = \"codex\"\nconfig_dir = {_s(disp)}\n")
    try:
        has_table = "codex" in tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None, None
    if has_table:
        text = remove_table(text, "codex")
    return text, f"已把 [codex] 轉成 [profiles.{p.name}]（目錄 {disp}）"


def edit_config(cfg, out, edit, *, stamp=now_stamp):
    """Apply edit(text) -> new text | None to config.toml as it is on disk now, after writing out an implicit Codex
    profile (first profile write only). The result is checked with load_config before anything is written, then the
    file is backed up (config.toml.bak-<stamp>, mode kept) and replaced atomically. Returns the new Config, or None
    (the reason printed, the file untouched)."""
    path = cfg.path
    try:
        text = path.read_text()
        cur = load_config(path, home=cfg.home, state_dir=cfg.state_dir)  # the file may have changed since cfg was read
    except (OSError, ConfigError) as e:
        out(f"❌ 讀不到設定檔（{e}），設定檔沒改")
        return None
    text, note = _convert_codex(cur, text)
    new = None if text is None else edit(text)
    if new is None:
        out(f"❌ 設定檔無法自動修改（{path}），設定檔沒改")
        return None
    check = path.with_name(f".{path.name}.check-{os.getpid()}")
    try:
        check.write_text(new)
        new_cfg = load_config(check, home=cfg.home, state_dir=cfg.state_dir)
    except (OSError, ConfigError) as e:
        out(f"❌ 改完的設定檔有問題（{str(e).replace(str(check), str(path))}），設定檔沒改")
        return None
    finally:
        check.unlink(missing_ok=True)
    try:
        backup = path.with_name(f"{path.name}.bak-{stamp()}")
        shutil.copy2(path, backup)
        try:
            atomic_write(path, new)
        except OSError:
            backup.unlink(missing_ok=True)  # a backup only of a write that happened
            raise
    except OSError as e:
        out(f"❌ 設定檔寫入失敗（{e}），設定檔沒改")
        return None
    if note:
        out(note)
    return replace(new_cfg, path=path)


# ---------- reading ----------

def running_sessions(cfg, run, name, *, proc_root=PROC_ROOT):
    """tmux sessions whose live agent runs under profile `name` now. Disabled Claude profiles are scanned too, so a
    session opened before the profile was disabled is seen. Raises TmuxUnreadable when tmux cannot be read."""
    claude = {n: p for n, p in cfg.all_profiles.items() if p.agent == "claude"}
    scan = replace(cfg, profiles=claude)
    panes = Tmux(run).require_panes()
    found, _w = live_by_pane(scan, run, panes, list_procs(run), proc_root)
    return sorted({p.session for p in panes if p.pane_id in found and found[p.pane_id].profile == name})


def _codex_account(run, config_dir, base):
    """First line `codex login status` prints (never auth.json)."""
    r = run(["codex", "login", "status"], env=dict(base) | {"CODEX_HOME": str(config_dir)})
    if r.returncode != 0:
        return UNKNOWN_ACCOUNT
    text = (r.stdout or "").strip() or (r.stderr or "").strip()
    return next((line.strip() for line in text.splitlines() if line.strip()), UNKNOWN_ACCOUNT)


def profile_rows(cfg, run, rows, *, env=None):
    """One ProfileRow per profile in config order; rows = status.status_rows (counts the running sessions)."""
    base = os.environ if env is None else env
    result = []
    for p in cfg.all_profiles.values():
        running = sum(1 for r in rows if r.state == State.RUNNING and r.profile == p.name and r.agent == p.agent)
        if p.agent == "codex":
            account, plugins = _codex_account(run, p.config_dir, base), None
        else:
            account, plugins = _email(run, p.config_dir, p.default, base) or UNKNOWN_ACCOUNT, len(p.plugins)
        result.append(ProfileRow(p.name, p.agent, p.config_dir, p.label, p.disabled, p.default, running, plugins,
                                 account))
    return result


def _line(cfg, pr):
    state = "停用" if pr.disabled else ("使用中（預設）" if pr.default else "使用中")
    parts = [pr.name, pr.agent, _disp(pr.config_dir, cfg.home), pr.account, state, f"{pr.running} 個執行中"]
    if pr.plugins is not None:
        parts.append(f"plugin {pr.plugins}")
    return "  ".join(parts)


def cmd_profile_list(cfg, run, out, *, env=None):
    rows = status_rows(cfg, run)
    if getattr(rows, "warning", None):
        out(f"⚠ {rows.warning}；執行中數字不準")
    for pr in profile_rows(cfg, run, rows, env=env):
        out(_line(cfg, pr))
    return 0


# ---------- rename ----------

def cmd_profile_rename(cfg, run, out, old, new, *, proc_root=PROC_ROOT):
    out = actionlog.tee(cfg, f"profile rename {old}", out)
    if not NAME_RE.match(new):
        out(f"❌ profile {NAME_RULE}")
        return 1
    if old not in cfg.all_profiles:
        out(f"❌ 沒有 profile {old}")
        return 1
    if new in cfg.all_profiles:
        out(f"❌ 已經有 profile {new}")
        return 1
    agent = cfg.all_profiles[old].agent
    try:
        running = running_sessions(cfg, run, old, proc_root=proc_root)
    except TmuxUnreadable as e:
        out(f"❌ {e}；不知道 profile {old} 有沒有 session 在跑，沒有改名")
        return 2
    if running:
        out(f"❌ profile {old} 還有 session 在跑：{'、'.join(running)}；先停掉再改名")
        return 1
    with registry.lock(cfg.state_dir):
        try:
            rows = registry.load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)
        except registry.RegistryError as e:
            out(f"❌ {e}")
            return 1
        if edit_config(cfg, out, lambda text: rename_profile(text, old, new)) is None:
            return 1
        hit = [r for r in rows if r.profile == old and r.agent == agent]
        for r in hit:
            r.profile = new
        if hit:
            try:
                registry.write_rows(cfg.registry_path, rows, now_iso(), cfg.history_dir)
            except OSError as e:
                out(f"❌ 設定檔已改名，但登記表寫入失敗（{e}）；{len(hit)} 列還記著 {old}")
                return 1
    out(f"✔ 已把 profile {old} 改名為 {new}（登記表 {len(hit)} 列一起改）")
    return 0
