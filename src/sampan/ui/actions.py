"""Action layer of the ui (shared by curses and plain text): the main loop and dispatch of cmd_ui, the question flows of actions such as n/s/m/e/U, the --plain menu."""
import os
from pathlib import Path

from .. import adopt as adopt_mod
from .. import restore as restore_mod
from .. import save as save_mod
from .. import session_run
from .. import update as update_mod
from ..prompts import recording
from ..registry import RegistryError, load_rows
from ..status import State, status_rows
from ..tmux import TMUX, Tmux
from .dirpick import recent_dirs, sessions_by_dir
from .state import row_text

HELP = ("編號 = 接上／切過去（已停止的先接回）；s 停掉　m 搬目錄　e 改名　R 接回　A 接手 tmux 外的　r retire　a activate"
        "　n 開新 session　w 立即存檔　/文字 搜尋　q 離開")


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


def _show(out, rows, current):
    out("═══ sampan sessions ═══")
    if not rows:
        out("  目前沒有任何 session")
    for i, r in enumerate(rows, 1):
        mark = "  ← 目前所在" if r.name == current else ""
        out(f"  [{i:>2}] {row_text(r)}{mark}")
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
        rows = load_rows(cfg.registry_path)
    except RegistryError:
        rows = []
    return {"recent": recent_dirs(rows), "sessions": sessions_by_dir(rows)}


def _default_start(cfg):
    """Where the `n` directory picker starts: [ui] start_dir (default ~/Projects if it exists, else ~)."""
    return str(cfg.start_dir if Path(cfg.start_dir).is_dir() else cfg.home)


def _session_cwd(cfg, name):
    try:
        rows = load_rows(cfg.registry_path)
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


def _rename(cfg, run, out, ask, name, rename_fn):
    new = (_ask(ask, f"{name} 的新名稱（Enter 取消）: ") or "").strip()
    if not new:
        out("已取消")
        return 0
    return rename_fn(cfg, run, out, name, new)


def _plain_search(ask, out, rows, query):
    """/text: case-insensitive substring match on the name. One hit attaches; several are listed with their original numbers and one is asked for."""
    hits = [(i, r) for i, r in enumerate(rows, 1) if query and query.lower() in r.name.lower()]
    if not hits:
        out(f"沒有名稱含「{query}」的 session")
        return ("invalid", [])
    if len(hits) == 1:
        return ("attach", [hits[0][1].name])
    for i, r in hits:
        out(f"  [{i:>2}] {row_text(r)}")
    answer = _ask(ask, "要開哪一個？(編號，Enter 返回): ")
    if not answer or answer.lower() == "q":
        return ("invalid", [])
    picked = _pick(lambda _p: answer, out, rows, "", many=False)
    return ("attach", picked) if picked else ("invalid", [])


def _plain_action(ask, out, rows, current):
    _show(out, rows, current)
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
    if choice == "U":
        return ("update", [])
    if choice.startswith("/"):
        return _plain_search(ask, out, rows, choice[1:].strip())
    if choice.isdigit():
        picked = _pick(lambda _p: choice, out, rows, "", many=False)
        return ("attach", picked) if picked else ("invalid", [])
    if c == "n":
        return ("new", [])
    if c == "s":
        picked = _pick(ask, out, rows, "要停掉哪些？(編號，可多個用空格分隔): ", many=True)
    elif c == "m":
        picked = _pick(ask, out, rows, "要搬哪一個？(編號): ", many=False)
    elif c == "e":
        picked = _pick(ask, out, rows, "要改哪一個的名稱？(編號): ", many=False)
    elif c in ("r", "a"):
        picked = _pick(ask, out, rows, f"要 {'retire' if c == 'r' else 'activate'} 哪一個？(編號): ", many=False)
    else:
        out(f"無效選擇：{choice}")
        return ("invalid", [])
    if picked is None:
        return ("quit", [])
    if picked is False:
        return ("invalid", [])
    return ({"s": "stop", "m": "move", "e": "rename", "r": "retired", "a": "active"}[c], picked)


UPDATED = object()  # second value of act(): update succeeded, sampan must be restarted
RESUME_HERE = object()  # second value of act() (RESUME_HERE, command): sampan shuts down and runs the reattach command in place in its own pane


def _resume_line(cfg, name):
    """The reattach command for the row called name in the registry (cd to its directory, with the profile environment); None if unreadable or the id is wrong."""
    from ..launch import launch_line

    try:
        row = next((r for r in load_rows(cfg.registry_path) if r.tmux == name), None)
        return launch_line(cfg, row.agent, row.profile, resume=row.session_id, cwd=row.cwd) if row else None
    except (RegistryError, ValueError, KeyError):
        return None


def _update(out, ask, updater, update_fn):
    """U: update after confirmation; returns whether sampan must be restarted (new version, or reinstalled).
    Nothing newer known: re-check now (the cache may predate a fresh tag), and if still nothing newer offer a reinstall."""
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
        out("查不到最新版（離線或連不到 GitHub）")
        pin = update_mod.clean_release(updater.current)
        if pin is None:  # a dev / unknown build with no known release: nothing sensible to reinstall
            return False
        question = f"要重新安裝目前版本（v{pin}）嗎？(y/N): "
    elif update_mod.newer(pin, updater.current):
        question = f"目前版本比最新版新；要改裝 v{pin} 嗎？(y/N): "
    else:
        out(f"目前已是最新版（v{pin}）")
        question = "要重新安裝目前版本嗎？(y/N): "
    if (_ask(ask, question) or "").lower() != "y":
        out("已取消")
        return False
    return update_fn(out, current=updater.current, latest=pin, force=True)


def _first_opened(out, opened, verb):
    """Sessions opened by R/A: attach to the first, mention the others in the message area."""
    for name in opened[1:]:
        out(f"{name} 也已{verb}")
    return opened[0] if opened else None


def cmd_ui(cfg, run, out, ask=input, *, current_session=None, own_pane=None, picker_fn=None,
           rows_fn=status_rows, exec_fn=os.execvp, stop_fn=session_run.cmd_stop,
           new_fn=session_run.cmd_new, set_state_fn=session_run.cmd_set_state, move_fn=session_run.cmd_move,
           rename_fn=session_run.cmd_rename,
           restore_fn=restore_mod.cmd_restore, restore_picker_fn=None,
           adopt_fn=adopt_mod.cmd_adopt, adopt_picker_fn=None, save_fn=save_mod.cmd_save, loop=False,
           attach_fn=None, choose_dir_fn=None, updater=None, update_fn=update_mod.do_update, argv=(), prompt_fn=None):
    """picker_fn(rows, current) returns (action, list of names); when None, use the plain-text menu where a number is typed.
    restore_picker_fn / adopt_picker_fn are the checklist interfaces for R / A; when None, restore / adopt ask in text themselves.
    loop=True: after an action return to the list, leaving only on q / Esc (or when an opened session is attached); False does just one action.
    With loop and picker_fn (curses), an action that raises is shown as "❌ <action> 失敗：<error>" and the list stays (Ctrl-C still interrupts).
    choose_dir_fn(start, allow_missing=…) is the directory chooser screen for n / m (returns a path or None); when None, the directory is typed.
    U (updater = update.Checker) updates after confirmation, re-checking first when no newer version is known and offering a reinstall when there is none;
    on success sampan is re-executed with the same arguments (argv).
    prompt_fn(name, agent, prompt): used when R reattach or m move waits for the agent (n and Enter-reattach of one do not wait, they go straight into tmux) and the agent stops at a startup menu (in curses it is
    CursesScreen.prompt_choice, with --plain prompts.plain_prompt); when "go in and handle it myself" is chosen, that session is attached when the action ends.
    None = do not look at prompts (as before)."""

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

    # one handler per action: (names, rows, kw) -> (rc, session to attach to, or None);
    # an unknown action raises directly and is never written to the registry as a state
    handlers = {
        "attach": _attach_row,
        "new": lambda names, rows, kw: _new(cfg, run, out, ask, new_fn, choose_dir_fn, {"wait": False}),  # go straight in once opened
        "stop": lambda names, rows, kw: (_stop(cfg, run, out, ask, names, own_pane, stop_fn, confirmed=False), None),
        "stop!": lambda names, rows, kw: (_stop(cfg, run, out, ask, names, own_pane, stop_fn, confirmed=True), None),
        "save": lambda names, rows, kw: (save_fn(cfg, run, out), None),
        "update": lambda names, rows, kw: (0, UPDATED) if _update(out, ask, updater, update_fn) else (1, None),
        "restore": _restore,
        "adopt": _adopt,
        "rename": lambda names, rows, kw: (_rename(cfg, run, out, ask, names[0], rename_fn), None),
        "move": _move_row,
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

    hint = updater.hint() if updater is not None and picker_fn is None else None
    if hint:
        out(hint)  # --plain: mention once at the start (cache only)
    while True:
        try:
            rows = rows_fn(cfg, run)
        except RegistryError as e:
            out(f"❌ {e}")
            return 2
        if getattr(rows, "warning", None) and not picker_fn:
            out(f"⚠ {rows.warning}")  # the curses version puts it in the hint line at the bottom of the screen
        action, names = picker_fn(rows, current_session) if picker_fn else _plain_action(ask, out, rows, current_session)
        if action == "quit":
            return 0
        if action == "invalid":
            rc, go = 1, None
        else:
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
