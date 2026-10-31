"""Action layer of the ui (shared by curses and plain text): the main loop and dispatch of cmd_ui, the question flows of actions such as n/s/m/e/U, the --plain menu."""
import os
from pathlib import Path

from .. import adopt as adopt_mod
from .. import folders as folders_mod
from .. import restart as restart_mod
from .. import restore as restore_mod
from .. import save as save_mod
from .. import send as send_mod
from .. import session_run
from .. import update as update_mod
from ..prompts import recording
from ..registry import RegistryError, load_rows
from ..status import State, status_rows
from ..tmux import TMUX, Tmux
from . import folder_actions
from .dirpick import recent_dirs, sessions_by_dir
from .state import row_text
from .tree import name_matches

HELP = ("編號 = 接上／切過去（已停止的先接回）；s 停掉　S 重開　m 搬目錄　P 換 profile　e 改名　R 接回　A 接手 tmux 外的　r retire　a activate　X 移除"
        "　n 開新 session　w 立即存檔　p plugin 管理　o 帳號　D 派工　/文字 搜尋　q 離開"
        "（資料夾請用 sampan folder …）")
FOLDER_HINT = "--plain 不整理資料夾，請用 sampan folder list／add／rename／rm／move"

UNMANAGED = (State.UNREGISTERED, State.NO_AGENT, State.MULTI)  # tmux sessions sampan does not manage


def _ask(ask, prompt):
    try:
        raw = ask(prompt)
    except (EOFError, KeyboardInterrupt):  # end of input or Ctrl-C both count as cancel
        return None
    return None if raw is None else raw.strip()


def _pick(ask, out, rows, prompt, *, many):
    raw = _ask(ask, prompt)
    if not raw:
        return None
    picked = []
    for tok in raw.split():
        if not tok.isdigit() or not 1 <= int(tok) <= len(rows):
            out(f"無效編號：{tok}")
            return False
        picked.append(rows[int(tok) - 1].name)
    if not many and len(picked) != 1:
        out("這個動作一次只能選一個")
        return False
    return picked


def _show(out, rows, current, prefix=None):
    out("═══ sampan sessions ═══")
    if not rows:
        out("  目前沒有任何 session")
    for i, r in enumerate(rows, 1):
        mark = "  ← 目前所在" if r.name == current else ""
        out(f"  [{i:>2}] {(prefix or {}).get(r.name, '')}{row_text(r)}{mark}")
    out("")
    out(HELP)


def _attach(run, out, name, current, exec_fn):
    if not Tmux(run).has_session(name):
        out(f"{name} 目前沒有在 tmux 裡執行；要接回請用 sampan restore {name}")
        return 1
    if current:
        return 0 if run([*TMUX, "switch-client", "-t", f"={name}"]).returncode == 0 else 1
    exec_fn(TMUX[0], [*TMUX, "attach-session", "-t", f"={name}"])
    return 0


def dir_context(cfg):
    """Data for the right side of the directory chooser: recently used directories and the registered sessions in each directory. Empty when the registry cannot be read."""
    try:
        rows = load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)
    except RegistryError:
        rows = []
    return {"recent": recent_dirs(rows), "sessions": sessions_by_dir(rows)}


def _default_start(cfg):
    """Where the `n` directory picker starts: [ui] start_dir (default ~/Projects if it exists, else ~)."""
    return str(cfg.start_dir if Path(cfg.start_dir).is_dir() else cfg.home)


def _session_cwd(cfg, name):
    try:
        rows = load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)
    except RegistryError:
        rows = []
    cwd = next((r.cwd for r in rows if r.tmux == name), "")
    return cwd if cwd and Path(cwd).is_dir() else _default_start(cfg)


def _new(cfg, run, out, ask, new_fn, choose_dir_fn=None, prompt_kw=None):
    """Returns (rc, name of the opened session or None). When choose_dir_fn is given (curses) use the directory chooser screen, otherwise type the directory.
    prompt_kw ({"on_prompt": …}) is passed to new_fn unchanged."""
    name = _ask(ask, "Session 名稱（不要用 . 或 :）: ")
    if not name:
        return 0, None
    target = "claude"
    if cfg.codex.enabled:
        out("  [1] Claude（預設）")
        out("  [2] Codex")
        raw = _ask(ask, "Agent（編號，留空用 Claude）: ")
        if raw is None:
            return 0, None
        if raw in ("2", "codex"):
            target = "codex"
        elif raw not in ("", "1", "claude"):
            out(f"無效編號：{raw}")
            return 1, None
    if target == "claude":
        profiles = list(cfg.profiles)
        for i, p in enumerate(profiles, 1):
            out(f"  [{i}] {p}{'（預設）' if cfg.profiles[p].default else ''}")
        raw = _ask(ask, "Profile（編號，留空用預設）: ")
        if raw is None:
            return 0, None
        if not raw:
            profile = cfg.default_profile.name
        elif raw.isdigit() and 1 <= int(raw) <= len(profiles):
            profile = profiles[int(raw) - 1]
        else:
            out(f"無效編號：{raw}")
            return 1, None
        target = f"claude:{profile}"
    if choose_dir_fn is not None:
        cwd = choose_dir_fn(_default_start(cfg), allow_missing=False)
        if not cwd:
            out("已取消")
            return 0, None
    else:
        cwd = _ask(ask, "工作目錄（建議用專案目錄，不要用 ~）: ")
    if not cwd:
        return 0, None
    rc = new_fn(cfg, run, out, name, target, cwd, **(prompt_kw or {}))
    # 2 = not created (name already used, directory missing…): an existing session of the same name was not opened by this run, so do not attach to it
    return rc, (name if rc in (0, 1) and Tmux(run).has_session(name) else None)


def _stop(cfg, run, out, ask, names, own_pane, stop_fn, *, confirmed=False):
    if not confirmed:
        out("將停掉：" + "、".join(names))
        if (_ask(ask, "確定？(y/N): ") or "").lower() != "y":
            out("已取消")
            return 0
    return stop_fn(cfg, run, out, names=tuple(names), profile=None, force=False,
                   dry_run=False, timeout=90, own_pane=own_pane)


def _restart(cfg, run, out, ask, plan, own_pane, restart_fn, kw, *, rows=None, confirmed=True):
    """S. plan: the RestartPlan confirmed in the list (restart!), or names to plan and confirm here (--plain).
    Never attaches: the sessions are reattached in the background."""
    if not confirmed:
        plan = restart_mod.plan_restart(rows, plan, resume_stopped=True, own_pane=own_pane)
        line = restart_mod.confirm_line(plan, limit=None)  # every name: the text output wraps
        if not line:
            why = restart_mod.skip_text(plan.skip).removeprefix("略過：") if plan.skip else ""
            out(f"沒有可以重開的：{why}" if why else "沒有可以重開的 session")
            return 0
        out(line.removesuffix("　確定？(y/N)"))
        if (_ask(ask, "確定？(y/N): ") or "").lower() != "y":
            out("已取消")
            return 0
    return restart_fn(cfg, run, out, ask, plan, own_pane=own_pane, on_prompt=kw.get("on_prompt"))


def _move(cfg, run, out, ask, name, agent, own_pane, move_fn, choose_dir_fn=None, prompt_kw=None):
    if choose_dir_fn is not None:
        raw = choose_dir_fn(_session_cwd(cfg, name), allow_missing=True)
        if not raw:
            out("已取消")
            return 0
    else:
        raw = _ask(ask, f"{name} 要搬到哪個目錄: ")
    if not raw:
        return 0
    dest = Path(raw).expanduser().resolve()
    made = "" if dest.is_dir() else "（目錄不存在，將建立）"
    out(f"將搬移：{name} → {dest}{made}")
    out(f"會結束這個 session 的 {agent}，在新目錄接回同一段對話，並自動信任新目錄")
    if (_ask(ask, "確定？(y/N): ") or "").lower() != "y":
        out("已取消")
        return 0
    return move_fn(cfg, run, out, name, str(dest), own_pane=own_pane, **(prompt_kw or {}))


def _switch(cfg, run, out, ask, r, own_pane, switch_fn, prompt_kw=None):
    """P: pick another profile for a Claude session, confirm, then continue the same conversation under it."""
    if r.agent != "claude":
        out(f"{r.name} 是 {r.agent}，只有 Claude session 能換 profile")
        return 1
    profiles = list(cfg.profiles)
    for i, p in enumerate(profiles, 1):
        label = cfg.profiles[p].label
        out(f"  [{i}] {p}{f'（{label}）' if label else ''}{'  ← 目前' if p == r.profile else ''}")
    raw = _ask(ask, "換到哪個 profile？(編號，Enter 取消): ")
    if not raw:
        out("已取消")
        return 0
    if not raw.isdigit() or not 1 <= int(raw) <= len(profiles):
        out(f"無效編號：{raw}")
        return 1
    new = profiles[int(raw) - 1]
    if new == r.profile:
        out(f"{r.name} 已經是 {new}")
        return 0
    label = cfg.profiles[new].label
    out(f"將換 profile：{r.name}　{r.profile} → {new}{f'（{label}）' if label else ''}")
    gap = session_run.gap_line(cfg, r.profile, new) if r.profile in cfg.profiles else None
    if gap:
        out(gap)
    out("會結束這個 session 的 claude，用新 profile 接回同一段對話" if r.state is State.RUNNING
        else "會用新 profile 接回這段對話")
    if (_ask(ask, "確定？(y/N): ") or "").lower() != "y":
        out("已取消")
        return 0
    return switch_fn(cfg, run, out, r.name, new, own_pane=own_pane, warn_gap=False, **(prompt_kw or {}))


def _switch_batch(cfg, run, out, ask, names, own_pane, switch_fn, rows_fn, prompt_kw=None):
    """P with several sessions (marked, or a folder): pick the profile, then switch_many plans, confirms and runs."""
    from .. import switch_many  # lazy: keep ui import-light
    profiles = list(cfg.profiles)
    for i, p in enumerate(profiles, 1):
        label = cfg.profiles[p].label
        out(f"  [{i}] {p}{f'（{label}）' if label else ''}")
    raw = _ask(ask, f"{len(names)} 個 session 換到哪個 profile？(編號，Enter 取消): ")
    if not raw:
        out("已取消")
        return 0
    if not raw.isdigit() or not 1 <= int(raw) <= len(profiles):
        out(f"無效編號：{raw}")
        return 1
    return switch_many.cmd_switch_many(
        cfg, run, out, lambda prompt: _ask(ask, prompt + ": "), names, profiles[int(raw) - 1], own_pane=own_pane,
        on_prompt=(prompt_kw or {}).get("on_prompt"), switch_fn=switch_fn, status_fn=rows_fn)


def _rename(cfg, run, out, ask, name, rename_fn):
    new = (_ask(ask, f"{name} 的新名稱（Enter 取消）: ") or "").strip()
    if not new:
        out("已取消")
        return 0
    return rename_fn(cfg, run, out, name, new)


def _alias(cfg, out, ask, name, alias_fn):
    new = (_ask(ask, "別名（空白＝清掉）：") or "").strip()
    return alias_fn(cfg, out, name, new) if new else alias_fn(cfg, out, name, "", clear=True)


def _forget(cfg, run, out, ask, name, forget_fn):
    """X: confirm, then remove the row from the registry (the conversation itself is not deleted)."""
    if (_ask(ask, f"從登記表移除 {name}？之後不會再列出，也不會被 R 接回；對話紀錄不刪 (y/N): ") or "").lower() != "y":
        out("已取消")
        return 0
    return forget_fn(cfg, run, out, name)


def tell_flow(cfg, run, out, ask, names):
    """t: ask for a message, then send it from the terminal. One name goes through send.submit (a busy one is queued),
    several through send.submit_batch (the busy ones are skipped and listed). A message starting with / asks once
    whether to send it directly (only y / Y); empty or cancelled records nothing. 0 sent or cancelled, 1 refused."""
    message = _ask(ask, "訊息：")
    if not message:
        out("已取消")
        return 0
    raw = message.startswith("/") and (_ask(ask, "直接送（不加標頭）？ y/N") or "").lower() == "y"
    now = send_mod._utcnow()
    if len(names) == 1:
        r = send_mod.submit(cfg, run, send_mod.TERMINAL, names[0], message, notify=False, now=now, raw=raw)
        items, skipped = [r], []
    else:
        r = send_mod.submit_batch(cfg, run, send_mod.TERMINAL, list(names), message, raw=raw, notify=False, now=now)
        items, skipped = ([], []) if isinstance(r, str) else (r.items, r.skipped)
    if isinstance(r, str):
        out(f"❌ {r}")
        return 1
    if len(names) > 1:
        out(f"批次 {r.id}：送給 {len(items)} 個 session" + (f"，跳過 {len(skipped)} 個" if skipped else ""))
    for d in items:
        out(f"派工 {d.id} → {d.to_name}：{send_mod.STATE_LABELS[d.state]}" + (f"（{d.reason}）" if d.reason else ""))
    for name, why in skipped:
        out(f"跳過 {name}：{why}")
    return 0


CANCEL_WAIT_S = 0.3  # the ui never waits longer than this for the dispatches lock


def cancel_flow(cfg, out, ask, d, *, cancel_fn=None):
    """D list, x: cancel a queued dispatch, or stop waiting for the reply of a sent one, after asking; any other state
    says why not. 0 done or declined, 1 refused."""
    refusal = send_mod.cancel_refusal(d)
    if refusal:
        out(refusal)
        return 1
    question = (f"取消派工 {d.id}（給 {d.to_name}）？(y/N): " if d.state == "queued"
                else f"不再等派工 {d.id} 的回覆？訊息已送到 {d.to_name}，收不回 (y/N): ")
    if (_ask(ask, question) or "").lower() != "y":
        out("已取消")
        return 0
    r = (cancel_fn or (lambda c, did: send_mod.cancel(c, did, wait_s=CANCEL_WAIT_S)))(cfg, d.id)
    if isinstance(r, str):
        out(f"❌ {r}")
        return 1
    out(send_mod.cancel_done_text(r))
    return 0


def _plain_search(ask, out, rows, query, prefix=None):
    """/text: case-insensitive substring match on the name or alias. One hit attaches; several are listed with their original numbers and one is asked for."""
    hits = [(i, r) for i, r in enumerate(rows, 1) if query and name_matches(r, query)]
    if not hits:
        out(f"沒有名稱含「{query}」的 session")
        return ("invalid", [])
    if len(hits) == 1:
        return ("attach", [hits[0][1].name])
    for i, r in hits:
        out(f"  [{i:>2}] {(prefix or {}).get(r.name, '')}{row_text(r)}")
    answer = _ask(ask, "要開哪一個？(編號，Enter 返回): ")
    if not answer or answer.lower() == "q":
        return ("invalid", [])
    picked = _pick(lambda _p: answer, out, rows, "", many=False)
    return ("attach", picked) if picked else ("invalid", [])


def _plain_action(ask, out, rows, current, tree=None):
    """tree (folders.Tree or None): the list follows its order, every folder open, each name after its folder path."""
    rows, prefix = folder_actions.plain_order(rows, tree)
    _show(out, rows, current, prefix)
    choice = _ask(ask, "選擇: ")
    if not choice or choice.lower() == "q":
        return ("quit", [])
    c = choice.lower()
    if choice == "R":
        return ("restore", [])
    if choice == "A":
        return ("adopt", [])
    if c == "w":
        return ("save", [])
    if choice == "D":
        return ("dispatches", [])
    if choice == "U":
        return ("update", [])
    if choice.startswith("/"):
        return _plain_search(ask, out, rows, choice[1:].strip(), prefix)
    if choice.isdigit():
        picked = _pick(lambda _p: choice, out, rows, "", many=False)
        return ("attach", picked) if picked else ("invalid", [])
    if choice == "P":
        picked = _pick(ask, out, rows, "要換哪一個的 profile？(編號): ", many=False)
        return ("switch", picked) if picked else ("quit" if picked is None else "invalid", [])
    if choice == "X":
        picked = _pick(ask, out, rows, "要從登記表移除哪一個？(編號): ", many=False)
        return ("forget", picked) if picked else ("quit" if picked is None else "invalid", [])
    if c == "n":
        return ("new", [])
    if choice == "S":
        picked = _pick(ask, out, rows, "要重開哪些？(編號，可多個用空格分隔): ", many=True)
        return ("restart", picked) if picked else ("quit" if picked is None else "invalid", [])
    if c in ("g", "f"):
        out(FOLDER_HINT)
        return ("invalid", [])
    if choice == "p":
        return ("plugins", [])
    if choice == "o":
        return ("profiles", [])
    if c == "s":
        picked = _pick(ask, out, rows, "要停掉哪些？(編號，可多個用空格分隔): ", many=True)
    elif c == "m":
        picked = _pick(ask, out, rows, "要搬哪一個？(編號): ", many=False)
    elif c == "e":
        picked = _pick(ask, out, rows, "要改哪一個的名稱？(編號): ", many=False)
    elif choice == "L":
        picked = _pick(ask, out, rows, "要改哪一個的別名？(編號): ", many=False)
    elif c in ("r", "a"):
        picked = _pick(ask, out, rows, f"要 {'retire' if c == 'r' else 'activate'} 哪一個？(編號): ", many=False)
    else:
        out(f"無效選擇：{choice}")
        return ("invalid", [])
    if picked is None:
        return ("quit", [])
    if picked is False:
        return ("invalid", [])
    return ({"s": "stop", "m": "move", "e": "rename", "l": "alias", "r": "retired", "a": "active"}[c], picked)


UPDATED = object()  # second value of act(): update succeeded, sampan must be restarted
RESUME_HERE = object()  # second value of act() (RESUME_HERE, command): sampan shuts down and runs the reattach command in place in its own pane


def _resume_line(cfg, name):
    """The reattach command for the row called name in the registry (cd to its directory, with the profile environment); None if unreadable or the id is wrong."""
    from ..steps import launch_cmd

    try:
        row = next((r for r in load_rows(cfg.registry_path, codex_profile=cfg.codex.profile) if r.tmux == name), None)
        return launch_cmd(cfg, row, cwd=row.cwd) if row else None
    except (RegistryError, ValueError, KeyError):
        return None


REINSTALL_HINT = "；要重新安裝請用 sampan update --force"


def _update(out, ask, updater, update_fn):
    """U: update after confirmation; returns whether sampan must be restarted (only after a real update).
    Nothing newer known: re-check now (the cache may predate a fresh tag), and if still nothing newer only say so;
    reinstalling is `sampan update --force`."""
    if updater is None:
        out("目前已是最新版")
        return False
    latest = updater.available()
    if not latest:
        out("▶ 檢查最新版…")
        updater.refresh(force=True)
        latest = updater.available()
    if latest:
        if (_ask(ask, f"更新到 v{latest}？(y/N): ") or "").lower() != "y":
            out("已取消")
            return False
        return update_fn(out, current=updater.current, latest=latest)
    pin = updater.latest
    if pin is None:
        out(f"查不到最新版（離線或連不到 GitHub）{REINSTALL_HINT}")
    elif update_mod.newer(pin, updater.current):
        out(f"目前版本（{updater.current}）比最新版 v{pin} 新，不更新{REINSTALL_HINT}")
    else:
        out(f"目前已是最新版（v{pin}）{REINSTALL_HINT}")
    return False


def _first_opened(out, opened, verb):
    """Sessions opened by R/A: attach to the first, mention the others in the message area."""
    for name in opened[1:]:
        out(f"{name} 也已{verb}")
    return opened[0] if opened else None


def cmd_ui(cfg, run, out, ask=input, *, current_session=None, own_pane=None, picker_fn=None,
           rows_fn=status_rows, exec_fn=os.execvp, stop_fn=session_run.cmd_stop,
           new_fn=session_run.cmd_new, set_state_fn=session_run.cmd_set_state, move_fn=session_run.cmd_move,
           rename_fn=session_run.cmd_rename, alias_fn=session_run.cmd_alias, switch_fn=session_run.cmd_switch, forget_fn=session_run.cmd_forget,
           restore_fn=restore_mod.cmd_restore, restore_picker_fn=None,
           adopt_fn=adopt_mod.cmd_adopt, adopt_picker_fn=None, save_fn=save_mod.cmd_save, loop=False,
           attach_fn=None, choose_dir_fn=None, updater=None, update_fn=update_mod.do_update, argv=(), prompt_fn=None, plugins_fn=None,
           profiles_fn=None, set_cfg_fn=None, dispatches_fn=None, choose_folder_fn=None, restart_fn=None,
           detach_fn=None):
    """dispatches_fn(ask=…) is the dispatch list for D (curses: CursesScreen.dispatches); when None, the dispatches are printed.
    picker_fn(rows, current) returns (action, list of names); when None, use the plain-text menu where a number is typed.
    restore_picker_fn / adopt_picker_fn are the checklist interfaces for R / A; when None, restore / adopt ask in text themselves.
    loop=True: after an action return to the list, leaving only on q / Esc (or when an opened session is attached); False does just one action.
    With loop and picker_fn (curses), an action that raises is shown as "❌ <action> 失敗：<error>" and the list stays (Ctrl-C still interrupts).
    choose_dir_fn(start, allow_missing=…) is the directory chooser screen for n / m (returns a path or None); when None, the directory is typed.
    U (updater = update.Checker) updates after confirmation, re-checking first when no newer version is known and offering a reinstall when there is none;
    on success sampan is re-executed with the same arguments (argv).
    prompt_fn(name, agent, prompt): used when R reattach or m move waits for the agent (n and Enter-reattach of one do not wait, they go straight into tmux) and the agent stops at a startup menu (in curses it is
    CursesScreen.prompt_choice, with --plain prompts.plain_prompt); when "go in and handle it myself" is chosen, that session is attached when the action ends.
    None = do not look at prompts (as before).
    plugins_fn(ask=…, own_pane=…, on_prompt=…) is the plugin screen for p (curses: CursesScreen.plugins); when None, the --plain plugin menu.
    detach_fn(cfg, run, names) binds the key that jumps out of a session and marks names as sampan sessions
    (detach_key.ensure; None = do nothing): once for the running sessions when the list is first shown, then for the
    session about to be attached; a warning it returns is shown once.
    profiles_fn(ask=…) is the profile screen for o (curses: CursesScreen.profiles); when None, the --plain profile menu.
    After it (even when it raises) the config is re-read, so the list and n / P see renamed, disabled or added profiles.
    The config is also re-read before each action when config.toml changed on disk (another terminal ran sampan profile
    …); set_cfg_fn(cfg) hands every re-read config on (curses: to CursesScreen.cfg).
    choose_folder_fn(labels) -> index or None is f's destination list (curses: CursesScreen.choose_folder); when None,
    the destinations are numbered in text. restart_fn defaults to restart.cmd_restart, run with stop_fn / restore_fn.
    --plain lists the sessions in folders.json's order (a broken file: flat, said once); the curses screen reads the
    folders itself."""

    def _stamp():
        try:
            st = cfg.path.stat()
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size

    seen = _stamp()
    folders_said = [None]  # the folders.json error last shown by --plain (each one is said once)
    if restart_fn is None:
        def restart_fn(c, rn, o, a, plan, **k):
            return restart_mod.cmd_restart(c, rn, o, a, plan, stop_fn=stop_fn, restore_fn=restore_fn, **k)

    def _plain_tree():
        try:
            tree = folders_mod.load(cfg.state_dir)
        except folders_mod.FoldersError as e:
            if str(e) != folders_said[0]:
                out(f"⚠ {e}，先照原樣列出")
            folders_said[0] = str(e)
            return None
        folders_said[0] = None
        return tree

    def _reload():
        nonlocal cfg, seen
        from .plugin_actions import reload_config  # lazy: plugin_actions imports this module
        seen = _stamp()  # taken first: a write during the read is seen next time
        cfg = reload_config(cfg, out)
        if set_cfg_fn is not None:
            set_cfg_fn(cfg)

    def _refresh():
        """Re-read the config only when config.toml changed since the last read (so a broken file warns once)."""
        if _stamp() != seen:
            _reload()

    def _attach_row(names, rows, kw):
        name = names[0]
        r = next((x for x in rows if x.name == name), None)
        if r is not None and r.state is State.UNKNOWN:
            out(f"{name}：讀不到 tmux，不知道它有沒有在跑，不接回也不接上；tmux 恢復後再試")
            return 1, None
        if r is not None and r.state is State.STOPPED:
            opened = []
            # only one reattached: go straight in once sent; the user answers any startup menu (trust, update…) inside tmux
            rc = restore_fn(cfg, run, out, ask, names=(name,), confirm=False, opened=opened, wait=False)
            if not opened and name == current_session:
                # sampan itself occupies this session's pane (e.g. sampan opened in the same pane after claude exited):
                # shut sampan down and reattach in place in this pane; when the agent exits we are back at the original shell
                line = _resume_line(cfg, name)
                if line:
                    out(f"→ {name}：sampan 就在這個 pane，關掉 sampan 在這裡接回原對話")
                    return 0, (RESUME_HERE, line)
            if not opened and Tmux(run).has_session(name):
                # could not reattach (e.g. the pane is occupied by another program) but the tmux session is still there: go in anyway and let the user look and handle it
                out(f"→ {name}：沒辦法自動接回，先進到 tmux session；裡面的程式結束、回到 shell 後，再按 Enter 就會接回原對話")
                return rc, name
            return rc, (opened[0] if opened else None)
        if not Tmux(run).has_session(name):
            out(f"{name} 目前沒有在 tmux 裡執行；要接回請用 sampan restore {name}")
            return 1, None
        return 0, name

    def _restore(names, rows, kw):
        opened = []
        rc = restore_fn(cfg, run, out, ask, picker_fn=restore_picker_fn, opened=opened, **kw)
        return rc, _first_opened(out, opened, "接回")

    def _adopt(names, rows, kw):
        opened = []
        rc = adopt_fn(cfg, run, out, ask, picker_fn=adopt_picker_fn, opened=opened)
        return rc, _first_opened(out, opened, "接手")

    def _move_row(names, rows, kw):
        agent = next((r.agent for r in rows if r.name == names[0] and r.agent != "-"), "agent")
        return _move(cfg, run, out, ask, names[0], agent, own_pane, move_fn, choose_dir_fn, kw), None

    def _switch_row(names, rows, kw):
        r = next((x for x in rows if x.name == names[0]), None)
        if r is None:
            out(f"找不到 {names[0]}")
            return 1, None
        return _switch(cfg, run, out, ask, r, own_pane, switch_fn, kw), None

    def _switch_many_rows(names, rows, kw):
        return _switch_batch(cfg, run, out, ask, names, own_pane, switch_fn, rows_fn, kw), None

    def _plugins(names, rows, kw):
        if plugins_fn is not None:
            return plugins_fn(ask=ask, own_pane=own_pane, on_prompt=kw.get("on_prompt")), None
        from .plugin_actions import plain_plugins  # lazy: plugin_actions imports this module
        return plain_plugins(cfg, run, out, ask, own_pane=own_pane, on_prompt=kw.get("on_prompt")), None

    def _profiles(names, rows, kw):
        try:
            if profiles_fn is not None:
                rc = profiles_fn(ask=ask)
            else:
                from .profile_actions import plain_profiles
                rc = plain_profiles(cfg, run, out, ask, rows_fn=rows_fn)
        finally:
            _reload()
        return rc, None

    def _new_here(names, rows, kw):
        prompt_kw = {"wait": False}  # go straight in once opened
        if names and names[0]:
            prompt_kw["folder"] = names[0]
        return _new(cfg, run, out, ask, new_fn, choose_dir_fn, prompt_kw)

    def _restart_names(names, rows, kw, confirmed):
        """restart! carries the RestartPlan confirmed in the list (bare names, already confirmed, are planned again);
        restart (--plain) carries names to plan and confirm."""
        if confirmed and names and isinstance(names[0], restart_mod.RestartPlan):
            plan = names[0]
        elif confirmed:
            plan = restart_mod.plan_restart(rows, names, resume_stopped=True, own_pane=own_pane)
        else:
            plan = list(names)
        return _restart(cfg, run, out, ask, plan, own_pane, restart_fn, kw, rows=rows, confirmed=confirmed), None

    def answer(prompt):
        return _ask(ask, prompt)

    def _dispatches(names, rows, kw):
        if dispatches_fn is not None:
            return dispatches_fn(ask=ask), None
        return send_mod.cmd_dispatches(cfg, out, all_=False), None

    # one handler per action: (names, rows, kw) -> (rc, session to attach to, or None);
    # an unknown action raises directly and is never written to the registry as a state
    handlers = {
        "attach": _attach_row,
        "new": _new_here,
        "stop": lambda names, rows, kw: (_stop(cfg, run, out, ask, names, own_pane, stop_fn, confirmed=False), None),
        "stop!": lambda names, rows, kw: (_stop(cfg, run, out, ask, names, own_pane, stop_fn, confirmed=True), None),
        "save": lambda names, rows, kw: (save_fn(cfg, run, out), None),
        "update": lambda names, rows, kw: (0, UPDATED) if _update(out, ask, updater, update_fn) else (1, None),
        "restore": _restore,
        "adopt": _adopt,
        "rename": lambda names, rows, kw: (_rename(cfg, run, out, ask, names[0], rename_fn), None),
        "alias": lambda names, rows, kw: (_alias(cfg, out, ask, names[0], alias_fn), None),
        "move": _move_row,
        "switch": _switch_row,
        "switch_many": _switch_many_rows,
        "forget": lambda names, rows, kw: (_forget(cfg, run, out, ask, names[0], forget_fn), None),
        "plugins": _plugins,
        "profiles": _profiles,
        "dispatches": _dispatches,
        "restart": lambda names, rows, kw: _restart_names(names, rows, kw, False),
        "restart!": lambda names, rows, kw: _restart_names(names, rows, kw, True),
        "collapse": lambda names, rows, kw: (folder_actions.save_collapsed(cfg, names[0], names[1] == "1") or 0, None),
        "group": lambda names, rows, kw: (folder_actions.group_flow(cfg, out, answer, names), None),
        "file": lambda names, rows, kw: (folder_actions.file_flow(cfg, out, answer, names, rows, choose_folder_fn),
                                         None),
        "tell": lambda names, rows, kw: (tell_flow(cfg, run, out, answer, names), None),
        "rename_folder": lambda names, rows, kw: (folder_actions.rename_flow(cfg, out, answer, names[0]), None),
        "rm_folder": lambda names, rows, kw: (folder_actions.remove_flow(cfg, out, answer, names[0]), None),
        "retired": lambda names, rows, kw: (set_state_fn(cfg, out, names[0], "retired"), None),
        "active": lambda names, rows, kw: (set_state_fn(cfg, out, names[0], "active"), None),
    }

    def act(action, names, rows, kw):
        """Returns (rc, session to attach to, or None). kw is {"on_prompt": …} for the subcommands that start an agent (may be empty)."""
        try:
            handler = handlers[action]
        except KeyError:
            raise ValueError(f"未知的 ui action：{action!r}") from None
        return handler(names, rows, kw)

    warned = set()

    def _detach(names):
        if detach_fn is None:
            return
        try:
            warning = detach_fn(cfg, run, names)
        except Exception as e:  # noqa: BLE001  never in the way of attaching
            warning = f"沒辦法設跳離鍵：{e}"
        if warning and warning not in warned:
            warned.add(warning)
            out(f"⚠ {warning}")

    first, marked = True, set()
    hint = updater.hint() if updater is not None and picker_fn is None else None
    if hint:
        out(hint)  # --plain: mention once at the start (cache only)
    while True:
        _refresh()  # the list and the account lines are drawn with the config as it is now
        try:
            rows = rows_fn(cfg, run)
        except RegistryError as e:
            out(f"❌ {e}")
            return 2
        if getattr(rows, "warning", None) and not picker_fn:
            out(f"⚠ {rows.warning}")  # the curses version puts it in the hint line at the bottom of the screen
        fresh = [r.name for r in rows if r.tmux_alive and r.state not in UNMANAGED and r.name not in marked]
        if fresh or first:  # sessions opened since (n, R, A, another terminal) get the mark too
            first = False
            _detach(fresh)
            marked.update(fresh)
        if picker_fn:
            action, names = picker_fn(rows, current_session)
        else:
            action, names = _plain_action(ask, out, rows, current_session, _plain_tree())
        if action == "quit":
            return 0
        if action == "invalid":
            rc, go = 1, None
        else:
            _refresh()  # picker_fn waits for keys: the config may have been edited elsewhere meanwhile
            kw, want = {}, []
            if prompt_fn is not None:
                on_prompt, want = recording(prompt_fn)
                kw = {"on_prompt": on_prompt}
            try:
                rc, go = act(action, names, rows, kw)
                if want:  # "go in and handle it myself" was chosen: attach to that session when the action ends
                    go = want[-1]
            except KeyboardInterrupt:
                out("")
                out("已中斷")
                rc, go = 130, None
            except Exception as e:  # noqa: BLE001  the curses loop must outlive a failed action
                if not (loop and picker_fn is not None):
                    raise
                out(f"❌ {action} 失敗：{e}")
                rc, go = 1, None
        if isinstance(go, tuple) and go[0] is RESUME_HERE:
            # in curses exec_fn only records it, and the exec happens after curses is closed: sampan is replaced by the reattach command
            exec_fn("/bin/sh", ["/bin/sh", "-c", go[1]])
            return 0
        if go is UPDATED:
            # in curses exec_fn only records it, and the real exec happens after curses is closed (endwin)
            exec_fn("sampan", ["sampan", *argv])
            return 0
        if isinstance(go, str) and not any(r.name == go and r.state in UNMANAGED for r in rows):
            _detach([go])  # never a tmux session sampan does not manage: there the key stays the program's
        if go and current_session and loop:
            # inside tmux: after switching over, sampan stays in its original pane, and switching back shows the list again
            if _attach(run, out, go, current_session, exec_fn) == 0:
                out(f"已切到 {go}（sampan 留在這個 pane）")
            continue
        if go and loop and attach_fn is not None and not current_session:
            # outside tmux: attach in the foreground, and after detach (or the session ending) return to the list
            if not Tmux(run).has_session(go):
                out(f"{go} 目前沒有在 tmux 裡執行")
                continue
            attach_fn([*TMUX, "attach-session", "-t", f"={go}"])
            out(f"已從 {go} 回到 sampan（q 離開）")
            continue
        if go:
            return _attach(run, out, go, current_session, exec_fn)
        if not loop:
            return rc
        if picker_fn is None:
            out("")
