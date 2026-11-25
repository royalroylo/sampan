"""Machine memory and load: one sampler, one classifier, shared by the limit checks, `status`, `doctor`, the MCP
`list_sessions` and the screen's resource line. Read-only; a reading that cannot be had is None ("unknown") and never
makes anything refuse.

macOS: `sysctl` (one spawn) + `vm_stat` (one spawn) + os.getloadavg(). Linux: /proc. "Being written out" is the rate
of Swapouts against the previous sample kept in `resources.json` (a CLI call alone has no previous sample)."""
import fcntl
import json
import os
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path

from . import fsutil

STATE_FILE = "resources.json"
MIN_GAP = 10  # a previous sample younger than this is not compared with (use the one before it)
MAX_GAP = 600  # ... nor one older than this
WRITE_EVERY = 30  # the state file is written at most this often
STALE_S = 30  # a watcher samples when the file is older than this
HOLD_S = 600  # after the last "high and being written out", stay red this long while still high
PROBE_S = 5  # the one case a sampler waits: two samples this far apart

COMPRESSOR_YELLOW, COMPRESSOR_HIGH = 0.30, 0.40
SWAP_YELLOW, SWAP_HIGH = 0.25, 0.45
LEVEL_YELLOW, LEVEL_RED = 30, 15
LOAD_YELLOW, LOAD_RED = 1.0, 2.0


@dataclass(frozen=True)
class Snapshot:
    at: float  # time.time()
    ram: int | None = None  # bytes
    swap_used: int | None = None
    swap_total: int | None = None
    compressed: int | None = None  # bytes the compressor itself occupies (None on Linux)
    level: int | None = None  # kern.memorystatus_level, percent free (None on Linux)
    load5: float | None = None
    cores: int | None = None
    swapouts: int | None = None  # cumulative counter
    boot: float | None = None  # boot time, seconds since the epoch
    rate: float | None = None  # Swapouts per minute against the previous sample, None = not known
    last_red_at: float | None = None  # last time "high and being written out" held (kept in the state file)

    @property
    def compressor_pct(self):
        return None if self.compressed is None or not self.ram else self.compressed / self.ram

    @property
    def swap_pct(self):
        return None if self.swap_used is None or not self.ram else self.swap_used / self.ram

    @property
    def load_per_core(self):
        return None if self.load5 is None or not self.cores else self.load5 / self.cores


# --- parsing -----------------------------------------------------------------------------------------------------

def parse_vm_stat(text):
    """(page_size, compressor_pages, swapouts), each None when absent."""
    page = re.search(r"page size of (\d+) bytes", text or "")
    comp = re.search(r"Pages occupied by compressor:\s+(\d+)", text or "")
    outs = re.search(r"^Swapouts:\s+(\d+)", text or "", re.M)
    return (int(page.group(1)) if page else None, int(comp.group(1)) if comp else None,
            int(outs.group(1)) if outs else None)


def parse_swapusage(text):
    """(total, used) in bytes from `vm.swapusage: total = 7168.00M  used = 6350.12M  free = ...`; M is MiB."""
    units = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30}
    found = {}
    for key, num, unit in re.findall(r"(total|used)\s*=\s*([0-9.]+)([KMG])", text or ""):
        try:
            found[key] = int(float(num) * units[unit])
        except ValueError:
            pass
    return found.get("total"), found.get("used")


def parse_boottime(text):
    m = re.search(r"sec\s*=\s*(\d+)", text or "")
    return float(m.group(1)) if m else None


def _int(text):
    try:
        return int(str(text).strip())
    except ValueError:
        return None


def _safe(run, args):
    try:
        return run(args)
    except Exception:  # noqa: BLE001  a reading that cannot be had is unknown, never an error
        return None


def _read_macos(run):
    """Fields from `sysctl` and `vm_stat`: two spawns. Any failure leaves its fields None."""
    f = {}
    r = _safe(run, ["sysctl", "-n", "vm.swapusage", "kern.memorystatus_level", "hw.memsize", "hw.ncpu", "kern.boottime"])
    lines = (r.stdout or "").splitlines() if r is not None and r.returncode == 0 else []
    if len(lines) >= 5:
        f["swap_total"], f["swap_used"] = parse_swapusage(lines[0])
        f["level"], f["ram"], f["cores"] = _int(lines[1]), _int(lines[2]), _int(lines[3])
        f["boot"] = parse_boottime(lines[4])
    r = _safe(run, ["vm_stat"])
    if r is not None and r.returncode == 0:
        page, comp, outs = parse_vm_stat(r.stdout)
        f["compressed"] = comp * page if comp is not None and page else None
        f["swapouts"] = outs
    return f


def _read_linux(proc_root):
    f = {}
    root = Path(proc_root)

    def read(name):
        try:
            return (root / name).read_text()
        except OSError:
            return ""

    kb = {k: int(v) for k, v in re.findall(r"^(\w+):\s+(\d+)\s*kB", read("meminfo"), re.M)}
    if "MemTotal" in kb:
        f["ram"] = kb["MemTotal"] << 10
    if "SwapTotal" in kb:
        f["swap_total"] = kb["SwapTotal"] << 10
        f["swap_used"] = (kb["SwapTotal"] - kb.get("SwapFree", kb["SwapTotal"])) << 10
    m = re.search(r"^pswpout\s+(\d+)", read("vmstat"), re.M)
    f["swapouts"] = int(m.group(1)) if m else None
    m = re.search(r"^btime\s+(\d+)", read("stat"), re.M)
    f["boot"] = float(m.group(1)) if m else None
    return f


def read_raw(run, *, platform=None, proc_root="/proc", load=None, ncpu=None):
    platform = platform or os.sys.platform
    f = _read_macos(run) if platform == "darwin" else _read_linux(proc_root) if platform.startswith("linux") else {}
    if f.get("cores") is None:
        f["cores"] = ncpu if ncpu is not None else os.cpu_count()
    try:
        f["load5"] = (load or os.getloadavg)()[1]
    except (OSError, AttributeError, IndexError):
        f["load5"] = None
    return f


# --- the state file ----------------------------------------------------------------------------------------------

def _load_state(state_dir):
    try:
        data = json.loads((Path(state_dir) / STATE_FILE).read_text())
        samples = [s for s in data.get("samples", []) if isinstance(s, dict)
                   and all(isinstance(s.get(k), (int, float)) for k in ("t", "swapouts"))]
        red = data.get("last_swapout_red_at")
        return samples[-2:], (float(red) if isinstance(red, (int, float)) and not isinstance(red, bool) else None), \
            float(data.get("boot")) if isinstance(data.get("boot"), (int, float)) else None
    except (OSError, ValueError, AttributeError, TypeError):
        return [], None, None


def state_age(state_dir, now=None):
    """Seconds since the state file was last written (the time it records itself, so a clock given to the sampler
    and the one the file used agree), None when there is none or it cannot be read."""
    try:
        at = json.loads((Path(state_dir) / STATE_FILE).read_text()).get("at")
        if isinstance(at, bool) or not isinstance(at, (int, float)):
            raise ValueError(at)
        return (time.time() if now is None else now) - at
    except (OSError, ValueError, AttributeError):
        return None


def rate_since(samples, now, boot, swapouts):
    """Swapouts per minute against the newest sample at least MIN_GAP old and at most MAX_GAP; None = not known
    (no sample, another boot, a counter that went down, a clock that went back)."""
    if swapouts is None:
        return None
    for s in reversed(samples):
        if s.get("boot") != boot:
            return None
        dt = now - s["t"]
        if dt < 0 or swapouts < s["swapouts"]:
            return None
        if dt < MIN_GAP:
            continue
        if dt > MAX_GAP:
            return None
        return (swapouts - s["swapouts"]) * 60.0 / dt
    return None


def _write_state(state_dir, sample, red, boot, now):
    """Add one sample to the state file under its own lock: another process may have written since this one read, so
    the file is read again inside the lock and only a sample newer than its last is appended (the red time is the later
    of the two). A sampler that cannot write still reports what it read."""
    path = Path(state_dir) / STATE_FILE
    try:
        fsutil.private_dir(path.parent)
        with open(path.with_name("resources.lock"), "a") as lk:
            fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
            try:
                samples, old_red, old_boot = _load_state(state_dir)
                if old_boot is not None and boot is not None and old_boot != boot:
                    samples, old_red = [], None
                if sample is not None and (not samples or sample["t"] > samples[-1]["t"]):
                    samples = samples + [sample]
                reds = [x for x in (red, old_red) if x is not None]
                fsutil.atomic_write(path, json.dumps({"samples": samples[-2:], "last_swapout_red_at": max(reds, default=None),
                                                      "boot": boot, "at": now}), mode=0o600)
            finally:
                fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


# --- classification ----------------------------------------------------------------------------------------------

def _level(value, yellow, high, *, below=False):
    if value is None:
        return None
    if below:
        return "red" if value < high else "yellow" if value < yellow else "green"
    return "red" if value > high else "yellow" if value > yellow else "green"


@dataclass(frozen=True)
class Classified:
    compressor: str | None  # "green" | "yellow" | "high" | None (unknown)
    swap: str | None
    level: str | None  # "green" | "yellow" | "red"
    load: str | None  # "green" | "yellow" | "red"; shown only, never in the memory light
    swapping: bool | None  # None = not known
    red: bool
    reasons: tuple  # why red
    memory: str  # "green" | "yellow" | "red" | "unknown"


def classify(snap, limits, now=None):
    """The levels of a snapshot and the memory light. Red (the only state that refuses a new agent) is any of:
    1 high (compressor or swap) and being written out; 2 compressor and swap both high; 3 within HOLD_S of the last
    time 1 held while compressor or swap is still high; 4 level below 15."""
    now = snap.at if now is None else now
    cp, sp = snap.compressor_pct, snap.swap_pct
    comp = None if cp is None else "high" if cp > COMPRESSOR_HIGH else "yellow" if cp > COMPRESSOR_YELLOW else "green"
    swap = None if sp is None else "high" if sp >= SWAP_HIGH else "yellow" if sp >= SWAP_YELLOW else "green"
    lvl = None if snap.level is None else "red" if snap.level < LEVEL_RED else "yellow" if snap.level < LEVEL_YELLOW \
        else "green"
    lpc = snap.load_per_core
    load = None if lpc is None else "red" if lpc > LOAD_RED else "yellow" if lpc > LOAD_YELLOW else "green"
    swapping = None if snap.rate is None else snap.rate > limits.swapout_per_min
    high = comp == "high" or swap == "high"
    reasons = []
    if high and swapping:
        reasons.append("壓縮或 swap 偏高，而且正在寫出 swap")
    if comp == "high" and swap == "high":
        reasons.append("壓縮與 swap 同時偏高")
    if (high and snap.last_red_at is not None and 0 <= now - snap.last_red_at <= HOLD_S
            and "壓縮或 swap 偏高，而且正在寫出 swap" not in reasons):
        reasons.append("剛寫出過 swap，壓縮或 swap 仍偏高")
    if lvl == "red":
        reasons.append(f"可用記憶體只剩 {snap.level}%")
    known = [x for x in (comp, swap, lvl) if x is not None]
    if reasons:
        memory = "red"
    elif not known:
        memory = "unknown"
    elif high or "yellow" in known:
        memory = "yellow"
    else:
        memory = "green"
    return Classified(comp, swap, lvl, load, swapping, bool(reasons), tuple(reasons), memory)


# --- the sampler -------------------------------------------------------------------------------------------------

def snapshot(run, state_dir, limits, *, now=None, platform=None, proc_root="/proc", load=None, ncpu=None,
             probe=False, sleep=time.sleep, clock=time.time):
    """Read the machine once, compare with the previous sample, keep the state file fresh.

    probe=True (a CLI check): when there is no usable previous sample, and the compressor or swap is high, and nothing
    else already makes it red, take a second sample PROBE_S later and use the two. Never waits otherwise."""
    now = clock() if now is None else now
    raw = read_raw(run, platform=platform, proc_root=proc_root, load=load, ncpu=ncpu)
    samples, red_at, state_boot = _load_state(state_dir)
    if raw.get("boot") is not None and state_boot is not None and raw["boot"] != state_boot:
        samples, red_at = [], None  # another boot: the counters started over
    boot = raw.get("boot")
    rate = rate_since([dict(s) for s in samples], now, boot, raw.get("swapouts"))
    snap = Snapshot(at=now, rate=rate, last_red_at=red_at, **{k: raw.get(k) for k in (
        "ram", "swap_used", "swap_total", "compressed", "level", "load5", "cores", "swapouts", "boot")})
    if probe and rate is None and raw.get("swapouts") is not None:
        c = classify(snap, limits, now)
        if (c.compressor == "high" or c.swap == "high") and not c.red:
            sleep(PROBE_S)
            raw2 = read_raw(run, platform=platform, proc_root=proc_root, load=load, ncpu=ncpu)
            later = now + PROBE_S
            if raw2.get("swapouts") is not None and raw2["swapouts"] >= raw["swapouts"]:
                snap = replace(snap, at=later, compressed=raw2.get("compressed", snap.compressed),
                               swap_used=raw2.get("swap_used", snap.swap_used), swapouts=raw2["swapouts"],
                               rate=(raw2["swapouts"] - raw["swapouts"]) * 60.0 / PROBE_S)
                now = later
    c = classify(snap, limits, now)
    new_red = red_at
    if (c.compressor == "high" or c.swap == "high") and c.swapping:
        new_red = now
    snap = replace(snap, last_red_at=new_red)
    if snap.swapouts is not None:
        if snap.rate is not None and snap.rate > limits.swapout_per_min / 2:
            _note(state_dir, f"swap 寫出 {snap.rate:.0f} 頁／分，壓縮 {_pct(snap.compressor_pct)}、"
                             f"swap {_pct(snap.swap_pct)}、level {snap.level}")
        age = state_age(state_dir, now)
        if new_red != red_at or age is None or age >= WRITE_EVERY:
            _write_state(state_dir, {"t": now, "swapouts": snap.swapouts, "boot": boot}, new_red, boot, now)
    return snap


def _pct(x):
    return "?" if x is None else f"{x * 100:.0f}%"


def _note(state_dir, text):
    """One line in actions.log when the write-out rate passes half the threshold (to calibrate it later)."""
    from types import SimpleNamespace

    from . import actionlog
    actionlog.append(SimpleNamespace(state_dir=Path(state_dir)), "limits", [text])
