"""`sampan restore`: explicitly, selectably, one by one, reattach the conversations in the registry (replaces session start).

Reattaches only the original conversation (claude --resume <id> / codex resume <id>); if it cannot be found it is skipped, never turned into a new conversation;
nothing is sent after reattaching; when it stops at a trust or other prompt, without on_prompt no key is pressed, and with on_prompt (ui, interactive terminal) the prompt is
relayed to the user to choose (prompts.Relay) and only the option the user chose is sent. The schedule must not call this (save does not import this module).
"""
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import actionlog, codex_daemon, steps
from .claude_state import transcript_problem
from .codex_state import _is_codex, find_rollout
from .observe import agent_sessions, live_by_pane
from .procs import list_procs
from .prompts import ATTACH, SKIP, Relay
from .registry import RegistryError, Row, id_problem, load_rows
from .screens import at_trust_prompt
from .status import _activity, _fmt_time, _fmt_tokens
from .tmux import SHELLS, Tmux, TmuxUnreadable


@dataclass(frozen=True)
class Candidate:
    row: Row
    last: datetime | None
    tokens: int | None
    selected: bool
    skip: str | None


def _is_agent(p):
    return (p.comm == "claude" and not p.zombie) or _is_codex(p)


def _old_agent_alive(procs, kill, pid):
    """Whether the registered agent pid is still a claude/codex process.

    pids get reused (common after a reboot), so only check whether that pid in ps is an agent; only when ps is unreadable fall back to
    os.kill(pid, 0) (another user's process → PermissionError, so it cannot be our agent)."""
    if not pid:
        return False
    if procs:
        p = procs.get(pid)
        return p is not None and _is_agent(p)
    try:
        kill(pid, 0)
    except OSError:  # ProcessLookupError, PermissionError
        return False
    return True


@dataclass
class _Live:
    panes: list
    running: set  # tmux sessions with an agent running
    by_id: dict  # running conversation id → tmux session
    found: dict  # pane id → LiveAgent
    procs: dict  # pid → Proc (empty when ps cannot be read)


def _observe(cfg, run):
    tmux = Tmux(run)
    panes = tmux.require_panes()  # no server → [] (restore creates the session itself); unreadable → TmuxUnreadable
    procs = list_procs(run)
    found, _w = live_by_pane(cfg, run, panes, procs)
    by_pane = {p.pane_id: p for p in panes}
    by_id = {la.session_id: by_pane[pid].session for pid, la in found.items() if la.session_id}
    return _Live(panes, agent_sessions(procs, panes), by_id, found, procs)


def _blocker(cfg, row, live, alive):
    """Why this row cannot be resumed now (None = it can). Rows whose session already runs an agent are filtered earlier."""
    bad_id = id_problem(row.session_id)
    if bad_id:
        return bad_id
    where = live.by_id.get(row.session_id)
    if where is not None:
        return f"這個對話已在 {where} 執行，不重複開"
    if _old_agent_alive(live.procs, alive, row.pid):
        return f"上次記錄的 {row.agent}（pid {row.pid}）還在執行，不重複開"
    session_panes = [p for p in live.panes if p.session == row.tmux]
    if session_panes and not any(p.command in SHELLS for p in session_panes):
        return f"tmux session 存在但沒有 shell pane（{', '.join(p.command for p in session_panes)}），不動"
    if not Path(row.cwd).is_dir():
        return f"目錄不存在 {row.cwd}"
    owner = cfg.owner(row.agent, row.profile)
    if owner is not None and owner.disabled:
        return f"profile {owner.name} 已停用（sampan profile enable {owner.name} 後可接回）"
    if row.agent == "codex":
        if not cfg.codex.enabled:
            return "設定檔停用了 Codex"
        if find_rollout(cfg.codex.home, row.session_id) is None:
            return f"找不到 rollout {row.session_id[:8]}，不改開新對話"
        return None
    if row.profile not in cfg.profiles:
        return f"profile {row.profile} 不在設定檔"
    return transcript_problem(cfg.profiles.values(), row.profile, row.session_id)


def candidates(cfg, run, rows, now, *, alive=os.kill):
    """Active rows whose tmux session has no agent running, with last activity, default selection and skip reason."""
    live = _observe(cfg, run)
    recent = timedelta(days=cfg.recent_days)
    result = []
    for row in rows:
        if row.state != "active" or row.tmux in live.running:
            continue
        skip = _blocker(cfg, row, live, alive)
        last, tokens, _window = _activity(cfg, row.agent, row.profile, row.session_id)
        selected = skip is None and last is not None and now - last <= recent
        result.append(Candidate(row, last, tokens, selected, skip))
    return result


def _show(out, cands):
    for i, c in enumerate(cands, 1):
        r = c.row
        mark = "x" if c.selected else (" " if c.skip is None else "-")
        out(f"  [{mark}] {i:>2}) {r.tmux}  {r.agent}  {r.profile or '-'}  {r.cwd}  "
            f"最後活動 {_fmt_time(c.last)}  context {_fmt_tokens(c.tokens)}")
        if c.skip:
            out(f"         ↷ 跳過：{c.skip}")


RESTORE_PROMPT = "Enter（或 y）接回勾選的 [x]；輸入編號改選（例如 1 3）；q 取消："


def _row_key(c):
    return c.row.tmux


def text_picker(out, ask, cands, *, key=_row_key, label=None, prompt=RESTORE_PROMPT, verb="接回"):
    """Enter (or y) accepts the default selection, typing numbers (separated by spaces or commas) changes it, q cancels; returns the list of key(c), None = cancelled.
    adopt uses this too (with its own key, label, prompt, verb)."""
    label = label or key
    try:
        raw = ask(prompt)
    except (EOFError, KeyboardInterrupt):
        return None
    if raw is None or raw.strip().lower() == "q":
        return None
    if raw.strip().lower() in ("y", "yes"):
        raw = ""
    raw = raw.replace(",", " ").split()
    if not raw:
        return [key(c) for c in cands if c.selected]
    if not all(x.isdigit() and 1 <= int(x) <= len(cands) for x in raw):
        out(f"❌ 無法辨識的選擇：{' '.join(raw)}（只能輸入 1-{len(cands)}）")
        return None
    picked = []
    for x in raw:
        c = cands[int(x) - 1]
        if c.skip:
            out(f"⚠ {label(c)} 無法{verb}（{c.skip}），略過")
        elif key(c) not in picked:
            picked.append(key(c))
    return picked


def _launch(cfg, tmux, row, live, out, sleep, dry_run):
    """Start one row. Returns the pane id it was sent to; None on dry-run or when tmux new-session fails."""
    pane = next((p for p in live.panes if p.session == row.tmux and p.command in SHELLS), None)
    cwd = row.cwd if pane else None  # a new session starts in row.cwd (new-session -c)
    if dry_run:
        cmd = steps.launch_cmd(cfg, row, cwd=cwd)
        out(f"[dry-run] {row.tmux}：{'送進既有 shell pane' if pane else '建立 tmux session'} → {cmd}")
        return None
    if pane is not None:
        target = pane.pane_id
    else:
        target = tmux.new_session(row.tmux, row.cwd)
        if target is None:
            return None
    steps.launch(cfg, tmux, target, row, cwd=cwd, sleep=sleep, clear=pane is not None)
    out(f"▶ {row.tmux}（{row.agent}{'：' + row.profile if row.profile else ''}，{row.session_id[:8]}）")
    return target


def _watch(cfg, run, launched, timeout, sleep, out=lambda _l: None, relay=None):
    """Watch launched panes for up to `timeout` seconds. Without a relay nothing is pressed; with one, a startup
    menu at the bottom of a pane is handed to relay.check (the user picks; answering does not use up the timeout).

    Returns (resumed, at_trust, unconfirmed) as lists of row names; sessions the user skipped or went into are in
    relay.skipped/relay.attach (not in unconfirmed). After an attach the others are no longer watched."""
    tmux = Tmux(run)
    pending = dict(launched)  # pane id → Row
    resumed, at_trust = [], []
    memo, i = {}, 0

    def snapshot(*a, **k):  # the daemon is read at most every 5th round (rounds are ~1 s apart)
        if i % 5 == 0 or "snap" not in memo:
            memo["snap"] = codex_daemon.snapshot(*a, **k)
        return memo["snap"]
    for i in range(timeout + 1):
        panes = tmux.panes()  # unreadable only affects the "reattached" judgement (counted as unconfirmed) and causes nothing extra to be done
        found, _w = live_by_pane(cfg, run, panes if panes is not None else [], list_procs(run), snapshot_fn=snapshot)
        for pane_id, row in list(pending.items()):
            screen = tmux.capture(pane_id)
            if relay is not None:
                res = relay.check(row.tmux, row.agent, pane_id, screen)
                if res in (SKIP, ATTACH):
                    del pending[pane_id]
                    if res == ATTACH:
                        if pending:
                            out(f"  → 進去 {row.tmux}；其他 session 已送出接回指令，不再等待")
                        return resumed, at_trust, [r.tmux for r in pending.values()]
                    continue
                if res is not None:
                    continue  # just answered: check next round whether it reattached
            elif at_trust_prompt(screen, row.agent):
                at_trust.append(row.tmux)
                del pending[pane_id]
                out(f"  · {row.tmux} 停在信任提示")
                continue
            la = found.get(pane_id)
            if la is not None and la.session_id == row.session_id and la.status != "unknown":
                resumed.append(row.tmux)
                del pending[pane_id]
                out(f"  · {row.tmux} 已接回")
        if not pending or i == timeout:
            break
        sleep(1)
    return resumed, at_trust, [row.tmux for row in pending.values()]


def cmd_restore(cfg, run, out, ask, *, names=(), select_all=False, dry_run=False, interval=3, sleep=time.sleep,
                picker_fn=None, now_fn=lambda: datetime.now(timezone.utc), timeout=10, alive=os.kill,
                confirm=True, opened=None, on_prompt=None, wait=True):
    """confirm=False: do not ask, reattach the default-selected ones directly (used when the ui presses Enter on a single stopped session).
    wait=False: finish once the reattach command is sent, without waiting or looking at the screen (when the ui reattaches one with Enter, it then goes straight into tmux and the user handles any menu).
    Unless a launched row gets the Remote Control reset (steps.rc_wanted): then it waits as with wait=True, so the ui
    goes in only after the reset.
    opened: given a list, append the names of tmux sessions created / sent the reattach command, in order (the ui then attaches to the first).
    on_prompt(name, agent, prompt): asked when an agent stops at a startup menu during the wait (see prompts.Relay); None = press no keys."""
    try:
        rows = load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)
    except RegistryError as e:
        out(f"❌ {e}")
        return 2
    known = {r.tmux for r in rows}
    for n in names:
        if n not in known:
            out(f"⚠ 登記表沒有 {n}")
    retired = []
    if names:
        rows = [r for r in rows if r.tmux in names]
        retired = [r.tmux for r in rows if r.state == "retired"]
        for n in retired:
            out(f"{n} 是 retired；先 sampan activate {n}")
    try:
        cands = candidates(cfg, run, rows, now_fn(), alive=alive)
    except TmuxUnreadable as e:
        out(f"❌ {e}；不知道哪些 session 還在，什麼都沒建、沒接回")
        return 1
    if names or select_all:
        cands = [Candidate(c.row, c.last, c.tokens, c.skip is None, c.skip) for c in cands]
    if not cands:
        if not retired:
            out("沒有要接回的 session（登記表裡 active 的都已在執行）")
        return 0
    out(f"可接回的 session（[x] = 預設勾選：{cfg.recent_days} 天內有活動）：")
    _show(out, cands)
    if dry_run or not confirm or all(c.skip for c in cands):
        picked = [c.row.tmux for c in cands if c.selected]  # no reattachable rows, nothing to ask
    else:
        picked = picker_fn(cands) if picker_fn is not None else text_picker(out, ask, cands)
        if picked is None:
            out("已取消，什麼都沒做")
            return 0
    if not dry_run and picked:  # from here on what happens is logged (a restart's own label stays when it runs this)
        out = actionlog.tee(cfg, "接回", out)
        on_prompt = actionlog.logged_prompt(cfg, "接回", on_prompt)
        out(f"接回：{'、'.join(dict.fromkeys(picked))}")
    by_name = {c.row.tmux: c for c in cands}
    tmux = Tmux(run)
    skipped = [(c.row.tmux, c.skip) for c in cands if c.skip]
    launched = {}
    # an agent just launched has no process/session file yet, so re-observing cannot see it: ids started in this run are tracked separately
    started_ids = {}
    for name in dict.fromkeys(picked):  # the result of picker_fn may contain duplicates
        c = by_name.get(name)
        if c is None or c.skip:
            continue  # not a candidate, or already listed in skipped
        if c.row.session_id in started_ids:
            skipped.append((name, f"同一個對話 {c.row.session_id[:8]} 本次已在 {started_ids[c.row.session_id]} 接回，不重複開"))
            continue
        if launched and not dry_run:
            sleep(interval)
        # check once more before starting (after the wait): if the user opened it themselves during confirmation, it must not be opened twice
        try:
            live = _observe(cfg, run)
        except TmuxUnreadable as e:  # cannot tell whether it is already running: skip it, press nothing
            skipped.append((name, f"{e}；沒有開"))
            continue
        if name in live.running:
            skipped.append((name, "tmux session 裡已有 agent 在跑"))
            continue
        why = _blocker(cfg, c.row, live, alive)
        if why:
            skipped.append((name, why))
            continue
        pane = _launch(cfg, tmux, c.row, live, out, sleep, dry_run)
        if pane is not None:
            launched[pane] = c.row
            if opened is not None:
                opened.append(name)
        if pane is not None or dry_run:
            started_ids[c.row.session_id] = name
        elif not dry_run:
            skipped.append((name, "tmux new-session 失敗"))
    if dry_run:
        return 0
    if launched and not wait and not any(steps.rc_wanted(cfg, r) for r in launched.values()):
        out(f"sampan restore：已送出接回指令 {len(launched)}、跳過 {len(skipped)}")
        for name, why in skipped:
            out(f"  ↷ {name}：{why}")
        return 1 if skipped else 0
    if launched:
        out(f"… 等待接回（最多 {timeout} 秒）")
    relay = Relay(on_prompt, tmux, out, sleep) if on_prompt is not None and launched else None
    resumed, at_trust, unconfirmed = (_watch(cfg, run, launched, timeout, sleep, out, relay) if launched
                                      else ([], [], []))
    at_prompt = (relay.skipped + ([relay.attach] if relay.attach else [])) if relay is not None else []
    out(f"sampan restore：接回 {len(resumed)}、跳過 {len(skipped)}、停在信任提示 {len(at_trust)}、"
        f"未確認 {len(unconfirmed)}" + (f"、停在提示 {len(at_prompt)}" if at_prompt else ""))
    for name in resumed:
        out(f"  ✔ {name}")
    for name, why in skipped:
        out(f"  ↷ {name}：{why}")
    for name in at_trust:
        out(f"  ⚠ {name}：停在信任提示，請到視窗確認（sampan 不會替你按）")
    for name in unconfirmed:
        out(f"  ⚠ {name}：停在提示或尚未接回，請到視窗確認")
    for name in at_prompt:
        out(f"  ⚠ {name} 停在提示，進去處理")
    # one after another, after the watch: a slow reset must not use up the other sessions' watch time
    pane_of = {row.tmux: pane for pane, row in launched.items()}
    rows_of = {row.tmux: row for row in launched.values()}
    for name in resumed:
        steps.rc_step(cfg, tmux, rows_of[name], pane_of[name], out, sleep=sleep)
    return 1 if skipped or at_trust or unconfirmed or at_prompt else 0
