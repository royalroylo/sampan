"""`sampan adopt`: take over claude/codex running outside tmux (e.g. in an iTerm or Terminal tab).

A running process cannot be moved into tmux, so "adopt" = the user runs /exit (Codex: /quit) in the original window themselves, sampan waits
for that process to exit, then reattaches the same conversation in a new tmux session (claude --resume <id> / codex resume <id> -C <cwd>) and registers it.
sampan sends no keys or signals to the outside process; rows whose conversation id or profile cannot be determined are only listed, not selectable, and never become a new conversation.
"""
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import claude_state, codex_state
from .claude_state import find_transcript, read_session_files, transcript_problem, transcript_stats
from .clock import now_iso
from .codex_state import (
    AMBIGUOUS,
    LSOF,
    _started_at,
    codex_pid_in_pane,
    find_rollout,
    rollout_from_lsof,
    rollout_last_time,
    threads_fallback,
)
from .hook import read_hook_records
from .launch import launch_line, send_line
from .observe import PROC_ROOT, profile_for_dir
from .procs import claude_pids, list_procs, proc_env
from .registry import Row, id_problem
from .restore import _is_agent, _observe, _watch, text_picker
from .session_run import EXIT_TEXT, _alive, _load, _register, name_problem
from .status import _parse_ts
from .tmux import Tmux, TmuxUnreadable

PROMPT = "Enter（或 y）接手勾選的 [x]；輸入編號改選（例如 1 3）；q 取消："
SNIPPET = 30  # how many characters at most of the quoted message snippet


@dataclass(frozen=True)
class Outside:
    key: str  # identifier used by the list (outermost pid)
    agent: str  # "claude" | "codex"
    pid: int  # outermost agent process
    pids: tuple  # processes that must all exit (codex: the node wrapper and the native executable)
    profile: str  # "" for codex
    session_id: object  # str | None
    cwd: str
    where: str  # cwd, home directory abbreviated to ~/
    last: object  # datetime | None
    ago: str  # "3 分鐘前" ("3 minutes ago")
    text: str  # beginning of the last user message
    selected: bool
    skip: object  # str | None: why it cannot be adopted

    @property
    def quote(self):
        return f"「{self.text}」" if self.text else ""


def _ancestors(procs, pid):
    seen, cur = set(), procs.get(pid)
    while cur is not None and cur.ppid not in seen and cur.ppid in procs:
        seen.add(cur.ppid)
        yield cur.ppid
        cur = procs.get(cur.ppid)


def _top_outside(procs, panes):
    """claude/codex processes that are neither inside a tmux pane nor under another agent (subagents, the codex binary
    under its node wrapper), and are in the foreground of a terminal (ps stat "+"): one started in the background by
    another program (e.g. codex app-server) has no window where the user could type /exit."""
    agents = {p.pid for p in procs.values() if _is_agent(p)}
    pane_pids = {p.pane_pid for p in panes}
    top = []
    for pid in sorted(agents):
        up = set(_ancestors(procs, pid))
        if pid in pane_pids or up & pane_pids or up & agents or "+" not in procs[pid].stat:
            continue
        top.append(procs[pid])
    return top


def proc_cwd(run, pid):
    if LSOF is None:
        return None
    r = run([LSOF, "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
    if r.returncode != 0:
        return None
    return next((line[1:] for line in r.stdout.splitlines() if line.startswith("n")), None)


def _short(cfg, path):
    home = str(cfg.home)
    if path == home:
        return "~"
    return "~" + path[len(home):] if path.startswith(home + "/") else path


def _ago(last, now):
    if last is None:
        return "-"
    secs = (now - last).total_seconds()
    if secs < 60:
        return "剛剛"
    if secs < 3600:
        return f"{int(secs // 60)} 分鐘前"
    if secs < 86400:
        return f"{int(secs // 3600)} 小時前"
    return f"{int(secs // 86400)} 天前"


def _snippet(text):
    text = " ".join(text.split())
    return text if len(text) <= SNIPPET else text[:SNIPPET] + "…"


def _claude_ident(cfg, run, pid, procs, proc_root):
    """(session_id, profile, cwd, skip) of a claude outside tmux: SessionStart hook record, else its session file.

    Never guessed from --resume arguments: after /resume or /clear in the window they point at another conversation."""
    alive = claude_pids(procs)
    rec = read_hook_records(cfg.state_dir, alive).get(pid)
    files = {lc.pid: lc for lc in read_session_files(cfg.profiles.values(), alive)[0]}
    lc = files.get(pid)
    if rec is not None:
        prof = profile_for_dir(cfg, rec.config_dir)
        if prof is not None:
            return rec.session_id, prof, (lc.cwd if lc else rec.cwd), None
    if lc is not None:
        return lc.session_id, lc.profile, lc.cwd, None
    env = proc_env(run, pid, proc_root)
    if env is None:
        return None, None, None, "沒有 session 檔，也讀不到行程的環境變數，判斷不出對話 id"
    config_dir = env.get("CLAUDE_CONFIG_DIR")
    prof = profile_for_dir(cfg, config_dir)
    if prof is None:
        return None, None, None, f"CLAUDE_CONFIG_DIR {config_dir} 不在設定檔（profile 不明）"
    return None, prof, None, "沒有 session 檔，判斷不出對話 id"


def _claude_row(cfg, run, p, procs, proc_root):
    sid, prof, cwd, skip = _claude_ident(cfg, run, p.pid, procs, proc_root)
    cwd = cwd or proc_cwd(run, p.pid) or "?"
    last = text = None
    if skip is None:
        skip = id_problem(sid) or transcript_problem(cfg.profiles.values(), prof, sid)
    if skip is None:
        path = find_transcript(cfg.profiles.values(), sid, prefer=prof)
        last, text = _parse_ts(transcript_stats(path)[0]), claude_state.last_user_text(path)
    return dict(agent="claude", pid=p.pid, pids=(p.pid,), profile=prof or "", session_id=sid, cwd=cwd,
                last=last, text=text, skip=skip)


def _codex_rows(cfg, run, tops, now):
    """Codex rows: thread id from the rollout the native process has open, else the threads table (unique guesses only)."""
    home = cfg.codex.home
    rows = []
    for p, deep in tops:
        cwd = proc_cwd(run, deep) or proc_cwd(run, p.pid) or "?"
        tid = rollout_from_lsof(run, deep, home) if cfg.codex.enabled else None
        rows.append(dict(agent="codex", pid=p.pid, pids=tuple(dict.fromkeys((p.pid, deep))), profile=cfg.codex.profile,
                         session_id=tid, cwd=cwd, last=None, text=None, skip=None))
    bound = {r["session_id"] for r in rows if r["session_id"]}
    guesses = {}
    for r in rows:
        if r["session_id"] or not cfg.codex.enabled or r["cwd"] == "?":
            continue
        started = _started_at(run, r["pids"][-1], now.timestamp())
        guesses[r["pid"]] = threads_fallback(home, r["cwd"], started, bound) if started is not None else None
    for r in rows:
        if not cfg.codex.enabled:
            r["skip"] = "設定檔停用了 Codex"
            continue
        if r["session_id"] is None:
            g = guesses.get(r["pid"])
            if g and g != AMBIGUOUS and list(guesses.values()).count(g) == 1:
                r["session_id"] = g
            else:
                r["skip"] = ("同目錄有兩段對話一樣接近，判斷不出 thread id" if g == AMBIGUOUS else
                             "同目錄有多個 Codex 對到同一段對話，判斷不出 thread id" if g else
                             "讀不到 rollout（還沒送出第一個 prompt？），判斷不出 thread id")
                continue
        path = find_rollout(home, r["session_id"])
        if path is None:
            r["skip"] = f"找不到 rollout {r['session_id'][:8]}，不改開新對話"
            continue
        r["last"], r["text"] = rollout_last_time(path), codex_state.last_user_text(path)
    return rows


def outside_agents(cfg, run, now, *, proc_root=PROC_ROOT, own_pid=None):
    """Top-level claude/codex processes outside tmux, newest activity first. None when tmux panes cannot be read."""
    tmux = Tmux(run)
    panes = tmux.panes()
    if panes is None:
        if tmux.server_running():
            return None  # without the panes we cannot tell which are in tmux
        panes = []
    procs = list_procs(run)
    own_pid = os.getpid() if own_pid is None else own_pid
    mine = set(_ancestors(procs, own_pid))
    tops = _top_outside(procs, panes)
    rows = [_claude_row(cfg, run, p, procs, proc_root) for p in tops if p.comm == "claude"]
    rows += _codex_rows(cfg, run, [(p, codex_pid_in_pane(procs, p.pid) or p.pid) for p in tops
                                   if p.comm != "claude"], now)
    result = []
    for r in rows:
        if r["pid"] in mine:
            r["skip"] = "這是執行 sampan 的 agent（自己），請在別的終端機執行"
        elif r["skip"] is None and not Path(r["cwd"]).is_dir():
            r["skip"] = f"讀不到工作目錄（{r['cwd']}）"
        last = r["last"]
        result.append(Outside(str(r["pid"]), r["agent"], r["pid"], r["pids"], r["profile"], r["session_id"], r["cwd"],
                              _short(cfg, r["cwd"]), last, _ago(last, now), _snippet(r["text"] or ""),
                              r["skip"] is None, r["skip"]))
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(result, key=lambda c: c.last or epoch, reverse=True)


def default_name(cwd, taken):
    """Last segment of cwd, with . and : replaced by - (tmux treats them as separators); on duplicates add -2, -3…"""
    base = re.sub(r"[.:]", "-", Path(cwd).name) or "adopt"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}-{n}", n + 1
    return name


def _taken(cfg, run, out, extra):
    rows = _load(cfg, out) or []
    # when tmux is unreadable cmd_adopt already stopped at the start (outside_agents returns None); this only picks a name to avoid clashes, and if it really collides new-session fails
    return {p.session for p in Tmux(run).panes() or []} | {r.tmux for r in rows} | set(extra)


def _name_problem(name, taken):
    problem = name_problem(name)
    if problem:
        return problem
    if name in taken:
        return f"{name} 已有 tmux session 或已在登記表裡，換一個"
    return None


def _ask_name(cfg, run, out, ask, c, used):
    """None = cancelled (Ctrl-C / end of input)."""
    while True:
        taken = _taken(cfg, run, out, used)
        default = default_name(c.cwd, taken)
        try:
            raw = ask(f"{c.where} 的 tmux session 名稱（Enter 用 {default}）：")
        except (EOFError, KeyboardInterrupt):
            return None
        if raw is None:
            return None
        name = raw.strip() or default
        why = _name_problem(name, taken)
        if why is None:
            return name
        out(f"❌ {why}")


def _current_id(cfg, run, c, procs, proc_root):
    """The conversation the outside agent is in now (the user may /resume or /clear while sampan waits); None = unknown."""
    if c.agent == "claude":
        sid, prof, _cwd, skip = _claude_ident(cfg, run, c.pid, procs, proc_root)
        return sid if skip is None and prof == c.profile else None
    return rollout_from_lsof(run, c.pids[-1], cfg.codex.home)


def _wait_gone(cfg, run, out, c, sleep, proc_root):
    """Poll about once a second until every pid of the agent is gone (no timeout); returns its last conversation id.
    An empty ps (it failed) is not taken as gone. KeyboardInterrupt passes through."""
    sid = c.session_id
    while True:
        procs = list_procs(run)
        if procs and not any(_alive(procs, pid, c.agent) for pid in c.pids):
            return sid
        now_id = _current_id(cfg, run, c, procs, proc_root) if procs else None
        if now_id and now_id != sid:
            out(f"  原視窗換了對話：改接 {now_id[:8]}")
            sid = now_id
        sleep(1)


def _resume_problem(cfg, run, c, sid):
    why = id_problem(sid)
    if why:
        return why
    if c.agent == "codex":
        if find_rollout(cfg.codex.home, sid) is None:
            return f"找不到 rollout {sid[:8]}，不改開新對話"
    else:
        why = transcript_problem(cfg.profiles.values(), c.profile, sid)
        if why:
            return why
    try:
        where = _observe(cfg, run).by_id.get(sid)
    except TmuxUnreadable as e:  # cannot tell whether it already runs: do not open (no keys sent)
        return f"{e}；不知道這個對話是不是已經在跑"
    return f"這個對話已在 {where} 執行，不重複開" if where else None


def _launch_cmd(cfg, c, sid):
    # codex gets -C <cwd> (added when launch_line is given a cwd); claude, like restore, relies on new-session -c for the directory
    return launch_line(cfg, c.agent, c.profile, resume=sid, cwd=c.cwd if c.agent == "codex" else None)


def _adopt_one(cfg, run, out, c, name, sid, sleep, timeout):
    """Create the tmux session, register, resume, verify. Returns "ok", "warn" or a skip reason string."""
    why = _resume_problem(cfg, run, c, sid)
    if why:
        return why
    if name in _taken(cfg, run, out, ()):
        return f"名稱 {name} 在等待期間被用掉了"
    tmux = Tmux(run)
    pane = tmux.new_session(name, c.cwd)
    if pane is None:
        return "tmux new-session 失敗"
    row = Row(name, c.agent, c.profile, sid, c.cwd, "active", now_iso(), tmux.server_id() or "", None)
    if not _register(cfg, out, row):
        return "登記表寫入失敗"
    send_line(tmux, pane, c.agent, _launch_cmd(cfg, c, sid), sleep)
    resumed, at_trust, _unconfirmed = _watch(cfg, run, {pane: row}, timeout, sleep)
    if resumed:
        out(f"✔ 開好 tmux session「{name}」，接回同一段對話（{c.profile or 'codex'}）")
        return "ok"
    if at_trust:
        out(f"⚠ 開好 tmux session「{name}」並登記，但停在信任提示，請到視窗確認（sampan 不會替你按）")
    else:
        out(f"⚠ 開好 tmux session「{name}」並登記，但 {timeout} 秒內沒看到接回，請到視窗確認")
    return "warn"


def _show(out, cands):
    for i, c in enumerate(cands, 1):
        mark = "x" if c.selected else (" " if c.skip is None else "-")
        out(f"  [{mark}] {i:>2}) {c.agent}  {c.profile or '-'}  {c.where}  {c.ago}  {c.quote}".rstrip())
        if c.skip:
            out(f"         ↷ 跳過：{c.skip}")


def cmd_adopt(cfg, run, out, ask, *, dry_run=False, picker_fn=None, sleep=time.sleep,
              now_fn=lambda: datetime.now(timezone.utc), timeout=15, proc_root=PROC_ROOT, own_pid=None, opened=None):
    """picker_fn(cands) returns a list of Outside.key (None = cancelled); when None, use the text menu where numbers are typed.
    opened: given a list, append the names of the tmux sessions that were opened, in order (the ui then attaches to the first).
    Returns 0 all adopted, 1 some skipped or unconfirmed, 2 registry/tmux unreadable, 130 cancelled while waiting or entering names."""
    if _load(cfg, out) is None:
        return 2
    cands = outside_agents(cfg, run, now_fn(), proc_root=proc_root, own_pid=own_pid)
    if cands is None:
        out("❌ 讀不到 tmux 的 pane，分不出哪些在 tmux 裡，什麼都沒做")
        return 2
    if not cands:
        out("沒有在 tmux 外執行的 claude／codex")
        return 0
    out("在 tmux 外執行的 agent（[x] = 要接手的）：")
    _show(out, cands)
    if dry_run:
        used = set()
        for c in cands:
            if c.skip is None:
                name = default_name(c.cwd, _taken(cfg, run, out, used))
                used.add(name)
                out(f"[dry-run] {c.where}：結束後建立 tmux session {name} → {_launch_cmd(cfg, c, c.session_id)}")
        return 0
    if all(c.skip for c in cands):
        return 1
    if picker_fn is not None:
        picked = picker_fn(cands)
    else:
        picked = text_picker(out, ask, cands, key=lambda c: c.key, label=lambda c: c.where, prompt=PROMPT, verb="接手")
    if picked is None:
        out("已取消，什麼都沒做")
        return 0
    by_key = {c.key: c for c in cands}
    todo = [by_key[k] for k in dict.fromkeys(picked) if k in by_key and not by_key[k].skip]
    problems, used = any(c.skip for c in cands), set()
    for c in todo:
        name = _ask_name(cfg, run, out, ask, c, used)
        if name is None:
            out("已取消：這一個與之後的都沒有動")
            return 130
        used.add(name)
        out(f"▶ 請到原本的終端機視窗（tmux 外）裡「{c.where}」那個 {c.agent.capitalize()} 輸入 {EXIT_TEXT[c.agent]}")
        out("  等待它結束…（按 Ctrl-C 取消，什麼都不會改）")
        try:
            sid = _wait_gone(cfg, run, out, c, sleep, proc_root)
        except KeyboardInterrupt:
            out("")
            out("已取消：這一個與之後的都沒有動")
            return 130
        out("✔ 已結束")
        try:
            result = _adopt_one(cfg, run, out, c, name, sid, sleep, timeout)
        except KeyboardInterrupt:
            out("")
            out(f"已中斷：「{name}」可能已開好並登記（sampan status 可確認），之後的都沒有動")
            return 130
        if result in ("ok", "warn"):
            if opened is not None:
                opened.append(name)
        else:
            out(f"↷ {c.where}：{result}，沒有開")
        problems = problems or result != "ok"
    return 1 if problems else 0
