"""sampan profile list / rename / disable / enable / forget, and the one safe way profile actions write config.toml (edit_config)."""
import os
import shutil
import tomllib
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from . import actionlog, registry, usage
from .clock import now_iso, now_stamp
from .config import ConfigError, load_config
from .config_edit import NAME_RE, _k, _s, append_section, remove_profile, remove_table, rename_profile, set_profile_flag
from .fsutil import atomic_write
from .init import NAME_RULE, _disp, _email
from .launch import claude_argv, codex_argv, shell_command
from .observe import PROC_ROOT, live_by_pane
from .procs import is_uuid, list_procs
from .status import State, status_rows
from .tmux import Tmux, TmuxUnreadable

UNKNOWN_ACCOUNT = "（讀不到）"
ACCOUNT_TIMEOUT = 5  # seconds for `claude auth status` / `codex login status`; slower counts as unreadable


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


def edit_config(cfg, out, edit, *, stamp=now_stamp, backups=None):
    """Apply edit(text) -> new text | None to config.toml as it is on disk now, after writing out an implicit Codex
    profile (first profile write only). The result is checked with load_config before anything is written, then the
    file is backed up (config.toml.bak-<stamp>, mode kept) and replaced atomically. Returns the new Config, or None
    (the reason printed, the file untouched). backups: when given a list, receives the backup's path."""
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
    if backups is not None:
        backups.append(backup)
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
    """One ProfileRow per profile in config order; rows = status.status_rows (counts the running sessions).
    Each account lookup gets ACCOUNT_TIMEOUT seconds (rc 124 then: shown as （讀不到）)."""
    base = os.environ if env is None else env
    outer = run

    def run(args, *, env=None, cwd=None):
        return outer(args, env=env, cwd=cwd, timeout=ACCOUNT_TIMEOUT)
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


def _line(cfg, pr, usage_str=""):
    state = "停用" if pr.disabled else ("使用中（預設）" if pr.default else "使用中")
    parts = [pr.name, pr.agent, _disp(pr.config_dir, cfg.home), pr.account, state, f"{pr.running} 個執行中"]
    if pr.plugins is not None:
        parts.append(f"plugin {pr.plugins}")
    if usage_str:
        parts.append(usage_str)
    return "  ".join(parts)


def _usage(cfg, p):
    return usage.codex_profile_usage(cfg, p) if p.agent == "codex" else usage.claude_usage(cfg, p.name)


def cmd_profile_list(cfg, run, out, *, env=None, now=None):
    now = now or datetime.now(timezone.utc)
    try:
        rows = status_rows(cfg, run)
    except registry.RegistryError as e:
        out(f"❌ {e}")
        return 2
    if getattr(rows, "warning", None):
        out(f"⚠ {rows.warning}；執行中數字不準")
    for pr in profile_rows(cfg, run, rows, env=env):
        out(_line(cfg, pr, usage.usage_text(_usage(cfg, cfg.all_profiles[pr.name]), now)))
    return 0


# ---------- rename ----------

def _undo_config(cfg, out, backup, err):
    """The registry could not follow a config edit: put the config back from its backup (mode kept), so the two never
    disagree. Returns 1."""
    try:
        atomic_write(cfg.path, backup.read_text())
    except OSError as e:
        out(f"❌ 登記表寫入失敗（{err}），設定檔也還原不了（{e}）；請用 {backup} 手動還原")
        return 1
    backup.unlink(missing_ok=True)  # the file is back as it was: nothing to keep a backup of
    out(f"❌ 登記表寫入失敗（{err}），設定檔已還原，什麼都沒改")
    return 1


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
        backups = []
        if edit_config(cfg, out, lambda text: rename_profile(text, old, new), backups=backups) is None:
            return 1
        hit = [r for r in rows if r.profile == old and r.agent == agent]
        for r in hit:
            r.profile = new
        if hit:
            try:
                registry.write_rows(cfg.registry_path, rows, now_iso(), cfg.history_dir)
            except OSError as e:
                return _undo_config(cfg, out, backups[0], e)
    out(f"✔ 已把 profile {old} 改名為 {new}（登記表 {len(hit)} 列一起改）")
    err = usage.move_usage(cfg, old, new)
    if err:
        out(f"⚠ 額度紀錄 {usage.usage_path(cfg, old)} 搬不過去（{err}）")
    return 0


# ---------- disable / enable / forget ----------

def _running_refusal(cfg, run, out, name, verb, proc_root):
    """None when no session runs under `name`; otherwise the refusal printed and the exit code returned."""
    try:
        running = running_sessions(cfg, run, name, proc_root=proc_root)
    except TmuxUnreadable as e:
        out(f"❌ {e}；不知道 profile {name} 有沒有 session 在跑，什麼都沒改")
        return 2
    if running:
        out(f"❌ profile {name} 還有 session 在跑：{'、'.join(running)}；先停掉再{verb}")
        return 1
    return None


def cmd_profile_disable(cfg, run, out, name, *, proc_root=PROC_ROOT):
    out = actionlog.tee(cfg, f"profile disable {name}", out)
    p = cfg.all_profiles.get(name)
    if p is None:
        out(f"❌ 沒有 profile {name}")
        return 1
    if p.default:
        out(f"❌ {name} 是預設 profile，不能停用；先把別的 profile 設成 default")
        return 1
    if p.disabled:
        out(f"profile {name} 已經停用")
        return 0
    refused = _running_refusal(cfg, run, out, name, "停用", proc_root)
    if refused is not None:
        return refused
    with registry.lock(cfg.state_dir):
        if edit_config(cfg, out, lambda text: set_profile_flag(text, name, "disabled", True)) is None:
            return 1
    out(f"✔ 已停用 {name}；目錄 {_disp(p.config_dir, cfg.home)} 保留（要刪的話先確認裡面沒有連到別處的捷徑）")
    return 0


def cmd_profile_enable(cfg, run, out, name):
    out = actionlog.tee(cfg, f"profile enable {name}", out)
    p = cfg.all_profiles.get(name)
    if p is None:
        out(f"❌ 沒有 profile {name}")
        return 1
    if not p.disabled:
        out(f"profile {name} 沒有停用")
        return 0
    with registry.lock(cfg.state_dir):
        if edit_config(cfg, out, lambda text: set_profile_flag(text, name, "disabled", None)) is None:
            return 1
    out(f"✔ 已恢復 {name}")
    return 0


def _forget_text(text, name, agent):
    """[profiles.name] removed; when it was the last Codex profile, Codex is switched off with [codex] enabled = false
    (otherwise the next load would fill an implicit Codex profile back in). None when that can't be done."""
    text = remove_profile(text, name)
    if text is None or agent != "codex":
        return text
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None
    if any(isinstance(p, dict) and p.get("agent") == "codex" for p in data.get("profiles", {}).values()):
        return text
    if "codex" in data:
        text = remove_table(text, "codex")
    return append_section(text, "[codex]\nenabled = false\n")


def _resume_line(cfg, p, row):
    """The manual resume command for a row of a profile no longer in the config (launch_line can't look it up)."""
    if not is_uuid(row.session_id):
        return f"  對話 id {row.session_id}"
    if p.agent == "codex":
        env = {} if p.config_dir == cfg.home / ".codex" else {"CODEX_HOME": str(p.config_dir)}
        line = shell_command(codex_argv(resume=row.session_id, extra=("-C", row.cwd) if row.cwd else ()),
                             env_set=env, cwd=row.cwd or None)
    else:
        line = shell_command(claude_argv(p, resume=row.session_id), env_set={"CLAUDE_CONFIG_DIR": str(p.config_dir)},
                             cwd=row.cwd or None)
    return f"  對話還在：{line}"


def cmd_profile_forget(cfg, run, out, ask, name, *, proc_root=PROC_ROOT):
    """Take a disabled profile out of the config file (its directory stays) together with its registry rows."""
    out = actionlog.tee(cfg, f"profile forget {name}", out)
    p = cfg.all_profiles.get(name)
    if p is None:
        out(f"❌ 沒有 profile {name}")
        return 1
    if not p.disabled:
        out(f"❌ 先 sampan profile disable {name}")
        return 1
    refused = _running_refusal(cfg, run, out, name, " forget", proc_root)
    if refused is not None:
        return refused
    try:
        answer = ask(f"輸入 {name} 確認：")
    except EOFError:
        answer = ""
    if (answer or "").strip() != name:
        out("已取消")
        return 0
    with registry.lock(cfg.state_dir):
        try:
            rows = registry.load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)
        except registry.RegistryError as e:
            out(f"❌ {e}")
            return 1
        hit = [r for r in rows if cfg.owner(r.agent, r.profile) is p]
        if hit:
            tmux = Tmux(run)
            try:
                tmux.require_panes()
            except TmuxUnreadable as e:
                out(f"❌ {e}；不知道 profile {name} 的 session 還在不在 tmux 裡，什麼都沒改")
                return 2
            live = [r.tmux for r in hit if tmux.has_session(r.tmux)]
            if live:
                out(f"❌ 這些還在 tmux 裡：{'、'.join(live)}；先 sampan stop")
                return 1
        backups = []
        if edit_config(cfg, out, lambda text: _forget_text(text, name, p.agent), backups=backups) is None:
            return 1
        if hit:
            keep = [r for r in rows if all(r is not h for h in hit)]
            try:
                registry.write_rows(cfg.registry_path, keep, now_iso(), cfg.history_dir)
            except OSError as e:
                return _undo_config(cfg, out, backups[0], e)
    out(f"✔ 已從設定檔拿掉 profile {name}（目錄 {_disp(p.config_dir, cfg.home)} 保留；登記表 {len(hit)} 列一起移除）")
    err = usage.move_usage(cfg, name, None)
    if err:
        out(f"⚠ 額度紀錄 {usage.usage_path(cfg, name)} 刪不掉（{err}）")
    for r in hit:
        out(_resume_line(cfg, p, r))
    return 0
