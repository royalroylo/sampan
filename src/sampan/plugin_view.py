"""Pure data model of the plugin screen: rows, info card, per-profile choices, change plan and save preview. No curses, no I/O beyond reading plugin state."""
import json
from dataclasses import dataclass
from pathlib import Path

from .config import same_source, usable_source
from .plugins import ProfilePluginState, read_state
from .textwidth import pad

STATES = ("最新", "有新版", "未裝", "不一致")
NO_SOURCE = "本機或不支援的來源"
LOCAL = "本機來源，sampan 不管理"
UNMANAGED = "來源不在設定檔，也不是 sampan 能管的來源（只能移除）"


def _mkt(pid):
    return pid.split("@", 1)[1] if "@" in pid else ""


@dataclass(frozen=True)
class Snapshot:
    states: dict
    missing: frozenset


def snapshot(cfg):
    """Read every profile's plugin state; a profile whose directory does not exist is `missing` and gets an empty state."""
    missing = frozenset(p.name for p in cfg.profiles.values() if not Path(p.config_dir).is_dir())
    empty = lambda: ProfilePluginState({}, [], set(), {}, set())  # noqa: E731
    states = {p.name: empty() if p.name in missing else read_state(p) for p in cfg.profiles.values()}
    return Snapshot(states, missing)


@dataclass(frozen=True)
class PluginRow:
    pid: str
    short: str
    mkt: str
    name: str
    installed: dict
    listed: frozenset
    total: int
    state: str
    source: str | None
    newer: dict


@dataclass(frozen=True)
class CatalogEntry:
    pid: str
    description: str
    source: str | None


def _read_marketplace(clone):
    """Plugin entries of a marketplace clone; [] when the file is missing or malformed."""
    try:
        data = json.loads((Path(clone) / ".claude-plugin" / "marketplace.json").read_text())
        return [e for e in data["plugins"] if isinstance(e, dict) and isinstance(e.get("name"), str)]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def catalog(cfg, snap):
    """Plugins offered by the marketplaces already cloned in any existing profile (no network)."""
    found = {}
    for p in cfg.profiles.values():
        if p.name in snap.missing:
            continue
        root = Path(p.config_dir) / "plugins" / "marketplaces"
        try:
            clones = sorted(d for d in root.iterdir() if d.is_dir())
        except OSError:
            continue
        for clone in clones:
            mkt = clone.name
            for e in _read_marketplace(clone):
                pid = f"{e['name']}@{mkt}"
                if pid in found:
                    continue
                desc = e.get("description", "")
                desc = desc.splitlines()[0] if isinstance(desc, str) and desc.strip() else ""
                found[pid] = CatalogEntry(pid, desc, cfg.marketplaces.get(mkt) or snap.states[p.name].marketplaces.get(mkt))
    return [found[k] for k in sorted(found)]


@dataclass(frozen=True)
class Latest:
    version: str | None
    commit: str | None


UNKNOWN = Latest(None, None)


def newest(profile, mkt, short, run):
    """Latest version known from the local marketplace clone; `profile` is a Profile. Never uses the network."""
    clone = Path(profile.config_dir) / "plugins" / "marketplaces" / mkt
    entry = next((e for e in _read_marketplace(clone) if e["name"] == short), None)
    if entry is None:
        return UNKNOWN
    if isinstance(entry.get("version"), str) and entry["version"]:
        return Latest(entry["version"], None)
    src = entry.get("source")
    if isinstance(src, str):
        try:
            base = clone.resolve()
            target = (base / src).resolve()  # an absolute src replaces base; symlinks are followed
            if not target.is_relative_to(base):
                raise ValueError("source escapes the marketplace clone")
            v = json.loads((target / ".claude-plugin" / "plugin.json").read_text()).get("version")
        except (OSError, ValueError, AttributeError):
            v = None
        if isinstance(v, str) and v:
            return Latest(v, None)
        r = run(["git", "-C", str(clone), "rev-parse", "HEAD"])
        return Latest(r.stdout.strip()[:12], None) if r.returncode == 0 and r.stdout.strip() else UNKNOWN
    if isinstance(src, dict) and isinstance(src.get("sha"), str):
        return Latest(None, src["sha"])
    return UNKNOWN


def is_newer(installed, commit, latest):
    """True only when a comparable value is known on both sides and the 12-character prefixes differ."""
    return bool((installed and latest.version and installed[:12] != latest.version[:12])
                or (commit and latest.commit and commit[:12] != latest.commit[:12]))


def build_rows(cfg, snap, latest=None):
    """`latest` maps (profile name, pid) -> Latest; None skips the newer-version check."""
    latest = latest or {}
    profiles = list(cfg.profiles.values())
    ids = sorted(set().union(*(set(p.plugins) for p in profiles), *(set(s.installed) for s in snap.states.values())))
    shorts = [pid.split("@", 1)[0] for pid in ids]
    rows = []
    for pid, short in zip(ids, shorts):
        mkt = _mkt(pid)
        installed = {p.name: snap.states[p.name].installed[pid] for p in profiles if pid in snap.states[p.name].installed}
        listed = frozenset(p.name for p in profiles if pid in p.plugins)
        live = [p for p in profiles if p.name not in snap.missing]
        if not installed:
            state = "未裝"
        elif (len({installed[p.name] for p in live if p.name in installed}) > 1
              or any((pid in p.plugins) != (p.name in installed) for p in live)):
            state = "不一致"
        else:
            state = "最新"
        newer = {}
        for p in live:
            lt = latest.get((p.name, pid))
            if p.name in installed and lt and is_newer(installed[p.name], snap.states[p.name].commits.get(pid), lt):
                newer[p.name] = lt.version or (lt.commit or "")[:12]
        if newer and state == "最新":
            state = "有新版"
        source = cfg.marketplaces.get(mkt) or next(
            (snap.states[p.name].marketplaces[mkt] for p in live if snap.states[p.name].marketplaces.get(mkt)), None)
        name = short if shorts.count(short) == 1 else pid
        rows.append(PluginRow(pid, short, mkt, name, installed, listed, len(profiles), state, source, newer))
    return rows


def card_lines(cfg, row):
    lines = [f"{row.short}（{row.mkt}）", "─" * 30, f"來源 {row.source or NO_SOURCE}"]
    for p in cfg.profiles.values():
        v, listed = row.installed.get(p.name), p.name in row.listed
        if not Path(p.config_dir).is_dir():
            cell = "目錄不存在"
        elif v and p.name in row.newer:
            cell = f"{v[:12]} → {row.newer[p.name][:12]}"
        elif v and not listed:
            cell = f"{v[:12]}（清單沒列）"
        elif v:
            cell = v[:12]
        elif listed:
            cell = "—（清單有、沒裝）"
        else:
            cell = "—（清單沒列）"
        lines.append(pad(p.name, 12) + cell)
    return lines


@dataclass(frozen=True)
class ProfileChoice:
    profile: str
    label: str
    installed: str | None
    listed: bool
    disabled: str | None
    remove_only: bool = False  # the marketplace can't go into the config: the plugin may only be uninstalled


def profile_choices(cfg, snap, pid, source):
    """One choice per profile. A marketplace outside [marketplaces] whose source sampan can't write there is remove-only:
    listing or installing would give a config load_config rejects (callers pass a usable source as new_source)."""
    mkt = _mkt(pid)
    remove_only = mkt not in cfg.marketplaces and not usable_source(source)
    out = []
    for p in cfg.profiles.values():
        st = snap.states[p.name]
        theirs = st.marketplaces.get(mkt)
        installed = st.installed.get(pid)
        disabled = None
        if p.name in snap.missing:
            disabled = "目錄不存在"
        elif mkt in st.marketplaces and not theirs and not source:
            disabled = LOCAL
        elif mkt in st.marketplaces and theirs != source and not same_source(theirs, source):
            disabled = f"來源不同：{theirs or NO_SOURCE}"
        elif remove_only and not installed:
            disabled = UNMANAGED
        out.append(ProfileChoice(p.name, p.label, installed, pid in p.plugins, disabled, remove_only))
    return out


def mismatch(c):
    if c.installed and not c.listed:
        return "已裝但清單沒列"
    if c.listed and not c.installed:
        return "清單有但沒裝"
    return None


@dataclass(frozen=True)
class Change:
    profile: str
    install: bool | None  # True = install, False = uninstall, None = config only
    listed: bool  # list state after save
    add_marketplace: bool


def plan_changes(snap, choices, checked, mkt):
    changes = []
    for c in choices:
        if c.disabled:
            continue
        want = bool(checked[c.profile])
        if want and c.remove_only:  # keep it as it is: listing it would break the config
            continue
        install = True if want and not c.installed else False if not want and c.installed else None
        if install is not None or c.listed != want:
            add = install is True and mkt not in snap.states[c.profile].marketplaces
            changes.append(Change(c.profile, install, want, add))
    return changes


def preview_lines(cfg, pid, changes, new_source=None):
    mkt = _mkt(pid)
    lines = ["將會："]
    for ch in changes:
        prof = cfg.profiles.get(ch.profile)
        lbl = f"（{prof.label}）" if prof and prof.label else ""
        p = ch.profile
        if ch.install is True:
            lines.append(f"  + 安裝到 {p}{lbl}" + (f"（同時加入 {mkt} 來源）" if ch.add_marketplace else ""))
        elif ch.install is False:
            lines.append(f"  − 從 {p}{lbl} 移除")
        elif ch.listed:
            lines.append(f"  · {p}{lbl}：清單加上（已經裝了）")
        else:
            lines.append(f"  · {p}{lbl}：清單拿掉（本來就沒裝）")
    if new_source:
        lines.append(f"  [marketplaces] 加上 {mkt} = {new_source}")
    lines.append(f"設定檔 {cfg.path} 會一起更新（先備份）")
    return lines
