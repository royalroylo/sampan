import math
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .detach_key import KEY_RE


class ConfigError(Exception):
    pass


HEAL_SUBAGENT_DEFAULT = True  # [save] heal_subagent when the file does not say (the one place the default is set)
SOURCE_RULE = "GitHub owner/repo 或 git 網址（https://…、ssh://…、git@host:path）"
_GITHUB_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+$")
_URL_RE = re.compile(r"^(?:https?|ssh)://[^/\s]+/\S+$|^[\w.-]+@[\w.-]+:\S+$")


_HTTP_USERINFO_RE = re.compile(r"^(https?://)[^/@\s]+@")
_ANY_USERINFO_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@")


def strip_credentials(src):
    """Strip user:token@ from http(s) URLs (Claude stores it in known_marketplaces.json) so it never reaches the config file or the screen."""
    return _HTTP_USERINFO_RE.sub(r"\1", src) if isinstance(src, str) else src


def redact(text):
    """Text with the user/token part of every scheme://user:token@ URL in it replaced by ***, for showing command errors."""
    return _ANY_USERINFO_RE.sub(r"\1***@", text)


def valid_source(src):
    """Whether a marketplace source is one sampan can manage: GitHub owner/repo, or a git URL."""
    return isinstance(src, str) and bool(_GITHUB_RE.match(src) or _URL_RE.match(src))


def usable_source(src):
    """Whether sampan may pass src to claude and write it to the config: a valid source without credentials."""
    return valid_source(src) and strip_credentials(src) == src


def source_key(src):
    """Normalised source used for comparison."""
    s = strip_credentials(src.strip()).rstrip("/").lower()
    s = s[:-4] if s.endswith(".git") else s
    for prefix in ("https://github.com/", "http://github.com/", "ssh://git@github.com/", "git@github.com:"):
        if s.startswith(prefix):
            return s[len(prefix):]
    return s


OFFICIAL_MARKETPLACE = ("claude-plugins-official", "anthropics/claude-plugins-official")


def claude_added(name, src):
    """Claude's own official marketplace, which Claude adds by itself: left alone when the config does not list it."""
    return name == OFFICIAL_MARKETPLACE[0] and same_source(src, OFFICIAL_MARKETPLACE[1])


def same_source(a, b):
    """Whether two marketplace sources point to the same repo (a github.com URL equals owner/repo; .git and case are ignored)."""
    return valid_source(a) and valid_source(b) and source_key(a) == source_key(b)


@dataclass(frozen=True)
class Profile:
    name: str
    config_dir: Path
    label: str
    plugins: tuple
    default: bool  # which profile `sampan new` and the pickers choose when none is named; nothing else
    claude_json: Path
    agent: str = "claude"  # "claude" or "codex"; for Codex, config_dir is CODEX_HOME
    disabled: bool = False
    uses_home: bool = False  # config_dir is ~/.claude: Claude runs without CLAUDE_CONFIG_DIR, its .claude.json is ~/.claude.json

    @property
    def projects_dir(self):
        return self.config_dir / "projects"

    def env(self, base):
        env = dict(base)
        if self.uses_home:
            env.pop("CLAUDE_CONFIG_DIR", None)
        else:
            env["CLAUDE_CONFIG_DIR"] = str(self.config_dir)
        return env


@dataclass(frozen=True)
class CodexConfig:
    enabled: bool
    home: Path
    profile: str = ""  # name of the Codex profile in effect; "" when there is none


LIMIT_KEYS = ("max_sessions", "count_mode", "pressure_guard", "swapout_per_min", "reserve_gb", "per_session_gb",
              "min_auto", "red_floor", "reserve_per_owner", "protect")


@dataclass(frozen=True)
class Limits:
    """[limits]: how many agents may run at once, and when a memory red light stops new ones (limits.py, resources.py).
    Absent section = these defaults, in force."""
    max_sessions: object = "auto"  # "auto", an integer, or 0 (no count limit)
    count_mode: str = "warn"  # "warn": over the limit only warns; "block": it refuses too
    pressure_guard: bool = True  # False: a memory red light is shown but never refuses
    swapout_per_min: int = 1000  # Swapouts per minute above which memory is "being written out"
    reserve_gb: float = 8  # constant of the "auto" formula (not the real system use)
    per_session_gb: float = 0.5  # constant of the "auto" formula
    min_auto: int = 4  # lower bound of "auto"
    red_floor: int = 4  # without [limits.owners]: agents still allowed in total under a memory red light
    reserve_per_owner: int = 3  # per owner in [limits.owners]
    protect: tuple = ("*-pm",)  # name patterns never offered as a session to stop
    owners: tuple = ()  # ((owner, (profile, ...)), ...)
    explicit: bool = False  # a [limits] section was written
    unknown: tuple = ()  # keys of [limits] this sampan does not know (ignored; the doctor says so)

    def owner_of(self, profile):
        """The owner a profile belongs to; "" (the others) when it is in no list."""
        for name, profiles in self.owners:
            if profile in profiles:
                return name
        return ""


@dataclass(frozen=True)
class Config:
    version: int
    marketplaces: dict
    profiles: dict  # enabled Claude profiles only
    codex: CodexConfig
    recent_days: int
    start_dir_setting: Path | None  # [ui] start_dir as written, None when absent
    home: Path
    state_dir: Path
    path: Path
    all_profiles: dict  # every profile in config-file order, disabled and Codex ones included
    implicit_codex: bool  # the Codex profile was filled in from [codex] / defaults, not written in the file
    codex_auto_start: bool = True  # [ui] codex_auto_start: start the Codex daemon when the UI opens
    detach_key: str = "C-q"  # [ui] detach_key: the tmux key that jumps out of a sampan session ("" = none)
    keep_days: int = 90  # [dispatches] keep_days: a finished message older than this loses its text and reply (the row stays)
    mcp_confirm: bool = True  # [mcp] confirm: the MCP tools that act are marked, so the agent's own permission prompt asks the user
    limits: Limits = Limits()
    resource_bar: bool = True  # [ui] resource_bar: the memory / load / count line at the bottom right of the main list
    heal_subagent: bool = HEAL_SUBAGENT_DEFAULT  # [save] heal_subagent: `sampan save` puts a Codex conversation registered as a review subagent back on its parent

    @property
    def codex_profiles(self):
        """Enabled Codex profiles, in config-file order."""
        return {n: p for n, p in self.all_profiles.items() if p.agent == "codex" and not p.disabled}

    def owner(self, agent, profile):
        """The profile (disabled ones included) a registry row of agent/profile belongs to, or None. An old Codex row
        has profile "": it belongs to the Codex profile when there is only one."""
        if agent == "codex" and not profile:
            codex = [p for p in self.all_profiles.values() if p.agent == "codex"]
            return codex[0] if len(codex) == 1 else None
        p = self.all_profiles.get(profile)
        return p if p is not None and p.agent == agent else None

    @property
    def start_dir(self):
        """Where the `n` directory picker starts: [ui] start_dir; by default ~/Projects if it exists, else ~."""
        if self.start_dir_setting is not None:
            return self.start_dir_setting
        projects = self.home / "Projects"
        return projects if projects.is_dir() else self.home

    @property
    def default_profile(self):
        """The profile chosen when none is named (`default = true`)."""
        return next(p for p in self.profiles.values() if p.default)

    @property
    def home_profile(self):
        """The enabled Claude profile whose config_dir is ~/.claude (what a claude with no CLAUDE_CONFIG_DIR uses), or None."""
        return next((p for p in self.profiles.values() if p.uses_home), None)

    @property
    def registry_path(self):
        return self.state_dir / "registry.json"

    @property
    def history_dir(self):
        return self.state_dir / "history"

    @property
    def save_log(self):
        return self.state_dir / "save.log"

    @property
    def hooks_dir(self):
        return self.state_dir / "hooks"


def load_config(path, *, home=None, state_dir=None):
    path = Path(path)
    home = Path(home) if home else Path.home()

    def expand(p):
        return home / p[2:] if p.startswith("~/") else Path(p)

    if not path.exists():
        raise ConfigError(f"找不到設定檔 {path}；先執行 sampan init")
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path} 讀取失敗：{e}") from e

    if data.get("version") != 1:
        raise ConfigError(f"{path}：version 要是 1，目前是 {data.get('version')!r}")

    def table(value, what):
        if not isinstance(value, dict):
            raise ConfigError(f"{what} 要是 table，目前是 {value!r}")
        return value

    marketplaces = dict(table(data.get("marketplaces", {}), "[marketplaces]"))
    for name, src in marketplaces.items():
        if not valid_source(src):
            raise ConfigError(f"marketplace {name}：來源要寫成 {SOURCE_RULE}，目前是 {src!r}")
        if strip_credentials(src) != src:
            raise ConfigError(f"marketplace {name}：網址裡有帳號或 token，請改成 {strip_credentials(src)}"
                              "（認證交給 git 的 credential helper，不要寫在設定檔）")

    all_profiles = {}
    for name, p in table(data.get("profiles", {}), "[profiles]").items():
        p = table(p, f"[profiles.{name}]")
        if not isinstance(p.get("config_dir"), str) or not p["config_dir"]:
            raise ConfigError(f"profile {name}：缺少 config_dir（要寫成字串路徑）")
        agent = p.get("agent", "claude")
        if agent not in ("claude", "codex"):
            raise ConfigError(f"profile {name}：agent 要是 \"claude\" 或 \"codex\"，目前是 {agent!r}")
        disabled = p.get("disabled", False)
        if not isinstance(disabled, bool):
            raise ConfigError(f"profile {name}：disabled 要是 true 或 false，目前是 {disabled!r}")
        config_dir = expand(p["config_dir"])
        label = p.get("label", "")
        if agent == "codex":
            for key in ("default", "plugins"):
                if key in p:
                    raise ConfigError(f"profile {name}：Codex profile 不能設 {key}")
            all_profiles[name] = Profile(name, config_dir, label, (), False, config_dir / ".claude.json", "codex", disabled)
            continue
        raw_plugins = p.get("plugins", [])
        if not isinstance(raw_plugins, list) or not all(isinstance(x, str) for x in raw_plugins):
            raise ConfigError(f"profile {name}：plugins 要是字串清單")
        plugins = tuple(raw_plugins)
        for pid in plugins:
            if "@" not in pid or pid.split("@", 1)[1] not in marketplaces:
                raise ConfigError(f"profile {name}：{pid} 的 marketplace 不在 [marketplaces]")
        default = bool(p.get("default", False))
        if default and disabled:
            raise ConfigError(f"profile {name}：default 的 profile 不能停用（先把別的 profile 設成 default）")
        # Whether Claude runs without CLAUDE_CONFIG_DIR depends on where the profile lives, not on which one is the
        # default: only ~/.claude is what Claude uses when the variable is unset (and then its login state is
        # ~/.claude.json, not ~/.claude/.claude.json)
        uses_home = os.path.realpath(config_dir) == os.path.realpath(home / ".claude")
        claude_json = home / ".claude.json" if uses_home else config_dir / ".claude.json"
        all_profiles[name] = Profile(name, config_dir, label, plugins, default, claude_json, "claude", disabled,
                                     uses_home)

    if sum(p.default for p in all_profiles.values() if p.agent == "claude") != 1:
        raise ConfigError("Claude 的 profiles 裡要剛好一個設 default = true")

    codex = table(data.get("codex", {}), "[codex]")
    codex_home = codex.get("home") or os.environ.get("CODEX_HOME") or "~/.codex"
    if not isinstance(codex_home, str):
        raise ConfigError(f"[codex] home 要是字串路徑，目前是 {codex_home!r}")
    codex_home = expand(codex_home)
    # Old configs have no Codex profile: fill one in from [codex] (no table at all = on, as before),
    # unless [codex] enabled = false. Once a Codex profile is written in the file, [codex] is ignored.
    implicit_codex = (not any(p.agent == "codex" for p in all_profiles.values())
                      and bool(codex.get("enabled", True)))
    if implicit_codex:
        cname, n = "codex", 0
        while cname in all_profiles:
            n += 1
            cname = f"codex-{n}"
        all_profiles[cname] = Profile(cname, codex_home, "", (), False, codex_home / ".claude.json", "codex")
    profiles = {n: p for n, p in all_profiles.items() if p.agent == "claude" and not p.disabled}
    active_codex = next((p for p in all_profiles.values() if p.agent == "codex" and not p.disabled), None)
    codex_cfg = (CodexConfig(True, active_codex.config_dir, active_codex.name) if active_codex
                 else CodexConfig(False, codex_home))
    restore = table(data.get("restore", {}), "[restore]")
    recent_days = restore.get("recent_days", 7)
    if isinstance(recent_days, bool) or not isinstance(recent_days, int) or recent_days < 0:
        raise ConfigError(f"[restore] recent_days 要是非負整數，目前是 {recent_days!r}")
    save_tbl = table(data.get("save", {}), "[save]")
    heal_subagent = save_tbl.get("heal_subagent", HEAL_SUBAGENT_DEFAULT)
    if not isinstance(heal_subagent, bool):
        raise ConfigError(f"[save] heal_subagent 要是 true 或 false，目前是 {heal_subagent!r}")
    disp = table(data.get("dispatches", {}), "[dispatches]")
    keep_days = disp.get("keep_days", 90)
    if isinstance(keep_days, bool) or not isinstance(keep_days, int) or keep_days < 0:
        raise ConfigError(f"[dispatches] keep_days 要是非負整數，目前是 {keep_days!r}")
    ui = table(data.get("ui", {}), "[ui]")
    start_dir = ui.get("start_dir")
    if start_dir is None:
        start = None
    elif isinstance(start_dir, str) and start_dir:
        start = expand(start_dir)
    else:
        raise ConfigError(f"[ui] start_dir 要是字串路徑，目前是 {start_dir!r}")
    codex_auto_start = ui.get("codex_auto_start", True)
    if not isinstance(codex_auto_start, bool):
        raise ConfigError(f"[ui] codex_auto_start 要是 true 或 false，目前是 {codex_auto_start!r}")
    detach_key = ui.get("detach_key", "C-q")
    if not isinstance(detach_key, str) or (detach_key and not KEY_RE.match(detach_key)):
        raise ConfigError(f"[ui] detach_key 要是 tmux 的按鍵名稱（例如 \"C-q\"，空字串＝不綁），目前是 {detach_key!r}")
    resource_bar = ui.get("resource_bar", True)
    if not isinstance(resource_bar, bool):
        raise ConfigError(f"[ui] resource_bar 要是 true 或 false，目前是 {resource_bar!r}")
    limits = _load_limits(data.get("limits"), table)
    mcp = table(data.get("mcp", {}), "[mcp]")
    mcp_confirm = mcp.get("confirm", True)
    if not isinstance(mcp_confirm, bool):
        raise ConfigError(f"[mcp] confirm 要是 true 或 false，目前是 {mcp_confirm!r}")
    return Config(
        version=1,
        marketplaces=marketplaces,
        profiles=profiles,
        codex=codex_cfg,
        recent_days=recent_days,
        start_dir_setting=start,
        home=home,
        state_dir=Path(state_dir) if state_dir else path.parent / "state",
        path=path,
        all_profiles=all_profiles,
        implicit_codex=implicit_codex,
        codex_auto_start=codex_auto_start,
        detach_key=detach_key,
        keep_days=keep_days,
        mcp_confirm=mcp_confirm,
        limits=limits,
        resource_bar=resource_bar,
        heal_subagent=heal_subagent,
    )


def _load_limits(raw, table):
    if raw is None:
        return Limits()
    raw = dict(table(raw, "[limits]"))
    owners_raw = raw.pop("owners", {})
    if "enforce" in raw:
        raise ConfigError("[limits] enforce 已改名：數量要不要擋用 count_mode = \"warn\" 或 \"block\"，"
                          "記憶體紅燈要不要擋用 pressure_guard = true 或 false")
    if "min_sessions" in raw:
        raise ConfigError("[limits] min_sessions 已拆成 min_auto（auto 的下限）與 red_floor（記憶體紅燈下全機至少可開幾個）")
    unknown = tuple(k for k in raw if k not in LIMIT_KEYS)  # kept, not an error: a newer sampan's key must not make an older one unreadable

    def num(key, kind, default, minimum=0):
        v = raw.get(key, default)
        if (isinstance(v, bool) or not isinstance(v, (int, float) if kind is float else int) or v < minimum
                or (kind is float and not math.isfinite(v))):
            raise ConfigError(f"[limits] {key} 要是{'非負數' if kind is float else '非負整數'}，目前是 {v!r}")
        return v

    mx = raw.get("max_sessions", "auto")
    if mx != "auto" and (isinstance(mx, bool) or not isinstance(mx, int) or mx < 0):
        raise ConfigError(f"[limits] max_sessions 要是 \"auto\" 或非負整數（0＝不限數量），目前是 {mx!r}")
    mode = raw.get("count_mode", "warn")
    if mode not in ("warn", "block"):
        raise ConfigError(f"[limits] count_mode 要是 \"warn\" 或 \"block\"，目前是 {mode!r}")
    guard = raw.get("pressure_guard", True)
    if not isinstance(guard, bool):
        raise ConfigError(f"[limits] pressure_guard 要是 true 或 false，目前是 {guard!r}")
    protect = raw.get("protect", ["*-pm"])
    if not isinstance(protect, list) or not all(isinstance(x, str) for x in protect):
        raise ConfigError("[limits] protect 要是字串清單")
    owners_raw = table(owners_raw, "[limits.owners]")
    owners, seen = [], {}
    for name, profiles in owners_raw.items():
        if not isinstance(profiles, list) or not all(isinstance(x, str) for x in profiles):
            raise ConfigError(f"[limits.owners] {name}：要是 profile 名稱的清單"
                              "（保底數量放在 [limits] 的 reserve_per_owner，不是這裡）")
        for prof in profiles:
            if prof in seen:
                raise ConfigError(f"[limits.owners] profile {prof} 同時在 {seen[prof]} 和 {name}")
            seen[prof] = name
        owners.append((name, tuple(profiles)))
    return Limits(
        max_sessions=mx, count_mode=mode, pressure_guard=guard,
        swapout_per_min=num("swapout_per_min", int, 1000, 1), reserve_gb=num("reserve_gb", float, 8),
        per_session_gb=num("per_session_gb", float, 0.5, 0.01), min_auto=num("min_auto", int, 4, 1),
        red_floor=num("red_floor", int, 4), reserve_per_owner=num("reserve_per_owner", int, 3),
        protect=tuple(protect), owners=tuple(owners), explicit=True, unknown=unknown)
