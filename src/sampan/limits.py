"""How many agents may run at once, and the check every way of starting one shares.

`count` turns the process table, the tmux panes, the registry, the reservations and the dispatch phases into one
number per owner; `decide` is the pure rule (count + what the call adds + the memory light → ok / warn / refuse);
`check` does the I/O around it: sample the machine, take the reservation lock, decide, write a reservation, and on a
refusal or warning list sessions the user could stop. sampan never stops a session for the user.

This is a guard against accidents, not a security boundary: an agent can start `claude` by hand, and it is still counted."""
import fcntl
import fnmatch
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import actionlog, dispatch, fsutil, resources, send_restore
from . import procs as procs_mod
from .codex_state import _is_codex, codex_pid_in_pane
from .registry import RegistryError, load_rows
from .tmux import Tmux

RES_FILE = "limits-reservations.json"
LOCK_FILE = "limits.lock"
LOCK_WAIT_S = 10
SHORT_COMMANDS = frozenset(("plugin", "mcp", "config", "update", "doctor", "install", "migrate-installer", "setup-token"))
MAX_CANDIDATES = 5
OTHERS = ""  # the owner of a profile that is in no [limits.owners] list, and of an agent outside tmux
GB = 1 << 30


# --- the number ---------------------------------------------------------------------------------------------------

def effective_max(limits, ram):
    """The count limit: None = no count limit (max_sessions = 0), else a number. "auto" = clamp(floor((RAM GiB −
    reserve_gb) / per_session_gb), min_auto, 64); with an unknown RAM, min_auto."""
    mx = limits.max_sessions
    if mx == 0:
        return None
    if mx != "auto":
        return mx
    if not ram:
        return limits.min_auto
    return max(limits.min_auto, min(64, int((ram / GB - limits.reserve_gb) // limits.per_session_gb)))


def short_lived(args):
    """A `claude plugin …`, `claude -p …` and the like: a command that ends by itself, not a session."""
    words = args.split()[1:]
    if any(w in ("-p", "--print") for w in words):
        return True
    first = next((w for w in words if not w.startswith("-")), None)
    return first in SHORT_COMMANDS


@dataclass(frozen=True)
class Item:
    name: str  # tmux session name; "" for an agent outside tmux
    owner: str
    kind: str  # claude | codex | outside | reserved | starting
    pid: int = 0


@dataclass(frozen=True)
class Count:
    items: tuple
    total: int
    by_owner: dict
    outside: int
    running_names: frozenset


def top_claudes(procs):
    """Interactive top-level claude processes: parent is not a claude, not a zombie, not a short command."""
    out = []
    for p in procs.values():
        if p.comm != "claude" or p.zombie or short_lived(p.args):
            continue
        parent = procs.get(p.ppid)
        if parent is not None and parent.comm == "claude" and not parent.zombie:
            continue
        out.append(p)
    return sorted(out, key=lambda p: p.pid)


def _codex_interactive(p):
    first = next((w for w in p.args.split()[1:] if not w.startswith("-")), "")
    return first not in ("app-server", "exec")


def count(cfg, procs, panes, rows, reservations, phases, *, exclude=()):
    """Everything that runs or is about to. panes None (tmux unreadable): every claude is counted as outside tmux.
    reservations: {name: expiry}-like iterable of names that are valid now (the caller filtered by expiry);
    phases: names of dispatch targets in LAUNCHING / STARTING. Reserved and starting names count only while no agent
    runs under that name, and once. exclude: names being started by this very check (not counted twice)."""
    lim = cfg.limits
    row_of = {r.tmux: r for r in rows}

    def owner_of(name):
        row = row_of.get(name)
        return lim.owner_of(row.profile) if row is not None else OTHERS

    items, seen = [], set()
    for p in top_claudes(procs):
        pane = next((q for q in (panes or ()) if procs_mod.is_descendant(procs, p.pid, q.pane_pid)), None)
        if pane is None:
            items.append(Item("", OTHERS, "outside", p.pid))
        else:
            items.append(Item(pane.session, owner_of(pane.session), "claude", p.pid))
            seen.add(pane.session)
    for q in panes or ():
        if q.session in seen:
            continue
        pid = codex_pid_in_pane(procs, q.pane_pid)
        if pid is not None and pid in procs and _is_codex(procs[pid]) and _codex_interactive(procs[pid]):
            items.append(Item(q.session, owner_of(q.session), "codex", pid))
            seen.add(q.session)
    skip = set(exclude)
    for kind, names in (("reserved", reservations), ("starting", phases)):
        for name in names:
            if name in seen or name in skip:
                continue
            seen.add(name)
            items.append(Item(name, owner_of(name), kind))
    by_owner = {}
    for i in items:
        by_owner[i.owner] = by_owner.get(i.owner, 0) + 1
    return Count(tuple(items), len(items), by_owner, sum(1 for i in items if i.kind == "outside"),
                 frozenset(i.name for i in items if i.name and i.kind in ("claude", "codex")))


# --- the rule -------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Verdict:
    kind: str  # ok | warn | refuse
    reason: str = ""
    why: str = ""  # "memory" | "count" | "owner" | ""
    over: bool = False  # the count is above the limit (warn or refuse)


def decide(cnt, limits, maxn, owner, adds, red, *, reasons=()):
    """Pure. cnt: Count without the agent(s) being started; adds: how many this call adds (a replacement is 0)."""
    if adds <= 0:
        return Verdict("ok")
    total = cnt.total
    if red and limits.pressure_guard:
        why = "、".join(reasons) or "記憶體紅燈"
        if limits.owners:
            mine = cnt.by_owner.get(owner, 0)
            if owner == OTHERS:
                return Verdict("refuse", f"記憶體紅燈（{why}）；這個 session 的 profile 不在 [limits.owners]，紅燈時沒有保底名額",
                               "memory")
            if mine + adds > limits.reserve_per_owner:
                return Verdict("refuse", f"記憶體紅燈（{why}）；你已有 {mine} 個，紅燈時每人保底 {limits.reserve_per_owner} 個",
                               "memory")
            return Verdict("ok", f"記憶體紅燈（{why}），但在保底 {limits.reserve_per_owner} 個之內")
        if total + adds > limits.red_floor:
            return Verdict("refuse", f"記憶體紅燈（{why}）；全機已有 {total} 個，紅燈時最多 {limits.red_floor} 個", "memory")
        return Verdict("ok", f"記憶體紅燈（{why}），但在 {limits.red_floor} 個之內")
    if maxn is None:
        return Verdict("ok")
    over = total + adds > maxn
    if not over and limits.count_mode == "block" and limits.owners:
        others = sum(max(0, limits.reserve_per_owner - cnt.by_owner.get(o, 0))
                     for o in (n for n, _ in limits.owners) if o != owner)
        if total + adds + others > maxn:
            return Verdict("refuse", f"執行中 {total}／上限 {maxn}；其他人還有 {others} 個保底名額沒用", "owner", True)
    if over:
        text = f"執行中 {total}／上限 {maxn}"
        if limits.count_mode == "block":
            return Verdict("refuse", text, "count", True)
        return Verdict("warn", f"已超過上限（{total + adds}／{maxn}）", "count", True)
    return Verdict("ok")


# --- reservations and the lock -----------------------------------------------------------------------------------

class _Lock:
    """limits.lock: held for a few milliseconds around the reservation file. Never taken while holding
    dispatches.lock (that one waits forever). Not obtained within LOCK_WAIT_S → `held` False and the caller goes on
    without the reservations (a lock must not make sampan refuse)."""

    def __init__(self, state_dir, wait=LOCK_WAIT_S, sleep=time.sleep, clock=time.monotonic):
        self.path, self.wait, self.sleep, self.clock = Path(state_dir) / LOCK_FILE, wait, sleep, clock
        self.fh, self.held = None, False

    def __enter__(self):
        try:
            fsutil.private_dir(self.path.parent)
            self.fh = os.fdopen(os.open(self.path, os.O_WRONLY | os.O_CREAT, fsutil.PRIVATE_FILE), "w")
        except OSError:
            return self
        end = self.clock() + self.wait
        while True:
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.held = True
                return self
            except OSError:
                if self.clock() >= end:
                    return self
                self.sleep(0.05)

    def __exit__(self, *exc):
        if self.fh is not None:
            if self.held:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
            self.fh.close()


def read_reservations(state_dir):
    try:
        data = json.loads((Path(state_dir) / RES_FILE).read_text())
        return [r for r in data.get("reservations", []) if isinstance(r, dict) and isinstance(r.get("name"), str)
                and isinstance(r.get("expires"), (int, float))]
    except (OSError, ValueError, AttributeError):
        return []


def _write_reservations(state_dir, res):
    try:
        fsutil.atomic_write(Path(state_dir) / RES_FILE, json.dumps({"reservations": res}), mode=0o600)
    except OSError:
        pass


def valid_reservations(res, now, running_names):
    """Names whose reservation has not expired and that have no agent yet (the only two things that matter)."""
    return [r["name"] for r in res if r["expires"] > now and r["name"] not in running_names]


def reserve(state_dir, name, now, ttl, *, pid=None, start=None):
    """Write a reservation (under the lock) for name, replacing an older one of the same name."""
    with _Lock(state_dir) as lk:
        if not lk.held:
            return
        res = [r for r in read_reservations(state_dir) if r["name"] != name and r["expires"] > now]
        res.append({"name": name, "pid": os.getpid() if pid is None else pid, "start": start, "expires": now + ttl})
        _write_reservations(state_dir, res)


def release(state_dir, name, *, now=None, pid=None, start=None):
    """The writer takes its own reservation back (a failed start). pid and start must match, so a pid reused by another
    process never deletes someone else's."""
    pid = os.getpid() if pid is None else pid
    with _Lock(state_dir) as lk:
        if not lk.held:
            return
        res = read_reservations(state_dir)
        keep = [r for r in res if not (r["name"] == name and r.get("pid") == pid and r.get("start") == start)]
        if len(keep) != len(res):
            _write_reservations(state_dir, keep)


def phase_names(cfg):
    """Dispatch targets being reattached right now (phase launching / starting): a read-only look at the database,
    never the dispatches lock."""
    try:
        items = dispatch.load(cfg.state_dir)
    except dispatch.DispatchError:
        return []
    names = []
    for d in items:
        if d.state == "queued" and d.extra.get(send_restore.PHASE) in (send_restore.LAUNCHING, send_restore.STARTING):
            if d.to_name not in names:
                names.append(d.to_name)
    return names


# --- candidates ------------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    name: str
    why: str  # "retired" | "idle"
    idle_since: object = None
    tokens: int | None = None

    def line(self, verb):
        if self.why == "retired":
            return f"retired 卻還在跑：{self.name}（{verb} {self.name}）"
        extra = f"  context {self.tokens // 1000}k" if self.tokens else ""
        return f"{self.name}（閒著{extra}）"


def candidates(cfg, run, owner, *, rows=None, env=None, limit=MAX_CANDIDATES):
    """Sessions the caller's owner could stop: running, idle, no open dispatch, not protected, not the one sampan runs
    in, and of the same owner. Retired-but-running first, then the idle ones that have been quiet the longest."""
    from . import status
    try:
        srows = status.status_rows(cfg, run)
        regs = rows if rows is not None else load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)
        open_names = set()
        for d in dispatch.load(cfg.state_dir):
            if d.state in ("queued", "sent"):
                open_names.update((d.from_name, d.to_name))
    except (RegistryError, dispatch.DispatchError, OSError):
        return []
    own_pane = (env if env is not None else os.environ).get("TMUX_PANE")
    prof = {r.tmux: r.profile for r in regs}
    out = []
    for r in srows:
        if r.state != status.State.RUNNING and not (r.registry == "retired" and r.live == "running"):
            continue
        if cfg.limits.owner_of(prof.get(r.name, r.profile)) != owner:
            continue
        if any(fnmatch.fnmatch(r.name, pat) for pat in cfg.limits.protect):
            continue
        if r.name in open_names or (own_pane and r.pane == own_pane):
            continue
        if r.registry == "retired":
            out.append(Candidate(r.name, "retired"))
        elif r.agent_status in ("idle", "shell"):
            out.append(Candidate(r.name, "idle", r.last, r.tokens))
    out.sort(key=lambda c: (c.why != "retired", c.idle_since is not None, str(c.idle_since or ""), -(c.tokens or 0)))
    return out[:limit]


def candidate_lines(cands, verb="sampan stop"):
    return [f"   {i}) {c.line(verb)}" for i, c in enumerate(cands, 1)]


# --- the check every entry shares ----------------------------------------------------------------------------------

@dataclass
class Outcome:
    verdict: Verdict
    cands: list = field(default_factory=list)
    count: Count | None = None
    snap: object = None
    classified: object = None
    maxn: object = None
    override: bool = False
    reserved: tuple | None = None  # (pid, start) of the reservation this check wrote

    @property
    def red(self):
        return bool(self.classified is not None and self.classified.red)

    @property
    def ok(self):
        return self.verdict.kind != "refuse" or self.override

    def lines(self, name):
        """The message for a refusal or a warning (the ❌ / ⚠ is added by the caller's own convention)."""
        v = self.verdict
        head = (f"不開 {name}：{v.reason}" if v.kind == "refuse" else f"{v.reason}，仍照常開 {name}")
        if self.snap is not None and self.classified is not None and self.classified.red and v.why != "memory":
            head += f"；記憶體紅燈（{'、'.join(self.classified.reasons)}）"
        if self.cands:
            return [head, "   可以先停（閒著、你的、最久沒動）："] + candidate_lines(self.cands)
        return [head, "   目前沒有可以代你判斷「可以停」的 session（沒有閒著、沒有未結案派工的）"]


def check(cfg, run, *, name, profile, adds=1, replacing=False, over_limit=False, ttl=None, write=True,
          now=None, clock=time.time, probe=True, sleep=time.sleep, platform=None, env=None, log=None,
          reserve_name=True):
    """The one check for a new agent called `name` under `profile`. replacing: this call stopped it itself a moment ago
    (it adds nothing, and the memory light does not stop it). Returns an Outcome; ok means go ahead (a reservation
    was written when write). Nothing here ever refuses because a reading is missing."""
    lim = cfg.limits
    now = clock() if now is None else now
    owner = lim.owner_of(profile)
    if replacing:
        adds = 0
    tmux = Tmux(run)
    panes = tmux.panes()
    procs = procs_mod.list_procs(run)
    try:
        rows = load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)
    except RegistryError:
        rows = []
    snap = cl = None
    if lim.pressure_guard or lim.max_sessions == "auto":
        snap = resources.snapshot(run, cfg.state_dir, lim, now=now, platform=platform, probe=probe and adds > 0, sleep=sleep)
        cl = resources.classify(snap, lim, now)
    maxn = effective_max(lim, snap.ram if snap is not None else None)
    with _Lock(cfg.state_dir) as lk:
        res = read_reservations(cfg.state_dir) if lk.held else []
        base = count(cfg, procs, panes, rows, [], [])
        live = base.running_names
        cnt = count(cfg, procs, panes, rows, valid_reservations(res, now, live), phase_names(cfg), exclude=(name,))
        red = cl.red if cl is not None else False
        verdict = decide(cnt, lim, maxn, owner, adds, red, reasons=cl.reasons if cl is not None else ())
        override = verdict.kind == "refuse" and over_limit
        reserved = None
        if write and lk.held and reserve_name and (verdict.kind != "refuse" or override) and (adds > 0 or replacing):
            kept = [r for r in res if r["name"] != name and r["expires"] > now]
            try:
                reserved = (os.getpid(), procs_mod.start_time(os.getpid(), run))
            except Exception:  # noqa: BLE001  the start time only guards against a reused pid
                reserved = (os.getpid(), None)
            kept.append({"name": name, "pid": reserved[0], "start": reserved[1],
                         "expires": now + (ttl if ttl is not None else send_restore.START_LIMIT_S)})
            _write_reservations(cfg.state_dir, kept)
    if not lk.held:
        (log or (lambda m: actionlog.append(cfg, "limits", [m])))(f"{name}：limits.lock 等了 {LOCK_WAIT_S} 秒沒拿到，不含佔位照算")
    out = Outcome(verdict, [], cnt, snap, cl, maxn, override, reserved)
    if verdict.kind != "ok":
        out.cands = candidates(cfg, run, owner, rows=rows, env=env)
    if override:
        actionlog.append(cfg, "limits", [f"{name}：已越過（{verdict.reason}）"])
    return out


def give_back(cfg, name, outcome):
    """The starter takes its own reservation back after a failed start."""
    if outcome is not None and outcome.reserved:
        release(cfg.state_dir, name, pid=outcome.reserved[0], start=outcome.reserved[1])


OVER_LIMIT_HELP = ("一定要開：在你自己的終端機加 --over-limit，或在畫面上按 y"
                   "（如果是在 Claude 的 ! 裡跑的，請改在畫面上按 y，或另開一個終端機視窗跑）")


def gate(cfg, run, out, **kw):
    return _gate(cfg, run, out, **kw)


def _gate(cfg, run, out, *, name, profile, adds=1, replacing=False, over_limit=False, ask=None, dry_run=False,
          ttl=None, check_fn=None, **kw):
    """check + the words. True = go ahead (a reservation is written unless dry_run), False = refused (said why, with the
    sessions that could be stopped). ask(question) -> bool: a person at a screen who may say yes to a refusal."""
    check_fn = check_fn or check
    try:
        o = check_fn(cfg, run, name=name, profile=profile, adds=adds, replacing=replacing, over_limit=over_limit,
                     write=not dry_run, ttl=ttl, **kw)
    except Exception as e:  # noqa: BLE001  a broken reading must never stop sampan from starting a session
        actionlog.append(cfg, "limits", [f"{name}：檢查上限時出錯（{type(e).__name__}: {e}），這次不擋"])
        return True
    if o.verdict.kind == "ok":
        return True
    lines = o.lines(name)
    if o.verdict.kind == "warn" or o.override:
        out(("⚠ " if not o.override else "⚠ 已越過：") + lines[0])
        for line in lines[1:]:
            out(line)
        return True
    out("❌ " + lines[0])
    for line in lines[1:]:
        out(line)
    if ask is not None and ask("仍要開？(y/N) "):
        retry = check_fn(cfg, run, name=name, profile=profile, adds=adds, replacing=replacing, over_limit=True,
                         write=not dry_run, ttl=ttl, **kw)
        return retry.ok
    out("   " + OVER_LIMIT_HELP)
    return False


class Gate:
    """gate() as an object: `passed`, and `give_back()` for a start that failed after the reservation was written."""

    def __init__(self, cfg, run, out, name, profile, **kw):
        self.cfg, self.name, self.outcome = cfg, name, None
        box = []
        real_check = kw.pop("check_fn", check)

        def spy(*a, **k):
            o = real_check(*a, **k)
            box.append(o)
            return o
        self.passed = _gate(cfg, run, out, name=name, profile=profile, check_fn=spy, **kw)
        self.outcome = box[-1] if box else None

    def give_back(self):
        give_back(self.cfg, self.name, self.outcome)


_HINT = {}  # (state dir) -> (monotonic time, answer): room_hint is looked at by every watcher, so it is cached for 5 s


def room_hint(cfg, run, row, items, *, clock=time.monotonic):
    """Why a stopped target may not be reattached right now ("" = there is room), for the first judgement of
    `send --restore`. Called inside the dispatches lock, so it takes neither limits.lock nor reads the reservations;
    it uses `ps`, the memory reading and the phases of the messages already loaded. The authoritative check is the
    one right before the launch (check / Gate)."""
    key = (str(cfg.state_dir), row.name)
    hit = _HINT.get(key)
    if hit is not None and clock() - hit[0] < 5:
        return hit[1]
    why = ""
    try:
        lim = cfg.limits
        procs = procs_mod.list_procs(run)
        panes = Tmux(run).panes()
        rows = load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)
        phases = [d.to_name for d in items if d.state == "queued" and d.extra.get(send_restore.PHASE) in
                  (send_restore.LAUNCHING, send_restore.STARTING)]
        snap = resources.snapshot(run, cfg.state_dir, lim, probe=False)
        cl = resources.classify(snap, lim)
        cnt = count(cfg, procs, panes, rows, [], phases, exclude=(row.name,))
        reg = next((r for r in rows if r.tmux == row.name), None)
        prof = reg.profile if reg is not None else ""
        v = decide(cnt, lim, effective_max(lim, snap.ram), lim.owner_of(prof), 1, cl.red, reasons=cl.reasons)
        if v.kind == "refuse":
            why = v.reason
    except Exception:  # noqa: BLE001  not knowing is "there is room"
        why = ""
    _HINT[key] = (clock(), why)
    return why


STOP_WAIT_S = 90  # how long a restart waits for one session to stop (restart.py, plugin_apply.py)


def hold_places(cfg, names, *, ttl=STOP_WAIT_S + send_restore.START_LIMIT_S, now=None):
    """Before a restart stops sessions: keep each one's place, so a `new` in between cannot take it. The restore that
    follows replaces the entry with its own."""
    now = time.time() if now is None else now
    for name in names:
        reserve(cfg.state_dir, name, now, ttl)


def let_go(cfg, names):
    """A session that could not be stopped keeps running: its held place is not needed."""
    for name in names:
        release(cfg.state_dir, name, pid=os.getpid(), start=None)
