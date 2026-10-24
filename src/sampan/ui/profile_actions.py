"""Flows of the profile (帳號) screen, shared by curses and --plain: they talk only through out(line) and ask(prompt) -> str | None.
Also the main list's account lines (account_lines), the profile card, and the --plain profile menu (plain_profiles)."""
from .. import profile_ops
from .. import statusline_setup as sl
from ..init import _disp
from ..registry import RegistryError
from ..status import State, status_rows
from ..usage import usage_text
from .actions import _ask
from .plugin_actions import reload_config
from .text import pad

TITLE = "sampan · 帳號"
MENU = "編號＝選 profile　a 新增　q 返回: "
ROW_MENU = "r 改名　d 停用／恢復　f forget　i 裝狀態列　q 返回: "
PROFILE_COLS = (6, 10)  # profile screen list: agent, running sessions
LABEL_W = 10  # width of the card's labels (「plugin 數」 is nine cells)


def profile_usage(cfg, p):
    """Usage of one Profile (Claude: the status-line record; Codex: its newest rollout)."""
    return profile_ops._usage(cfg, p)


def _display_name(p):
    return f"{p.name}（Codex）" if p.agent == "codex" else p.name


def account_lines(cfg, rows, now, usage_fn=None):
    """One line per enabled profile, in config order: 「{名稱} {N} 個執行中」 plus 「・{額度}」 when there is one;
    Codex names carry 「（Codex）」. N counts the RUNNING rows of that profile and agent.
    usage_fn(cfg, profile) -> Usage | None (default profile_usage)."""
    usage_fn = usage_fn or profile_usage
    out = []
    for p in cfg.all_profiles.values():
        if p.disabled:
            continue
        n = sum(1 for r in rows if r.state is State.RUNNING and r.profile == p.name and r.agent == p.agent)
        text = usage_text(usage_fn(cfg, p), now)
        out.append(f"{_display_name(p)} {n} 個執行中" + (f"・{text}" if text else ""))
    return out


def statusline_state(p):
    """已接上 / 沒裝 for a Claude profile, — for Codex or an unreadable settings.json."""
    if p.agent != "claude":
        return "—"
    data, _why = sl._read(p)
    if data is None:
        return "—"
    return "已接上" if sl._tokens(sl._command(data) or "") is not None else "沒裝"


def _name_cell(pr):
    return pr.name + ("（停用）" if pr.disabled else "")


def profile_cells(pr):
    """One profile screen row: name (（停用） added), agent, running sessions; a disabled row is dim."""
    style = "muted" if pr.disabled else ""
    return ((_name_cell(pr), style), (pr.agent, style), (f"{pr.running} 個執行中", style))


def card_lines(cfg, pr, now, usage_fn=None):
    """The profile card: name, then 目錄、帳號、label、plugin 數 (Claude)、額度、狀態列 as (label, value) styled lines."""
    p = cfg.all_profiles[pr.name]
    usage_fn = usage_fn or profile_usage
    head = pr.name + ("（停用）" if pr.disabled else "（預設）" if pr.default else "")
    fields = [("目錄", _disp(pr.config_dir, cfg.home)), ("帳號", pr.account), ("label", pr.label or "—")]
    if pr.plugins is not None:
        fields.append(("plugin 數", str(pr.plugins)))
    fields += [("額度", usage_text(usage_fn(cfg, p), now) or "—"), ("狀態列", statusline_state(p))]
    return [head, "─" * 30] + [((pad(k, LABEL_W), "label"), (v, "")) for k, v in fields]


def load_rows(cfg, run, out, rows_fn=status_rows):
    """profile_rows with the sessions as they are now (runs `claude auth status` / `codex login status` per profile)."""
    try:
        rows = rows_fn(cfg, run)
    except RegistryError as e:
        out(f"⚠ {e}；執行中數字不準")
        rows = []
    return profile_ops.profile_rows(cfg, run, rows)


# ---------- the actions (each returns the profile name to put the cursor on, or None) ----------

def rename_flow(cfg, run, out, ask, name):
    new = _ask(ask, f"{name} 的新名稱（Enter 取消）: ")
    if not new:
        out("已取消")
        return None
    return new if profile_ops.cmd_profile_rename(cfg, run, out, name, new) == 0 else None


def toggle_flow(cfg, run, out, ask, name):
    """d: enable a disabled profile; disable an enabled one after a confirmation."""
    p = cfg.all_profiles.get(name)
    if p is not None and p.disabled:
        profile_ops.cmd_profile_enable(cfg, run, out, name)
        return None
    if p is not None and not p.default:
        if (_ask(ask, f"停用 {name}？目錄保留，之後可以按 d 恢復 (y/N): ") or "").lower() != "y":
            out("已取消")
            return None
    profile_ops.cmd_profile_disable(cfg, run, out, name)  # refuses the default profile itself
    return None


def forget_flow(cfg, run, out, ask, name):
    p = cfg.all_profiles.get(name)
    if p is not None and not p.disabled:
        out("❌ 只有停用的 profile 能 forget；先按 d 停用")
        return None
    profile_ops.cmd_profile_forget(cfg, run, out, lambda prompt: _ask(ask, prompt), name)
    return None


def statusline_flow(cfg, out, ask, name, sampan_path=None):
    """i: install the sampan status line for this one Claude profile (statusline_setup.install asks to confirm)."""
    p = cfg.all_profiles.get(name)
    if p is None:
        out(f"❌ 沒有 profile {name}")
        return None
    if p.agent != "claude":
        out("Codex 沒有狀態列；i 只裝 Claude profile 的")
        return None
    if p.disabled:
        out(f"profile {name} 已停用；先按 d 恢復再裝狀態列")
        return None
    if sampan_path is None:
        from ..doctor import current_sampan
        sampan_path = current_sampan()
    sl.install(cfg, out, lambda prompt: _ask(ask, prompt), [p], sampan_path)
    return None


def add_flow(cfg, run, out, ask, interactive=None, *, login_outside=True):
    """a: ask the name and Claude / Codex, then sampan profile add. The login needs the terminal: interactive(args, env)
    runs it (curses steps out first); login_outside=False with no interactive (a curses screen that can't step out)
    only says which command to run after leaving."""
    from ..init import cmd_profile_add
    name = _ask(ask, "新 profile 名稱: ")
    if not name:
        out("已取消")
        return None
    kind = (_ask(ask, "Claude 還是 Codex？(c/x): ") or "").lower()
    if kind not in ("c", "x"):
        out("已取消" if not kind else f"無效選擇：{kind}")
        return None
    codex = kind == "x"
    if interactive is None and not login_outside:
        out(f"新增帳號要登入，請離開後執行：sampan profile add {name}{' --codex' if codex else ''}")
        return None
    kw = {} if interactive is None else {"interactive": interactive}
    rc = cmd_profile_add(cfg, run, out, name, dir=None, home=cfg.home, codex=codex, **kw)
    return name if rc == 0 else None


def profile_action(cfg, run, out, ask, kind, name, *, interactive=None, login_outside=True, sampan_path=None):
    """One action of the profile screen (curses and --plain). Returns the profile to move the cursor to, or None."""
    if kind == "rename":
        return rename_flow(cfg, run, out, ask, name)
    if kind == "toggle":
        return toggle_flow(cfg, run, out, ask, name)
    if kind == "forget":
        return forget_flow(cfg, run, out, ask, name)
    if kind == "statusline":
        return statusline_flow(cfg, out, ask, name, sampan_path)
    if kind == "add":
        return add_flow(cfg, run, out, ask, interactive, login_outside=login_outside)
    raise ValueError(f"未知的 profile action：{kind!r}")


# ---------- --plain ----------

PLAIN_KEYS = {"r": "rename", "d": "toggle", "f": "forget", "i": "statusline"}


def _plain_line(pr, usage):
    parts = [_name_cell(pr), pr.agent, pr.account, f"{pr.running} 個執行中"]
    return "  ".join(parts + ([usage] if usage else []))


def plain_profiles(cfg, run, out, ask, *, rows_fn=status_rows, usage_fn=None, now_fn=None, sampan_path=None,
                   interactive=None):
    """The --plain profile menu: a number picks a profile (its card, then r/d/f/i), a adds, q returns. Returns 0."""
    from datetime import datetime, timezone
    now_fn = now_fn or (lambda: datetime.now(timezone.utc))
    usage_fn = usage_fn or profile_usage
    while True:
        cfg = reload_config(cfg, out)
        now = now_fn()
        prows = load_rows(cfg, run, out, rows_fn)
        out(f"═══ {TITLE} ═══")
        for i, pr in enumerate(prows, 1):
            out(f"  [{i:>2}] {_plain_line(pr, usage_text(usage_fn(cfg, cfg.all_profiles[pr.name]), now))}")
        raw = _ask(ask, MENU)
        if raw is None or raw in ("", "q"):
            return 0
        if raw == "a":
            add_flow(cfg, run, out, ask, interactive)
        elif raw.isdigit() and 1 <= int(raw) <= len(prows):
            pr = prows[int(raw) - 1]
            for line in card_lines(cfg, pr, now, usage_fn)[2:]:
                out("  " + "".join(text for text, _style in line))
            key = _ask(ask, ROW_MENU)
            if key in PLAIN_KEYS:
                profile_action(cfg, run, out, ask, PLAIN_KEYS[key], pr.name, sampan_path=sampan_path)
            elif key not in (None, "", "q"):
                out(f"無效選擇：{key}")
        elif raw.isdigit():
            out(f"無效編號：{raw}")
        else:
            out(f"無效選擇：{raw}")
        out("")
