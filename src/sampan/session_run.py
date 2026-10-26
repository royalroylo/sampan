import os
import re
import signal
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from . import actionlog
from .claude_state import move_transcript, transcript_carry, transcript_problem
from .clock import now_iso
from .codex_state import _is_codex, codex_pid_in_pane, find_rollout
from .dispatch import outbox_path
from .launch import disabled_problem, launch_line, send_line
from .observe import PROC_ROOT, SPLIT_HINT, live_by_pane, multi_agent_sessions
from .procs import descendant_claude, is_descendant, list_procs
from .prompts import ATTACH, SKIP, Relay
from .registry import STATES, RegistryError, Row, id_problem, load_rows, lock, set_state, write_rows
from .screens import CLAUDE_TRUST_OPTION, CODEX_TRUST_YES, bottom_trust_path, codex_activity
from .send import TERMINAL, submit_new
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
        return load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)
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


def _with_message(cfg, out, agent, profile, session_id, message, caller, name, cmd):
    """The launch command to send: with a message, record its dispatch and add the startup prompt. None = failed."""
    if message is None:
        return cmd
    d = submit_new(cfg, caller or TERMINAL, name, agent, profile or cfg.codex.profile, message,
                   notify=caller is not None, now=datetime.now(timezone.utc))
    if isinstance(d, str):
        out(f"❌ {d}")
        return None
    return launch_line(cfg, agent, profile, session_id=session_id, prompt_file=outbox_path(cfg.state_dir, d.id))


def cmd_new(cfg, run, out, name, target, cwd, *, dry_run=False, sleep=time.sleep, timeout=15, uuid4=uuid.uuid4,
            on_prompt=None, wait=True, message=None, caller=None):
    """target: claude:<profile>, <profile> (taken as claude) or codex.

    wait=False: return as soon as the launch command is sent (the ui's n then goes straight into tmux and the user answers the startup menu there). A Claude row is
    registered before launching; Codex only has a conversation once the first prompt is sent, and sampan save registers it afterwards.

    on_prompt(name, agent, prompt): asked when the agent stops at a menu on startup (trust, update, hooks review) (prompts.Relay);
    None (plain non-interactive CLI, tests) = do not look at the screen, press no keys. Time spent answering prompts does not count toward timeout.

    message: the first message, given to the agent as its startup prompt (`claude "$(cat <outbox>)"`), recorded as a
    `sent` dispatch (notify only when caller is a sampan session). Without it nothing changes."""
    if message is not None and not message.strip():
        out("❌ 訊息是空的")
        return 2
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
        if disabled_problem(cfg, profile):
            out(disabled_problem(cfg, profile))
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
    cmd = launch_line(cfg, agent, profile, session_id=session_id,
                      prompt_file=outbox_path(cfg.state_dir, "<派工編號>") if message is not None else None)
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
        cmd = _with_message(cfg, out, agent, profile, session_id, message, caller, name, cmd)
        if cmd is None:
            return 2
        send_line(tmux, pane, agent, cmd, sleep)
        out(f"▶ {name}（{profile}，{session_id[:8]}）已登記")
    else:
        cmd = _with_message(cfg, out, agent, profile, session_id, message, caller, name, cmd)
        if cmd is None:
            return 2
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
            row = Row(name, "codex", cfg.codex.profile, la.session_id, cwd, "active", now_iso(), tmux.server_id() or "", la.pid)
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


def cmd_forget(cfg, run, out, name, *, dry_run=False):
    """Remove a row from the registry. Only for a session that is not in tmux; the transcript, the rollout and
    the tmux server are never touched, and the previous registry stays in history/."""
    if not dry_run:
        out = actionlog.tee(cfg, f"forget {name}", out)
    with lock(cfg.state_dir):
        rows = _load(cfg, out)
        if rows is None:
            return 2
        row = next((r for r in rows if r.tmux == name), None)
        if row is None:
            out(f"❌ 登記表沒有 {name}")
            return 2
        tmux = Tmux(run)
        try:
            tmux.require_panes()
        except TmuxUnreadable as e:
            out(f"❌ {e}；不知道 {name} 還在不在 tmux 裡，什麼都沒移除")
            return 2
        if tmux.has_session(name):
            out(f"❌ {name} 還在 tmux 裡；先 sampan stop {name}（只剩 shell 的話：tmux kill-session -t ={name}）")
            return 1
        if dry_run:
            out(f"（dry-run）會從登記表移除 {name}，什麼都沒改")
        else:
            write_rows(cfg.registry_path, [r for r in rows if r is not row], now_iso(), cfg.history_dir)
            out(f"✔ 已從登記表移除 {name}")
    try:
        out(f"  對話還在：{launch_line(cfg, row.agent, row.profile, resume=row.session_id, cwd=row.cwd)}")
    except (ValueError, KeyError):
        out(f"  對話 id {row.session_id}")
    return 0


def cmd_stop(cfg, run, out, *, names=(), profile=None, force=False, dry_run=False, keep_tmux=False,
             sleep=time.sleep, timeout=90, own_pane=None, kill=os.kill, proc_root=Path("/proc"),
             stopped=None, left=None, why=None):
    """stopped / left: when given lists, receive the names of sessions that ended, and of wanted sessions that were not stopped.
    why: when given a dict, receives {name: reason} for each one in left (shown next to the name by a restart)."""
    from .save import cmd_save

    # save's lines are held back until we know tmux is readable: when it is not, save has said so already and
    # the stop message below says it again, so only that one is shown
    save_lines = []
    save_rc = 0 if dry_run else cmd_save(cfg, run, save_lines.append, quiet=True, proc_root=proc_root)
    if save_rc == 2:
        for line in save_lines:
            out(line)
        return 2
    tmux = Tmux(run)
    try:
        panes = tmux.require_panes()
    except TmuxUnreadable as e:
        if save_rc == 0:  # save could read tmux and may have warnings worth keeping; never the unreadable line twice
            for line in save_lines:
                if str(e) not in line:
                    out(line)
        out(f"❌ {e}；不知道哪些 session 在跑，什麼都沒停")
        return 2
    for line in save_lines:
        out(line)
    procs = list_procs(run)
    found, multi = _agents(cfg, run, panes, procs, proc_root)

    def wanted(session, prof):
        return (not names or session in names) and (not profile or prof is None or prof == profile)

    busy, busy_names, sent, skipped_self, unknown, worktrees, blind = [], [], [], [], [], [], []
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
            busy_names.append(pane.session)
            continue
        if _in_worktree(lc):
            # already at the question (an earlier stop): /exit + Enter would confirm the highlighted option, and
            # "Remove worktree" deletes the branch's commits; a screen we can't read gets no keys either
            screen = tmux.capture(pane.pane_id)
            if not screen.strip():
                blind.append(pane.session)
                continue
            if _worktree_question(screen):
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
        for pid, (pane, lc) in list(waiting.items()):
            # a session that created its worktree in this run asks Keep / Remove worktree on /exit, and a wrong
            # choice deletes the branch's commits: never answer it, leave it at the question for the user
            if _in_worktree(lc) and _worktree_question(tmux.capture(pane.pane_id)):
                worktrees.append(waiting.pop(pid)[0].session)
        if waiting and waited % 10 == 0:
            still = "、".join(sorted(p.session for p, _lc in waiting.values()))
            out(f"  … 還在等 {still}（{waited}／{timeout} 秒）")
    for pid, (pane, lc) in list(waiting.items()):  # the question may have come up after the last look
        if _in_worktree(lc) and _worktree_question(tmux.capture(pane.pane_id)):
            worktrees.append(waiting.pop(pid)[0].session)
    timed_out = list(waiting.values())
    if stopped is not None:
        stopped.extend(done)
    if left is not None:
        left.extend(busy_names + worktrees + blind + multi_hit + unknown + skipped_self
                    + [p.session for p, _lc in timed_out])
    if why is not None:
        why.update({s: "忙碌中" for s in busy_names} | {s: "在問要不要保留 worktree" for s in worktrees}
                   | {s: "同一個 tmux session 有多個 agent" for s in multi_hit} | {s: "無法判斷是否閒置" for s in unknown}
                   | {s: "sampan 自己所在的 pane" for s in skipped_self} | {s: "worktree，讀不到畫面" for s in blind}
                   | {p.session: f"{timeout} 秒內沒結束" for p, _lc in timed_out})
    closed, kept = _close_sessions(tmux, done) if done and not keep_tmux else ([], [])
    if force and timed_out:
        procs = list_procs(run)
        for pane, lc in timed_out:
            if _in_worktree(lc):  # it may be at the worktree question by now; what SIGTERM does there is unknown
                out(f"  ↷ {pane.session}：worktree session，不送 SIGTERM；請進去看看")
            elif _alive(procs, lc.pid, lc.agent) and is_descendant(procs, lc.pid, pane.pane_pid):
                kill(lc.pid, signal.SIGTERM)
                out(f"  SIGTERM → {pane.session}（pid {lc.pid}）")
            else:
                out(f"  ↷ {pane.session}：pid {lc.pid} 已不是這個 pane 的 {lc.agent}，不送 SIGTERM")
    out(f"sampan stop：已結束 {len(done)}、忙碌中略過 {len(busy)}、worktree {len(worktrees) + len(blind)}、"
        f"多個 agent {len(multi_hit)}、無法判斷 {len(unknown)}、逾時 {len(timed_out)}")
    for s in closed:
        out(f"  ✔ {s}：已結束，tmux session 已關閉（sampan restore {s} 可接回）")
    for s in kept:
        out(f"  ⧉ {s}：已結束；tmux 裡還有其他視窗或 pane，保留 session")
    for s in worktrees:
        out(f"  ⎇ {s}：在問要不要保留 worktree，sampan 沒有替你選；請進去選「Keep worktree」，再 sampan restore {s}")
    for s in blind:
        out(f"  ⎇ {s}：worktree session，讀不到畫面，沒送 /exit；請自己進去 /exit")
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
    return 1 if busy or unknown or timed_out or worktrees or blind or multi_hit else 0


def _in_worktree(lc):
    return lc.agent == "claude" and WORKTREE_MARK in lc.cwd


_KEEP_RE = re.compile(r"^\W*\d+\.\s+Keep worktree")
_REMOVE_RE = re.compile(r"^\W*\d+\.\s+Remove worktree")


def _worktree_question(screen):
    """Whether the bottom of the pane shows Claude's Keep / Remove worktree menu (asked on /exit of a worktree that
    run created). Only numbered option lines near the bottom count, so the words in the scrollback don't."""
    tail = [line.strip() for line in screen.splitlines() if line.strip()][-12:]
    return any(_KEEP_RE.match(x) for x in tail) and any(_REMOVE_RE.match(x) for x in tail)


def _close_sessions(tmux, sessions):
    """Sessions whose agent has ended: close only if that one pane is all that is left (panes are listed again, so ones opened during the wait count); returns (closed, kept)."""
    panes = tmux.panes()
    if panes is None:  # if unreadable, close no session
        return [], list(sessions)
    closed, kept = [], []
    for s in sessions:
        left = sum(p.session == s for p in panes)
        if left == 0 or (left == 1 and tmux.kill_session(s)):  # 0: the agent was the pane's own process, the session closed with it
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


def _shell_pane(tmux, name, pane_id):
    """pane_id, if it is still in session name and back at a shell; else None."""
    return next((p for p in tmux.panes() or [] if p.session == name and p.pane_id == pane_id and p.command in SHELLS),
                None)  # unreadable → None: the caller treats it as not back at the shell, stops and sends no keys


def cmd_move(cfg, run, out, name, dest, *, dry_run=False, sleep=time.sleep, timeout=60,
             own_pane=None, proc_root=Path("/proc"), on_prompt=None, profile=None):
    """Move a session to another directory and continue the same conversation: end the agent → reattach the original conversation in the new directory → automatically trust the new directory → save.

    Only a trust prompt "whose displayed path is exactly the new directory" is answered automatically, always through prompts.Relay: Claude moves to "Yes, I trust
    this folder" according to where the cursor actually is and then presses Enter, Codex sends the number of "Yes, continue". Other prompts (Codex update, hooks review, trust for another directory,
    an unrecognised menu) sampan does not answer for you: with on_prompt it is relayed to it to ask, without one (command line) no key is pressed and it stops waiting and explains.
    Nothing is touched if the session is busy, a worktree, our own pane, has several agents, or its state cannot be determined.
    profile (Claude only, used by cmd_switch): reattach under this profile instead of the registered one; a transcript the new
    profile cannot see is moved to it (never copied) after the agent has exited."""
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
    new_prof = profile or row.profile
    switching = new_prof != row.profile
    where_to = f"用 {new_prof} " if switching else "在新目錄"
    # a switch stays in the recorded directory, spelled as recorded: Claude finds the transcript by the cwd's path
    target = Path(dest).expanduser() if switching else Path(dest).expanduser().resolve()
    if target.exists() and not target.is_dir():
        out(f"❌ 不是目錄：{target}")
        return 2
    if Path(row.cwd).resolve() == target and not switching:
        out(f"= {name} 已經在 {target}")
        return 0
    if switching and not target.is_dir():
        out(f"❌ {name}：目錄不存在 {target}，不換")
        return 2
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
    root = procs.get(pane.pane_pid)
    if root is not None and (root.comm == "claude" or _is_codex(root)):
        # started as `tmux new-session … claude`: no shell behind the agent, so the pane closes when it exits
        out(f"❌ {name}：agent 是這個 pane 直接啟動的（後面沒有 shell），結束後 pane 就關了，沒辦法在原地換目錄；"
            f"先 sampan stop {name}、sampan restore {name}（會開一個有 shell 的 session），再搬")
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
            out(f"❌ {name} 忙碌中（{lc.status}），等它閒置再{'換' if switching else '搬'}")
            return 1
        if agent == "claude" and WORKTREE_MARK in lc.cwd:
            out(f"❌ {name} 是 worktree session（/exit 可能會問要不要保留 worktree），請手動 /exit 後再處理")
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
    carry = None
    if agent == "claude":
        if switching:
            carry, why = transcript_carry(cfg.profiles.values(), row.profile, new_prof, session_id)
        else:
            why = transcript_problem(cfg.profiles.values(), row.profile, session_id)
        if why:
            out(f"❌ {name}：{why}，不動")
            return 1
    cmd = launch_line(cfg, agent, new_prof, resume=session_id, cwd=str(target))
    if dry_run:
        made = "" if target.is_dir() else "（目錄不存在，將建立）"
        moved = f"搬逐字稿到 {new_prof} → " if carry else ""
        out(f"[dry-run] {name}：{f'結束 {agent} → ' if exit_pid else ''}{moved}{cmd}{made}")
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
                out(f"❌ {name}：{timeout} 秒內 {agent} 沒結束，沒有{where_to}啟動")
                if switching:
                    out(f"  可能停在額度或其他選單：請進去按 Esc 再打 /exit，claude 結束後再按一次 P（或 sampan switch {name} {new_prof}）")
                return 1
            sleep(1)
            waited += 1
    if _shell_pane(tmux, name, pane.pane_id) is None:
        out(f"❌ {name}：{agent} 結束後找不到 shell pane，沒有{where_to}啟動")
        return 1
    if carry is not None:
        try:
            move_transcript(carry, cfg.profiles[new_prof].projects_dir)
        except OSError as e:
            out(f"❌ {name}：逐字稿搬不過去（{e}），沒有{where_to}啟動；對話還在 {row.profile}，sampan restore {name} 可接回")
            return 1
        out(f"  已把逐字稿搬到 {new_prof}")
    if switching and not _set_profile(cfg, name, session_id, new_prof):
        # registry and transcript must agree before anything is launched: if the relaunch then fails (e.g. the new
        # account is not logged in), restore and switch still find the conversation under the new profile
        out(f"⚠ {name}：登記表沒能改成 {new_prof}；之後 sampan save 會依實際狀態更新")
    tmux.send_keys(pane.pane_id, "C-u")
    send_line(tmux, pane.pane_id, agent, cmd, sleep)
    out(f"▶ {name}：{f'用 {new_prof}' if switching else f'在 {target}'} 接回 {session_id[:8]}")
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
        if hit is not None and hit.agent == agent and ((agent == "claude" and not switching) or
                                                       hit.session_id == session_id and
                                                       (agent == "claude" or hit.status == "idle")):
            cmd_save(cfg, run, lambda _m: None, quiet=True, proc_root=proc_root)  # report only the move result
            now = next((r for r in load_rows(cfg.registry_path, codex_profile=cfg.codex.profile) if r.tmux == name), None)
            if switching:
                out(f"✔ {name} 已換到 {now.profile if now else new_prof}（{hit.session_id[:8]}）")
            else:
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


def _set_profile(cfg, name, session_id, profile):
    """Record profile on the registry row of name, only while it still holds session_id; returns whether it did."""
    with lock(cfg.state_dir):
        try:
            rows = load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)
        except RegistryError:
            return False
        row = next((r for r in rows if r.tmux == name and r.session_id == session_id), None)
        if row is None:
            return False
        if row.profile != profile:
            row.profile = profile
            write_rows(cfg.registry_path, rows, now_iso(), cfg.history_dir)
        return True


def plugin_gap(cfg, old, new):
    """Plugins listed for profile old that profile new does not list (they will not be there after a switch)."""
    have = set(cfg.profiles[new].plugins)
    return [pid for pid in cfg.profiles[old].plugins if pid not in have]


def gap_line(cfg, old, new):
    gap = plugin_gap(cfg, old, new)
    return f"⚠ {new} 沒有裝：{'、'.join(gap)}（換過去後這些 plugin 用不到）" if gap else None


def cmd_switch(cfg, run, out, name, profile, *, dry_run=False, own_pane=None, on_prompt=None, warn_gap=True,
               sleep=time.sleep, timeout=60, proc_root=Path("/proc"), restore_fn=None):
    """Continue a Claude session's conversation under another profile (another account, e.g. when one has used up its
    quota). A running session goes through cmd_move's flow in its own directory: /exit → relaunch with the new
    CLAUDE_CONFIG_DIR → trust only that directory → save. A stopped one gets its registry row changed and is restored
    under the new profile right away. A transcript the new profile cannot see is moved to it, never copied, so there is
    only ever one copy of the conversation."""
    if not dry_run:
        out = actionlog.tee(cfg, f"switch {name} → {profile}", out)
        on_prompt = actionlog.logged_prompt(cfg, f"switch {name} → {profile}", on_prompt)
    rows = _load(cfg, out)
    if rows is None:
        return 2
    row = next((r for r in rows if r.tmux == name), None)
    if row is None:
        out(f"❌ 登記表沒有 {name}")
        return 2
    if row.agent != "claude":
        out(f"❌ {name} 是 {row.agent}，只有 Claude session 能換 profile")
        return 2
    if profile not in cfg.profiles:
        out(f"❌ 未知的 profile：{profile}（設定檔裡有：{'、'.join(cfg.profiles)}）")
        return 2
    if profile == row.profile:
        out(f"= {name} 已經是 {profile}")
        return 0
    if row.profile not in cfg.profiles:
        out(f"❌ {name}：profile {row.profile} 不在設定檔")
        return 2
    gap = gap_line(cfg, row.profile, profile) if warn_gap else None
    if gap:
        out(gap)
    try:
        panes = Tmux(run).require_panes()
    except TmuxUnreadable as e:
        out(f"❌ {e}；不動 {name}")
        return 2
    if any(p.session == name for p in panes):
        return cmd_move(cfg, run, out, name, row.cwd, profile=profile, dry_run=dry_run, sleep=sleep, timeout=timeout,
                        own_pane=own_pane, proc_root=proc_root, on_prompt=on_prompt)
    # stopped: no agent to end; change the registry row, then reattach under the new profile
    if row.state != "active":
        out(f"❌ {name} 是 {row.state}；要接回請先 sampan activate {name}")
        return 2
    if not Path(row.cwd).is_dir():
        out(f"❌ {name}：目錄不存在 {row.cwd}，不換")
        return 2
    from .restore import _observe, _old_agent_alive
    try:
        live = _observe(cfg, run)
    except TmuxUnreadable as e:
        out(f"❌ {e}；不動 {name}")
        return 2
    where = live.by_id.get(row.session_id)
    if where is not None or _old_agent_alive(live.procs, os.kill, row.pid):
        out(f"❌ {name}：這個對話{f'已在 {where} 執行' if where else '上次記錄的 claude 還在執行'}，不換（避免兩邊同時寫）")
        return 1
    key = (row.session_id, row.cwd, row.profile, row.state)
    _carry, why = transcript_carry(cfg.profiles.values(), row.profile, profile, row.session_id)
    if why:
        out(f"❌ {name}：{why}，不動")
        return 1
    if dry_run:
        moved = f"搬逐字稿到 {profile}、" if _carry else ""
        out(f"[dry-run] {name}（已停止）：{moved}登記表改成 {profile}，再用 {profile} 接回")
        return 0
    with lock(cfg.state_dir):
        rows = _load(cfg, out)
        if rows is None:
            return 2
        row = next((r for r in rows if r.tmux == name), None)
        if row is None or (row.session_id, row.cwd, row.profile, row.state) != key:
            out(f"❌ {name} 的登記剛被改過，什麼都沒動；再試一次")
            return 1
        carry, why = transcript_carry(cfg.profiles.values(), row.profile, profile, row.session_id)  # again, under the lock
        if why:
            out(f"❌ {name}：{why}，不動")
            return 1
        if carry is not None:
            try:
                move_transcript(carry, cfg.profiles[profile].projects_dir)
            except OSError as e:
                out(f"❌ {name}：逐字稿搬不過去（{e}），什麼都沒改")
                return 1
            out(f"  已把逐字稿搬到 {profile}")
        row.profile = profile
        write_rows(cfg.registry_path, rows, now_iso(), cfg.history_dir)
    out(f"✔ {name}：登記表改成 {profile}，接回中…")
    if restore_fn is None:
        from .restore import cmd_restore as restore_fn
    return restore_fn(cfg, run, out, lambda _p: "", names=(name,), confirm=False, on_prompt=on_prompt)
