"""Pure data model of the plugin screen: rows, info card, per-profile choices, change plan and save preview. No curses, no I/O beyond reading plugin state."""
from dataclasses import dataclass
from pathlib import Path

from .config import same_source
from .plugins import ProfilePluginState, read_state
from .ui.text import pad

STATES = ("最新", "有新版", "未裝", "不一致")
NO_SOURCE = "本機或不支援的來源"


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


def build_rows(cfg, snap, latest=None):  # `latest` is accepted for the update check (not wired in yet)
    profiles = list(cfg.profiles.values())
    ids = sorted(set().union(*(set(p.plugins) for p in profiles), *(set(s.installed) for s in snap.states.values())))
    shorts = [pid.split("@", 1)[0] for pid in ids]
    rows = []
    for pid, short in zip(ids, shorts):
        mkt = pid.split("@", 1)[1] if "@" in pid else ""
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
        source = cfg.marketplaces.get(mkt) or next(
            (snap.states[p.name].marketplaces[mkt] for p in live if snap.states[p.name].marketplaces.get(mkt)), None)
        name = short if shorts.count(short) == 1 else pid
        rows.append(PluginRow(pid, short, mkt, name, installed, listed, len(profiles), state, source, {}))
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


def profile_choices(cfg, snap, pid, source):
    mkt = pid.split("@", 1)[1] if "@" in pid else ""
    out = []
    for p in cfg.profiles.values():
        st = snap.states[p.name]
        disabled = None
        if p.name in snap.missing:
            disabled = "目錄不存在"
        elif mkt in st.marketplaces and not same_source(st.marketplaces[mkt], source):
            disabled = f"來源不同：{st.marketplaces[mkt] or NO_SOURCE}"
        out.append(ProfileChoice(p.name, p.label, st.installed.get(pid), pid in p.plugins, disabled))
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
        install = True if want and not c.installed else False if not want and c.installed else None
        if install is not None or c.listed != want:
            add = install is True and mkt not in snap.states[c.profile].marketplaces
            changes.append(Change(c.profile, install, want, add))
    return changes


def preview_lines(cfg, pid, changes, new_source=None):
    mkt = pid.split("@", 1)[1] if "@" in pid else ""
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
