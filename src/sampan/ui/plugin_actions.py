"""Question flows of the plugin screen, shared by curses and --plain: they talk only through out(line) and ask(prompt) -> str | None.
Also the --plain plugin menu (plain_plugins)."""
import re

from ..config import SOURCE_RULE, ConfigError, load_config, strip_credentials, valid_source
from ..plugin_apply import apply_changes, check_latest, restart_hint, restart_profiles, running_counts, update_plugins
from ..plugin_view import _mkt, build_rows, catalog, plan_changes, preview_lines, profile_choices, snapshot
from ..status import status_rows
from ..textwidth import pad
from .actions import _ask
from .plugin_state import ProfileToggles

PID_RE = re.compile(r"^[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+$")
MENU = "編號＝選 profile　u 編號＝更新　U 全部更新　a 新增　r 檢查新版　q 返回: "
TOGGLE = "輸入編號切換（可多個），s 儲存，q 返回: "
MAX_HITS = 30


def reload_config(cfg, out=None):
    """The config as it is on disk now (a save rewrites the plugin lists); the old one, with a warning, when it can't be read."""
    try:
        return load_config(cfg.path, home=cfg.home, state_dir=cfg.state_dir)
    except (ConfigError, OSError, ValueError) as e:
        if out is not None:
            out(f"⚠ 設定檔讀取失敗（{e}），沿用目前內容")
        return cfg


def _usable(src):
    return bool(src) and valid_source(src) and strip_credentials(src) == src


def save_flow(cfg, run, out, ask, pid, choices, checked, *, new_source=None):
    """Preview the plan, confirm, apply. Returns the profiles that changed (need a restart)."""
    snap = snapshot(cfg)
    changes = plan_changes(snap, choices, checked, _mkt(pid))
    if not changes:
        out("沒有要變更的")
        return []
    for line in preview_lines(cfg, pid, changes, new_source):
        out(line)
    if (_ask(ask, "確定？(y/N): ") or "").lower() != "y":
        out("已取消")
        return []
    return apply_changes(cfg, run, out, pid, changes, new_source=new_source).changed


def update_flow(cfg, run, out, ask, pids):
    """pids=None updates everything after a confirmation; a list of plugins runs without asking."""
    if pids is None and (_ask(ask, "全部 plugin、全部 profile 都更新？(y/N): ") or "").lower() != "y":
        out("已取消")
        return []
    return update_plugins(cfg, run, out, pids)


def add_flow(cfg, out, ask, snap, choose_fn):
    """Pick (choose_fn) or type a plugin id and find its marketplace source. Returns (pid, new_source) or None (cancelled / invalid).
    new_source is set only when the marketplace is not in [marketplaces] yet (it is added on save)."""
    pid = choose_fn(catalog(cfg, snap))
    if pid is None:
        return None
    if pid == "":
        pid = _ask(ask, "plugin@marketplace: ")
        if not pid:
            return None
    if not PID_RE.match(pid):
        out("❌ 要寫成 plugin@marketplace")
        return None
    mkt = _mkt(pid)
    if cfg.marketplaces.get(mkt):
        return pid, None
    known = next((st.marketplaces[mkt] for name, st in snap.states.items()
                  if name not in snap.missing and _usable(st.marketplaces.get(mkt))), None)
    if known:
        return pid, known
    src = _ask(ask, f"{mkt} 的來源（owner/repo 或 git 網址）: ")
    if not _usable(src):
        out(f"❌ 來源要寫成 {SOURCE_RULE}")
        return None
    return pid, src


def restart_offer(cfg, run, out, ask, changed, getkey, *, own_pane=None, on_prompt=None, rows_fn=status_rows, **fns):
    """Offer to restart the running sessions of the changed profiles; only the R key restarts.
    fns: restart_fn (default restart_profiles) and anything else it takes (stop_fn, restore_fn)."""
    restart_fn = fns.pop("restart_fn", restart_profiles)
    counts = running_counts(rows_fn(cfg, run), changed)
    if not counts:
        out("（這些 profile 目前沒有執行中的 session）")
        return 0
    hint = restart_hint(counts)
    out(hint)
    if getkey(hint) == "R":
        return restart_fn(cfg, run, out, ask, list(counts), own_pane=own_pane, on_prompt=on_prompt, **fns)
    for p in counts:
        out(f"稍後要生效：sampan stop --profile {p} 後 sampan restore")
    return 0


def _plain_choose(out, ask):
    """choose_fn for add_flow in --plain: search the catalog by substring and pick a number."""
    def choose(entries):
        q = _ask(ask, "搜尋文字（Enter 自己輸入 plugin@marketplace）: ")
        if q is None or q == "":
            return q
        hits = [e for e in entries if q.lower() in e.pid.lower() or q.lower() in e.description.lower()][:MAX_HITS]
        if not hits:
            out(f"沒有符合「{q}」的 plugin")
            return None
        for i, e in enumerate(hits, 1):
            out(f"  [{i:>2}] {e.pid}" + (f"  {e.description}" if e.description else ""))
        raw = _ask(ask, "選哪一個？(編號，Enter 取消): ")
        if not raw:
            return None
        if not raw.isdigit() or not 1 <= int(raw) <= len(hits):
            out(f"無效編號：{raw}")
            return None
        return hits[int(raw) - 1].pid
    return choose


def _toggle_loop(cfg, run, out, ask, pid, source, checked=None, *, new_source=None, own_pane=None, on_prompt=None,
                 **fns):
    """Layer 2 in --plain: toggle profiles by number, s saves (then offers the restart), q returns."""
    choices = profile_choices(cfg, snapshot(cfg), pid, source)
    t = ProfileToggles(choices, checked)
    while True:
        out(f"── {pid} ──")
        for j, c in enumerate(choices, 1):
            out(f"  [{j}] {t.mark(c)} {c.profile}  {c.installed[:12] if c.installed else '—'}  {t.note(c) or ''}")
        raw = _ask(ask, TOGGLE)
        if raw == "s":
            changed = save_flow(cfg, run, out, ask, pid, choices, t.checked, new_source=new_source)
            if changed:
                restart_offer(cfg, run, out, ask, changed, lambda p: _ask(ask, p + "（輸入 R）: "),
                              own_pane=own_pane, on_prompt=on_prompt, **fns)
            return
        if raw is None or raw in ("", "q"):
            if raw is None or not t.dirty() or (_ask(ask, "放棄變更？(y/N): ") or "").lower() == "y":
                return
            continue
        for tok in raw.split():
            if not tok.isdigit() or not 1 <= int(tok) <= len(choices):
                out(f"無效編號：{tok}")
                continue
            c = choices[int(tok) - 1]
            if c.disabled:
                out(f"{c.profile} 不能改：{c.disabled}")
            else:
                t.checked[c.profile] = not t.checked[c.profile]


def plain_plugins(cfg, run, out, ask, *, own_pane=None, on_prompt=None, **fns):
    """The --plain plugin menu. fns are passed to restart_offer (rows_fn, restart_fn…). Returns 0."""
    latest = None
    while True:
        cfg = reload_config(cfg, out)
        rows = build_rows(cfg, snapshot(cfg), latest)
        out("═══ sampan · plugin 管理 ═══")
        if not rows:
            out("  目前沒有任何 plugin")
        for i, r in enumerate(rows, 1):
            out(f"  [{i:>2}] {pad(r.name, 28)} {len(r.installed)}/{r.total}  {r.state}")
        raw = _ask(ask, MENU)
        if raw is None or raw in ("", "q"):
            return 0
        restart = lambda changed: changed and restart_offer(  # noqa: E731
            cfg, run, out, ask, changed, lambda p: _ask(ask, p + "（輸入 R）: "),
            own_pane=own_pane, on_prompt=on_prompt, **fns)
        if raw.isdigit():
            if not 1 <= int(raw) <= len(rows):
                out(f"無效編號：{raw}")
                continue
            r = rows[int(raw) - 1]
            _toggle_loop(cfg, run, out, ask, r.pid, r.source, own_pane=own_pane, on_prompt=on_prompt, **fns)
        elif raw == "U":
            restart(update_flow(cfg, run, out, ask, None))
        elif raw.startswith("u") and raw[1:].strip().isdigit() and 1 <= int(raw[1:]) <= len(rows):
            restart(update_flow(cfg, run, out, ask, [rows[int(raw[1:]) - 1].pid]))
        elif raw == "a":
            picked = add_flow(cfg, out, ask, snapshot(cfg), _plain_choose(out, ask))
            if picked:
                pid, new_source = picked
                source = new_source or cfg.marketplaces.get(_mkt(pid))
                # boxes start at the install state: all unchecked for a plugin no profile has yet
                _toggle_loop(cfg, run, out, ask, pid, source,
                             new_source=new_source, own_pane=own_pane, on_prompt=on_prompt, **fns)
        elif raw == "r":
            latest = check_latest(cfg, run, out, snapshot(cfg))
        else:
            out(f"無效選擇：{raw}")
        out("")
