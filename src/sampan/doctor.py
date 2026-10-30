import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from . import actionlog, codex_approval, codex_daemon, launchd, mcp_install
from . import deps as deps_mod
from . import update as update_mod
from .claude_state import stale_session_files
from .clock import fmt_local, parse_iso
from .codex_state import _state_db
from .config import claude_added, same_source, source_key, valid_source
from .hook import hook_status
from .observe import PROC_ROOT, SPLIT_HINT, live_by_pane, multi_agent_sessions
from .plugins import read_state
from .procs import claude_pids, list_procs
from .registry import RegistryError, load_rows
from .tmux import Tmux, unreadable_message

ICON = {"ok": "✅", "info": "ℹ️ ", "warn": "⚠️ ", "fail": "❌"}


@dataclass(frozen=True)
class Check:
    level: str
    title: str
    detail: str = ""


def _present(cfg):
    """Profiles whose directory still exists; missing ones are left alone (claude is not run, no directory is created)."""
    return [p for p in cfg.profiles.values() if Path(p.config_dir).is_dir()]


def _profile_dirs(cfg):
    out = []
    for p in cfg.profiles.values():
        if not Path(p.config_dir).is_dir():
            out.append(Check("warn", f"{p.name}：設定裡的目錄 {p.config_dir} 不存在，已略過",
                             "目錄被刪掉或搬走了；復原它，或從 config.toml 移除這個 profile（sampan 不會自己建立）"))
    claude = [p for p in cfg.all_profiles.values() if p.agent == "claude"]
    out += [Check("info", f"{p.name} 已停用") for p in claude if p.disabled]
    root = cfg.home / ".claude-profiles"
    known = {Path(p.config_dir) for p in claude}  # a disabled profile's directory is not a new one
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if d.is_dir() and d not in known and ".retired." not in d.name:
            out.append(Check("info", f"{d} 有 profile 目錄但不在設定裡",
                             f"要管理它：sampan profile add {d.name} --dir {d}"))
    return out


def _owners(cfg, uid):
    out = []
    for p in _present(cfg):
        for f in (p.claude_json, Path(p.config_dir) / ".credentials.json", Path(p.config_dir) / "settings.json"):
            if f.exists() and f.stat().st_uid != uid:
                out.append(Check("fail", f"{f} 擁有者不是目前使用者（uid {f.stat().st_uid}）",
                                 f"在一般終端機執行：sudo chown $(whoami) {f}"))
    return out or [Check("ok", "設定檔擁有者正確")]


def _in_use(st, marketplace):
    """Which plugins this profile has installed or enabled from a marketplace."""
    return sorted(pid for pid in set(st.installed) | st.enabled if pid.split("@", 1)[-1] == marketplace)


def _keep_unlisted(p, name, where, used, src):
    add = f"{name} = \"{src}\"" if src else f"{name} = <來源>"
    return Check("warn", f"{p.name}：{where} {name} 不在清單，但還有 {len(used)} 個 plugin 從它載入（{', '.join(used)}）",
                 f"不要移除（這些 plugin 會失效）；要讓 sampan 管理就再跑 sampan init，或在 [marketplaces] 加上 {add}")


def _not_installed(cfg, p, st):
    """What the list asks for but a sync never got in (e.g. marketplace add failed); doctor used to call this matching.
    A marketplace with the wrong source is reported on its own, and sync skips its plugins, so they are not repeated."""
    fix = f"sampan plugins sync --profile {p.name}（看它印出的錯誤：常見是私有 repo 沒有認證）"
    out = [Check("fail", f"{p.name}：清單上的 marketplace {m} 沒有加進來", fix)
           for m in sorted({pid.split("@", 1)[1] for pid in p.plugins} - set(st.marketplaces))]
    wrong = {m for m, repo in st.marketplaces.items() if m in cfg.marketplaces and not same_source(repo, cfg.marketplaces[m])}
    missing = [pid for pid in p.plugins if pid not in st.installed and pid.split("@", 1)[1] not in wrong]
    if missing:
        out.append(Check("fail", f"{p.name}：清單上的 plugin 沒裝上：{', '.join(missing)}", fix))
    return out


def _plugins(cfg):
    out = []
    for p in _present(cfg):
        st = read_state(p)
        for name in sorted(st.extra_marketplaces - set(cfg.marketplaces)):
            used = _in_use(st, name)
            if used:
                if name not in st.marketplaces:
                    out.append(_keep_unlisted(p, name, "settings.json 的 extraKnownMarketplaces", used, None))
                continue
            out.append(Check("fail", f"{p.name}：settings.json 的 extraKnownMarketplaces 有清單外項目 {name}",
                             "從該 profile 的 settings.json 移除"))
        out += _scoped(cfg, p, st)
        out += _not_installed(cfg, p, st)
        for name, repo in sorted(st.marketplaces.items()):
            if name not in cfg.marketplaces:
                used = _in_use(st, name)
                if used:
                    out.append(_keep_unlisted(p, name, "marketplace", used, repo))
                elif not claude_added(name, repo):
                    out.append(Check("fail", f"{p.name}：清單外的 marketplace {name}（{repo or '本機或不支援的來源'}）",
                                     f"CLAUDE_CONFIG_DIR={p.config_dir} claude plugin marketplace remove {name}"))
            elif not same_source(repo, cfg.marketplaces[name]):
                out.append(Check("fail", f"{p.name}：marketplace {name} 的來源是 {repo or '本機或不支援的來源'}，應為 {cfg.marketplaces[name]}",
                                 "直接修 known_marketplaces.json 的 source 再 marketplace update（update 不會改來源）"))
        names_by_repo = {}
        for k, v in st.marketplaces.items():
            if valid_source(v):
                names_by_repo.setdefault(source_key(v), []).append(k)
        for names in names_by_repo.values():
            if len(names) > 1:
                out.append(Check("fail", f"{p.name}：{', '.join(sorted(names))} 指向同一個 repo {st.marketplaces[names[0]]}",
                                 "只留清單上的名稱"))
    return out or [Check("ok", "plugin 與 marketplace 設定符合清單")]


def _home_sessions(cfg, profile):
    try:
        rows = load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)
    except (RegistryError, OSError):
        return []
    return [r.tmux for r in rows if r.state == "active" and r.profile == profile.name and Path(r.cwd) == cfg.home]


def _scoped(cfg, p, st):
    out, home_ids = [], []
    for pid, scope, path in st.scoped:
        if scope == "local":
            out.append(Check("warn", f"{p.name}：{pid} 在 {path} 另有 local scope 安裝",
                             "與 user scope 重複，日後升版可能分岔；sampan plugins sync --prune 會移除"))
        elif path and Path(path) == cfg.home:
            if not p.default:  # the default profile's ~/.claude/settings.json is its user settings anyway
                home_ids.append(pid)
        else:
            out.append(Check("warn", f"{p.name}：{pid} 在 {path} 有 {scope} scope 安裝",
                             f"由 {path}/.claude/settings.json 啟用（進 git），要移除請改那個檔案"))
    if home_ids:
        names = _home_sessions(cfg, p)
        out.append(Check("warn", f"{p.name}：{len(home_ids)} 個 plugin 以 project scope 從 ~/.claude/settings.json 載入",
                         f"{p.name} 有 session 的工作目錄是 ~，會把 預設 profile 的 ~/.claude/settings.json（含權限與 hook）"
                         f"當成專案設定讀入；把這些 session 改到專案目錄："
                         + (", ".join(names) if names else "（登記表裡沒有，可能是手動開的）")))
    return out


def _auth(cfg, run):
    out = []
    for p in _present(cfg):
        r = run(["claude", "auth", "status", "--json"], env=p.env(os.environ))
        try:
            data = json.loads(r.stdout)
        except ValueError:
            out.append(Check("warn", f"{p.name}：讀不到登入狀態", (r.stderr or r.stdout).strip()[:200]))
            continue
        if data.get("loggedIn"):
            out.append(Check("ok", f"{p.name}：已登入 {data.get('email', '')}".rstrip()))
        else:
            out.append(Check("fail", f"{p.name}：未登入", f"sampan claude {p.name} 後執行 /login"))
    return out


KEYCHAIN_SERVICE = "Claude Code-credentials"  # Claude adds "-" + sha256(CLAUDE_CONFIG_DIR)[:8] when the variable is set
_MDAT_RE = re.compile(r'"mdat"<timedate>=\S*\s+"(\d{14})Z')


def _keychain_service(p):
    return KEYCHAIN_SERVICE if p.default else \
        f"{KEYCHAIN_SERVICE}-{hashlib.sha256(str(p.config_dir).encode()).hexdigest()[:8]}"


def _keychain_mtime(run, service):
    """When the login keychain item was last written; None when there is none or it cannot be read. Only the item's
    attributes are read (never -w / -g: no secret, and no permission dialog)."""
    r = run(["security", "find-generic-password", "-s", service])
    m = _MDAT_RE.search(r.stdout or "") if r.returncode == 0 else None
    return datetime.strptime(m.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc) if m else None


def _two_logins(cfg, run, platform):
    """macOS: a profile with a login both in the keychain and in .credentials.json. Claude uses the keychain when it
    can open it (a tmux server started from the desktop) and the file when it cannot (one started over SSH), so one
    copy goes stale; after a reboot the other kind of start picks the stale one and the profile reads as logged out."""
    if platform != "darwin":
        return []
    out = []
    for p in _present(cfg):
        f = Path(p.config_dir) / ".credentials.json"
        if not f.is_file():
            continue
        kc = _keychain_mtime(run, _keychain_service(p))
        if kc is None:
            continue
        ft = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc)
        newer = "，檔案比較新（鑰匙圈那份可能已經過期）" if ft > kc else ""
        out.append(Check("warn", f"{p.name}：登入資訊有兩份（鑰匙圈 {fmt_local(kc)}、.credentials.json {fmt_local(ft)}）",
                         f"Claude 能開鑰匙圈時（tmux 從桌面的終端機開）用鑰匙圈那份，不能時（從 SSH 開）用檔案那份{newer}；"
                         f"兩份不同步，重開機後可能變成未登入。處理：在桌面（不是 SSH）開的終端機裡，於 {p.name} 的 session 執行 /login"))
    return out


def _stale(cfg, run):
    stale = stale_session_files(_present(cfg), claude_pids(list_procs(run)))
    if not stale:
        return [Check("ok", "沒有殘留的 session 檔")]
    return [Check("warn", f"有 {len(stale)} 個殘留的 session 檔（pid 已不存在）",
                  "不影響 save（只讀活著且在 pane 底下的 pid），可手動刪除：" + ", ".join(str(f) for f in stale[:5]))]


def _action_log(cfg):
    path = actionlog.log_path(cfg)
    ts = parse_iso(actionlog.last_stamp(cfg))
    return [Check("info", f"操作紀錄：{path}" + (f"（最後一筆 {fmt_local(ts)}）" if ts else "（還沒有紀錄）"),
                  "plugin、來源、重開、forget、switch 的訊息都記在這裡")]


def _save_log(cfg, now):
    label = launchd.LABEL
    try:
        last = cfg.save_log.read_text().strip().splitlines()[-1]
        ts = parse_iso(last.split(" ", 1)[0])
    except (OSError, IndexError):
        ts = None
    if ts is None:
        return [Check("warn", "自動存檔沒有紀錄", f"確認 launchd {label} 已載入")]
    if now - ts > timedelta(hours=2):
        return [Check("warn", f"自動存檔最後一次是 {fmt_local(ts)}，超過 2 小時",
                      f"launchctl print gui/$(id -u)/{label}")]
    if " error " in f" {last} ":
        return [Check("warn", "自動存檔最後一次失敗", last)]
    return [Check("ok", f"自動存檔正常（{fmt_local(ts)}）")]


def current_sampan():
    """The sampan on PATH, else the path this process was started as. Not resolved: a uv tool's ~/.local/bin/sampan is a
    symlink into the tool's own venv, and the stable path is the one to record."""
    return shutil.which("sampan") or str(Path(sys.argv[0]).absolute())


def _launchd(cfg, run, sampan_path):
    path = launchd.plist_path(cfg.home)
    state, program = launchd.check(cfg.home, sampan_path, run)
    if state == "missing":
        return [Check("warn", f"自動存檔排程沒有安裝（找不到 {path}）", "執行 sampan init 安裝")]
    if state == "unreadable":
        return [Check("warn", f"自動存檔排程的 plist 讀不到或格式不對（{path}）", "執行 sampan init 重新安裝")]
    if state == "wrong":
        return [Check("warn", "自動存檔排程呼叫的不是目前的 sampan",
                      f"plist 是 {program}，目前是 {launchd.expected_program(sampan_path)}；執行 sampan init 重新安裝")]
    if state == "schedule":
        return [Check("warn", "自動存檔排程是舊版的（每小時一次，或睡眠時會暫停的舊計時方式）",
                      "執行 sampan init 重新安裝（改成每 5 分鐘，睡眠中錯過的會在醒來時補跑）")]
    if state == "not_loaded":
        return [Check("warn", "自動存檔排程的 plist 正確，但 launchd 沒有載入（不會自動存檔）",
                      f"執行 sampan init 重新載入，或 launchctl bootstrap gui/$(id -u) {path}")]
    return [Check("ok", "自動存檔排程指向目前的 sampan")]


def _hooks(cfg):
    out = []
    for p in _present(cfg):
        state = hook_status(p)
        if state == "installed":
            out.append(Check("ok", f"{p.name}：SessionStart hook 已安裝"))
        elif state == "stale":
            out.append(Check("warn", f"{p.name}：SessionStart hook 指向的 sampan 已不存在",
                             "重新安裝：sampan init；或從該 profile 的 settings.json 移除"))
    return out


def _statusline(cfg, now):
    from . import statusline_setup as sl
    from .usage import claude_usage, usage_text
    out = []
    for p in _present(cfg):
        data, _why = sl._read(p)
        if data is None:
            continue
        toks = sl._tokens(sl._command(data) or "")
        if toks is None:
            out.append(Check("info", f"{p.name}：沒有安裝 sampan 狀態列",
                             "要記下 Claude 的 5h／週額度：sampan statusline install"))
        elif os.path.isabs(toks[0]) and not Path(toks[0]).exists():
            out.append(Check("warn", f"{p.name}：狀態列指向的 sampan 已不存在（{toks[0]}）",
                             "重新安裝：sampan statusline install"))
        else:
            out.append(Check("info", f"{p.name}：狀態列已接上（額度 {usage_text(claude_usage(cfg, p), now) or '還沒有資料'}）"))
    return out


def _codex(cfg):
    if not cfg.codex.enabled:
        return []
    db = _state_db(cfg.codex.home)
    if db is None:
        return [Check("warn", f"讀不到 Codex 的 state_*.sqlite（{cfg.codex.home}）",
                      f"沒有用 Codex 的話：sampan profile disable {cfg.codex.profile}")]
    try:
        con = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True)
        try:
            con.execute("select id from threads limit 1").fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        return [Check("warn", f"Codex 的 {db.name} 讀不到：{e}", "Codex 對話只能靠 lsof 找到 rollout，退回比對會失效")]
    return [Check("ok", "Codex 狀態資料庫可讀")]


def _codex_daemon(cfg, run, snapshot_fn=None):
    """Whether the Codex app-server daemon answers (one snapshot, ≤3 s); nothing without an enabled Codex profile."""
    if not cfg.codex.enabled:
        return []
    try:
        snap = (snapshot_fn or codex_daemon.snapshot)(cfg.codex.home, run, now=datetime.now(timezone.utc))
    except Exception:  # noqa: BLE001  doctor reports, it never crashes on the daemon
        snap = None
    if snap is None:
        return [Check("info", "Codex 常駐服務沒在執行（codex remote-control start）")]
    return [Check("info", f"Codex 常駐服務：執行中（{snap.home}）")]


def _codex_approval(cfg):
    """Codex's approval_mode tables for sampan's acting tools disagree with [mcp] confirm (e.g. the user chose
    "always allow" in Codex, which writes "approve"), or a config.toml Codex cannot read. Absent: quiet."""
    try:
        text = (Path(cfg.codex.home) / "config.toml").read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as e:
        return [Check("warn", f"Codex config.toml 讀不了：{e}")]
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        return [Check("warn", f"Codex config.toml 讀不了：{e}")]
    if not codex_approval.needs_fix(text, cfg.mcp_confirm):
        return []
    if cfg.mcp_confirm:
        return [Check("warn", "Codex 的 sampan 工具沒設成每次詢問（[mcp] confirm = true）：跑 sampan mcp install 補上")]
    return [Check("warn", "Codex 的 sampan 工具還設著每次詢問（[mcp] confirm = false）：跑 sampan mcp install 拿掉")]


def _mcp(cfg):
    """Where the sampan MCP is installed; quiet when it is nowhere."""
    st = mcp_install.status(cfg)
    here = [n for n, v in st.items() if v]
    codex = _codex_approval(cfg) if cfg.codex.enabled else []  # a broken config.toml reads as "not installed" in st
    return (_mcp_where(st, here) if here else []) + codex


def _mcp_where(st, here):
    missing = [n for n, v in st.items() if not v]
    if not missing:
        return [Check("info", f"sampan MCP：裝在 {'、'.join(here)}")]
    return [Check("info", f"sampan MCP：裝在 {'、'.join(here)}；未裝：{'、'.join(missing)}", "sampan mcp install 補裝")]


def _multi_agent(cfg, run, proc_root):
    tmux = Tmux(run)
    panes = tmux.panes()
    if panes is None:
        return [Check("warn", unreadable_message(tmux.unreadable_reason),
                      "多 agent 等依賴 tmux 的檢查略過；確認 tmux 能正常執行（tmux ls）再跑一次 sampan doctor")]
    procs = list_procs(run)
    found, _warnings = live_by_pane(cfg, run, panes, procs, proc_root)
    return [Check("warn", f"{s}：同一個 tmux session 有多個 agent，sampan 不記、不接回、不停、不搬", SPLIT_HINT)
            for s in sorted(multi_agent_sessions(procs, panes, found))]


def _deps(cfg, which, env, platform):
    return [Check(d.level, d.title, d.hint) for d in deps_mod.check(which, env, cfg.home, platform=platform)
            if d.level != "ok" or d.name in ("tmux", "claude")]


def _version(current, latest_fn):
    """sampan version and the latest version (latest_fn looks up the repo tag, cached)."""
    try:
        latest = latest_fn()
    except Exception:  # noqa: BLE001  if it cannot be found, it is simply unknown
        latest = None
    if latest is None:
        return [Check("info", f"sampan 版本：{current}（查不到最新版本）",
                      f"連不到 {update_mod.REPO}（沒有網路或沒有權限）；6 小時後再查")]
    if update_mod.newer(current, latest):
        return [Check("warn", f"sampan 版本：{current}（最新 v{latest}，sampan 畫面按 U 更新）",
                      f"或在終端機執行：{' '.join(update_mod.UPGRADE)}")]
    if update_mod.base_version(current) is None:
        return [Check("info", f"sampan 版本：{current}（最新 v{latest}；這個版本號無法比較）")]
    return [Check("ok", f"sampan 版本：{current}（已是最新）")]


# options a .tmux.conf may use that older tmux builds reject (the version that added them, from tmux's CHANGES). An
# unknown option makes tmux show an error screen on every attach; distributions often ship an older tmux than Homebrew.
TMUX_OPTION_SINCE = {
    "extended-keys": (3, 2), "pane-border-lines": (3, 2),
    "allow-passthrough": (3, 3), "pane-border-indicators": (3, 3),
    "popup-style": (3, 3), "popup-border-style": (3, 3), "popup-border-lines": (3, 3),
    "menu-style": (3, 4), "menu-selected-style": (3, 4), "menu-border-style": (3, 4), "menu-border-lines": (3, 4),
    "extended-keys-format": (3, 5),
}
_SET_COMMANDS = {"set", "set-option", "setw", "set-window-option"}
_TMUX_VERSION_RE = re.compile(r"tmux (?:next-)?(\d+)\.(\d+)")


def _tmux_conf(cfg, run):
    """Options in ~/.tmux.conf (or ~/.config/tmux/tmux.conf) newer than the tmux installed here. Read only: nothing is
    started (no tmux server is run to try the file); a version tmux -V does not show as N.N is not judged."""
    r = run(["tmux", "-V"])
    m = _TMUX_VERSION_RE.search(r.stdout or "") if r.returncode == 0 else None
    if not m:
        return []
    have = (int(m.group(1)), int(m.group(2)))
    found = []
    for conf in (cfg.home / ".tmux.conf", cfg.home / ".config/tmux/tmux.conf"):
        try:
            lines = conf.read_text(errors="replace").splitlines()
        except OSError:
            continue
        for no, line in enumerate(lines, 1):
            words = line.split("#", 1)[0].split()
            if not words or words[0] not in _SET_COMMANDS:
                continue
            name = next((w for w in words[1:] if not w.startswith("-")), None)
            since = TMUX_OPTION_SINCE.get(name)
            if since and since > have:
                found.append((name, since, f"{conf.name}:{no}"))
    if not found:
        return []
    names = "、".join(dict.fromkeys(f"{n}（{a}.{b}）" for n, (a, b), _ in found))
    return [Check("warn", f"tmux 設定用到這台 tmux {have[0]}.{have[1]} 不認得的選項：{names}",
                  f"每次接上 tmux 都會跳錯誤畫面。拿掉或註解掉這幾行（{'、'.join(w for _, _, w in found)}），"
                  f"或升級 tmux；改完要重開 tmux server 錯誤畫面才會消失")]


def _detach_key(cfg, run):
    """The jump-out key ([ui] detach_key) clashes with a tmux binding sampan did not write."""
    from .detach_key import conflict
    why = conflict(cfg, run)
    return [Check("warn", why)] if why else []


def run_checks(cfg, run, *, now, uid, sampan_path=None, proc_root=PROC_ROOT, which=shutil.which, env=None,
               current=None, latest_fn=None, platform=sys.platform, daemon_fn=None):
    """latest_fn by default uses the run passed in to look up the latest version (honouring the cache); cmd_doctor passes a version with a timeout."""
    sampan_path = sampan_path or current_sampan()
    current = current or update_mod.current_version()
    latest_fn = latest_fn or (lambda: update_mod.check(cfg.state_dir, run=run)[0])
    return (_version(current, latest_fn) + _deps(cfg, which, env, platform) + _profile_dirs(cfg) + _owners(cfg, uid) + _plugins(cfg) + _auth(cfg, run) + _two_logins(cfg, run, platform) + _stale(cfg, run)
            + _hooks(cfg) + _statusline(cfg, now) + _codex(cfg) + _codex_daemon(cfg, run, daemon_fn) + _mcp(cfg) + _detach_key(cfg, run) + _tmux_conf(cfg, run) + _multi_agent(cfg, run, proc_root) + _action_log(cfg)
            + (_launchd(cfg, run, sampan_path) + _save_log(cfg, now) if platform == "darwin" else
               [Check("info", "這台不是 macOS，沒有自動存檔排程（需要時自己跑 sampan save，或用 cron／systemd timer）")]))


def cmd_doctor(cfg, run, out):
    checks = run_checks(cfg, run, now=datetime.now(timezone.utc), uid=os.getuid(),
                        latest_fn=lambda: update_mod.check(cfg.state_dir)[0])
    for c in checks:
        out(f"{ICON[c.level]} {c.title}")
        if c.detail and c.level != "ok":
            out(f"     → {c.detail}")
    return 1 if any(c.level == "fail" for c in checks) else 0
