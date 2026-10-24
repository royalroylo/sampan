"""Pure data model of the marketplace tab: rows, info card, per-profile choices, change plan and save preview. No curses, no I/O beyond reading plugin state."""
from dataclasses import dataclass

from .clock import parse_iso
from .config import same_source
from .plugin_view import NO_SOURCE, ProfileChoice, _mkt, catalog
from .textwidth import pad


@dataclass(frozen=True)
class MarketRow:
    name: str
    source: str | None
    registered: dict
    total: int
    state: str
    used: dict


def _used(cfg, name):
    out = {}
    for p in cfg.profiles.values():
        pids = [pid for pid in p.plugins if _mkt(pid) == name]
        if pids:
            out[p.name] = pids
    return out


def build_market_rows(cfg, snap):
    names = set(cfg.marketplaces)
    for st in snap.states.values():
        names |= set(st.marketplaces)
    rows = []
    for name in sorted(names):
        source = cfg.marketplaces.get(name)
        registered = {p.name: snap.states[p.name].marketplaces[name]
                      for p in cfg.profiles.values() if name in snap.states[p.name].marketplaces}
        used = _used(cfg, name)
        if source is None:
            state = "清單外"
        elif any(not same_source(theirs, source) for theirs in registered.values()):
            state = "來源不同"
        elif not used:
            state = "沒用到"
        else:
            state = "一致"
        rows.append(MarketRow(name, source, registered, len(cfg.profiles), state, used))
    return rows


def _updated(s):
    dt = parse_iso(s)
    return dt.strftime("%m-%d %H:%M") if dt else "—"


def market_card(cfg, snap, row):
    lines = [row.name, "─" * 30, f"來源 {row.source or '（不在設定檔）'}"]
    for p in cfg.profiles.values():
        if p.name not in row.registered:
            lines.append(pad(p.name, 12) + "—")
            continue
        info = snap.states[p.name].market_info.get(row.name) or {}
        auto = {True: "開", False: "關"}.get(info.get("auto"), "？")
        cell = f"已加入  更新於 {_updated(info.get('updated'))}  自動更新 {auto}"
        theirs = row.registered[p.name]
        if row.source is not None and not same_source(theirs, row.source):
            cell += f"  來源 {theirs or NO_SOURCE}"
        lines.append(pad(p.name, 12) + cell)
    n = sum(1 for e in catalog(cfg, snap) if _mkt(e.pid) == row.name)
    lines.append(f"目錄有 {n} 個 plugin")
    return lines


def market_choices(cfg, snap, name, source):
    out = []
    for p in cfg.profiles.values():
        st = snap.states[p.name]
        theirs = st.marketplaces.get(name)
        n = len(_used(cfg, name).get(p.name, []))
        disabled = None
        if p.name in snap.missing:
            disabled = "目錄不存在"
        elif source is not None and name in st.marketplaces and not same_source(theirs, source):  # outside the config: any source
            disabled = f"來源不同：{theirs or NO_SOURCE}"
        elif name in st.marketplaces and n > 0:
            disabled = f"清單還有 {n} 個 plugin 用它（先在 plugin 頁移掉）"
        out.append(ProfileChoice(p.name, p.label, theirs if name in st.marketplaces else None, n > 0, disabled))
    return out


@dataclass(frozen=True)
class MarketChange:
    profile: str
    add: bool
    drops: tuple


def plan_market(snap, choices, checked, name):
    changes = []
    for c in choices:
        if c.disabled:
            continue
        registered, want = c.profile in snap.states and name in snap.states[c.profile].marketplaces, bool(checked[c.profile])
        if want and not registered:
            changes.append(MarketChange(c.profile, True, ()))
        elif registered and not want:
            st = snap.states[c.profile]
            drops = tuple(sorted(pid for pid in st.installed if _mkt(pid) == name))
            changes.append(MarketChange(c.profile, False, drops))
    return changes


def market_preview(cfg, name, changes, *, new):
    lines = ["將會："]
    for ch in changes:
        lbl = f"（{cfg.profiles[ch.profile].label}）"
        if ch.add:
            lines.append(f"  + 加入到 {ch.profile}{lbl}")
        else:
            extra = f"（連同已裝的 {'、'.join(d.split('@', 1)[0] for d in ch.drops)} 一起解除安裝）" if ch.drops else ""
            lines.append(f"  − 從 {ch.profile}{lbl} 移除{extra}")
    if new:
        lines.append("  [marketplaces] 會加上這個來源（名稱加入後才知道）")
    lines.append(f"設定檔 {cfg.path} 會一起更新（先備份）")
    return lines
