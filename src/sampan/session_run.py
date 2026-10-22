import os
import signal
import time
import uuid
from pathlib import Path

from .claude_state import transcript_problem
from .clock import now_iso
from .codex_state import _is_codex, codex_pid_in_pane, find_rollout
from .launch import launch_line, send_line
from .observe import PROC_ROOT, SPLIT_HINT, live_by_pane, multi_agent_sessions
from .procs import descendant_claude, is_descendant, list_procs
from .prompts import ATTACH, SKIP, Relay
from .registry import STATES, RegistryError, Row, id_problem, load_rows, lock, set_state, write_rows
from .screens import CLAUDE_TRUST_OPTION, CODEX_TRUST_YES, bottom_trust_path, codex_activity
from .tmux import SHELLS, Tmux, TmuxUnreadable

WORKTREE_MARK = "/.claude/worktrees/"
EXIT_TEXT = {"claude": "/exit", "codex": "/quit"}


def _agents(cfg, run, panes, procs, proc_root=PROC_ROOT):
    """(found, multi): agents new/stop/move can act on, by pane id, and sessions with more than one agent.

    A claude without a session file (status unknown) is left out of found: it cannot be told idle or busy."""
    found, _w = live_by_pane(cfg, run, panes, procs, proc_root)
    multi = multi_agent_sessions(procs, panes, found)
    return _actionable(found), multi


def _actionable(found):
    return {k: v for k, v in found.items() if v.agent == "codex" or v.status != "unknown"}


def _alive(procs, pid, agent):
    """Is pid still a process of that agent (claude/codex)?"""
    p = procs.get(pid)
    if p is None:
        return False
    return _is_codex(p) if agent == "codex" else (p.comm == "claude" and not p.zombie)


def _quit(tmux, pane_id, agent, sleep):
    tmux.send_keys(pane_id, "C-u")
    send_line(tmux, pane_id, agent, EXIT_TEXT[agent], sleep)


def _load(cfg, out):
    try:
        return load_rows(cfg.registry_path)
    except RegistryError as e:
        out(f"❌ {e}")
        return None


def _register(cfg, out, row):
    with lock(cfg.state_dir):
        rows = _load(cfg, out)
        if rows is None:
            return False
        rows = [r for r in rows if r.tmux != row.tmux] + [row]
        write_rows(cfg.registry_path, sorted(rows, key=lambda r: r.tmux), now_iso(), cfg.history_dir)
    return True


def _relay_stop(out, name, res):
    """Message for when Relay's outcome is "go in and handle it myself" or "leave it"; returns whether to end the wait."""
    if res == ATTACH:
        out(f"→ {name}：進去處理提示")
        return True
    if res == SKIP:
        out(f"⚠ {name} 停在提示，進去處理")
        return True
    return False


def cmd_new(cfg, run, out, name, target, cwd, *, dry_run=False, sleep=time.sleep, timeout=15, uuid4=uuid.uuid4,
            on_prompt=None, wait=True):
    """target: claude:<profile>, <profile> (taken as claude) or codex.

    wait=False: return as soon as the launch command is sent (the ui's n then goes straight into tmux and the user answers the startup menu there). A Claude row is
    registered before launching; Codex only has a conversation once the first prompt is sent, and sampan save registers it afterwards.

    on_prompt(name, agent, prompt): asked when the agent stops at a menu on startup (trust, update, hooks review) (prompts.Relay);
    None (plain non-interactive CLI, tests) = do not look at the screen, press no keys. Time spent answering prompts does not count toward timeout."""
    if "." in name or ":" in name:
        out(f"❌ 名稱不能含 . 或 :（tmux 會當成 window／pane 分隔）：{name}")
        return 2
    if target == "codex":
        agent, profile = "codex", ""
        if not cfg.codex.enabled:
            out("❌ 設定檔停用了 Codex（[codex] enabled = false）")
            return 2
    else:
        agent, _, profile = target.rpartition(":") if ":" in target else ("claude", "", target)
        if agent != "claude":
            out(f"❌ 未知的 agent：{agent}（用 claude:<profile> 或 codex）")
            return 2
        if profile not in cfg.profiles:
            out(f"❌ 未知的 profile：{profile}（可用：{', '.join(cfg.profiles)}）")
            return 2
    tmux = Tmux(run)
    if tmux.has_session(name):
        out(f"❌ tmux session {name} 已存在")
        return 2
    cwd = str(Path(cwd).expanduser().resolve())
    if not Path(cwd).is_dir():
        out(f"❌ 目錄不存在：{cwd}")
        return 2
    if _load(cfg, out) is None:  # registry broken: stop first, so no unregistered empty session is left behind
        return 2
    session_id = str(uuid4()) if agent == "claude" else None
    cmd = launch_line(cfg, agent, profile, session_id=session_id)
    if dry_run:
        out(f"[dry-run] 建立 tmux session {name}（{cwd}）→ {cmd}")
        return 0
    pane = tmux.new_session(name, cwd)
    if pane is None:
        out(f"❌ tmux new-session 失敗：{name}")
        return 1
    if agent == "claude":
        row = Row(name, "claude", profile, session_id, cwd, "active", now_iso(), tmux.server_id() or "", None)
        if not _register(cfg, out, row):
            return 2
        send_line(tmux, pane, agent, cmd, sleep)
        out(f"▶ {name}（{profile}，{session_id[:8]}）已登記")
    else:
        send_line(tmux, pane, agent, cmd, sleep)
        out(f"▶ {name}（codex）已開啟，等 Codex 建立對話…")
    if not wait:
        return 0
    relay = Relay(on_prompt, tmux, out, sleep) if on_prompt is not None else None
    idle_ticks = 0
    for _ in range(timeout):
        sleep(1)
        panes = tmux.panes()
        if panes is None:  # unreadable this round: make no judgement, look again next round
            continue
        la = _agents(cfg, run, panes, list_procs(run))[0].get(pane)
        if agent == "claude" and la is not None and la.agent == "claude":
            return 0
        if agent == "codex" and la is not None and la.agent == "codex" and la.session_id:
            row = Row(name, "codex", "", la.session_id, cwd, "active", now_iso(), tmux.server_id() or "", la.pid)
            if not _register(cfg, out, row):
                return 2
            out(f"▶ {name}（codex，{la.session_id[:8]}）已登記")
            return 0
        screen = tmux.capture(pane) if relay is not None or agent == "codex" else None
        if relay is not None and _relay_stop(out, name, relay.check(name, agent, pane, screen)):
            return 1
        # Codex only creates the conversation once the first prompt is sent: if it is already at the input box, stop waiting; sampan save registers it later
        idle_ticks = idle_ticks + 1 if agent == "codex" and la is not None and la.agent == "codex" \
            and codex_activity(screen or "") == "idle" else 0
        if idle_ticks >= 2:
            out(f"▶ {name}（codex）已就緒；送出第一個 prompt 後 sampan save 會登記這段對話")
            return 1
    if agent == "claude":
        out(f"⚠ {name} 已開啟並登記，但 {timeout} 秒內沒看到 session 檔（可能停在信任資料夾的提示，到視窗裡確認）")
    else:
        out(f"⚠ {name} 已開啟，但 {timeout} 秒內沒認出 Codex 的對話（可能停在信任資料夾等提示，或要送出第一個 prompt"
            f" 才會建立）；認出後 sampan save 會補登")
    return 1


def cmd_set_state(cfg, out, name, state):
    if state not in STATES:
        out(f"❌ state 只能是 {'、'.join(STATES)}，不是 {state!r}")
        return 2
    with lock(cfg.state_dir):
        rows = _load(cfg, out)
        if rows is None:
            return 2
        if not set_state(rows, name, state):
            out(f"❌ 登記表沒有 {name}")
            return 2
        write_rows(cfg.registry_path, rows, now_iso(), cfg.history_dir)
    out(f"{name} → {state}")
    return 0


def cmd_rename(cfg, run, out, old, new):
    """Rename a tmux session and sync the registry; the agent keeps running and no keys are sent. Only the name of the registry row changes, other fields are untouched."""
    new = (new or "").strip()
    if not new or "." in new or ":" in new:
        out(f"❌ 名稱不能是空的，也不能含 . 或 :（tmux 會當成 window／pane 分隔）：{new!r}")
        return 2
    tmux = Tmux(run)
    with lock(cfg.state_dir):
        rows = _load(cfg, out)
        if rows is None:
            return 2
        row = next((r for r in rows if r.tmux == old), None)
        live = tmux.has_session(old)
        if row is None and not live:
            out(f"❌ 沒有叫 {old} 的 tmux session，登記表也沒有")
            return 2
        if new == old or any(r.tmux == new for r in rows) or tmux.has_session(new):
            out(f"❌ {new} 已有 tmux session 或已在登記表裡，換一個")
            return 2
        if live and not tmux.rename_session(old, new):
            out("❌ tmux rename-session 失敗，什麼都沒改")
            return 1
        if row is not None:
            row.tmux = new
            write_rows(cfg.registry_path, sorted(rows, key=lambda r: r.tmux), now_iso(), cfg.history_dir)
    out(f"{old} → {new}" + ("" if live else "（tmux session 沒在執行，只改登記表）"))
    return 0


def cmd_stop(cfg, run, out, *, names=(), profile=None, force=False, dry_run=False, keep_tmux=False,
             sleep=time.sleep, timeout=90, own_pane=None, kill=os.kill, proc_root=Path("/proc")):
    from .save import cmd_save

    if not dry_run and cmd_save(cfg, run, out, quiet=True, proc_root=proc_root) == 2:
        return 2
    tmux = Tmux(run)
    try:
        panes = tmux.require_panes()
    except TmuxUnreadable as e:
        out(f"❌ {e}；不知道哪些 session 在跑，什麼都沒停")
        return 2
    procs = list_procs(run)
    found, multi = _agents(cfg, run, panes, procs, proc_root)

    def wanted(session, prof):
        return (not names or session in names) and (not profile or prof is None or prof == profile)

    busy, sent, skipped_self, unknown, worktrees = [], [], [], [], []
    multi_hit = sorted(s for s in multi if wanted(s, None))
    for pane in panes:
        if pane.session in multi:
            continue
        lc = found.get(pane.pane_id)
        if lc is None:
            if descendant_claude(procs, pane.pane_pid) is not None and wanted(pane.session, None):
                unknown.append(pane.session)
            continue
        if not wanted(pane.session, lc.profile):
            continue
        if own_pane and pane.pane_id == own_pane:
            skipped_self.append(pane.session)
            continue
        if lc.status != "idle":  # Codex's unknown (approval, trust or other menus, or an unrecognised screen) also counts as busy
            busy.append(f"{pane.session}（{lc.status}）")
            continue
        if lc.agent == "claude" and WORKTREE_MARK in lc.cwd:
            # /exit pops up a "Keep worktree / Remove worktree" confirmation, and a wrong choice deletes the commits on the branch: do not handle it automatically
            worktrees.append(pane.session)
            continue
        if dry_run:
            out(f"[dry-run] {pane.session}：送 {EXIT_TEXT[lc.agent]}")
            continue
        _quit(tmux, pane.pane_id, lc.agent, sleep)
        out(f"▶ {pane.session}：已送出 {EXIT_TEXT[lc.agent]}，等待結束…")
        sent.append((pane, lc))
    waiting = {lc.pid: (pane, lc) for pane, lc in sent}
    done, waited = [], 0
    while waiting and waited < timeout:
        sleep(2)
        waited += 2
        procs = list_procs(run)
        for pid in [p for p, (_pane, lc) in waiting.items() if not _alive(procs, lc.pid, lc.agent)]:
            done.append(waiting.pop(pid)[0].session)
            out(f"  · {done[-1]} 已結束（{waited} 秒）")
        if waiting and waited % 10 == 0:
            names = "、".join(sorted(p.session for p, _lc in waiting.values()))
            out(f"  … 還在等 {names}（{waited}／{timeout} 秒）")
    timed_out = list(waiting.values())
    closed, kept = _close_sessions(tmux, done) if done and not keep_tmux else ([], [])
    if force and timed_out:
        procs = list_procs(run)
        for pane, lc in timed_out:
            if _alive(procs, lc.pid, lc.agent) and is_descendant(procs, lc.pid, pane.pane_pid):
                kill(lc.pid, signal.SIGTERM)
                out(f"  SIGTERM → {pane.session}（pid {lc.pid}）")
            else:
                out(f"  ↷ {pane.session}：pid {lc.pid} 已不是這個 pane 的 {lc.agent}，不送 SIGTERM")
    out(f"sampan stop：已結束 {len(done)}、忙碌中略過 {len(busy)}、worktree {len(worktrees)}、"
        f"多個 agent {len(multi_hit)}、無法判斷 {len(unknown)}、逾時 {len(timed_out)}")
    for s in closed:
        out(f"  ✔ {s}：已結束，tmux session 已關閉（sampan restore {s} 可接回）")
    for s in kept:
        out(f"  ⧉ {s}：已結束；tmux 裡還有其他視窗或 pane，保留 session")
    for s in worktrees:
        out(f"  ⎇ {s}：worktree session，/exit 會問要不要保留 worktree。請手動 /exit 並選「1. Keep worktree」")
    for b in busy:
        out(f"  ⏸ {b}：忙碌中，沒動它，請自己處理或稍後再跑一次")
    for s in multi_hit:
        out(f"  ✂ {s}：同一個 tmux session 有多個 agent，沒動它；{SPLIT_HINT}")
    for s in unknown:
        out(f"  ? {s}：有 claude 但沒有 session 檔，無法判斷是否閒置，沒動它")
    for pane, _lc in timed_out:
        out(f"  ⌛ {pane.session}：{timeout} 秒內沒結束{'' if force else '，可加 --force'}")
    for s in skipped_self:
        out(f"  ↷ {s}：這是執行 sampan 的 pane（自己），跳過；完整關機請在一般終端機執行")
    return 1 if busy or unknown or timed_out or worktrees or multi_hit else 0


def _close_sessions(tmux, sessions):
    """Sessions whose agent has ended: close only if that one pane is all that is left (panes are listed again, so ones opened during the wait count); returns (closed, kept)."""
    panes = tmux.panes()
    if panes is None:  # if unreadable, close no session
        return [], list(sessions)
    closed, kept = [], []
    for s in sessions:
        if sum(p.session == s for p in panes) == 1 and tmux.kill_session(s):
            closed.append(s)
        else:
            kept.append(s)
    return closed, kept


def _shows(p, target):
    """Whether the trust prompt shows exactly target (only line wrapping is ignored, never a space inside the path)."""
    return p.trust_path is not None and p.trust_path == str(target)


def _trust_yes(p, target):
    """When the trust prompt shows exactly target, the position of the "trust" option; None for other prompts or other directories (left to the user)."""
    if not _shows(p, target):
        return None
    want = CLAUDE_TRUST_OPTION if p.agent == "claude" else CODEX_TRUST_YES
    return next((k for k, o in enumerate(p.options) if (want in o if p.agent == "claude" else o.startswith(want))),
                None)


def _stuck_message(name, agent, p, target, pressed=False):
    """Why move stopped at a prompt it will not answer, and how to get to it (CLI without a ui). pressed: sampan already answered the trust prompt earlier."""
    if p.trust_path is not None:
        want = str(target)
        if want.startswith(p.trust_path) and p.trust_path != want:  # Codex cuts a path wider than the pane, no wrapping
            what = f"信任提示的路徑被畫面寬度截斷（{p.trust_path}），無法確認是 {target}；把視窗拉寬再搬，或到視窗裡確認"
        else:
            what = f"信任提示顯示的是 {p.trust_path}，不是 {target}"
    elif not p.options:
        what = "sampan 認不出選項" + (f"：{p.detail[-1]}" if p.detail else "")
    else:
        what = p.question or "認不出問題"
    return f"❌ {name} 停在 {agent} 的提示（{what}），sampan 不替你回答，{'信任之後沒有再按任何鍵' if pressed else '沒有按任何鍵'}，請進去處理：tmux attach -t {name}"


def _shell_pane(tmux, name):
    return next((p for p in tmux.panes() or [] if p.session == name and p.command in SHELLS), None)  # unreadable → None: the caller treats it as not back at the shell, stops and sends no keys


def cmd_move(cfg, run, out, name, dest, *, dry_run=False, sleep=time.sleep, timeout=60,
             own_pane=None, proc_root=Path("/proc"), on_prompt=None):
    """Move a session to another directory and continue the same conversation: end the agent → reattach the original conversation in the new directory → automatically trust the new directory → save.

    Only a trust prompt "whose displayed path is exactly the new directory" is answered automatically, always through prompts.Relay: Claude moves to "Yes, I trust
    this folder" according to where the cursor actually is and then presses Enter, Codex sends the number of "Yes, continue". Other prompts (Codex update, hooks review, trust for another directory,
    an unrecognised menu) sampan does not answer for you: with on_prompt it is relayed to it to ask, without one (command line) no key is pressed and it stops waiting and explains.
    Nothing is touched if the session is busy, a worktree, our own pane, has several agents, or its state cannot be determined."""
    from .save import cmd_save
    rows = _load(cfg, out)
    if rows is None:
        return 2
    row = next((r for r in rows if r.tmux == name), None)
    if row is None:
        out(f"❌ 登記表沒有 {name}")
        return 2
    agent = row.agent
    if agent == "codex" and not cfg.codex.enabled:
        out(f"❌ {name}：設定檔停用了 Codex")
        return 2
    if agent == "claude" and row.profile not in cfg.profiles:
        out(f"❌ {name}：profile {row.profile} 不在設定檔")
        return 2
    target = Path(dest).expanduser().resolve()
    if target.exists() and not target.is_dir():
        out(f"❌ 不是目錄：{target}")
        return 2
    if Path(row.cwd).resolve() == target:
        out(f"= {name} 已經在 {target}")
        return 0
    tmux = Tmux(run)
    try:
        all_panes = tmux.require_panes()
    except TmuxUnreadable as e:
        out(f"❌ {e}；不動 {name}")
        return 2
    panes = [p for p in all_panes if p.session == name]
    if not panes:
        out(f"❌ {name} 目前沒有 tmux session；先 sampan restore {name}")
        return 2
    procs = list_procs(run)
    live, _w = live_by_pane(cfg, run, all_panes, procs, proc_root)
    mine = {p.pane_id for p in panes}
    found = _actionable({k: v for k, v in live.items() if k in mine})
    multi = multi_agent_sessions(procs, panes, found)
    session_of = {p.pane_id: p.session for p in all_panes}
    elsewhere = {la.session_id: session_of[k] for k, la in live.items() if k not in mine and la.session_id}
    if name in multi:
        out(f"❌ {name}：同一個 tmux session 有多個 agent，不動；{SPLIT_HINT}")
        return 1
    pane = next((p for p in panes if p.pane_id in found or descendant_claude(procs, p.pane_pid)
                 or codex_pid_in_pane(procs, p.pane_pid)), None)
    if pane is None:
        pane = next((p for p in panes if p.command in SHELLS), None)
        if pane is None:
            out(f"❌ {name}：沒有 agent 也沒有 shell pane（{', '.join(p.command for p in panes)}），不動")
            return 1
    if own_pane and pane.pane_id == own_pane:
        out(f"❌ {name} 是執行 sampan 的 pane（自己），請在別的視窗執行")
        return 1
    lc = found.get(pane.pane_id)
    proc = descendant_claude(procs, pane.pane_pid)
    exit_pid, escape = None, False
    session_id = row.session_id
    if lc is not None:
        if lc.agent != agent:
            out(f"❌ {name}：登記的是 {agent}，pane 裡跑的是 {lc.agent}；先 sampan save")
            return 1
        if lc.status != "idle":
            out(f"❌ {name} 忙碌中（{lc.status}），等它閒置再搬")
            return 1
        if agent == "claude" and WORKTREE_MARK in lc.cwd:
            out(f"❌ {name} 是 worktree session（/exit 會問要不要保留 worktree），請手動處理")
            return 1
        if lc.session_id is None:
            out(f"❌ {name}：Codex 還沒有對話（沒送出第一個 prompt），沒有可接回的，不動")
            return 1
        if agent == "claude" and lc.profile != row.profile:
            out(f"❌ {name}：登記的是 profile {row.profile}，pane 裡的 claude 是 {lc.profile}；先 sampan save")
            return 1
        session_id, exit_pid = lc.session_id, lc.pid
    elif proc is not None:
        # a claude without a session file: its state is only known when it is "stopped at the old directory's trust prompt" (Esc = do not trust and exit; no conversation is running yet)
        shown = bottom_trust_path(tmux.capture(pane.pane_id)) if agent == "claude" else None
        if shown is None or shown not in {str(x) for x in (pane.path, row.cwd)}:
            out(f"❌ {name}：有 claude 但沒有 session 檔，判斷不出狀態，不動")
            return 1
        exit_pid, escape = proc.pid, True
    elif codex_pid_in_pane(procs, pane.pane_pid) is not None:
        out(f"❌ {name}：有 codex 但認不出來（設定檔停用了 Codex？），不動")
        return 1
    bad_id = id_problem(session_id)
    if bad_id:
        out(f"❌ {name}：{bad_id}，不動")
        return 1
    where = elsewhere.get(session_id)
    if where is not None:
        out(f"❌ {name}：這個對話已在 {where} 執行，不重複開")
        return 1
    if agent == "codex":
        if find_rollout(cfg.codex.home, session_id) is None:
            out(f"❌ {name}：找不到 rollout {session_id[:8]}，搬過去會變成新對話，不動")
            return 1
    else:
        why = transcript_problem(cfg.profiles.values(), row.profile, session_id)
        if why:
            out(f"❌ {name}：{why}，不動")
            return 1
    cmd = launch_line(cfg, agent, row.profile, resume=session_id, cwd=str(target))
    if dry_run:
        made = "" if target.is_dir() else "（目錄不存在，將建立）"
        out(f"[dry-run] {name}：{f'結束 {agent} → ' if exit_pid else ''}{cmd}{made}")
        return 0
    if not target.is_dir():
        target.mkdir(parents=True)
        out(f"已建立 {target}")
    if exit_pid is not None:
        if escape:
            tmux.send_keys(pane.pane_id, "Escape")
        else:
            _quit(tmux, pane.pane_id, agent, sleep)
        waited = 0
        out(f"▶ {name}：等待 {agent} 結束…")
        while _alive(list_procs(run), exit_pid, agent):
            if waited >= timeout:
                out(f"❌ {name}：{timeout} 秒內 {agent} 沒結束，沒有在新目錄啟動")
                return 1
            sleep(1)
            waited += 1
    if _shell_pane(tmux, name) is None:
        out(f"❌ {name}：{agent} 結束後找不到 shell pane，沒有在新目錄啟動")
        return 1
    tmux.send_keys(pane.pane_id, "C-u")
    send_line(tmux, pane.pane_id, agent, cmd, sleep)
    out(f"▶ {name}：在 {target} 接回 {session_id[:8]}")
    trusted, redraw_left = False, None
    waiting = []  # filled when auto itself answered SKIP (slow redraw), so a SKIP chosen by the user is never mistaken for it

    def auto(p):  # automatically trust only a trust prompt that "shows exactly the new directory", once
        nonlocal trusted
        if trusted:
            # our keys are already sent: the same prompt still on screen is a slow redraw, never a reason to press again or to ask
            if _shows(p, target):
                waiting.append(1)
                return SKIP
            return None
        yes = _trust_yes(p, target)
        if yes is not None:
            trusted = True
            out(f"  已信任 {target}")
        return yes
    stuck = []

    def stop(_name, _agent, p):  # no ui to ask: sampan answers nothing else, and stops waiting for this session
        stuck.append(p)
        return SKIP
    relay = Relay(on_prompt or stop, tmux, out, sleep, auto=auto)
    for _ in range(timeout):
        sleep(1)
        panes_now = tmux.panes()
        if panes_now is None:  # unreadable this round: make no judgement, look again next round
            continue
        hit = _agents(cfg, run, panes_now, list_procs(run), proc_root)[0].get(pane.pane_id)
        if hit is not None and hit.agent == agent and (agent == "claude" or
                                                       (hit.status == "idle" and hit.session_id == session_id)):
            cmd_save(cfg, run, lambda _m: None, quiet=True, proc_root=proc_root)  # report only the move result
            now = next((r for r in load_rows(cfg.registry_path) if r.tmux == name), None)
            out(f"✔ {name} 已搬到 {now.cwd if now else hit.cwd}（{hit.session_id[:8]}）")
            return 0
        waiting.clear()
        res = relay.check(name, agent, pane.pane_id, tmux.capture(pane.pane_id))
        if res == SKIP and waiting:
            relay.skipped.clear()  # not a refusal: keep watching (a different prompt is still relayed)
            redraw_left = 4 if redraw_left is None else redraw_left - 1  # about 5 s in all with the settle second
            if redraw_left <= 0:
                out(f"⚠ {name}：已按下信任，但畫面還沒更新，請進去確認：tmux attach -t {name}")
                return 1
            continue
        if res is None:
            redraw_left = None  # the prompt left the screen: the redraw grace starts over
        if res == SKIP and stuck:
            out(_stuck_message(name, agent, stuck[-1], target, pressed=trusted))
            return 1
        if _relay_stop(out, name, res):
            return 1
    out(f"⚠ {name}：{timeout} 秒內沒看到 {agent} 接回，請到視窗裡看；之後 sampan save 會更新登記表")
    return 1
