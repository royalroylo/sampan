"""sampan init and sampan profile add: detect profiles, generate or extend the config file, the optional hook, the automatic save schedule."""
import copy
import difflib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

from . import launchd, paths
from .config import SOURCE_RULE, ConfigError, load_config, same_source, valid_source
from .deps import PATH_LINE
from .deps import ensure as ensure_deps
from .fsutil import atomic_write
from .hook import hook_status, install_hook, uninstall_hook
from .observe import PROC_ROOT
from .plugins import read_state
from .procs import claude_pids, list_procs, proc_env

NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
NO_SCHEDULE_NOTE = "ℹ 這台不是 macOS，沒有自動存檔排程；需要時自己跑 `sampan save`（或用 cron／systemd timer）"
NAME_RULE = "名稱只能用英數字、- 和 _"


@dataclass(frozen=True)
class Detected:
    config_dir: Path
    email: str | None
    name: str
    plugins: list[str]
    marketplaces: dict[str, str | None]  # name -> GitHub owner/repo or git URL; None for other sources such as local directories
    default: bool


# ---------- detection ----------

def _key(path):
    return os.path.realpath(path)


def _profile_env(config_dir, default, base):
    env = dict(base)
    if default:
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    return env


def _email(run, config_dir, default, base):
    r = run(["claude", "auth", "status", "--json"], env=_profile_env(config_dir, default, base))
    try:
        data = json.loads(r.stdout or "")
    except ValueError:
        return None
    email = data.get("email") if isinstance(data, dict) else None
    return email if isinstance(email, str) and email else None


def _sanitize(name):
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "default"


def detect_profiles(home, run, procs, *, env=None, proc_root=PROC_ROOT):
    """~/.claude (default), ~/.claude-profiles/*/, and the CLAUDE_CONFIG_DIR of running claude processes (deduplicated)."""
    home = Path(home)
    base = os.environ if env is None else env
    dirs = []
    if (home / ".claude").is_dir():
        dirs.append((home / ".claude", True))
    root = home / ".claude-profiles"
    for d in sorted(root.iterdir()) if root.is_dir() else []:
        if d.is_dir() and not d.name.startswith("."):
            dirs.append((d, False))
    seen = {_key(d) for d, _ in dirs}
    running = set()
    for pid in sorted(claude_pids(procs)):
        value = (proc_env(run, pid, proc_root) or {}).get("CLAUDE_CONFIG_DIR")
        if value and Path(value).expanduser().is_dir():
            running.add(Path(value).expanduser())
    for d in sorted(running):
        if _key(d) not in seen:
            seen.add(_key(d))
            dirs.append((d, False))

    found = []
    for d, default in dirs:
        st = read_state(SimpleNamespace(config_dir=d))
        email = _email(run, d, default, base)
        if default:
            name = email.split("@", 1)[0] if email else "default"
        else:
            name = d.name
        found.append(Detected(d, email, _sanitize(name), sorted(st.installed), dict(st.marketplaces), default))
    return found


def detect_codex(home, env, which=shutil.which):
    """(enabled, codex home): enabled only if codex is on PATH or the codex home exists."""
    codex_home = Path(env["CODEX_HOME"]).expanduser() if env.get("CODEX_HOME") else Path(home) / ".codex"
    return bool(which("codex")) or codex_home.is_dir(), codex_home


# ---------- TOML generation ----------

def _s(value):
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def _k(name):
    return name if NAME_RE.match(name) else _s(name)


def _disp(path, home):
    path = Path(path)
    if home is not None:
        try:
            rel = path.relative_to(home)
        except ValueError:
            rel = None
        if rel is not None and str(rel) != ".":
            return f"~/{rel}"
    return str(path)


def _array(items):
    return "[]" if not items else "[\n" + "".join(f"  {_s(x)},\n" for x in items) + "]"


def _source(pid, marketplaces, chosen):
    """(repo, None) when pid can go into the config, else (None, reason)."""
    if "@" not in pid:
        return None, "不是 plugin@marketplace 格式"
    m = pid.split("@", 1)[1]
    if m not in marketplaces:
        return None, f"找不到 marketplace {m}"
    repo = marketplaces[m]
    if not valid_source(repo):
        return None, f"marketplace {m} 的來源不是 {SOURCE_RULE}"
    if m in chosen and not same_source(chosen[m], repo):
        return None, f"marketplace {m} 指向 {repo}，和設定裡的 {chosen[m]} 不同"
    return repo, None


def _select(d, chosen):
    """Plugins of d usable in the config; adds their marketplaces to chosen. -> (keep, [(pid, reason)])."""
    keep, skipped = [], []
    for pid in d.plugins:
        repo, why = _source(pid, d.marketplaces, chosen)
        if why:
            skipped.append((pid, why))
            continue
        chosen.setdefault(pid.split("@", 1)[1], repo)
        keep.append(pid)
    return keep, skipped


def _profile_section(name, config_dir, default, label, plugins, skipped):
    lines = [f"# 略過 {pid}：{why}（sampan 只管 GitHub owner/repo 或 git 網址的 marketplace）" for pid, why in skipped]
    lines.append(f"[profiles.{_k(name)}]")
    lines.append(f"config_dir = {_s(config_dir)}")
    if default:
        lines.append("default = true")
    lines.append(f"label = {_s(label)}")
    lines.append(f"plugins = {_array(plugins)}")
    return "\n".join(lines) + "\n"


def render_config(detected, *, codex_enabled, codex_home, recent_days=7, home=None):
    chosen, sections = {}, []
    for d in sorted(detected, key=lambda d: not d.default):
        keep, skipped = _select(d, chosen)
        sections.append(_profile_section(d.name, _disp(d.config_dir, home), d.default, d.email or "", keep, skipped))
    head = [
        "# sampan 設定檔（sampan init 產生）。可以手改；再跑 sampan init 只會提議新增的 profile 與 plugin。",
        "version = 1",
        "",
        "[restore]",
        f"recent_days = {recent_days}          # sampan restore 只接回最近幾天有動過的 session",
        "",
        "[codex]",
        f"enabled = {'true' if codex_enabled else 'false'}",
        f"home = {_s(_disp(codex_home, home))}",
        "",
        "[marketplaces]",
        *(f"{_k(n)} = {_s(r)}" for n, r in chosen.items()),
        "",
    ]
    return "\n".join(head) + "\n" + "\n".join(sections)


# ---------- text-level additions to an existing config file (existing content untouched) ----------

_MARKETPLACES_RE = re.compile(r"^[ \t]*\[[ \t]*marketplaces[ \t]*\][ \t]*(#.*)?$", re.M)


def _profile_header(name):
    q = re.escape(name)
    return re.compile(rf"""^[ \t]*\[[ \t]*profiles[ \t]*\.[ \t]*(?:{q}|"{q}"|'{q}')[ \t]*\][ \t]*(#.*)?$""", re.M)


def _line_end(text, pos):
    nl = text.find("\n", pos)
    return len(text) if nl == -1 else nl + 1


def _array_close(text, open_i):
    """Index of the ']' closing the array opened at open_i, and of the last significant char before it."""
    i, depth, last = open_i + 1, 1, open_i
    while i < len(text):
        c = text[i]
        if c == "#":
            i = _line_end(text, i)
            continue
        if c in "\"'":
            j = i + 1
            while j < len(text) and text[j] != c:
                j += 2 if c == '"' and text[j] == "\\" else 1
            last, i = j, j + 1
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return i, last
        if not c.isspace():
            last = i
        i += 1
    return None, None


def _append_plugins(text, name, pids):
    m = _profile_header(name).search(text)
    if m is None:
        return None
    start = _line_end(text, m.end())
    end = next((start + mm.start() for mm in re.finditer(r"^[ \t]*\[", text[start:], re.M)), len(text))
    pm = re.compile(r"^[ \t]*plugins[ \t]*=[ \t]*", re.M).search(text, start, end)
    if pm is None:
        prefix = text[:start] if text[:start].endswith("\n") else text[:start] + "\n"
        return prefix + f"plugins = [{', '.join(_s(p) for p in pids)}]\n" + text[start:]
    open_i = pm.end()
    if text[open_i:open_i + 1] != "[":
        return None
    close_i, last = _array_close(text, open_i)
    if close_i is None:
        return None
    if last == open_i:
        return text[:open_i + 1] + ", ".join(_s(p) for p in pids) + text[open_i + 1:]
    comma = "" if text[last] == "," else ","
    nl = text.rfind("\n", last, close_i)
    if nl != -1:
        items = "".join(f"\n  {_s(p)}," for p in pids)
        return text[:last + 1] + comma + text[last + 1:nl] + items + text[nl:]
    return text[:last + 1] + comma + " " + ", ".join(_s(p) for p in pids) + text[last + 1:]


def _add_marketplaces(text, new):
    entries = "".join(f"{_k(n)} = {_s(r)}\n" for n, r in new.items())
    m = _MARKETPLACES_RE.search(text)
    if m is None:
        return text.rstrip("\n") + "\n\n[marketplaces]\n" + entries
    pos = _line_end(text, m.end())
    prefix = text[:pos] if text[:pos].endswith("\n") else text[:pos] + "\n"
    return prefix + entries + text[pos:]


# ---------- interaction ----------

def _answer(ask, prompt):
    try:
        return (ask(prompt) or "").strip()
    except EOFError:
        return ""


def _yes(ask, prompt, default):
    ans = _answer(ask, prompt).lower()
    return default if not ans else ans in ("y", "yes", "是")


def _unique(name, taken):
    if name not in taken:
        return name
    n = 2
    while f"{name}-{n}" in taken:
        n += 1
    return f"{name}-{n}"


def _ask_name(d, ask, out, taken, home):
    suggestion = _unique(d.name, taken)
    while True:
        name = _answer(ask, f"profile {_disp(d.config_dir, home)} 的名稱 [{suggestion}]：") or suggestion
        if not NAME_RE.match(name):
            out(f"❌ {NAME_RULE}")
        elif name in taken:
            out(f"❌ 名稱 {name} 已經用過")
        else:
            taken.add(name)
            return replace(d, name=name)


def _propose_additions(existing, text, detected, ask, out, home):
    """Ask about each new profile/plugin; return the new text (== text when nothing accepted), or None."""
    expected = copy.deepcopy(tomllib.loads(text))
    chosen = dict(existing.marketplaces)
    by_dir = {_key(p.config_dir): p for p in existing.profiles.values()}
    taken = set(existing.profiles)
    plugin_adds, new_mk, new_profiles = {}, {}, []
    for d in detected:
        p = by_dir.get(_key(d.config_dir))
        if p is not None:
            for pid in d.plugins:
                if pid in p.plugins:
                    continue
                repo, why = _source(pid, d.marketplaces, chosen)
                if why:
                    out(f"⚠ {p.name}：略過新安裝的 {pid}（{why}）")
                    continue
                if _yes(ask, f"在 {p.name} 新增 plugin {pid}？[Y/n] ", True):
                    plugin_adds.setdefault(p.name, []).append(pid)
                    m = pid.split("@", 1)[1]
                    if m not in chosen:
                        chosen[m] = new_mk[m] = repo
            continue
        if d.default:
            out(f"⚠ {_disp(d.config_dir, home)} 不在設定裡，但設定已有預設 profile "
                f"{existing.default_profile.name}；略過")
            continue
        trial = dict(chosen)
        keep, skipped = _select(d, trial)
        for pid, why in skipped:
            out(f"⚠ {_disp(d.config_dir, home)}：略過 {pid}（{why}）")
        if not _yes(ask, f"新增 profile {_disp(d.config_dir, home)}（{len(keep)} 個 plugin）？[Y/n] ", True):
            continue
        d = _ask_name(d, ask, out, taken, home)
        new_mk.update({m: r for m, r in trial.items() if m not in chosen})
        chosen = trial
        new_profiles.append((d, keep, skipped))

    new = text
    for name, pids in plugin_adds.items():
        new = _append_plugins(new, name, pids) if new is not None else None
        expected["profiles"][name].setdefault("plugins", []).extend(pids)
    if new is not None and new_mk:
        new = _add_marketplaces(new, new_mk)
        expected.setdefault("marketplaces", {}).update(new_mk)
    for d, keep, skipped in new_profiles:
        if new is None:
            break
        disp = _disp(d.config_dir, home)
        new = new.rstrip("\n") + "\n\n" + _profile_section(d.name, disp, False, d.email or "", keep, skipped)
        expected["profiles"][d.name] = {"config_dir": disp, "label": d.email or "", "plugins": keep}
    try:
        ok = new is not None and tomllib.loads(new) == expected
    except tomllib.TOMLDecodeError:
        ok = False
    if not ok:
        out("❌ 無法自動補進設定檔（格式和預期不同），請手動加入：")
        for name, pids in plugin_adds.items():
            out(f"   [profiles.{name}] plugins 加上 {', '.join(pids)}")
        for m, r in new_mk.items():
            out(f"   [marketplaces] {m} = \"{r}\"")
        for d, _keep, _skipped in new_profiles:
            out(f"   [profiles.{d.name}] config_dir = \"{_disp(d.config_dir, home)}\"")
        return None
    return new


def _offer_hooks(cfg, ask, out, sampan_path):
    present = [p for p in cfg.profiles.values() if Path(p.config_dir).is_dir()]
    states = {p.name: hook_status(p) for p in present}
    for p in present:
        if states[p.name] == "stale" and _yes(
                ask, f"{p.name} 的 SessionStart hook 指向已不存在的 sampan，要修復嗎？[Y/n] ", True):
            ok = uninstall_hook(p) and install_hook(p, sampan_path)
            out(f"✔ {p.name}：hook 已修復" if ok else f"⚠ {p.name}：hook 修復失敗（settings.json 讀不到或寫不進去）")
    absent = [p for p in present if states[p.name] == "absent"]
    if not absent:
        return
    out("SessionStart hook（選用）：每個 claude 啟動時回報它在哪段對話，save 不用靠推測；"
        "會加進各 profile 的 settings.json（先備份）。沒有也能運作。")
    if not _yes(ask, f"為 {', '.join(p.name for p in absent)} 安裝 SessionStart hook？[y/N] ", False):
        return
    for p in absent:
        if install_hook(p, sampan_path):
            out(f"✔ {p.name}：hook 已安裝")
        else:
            out(f"⚠ {p.name}：settings.json 讀不到、格式不對或寫不進去，略過")


def sampan_env(env):
    """SAMPAN_CONFIG/SAMPAN_STATE_DIR in effect, as absolute paths (launchd does not expand ~), for the plist."""
    return {k: str(Path(v).expanduser().absolute()) for k, v in paths.effective_env(env).items()}


def _offer_launchd(run, out, ask, *, home, state_dir, sampan_path, first, uid, env):
    state, program = launchd.check(home, sampan_path, run, uid=uid, env=env)
    if state == "ok":
        out("✔ 自動存檔排程已安裝並已載入（每個整點 sampan save --quiet）")
        return
    if first and state == "missing":
        prompt = f"安裝自動存檔排程（每個整點 sampan save --quiet，{launchd.plist_path(home)}）？[Y/n] "
    else:
        shown = "、".join(f"{k}={v}" for k, v in env.items()) or "沒有 SAMPAN_CONFIG／SAMPAN_STATE_DIR"
        why = {"missing": "沒有安裝", "unreadable": "的 plist 讀不到",
               "wrong": f"呼叫的是 {program}，不是目前的 sampan",
               "env": f"的 SAMPAN_CONFIG／SAMPAN_STATE_DIR 與目前不同（目前：{shown}）",
               "schedule": "是舊版的計時方式（睡眠時會暫停；新版改成每個整點，醒來時補跑）",
               "not_loaded": "的 plist 正確，但 launchd 沒有載入"}[state]
        prompt = f"自動存檔排程{why}，要重新安裝並載入嗎？[Y/n] "
    if not _yes(ask, prompt, True):
        out("略過自動存檔排程（sampan doctor 會提醒）")
        return
    try:
        launchd.write_plist(sampan_path, state_dir, home, env)
    except OSError as e:
        out(f"⚠ 寫不進 {launchd.plist_path(home)}：{e}；自動存檔排程沒有安裝")
        return
    if launchd.load(run, home, uid=uid):
        out(f"✔ 已安裝自動存檔排程（每個整點 sampan save --quiet，{launchd.plist_path(home)}）")
    else:
        out("⚠ 自動存檔排程沒有載入成功（launchctl bootstrap 失敗）；可稍後再跑 sampan init 重新載入，"
            "或用 sampan doctor 檢查")


def _path_hint(out, home, env):
    parts = env.get("PATH", "").split(":")
    if str(Path(home) / ".local" / "bin") not in parts and "~/.local/bin" not in parts:
        out("⚠ ~/.local/bin 不在 PATH（uv tool install 把 sampan 裝在那裡）；在 shell 設定檔（如 ~/.zshrc）加上：")
        out(f"   {PATH_LINE}")


def cmd_init(cfg_path, state_dir, run, out, ask, *, home, sampan_path, env=None, which=shutil.which,
             procs=None, proc_root=PROC_ROOT, uid=None, no_launchd=False, deps=None, platform=sys.platform):
    home, cfg_path = Path(home), Path(cfg_path)
    env = os.environ if env is None else env
    deps = deps or (lambda out, ask: ensure_deps(out, ask, which=which, env=env, home=home, platform=platform))
    if not deps(out, ask):
        return 1
    first = not cfg_path.exists()
    existing = None
    if not first:
        try:
            existing = load_config(cfg_path, home=home, state_dir=state_dir)
        except ConfigError as e:
            out(f"❌ {e}")
            out("先修好設定檔再執行 sampan init（init 不會改動讀不懂的設定檔）")
            return 2
    procs = list_procs(run) if procs is None else procs
    detected = detect_profiles(home, run, procs, env=env, proc_root=proc_root)

    if first:
        if not any(d.default for d in detected):
            out(f"❌ 找不到 {home / '.claude'}：先執行一次 claude 並登入，再跑 sampan init")
            return 1
        taken = set()
        detected = [_ask_name(d, ask, out, taken, home) for d in detected]
        codex_enabled, codex_home = detect_codex(home, env, which)
        text = render_config(detected, codex_enabled=codex_enabled, codex_home=codex_home, home=home)
        out(f"▶ 將寫入 {cfg_path}：")
        out(text)
        if not no_launchd and platform == "darwin":
            out(f"▶ 接著會問要不要安裝自動存檔排程 {launchd.plist_path(home)}"
                "（每個整點 sampan save --quiet；sampan init --no-launchd 可略過）")
        if not _yes(ask, f"寫入 {cfg_path}？[Y/n] ", True):
            out("未寫入，結束")
            return 1
        atomic_write(cfg_path, text)
        out(f"✔ 已寫入 {cfg_path}")
    else:
        old = cfg_path.read_text()
        new = _propose_additions(existing, old, detected, ask, out, home)
        if new is None:
            return 2
        if new == old:
            out(f"✔ 設定檔 {cfg_path} 沒有要新增的項目")
        else:
            out(f"▶ {cfg_path} 的變更：")
            for line in difflib.unified_diff(old.splitlines(), new.splitlines(), str(cfg_path), "（新增後）",
                                             lineterm=""):
                out(line)
            if not _yes(ask, f"寫入 {cfg_path}？[Y/n] ", True):
                out("未寫入，結束")
                return 1
            atomic_write(cfg_path, new)
            out(f"✔ 已更新 {cfg_path}")

    try:
        cfg = load_config(cfg_path, home=home, state_dir=state_dir)
    except ConfigError as e:
        out(f"❌ {e}")
        return 2
    _offer_hooks(cfg, ask, out, sampan_path)
    if platform != "darwin":
        out(NO_SCHEDULE_NOTE)
    elif no_launchd:
        out("略過自動存檔排程（--no-launchd）")
    else:
        _offer_launchd(run, out, ask, home=home, state_dir=state_dir, sampan_path=sampan_path, first=first, uid=uid,
                       env=sampan_env(env))
    _path_hint(out, home, env)
    return 0


# ---------- profile add ----------

def _login_command(run, base):
    """`claude auth login` if `claude auth --help` lists it (read-only check), else None."""
    r = run(["claude", "auth", "--help"], env=dict(base))
    if r.returncode == 0 and re.search(r"^\s*login\b", r.stdout or "", re.M):
        return ["claude", "auth", "login"]
    return None


def cmd_profile_add(cfg, run, out, name, *, dir, home, env=None, interactive=None):
    home = Path(home)
    base = os.environ if env is None else env
    interactive = interactive or (lambda args, env: subprocess.call(args, env=env))
    if not NAME_RE.match(name):
        out(f"❌ profile {NAME_RULE}")
        return 1
    if name in cfg.profiles:
        out(f"❌ 已經有 profile {name}")
        return 1
    if dir is None:
        target = home / ".claude-profiles" / name
        if target.exists():
            out(f"❌ {target} 已存在；要管理這個既有目錄：sampan profile add {name} --dir {shlex.quote(str(target))}")
            return 1
    else:
        target = Path(dir).expanduser().absolute()
        if not target.is_dir():
            out(f"❌ 目錄 {target} 不存在（--dir 只接受既有的目錄，不會建立）")
            return 1
    known = {_key(p.config_dir): p.name for p in cfg.profiles.values()}
    if _key(target) in known:
        out(f"❌ {target} 已經是 profile {known[_key(target)]}")
        return 1

    need_login = False
    if dir is None:
        target.mkdir(parents=True)
        out(f"▶ 已建立 {target}")
        login = _login_command(run, base)
        if login is None:
            need_login = True
        else:
            out(f"▶ 登入新的 profile：{shlex.join(login)}")
            if interactive(login, _profile_env(target, False, base)) != 0:
                out("⚠ 登入沒有完成")
                need_login = True

    disp = _disp(target, home)
    text = cfg.path.read_text()
    new = text.rstrip("\n") + "\n\n" + _profile_section(name, disp, False, _email(run, target, False, base) or "", [], [])
    try:
        ok = name in tomllib.loads(new).get("profiles", {})
    except tomllib.TOMLDecodeError:
        ok = False
    if not ok:
        out(f"❌ 無法自動加進 {cfg.path}，請手動加入：[profiles.{name}] config_dir = \"{disp}\"")
        return 1
    atomic_write(cfg.path, new)
    out(f"✔ 已把 profile {name}（{disp}）寫進 {cfg.path}")
    if need_login:
        out(f"⚠ 還沒登入：執行 sampan claude {name}，在裡面輸入 /login")
    out(f"plugin：在設定檔 [profiles.{name}] 的 plugins 加上要的項目，再執行 sampan plugins sync --profile {name}")
    return 0
