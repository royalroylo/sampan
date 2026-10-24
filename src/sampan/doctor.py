import json
import os
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from . import actionlog, launchd
from . import deps as deps_mod
from . import update as update_mod
from .claude_state import stale_session_files
from .clock import fmt_local, parse_iso
from .codex_state import _state_db
from .config import same_source, source_key, valid_source
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
    root = cfg.home / ".claude-profiles"
    known = {Path(p.config_dir) for p in cfg.profiles.values()}
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
        for name, repo in sorted(st.marketplaces.items()):
            if name not in cfg.marketplaces:
                used = _in_use(st, name)
                if used:
                    out.append(_keep_unlisted(p, name, "marketplace", used, repo))
                else:
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
        return [Check("warn", "自動存檔排程是舊版的計時方式（睡眠時會暫停，可能好幾小時沒存檔）",
                      "執行 sampan init 重新安裝（改成每個整點，睡眠中錯過的會在醒來時補跑）")]
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


def _codex(cfg):
    if not cfg.codex.enabled:
        return []
    db = _state_db(cfg.codex.home)
    if db is None:
        return [Check("warn", f"讀不到 Codex 的 state_*.sqlite（{cfg.codex.home}）",
                      "沒有用 Codex 的話，把 config.toml 的 [codex] enabled 設成 false")]
    try:
        con = sqlite3.connect(f"file:{quote(str(db))}?mode=ro", uri=True)
        try:
            con.execute("select id from threads limit 1").fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        return [Check("warn", f"Codex 的 {db.name} 讀不到：{e}", "Codex 對話只能靠 lsof 找到 rollout，退回比對會失效")]
    return [Check("ok", "Codex 狀態資料庫可讀")]


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


def run_checks(cfg, run, *, now, uid, sampan_path=None, proc_root=PROC_ROOT, which=shutil.which, env=None,
               current=None, latest_fn=None, platform=sys.platform):
    """latest_fn by default uses the run passed in to look up the latest version (honouring the cache); cmd_doctor passes a version with a timeout."""
    sampan_path = sampan_path or current_sampan()
    current = current or update_mod.current_version()
    latest_fn = latest_fn or (lambda: update_mod.check(cfg.state_dir, run=run)[0])
    return (_version(current, latest_fn) + _deps(cfg, which, env, platform) + _profile_dirs(cfg) + _owners(cfg, uid) + _plugins(cfg) + _auth(cfg, run) + _stale(cfg, run)
            + _hooks(cfg) + _codex(cfg) + _multi_agent(cfg, run, proc_root) + _action_log(cfg)
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
