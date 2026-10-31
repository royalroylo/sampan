"""Switch several Claude sessions to another profile at once (e.g. one account's quota ran out): plan, confirm, then
one cmd_switch after another, ending with one result table."""
from . import actionlog, folders, ids, session_run
from .registry import RegistryError, load_rows
from .restart import BUSY
from .status import State, status_rows


def plan(cfg, rows, names, profile, *, from_profile=None, own_pane=None):
    """(names to switch, [(name, reason skipped)]) in the given order, each once. rows are StatusRows."""
    by_name = {r.name: r for r in rows}
    try:
        cwds = {r.tmux: r.cwd for r in load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)}
    except RegistryError:
        cwds = {}
    go, skip, seen = [], [], set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        row = by_name.get(name)
        if row is None:
            why = "找不到"
        elif row.state is State.RETIRED:
            why = "已 retired"
        elif row.agent == "codex":
            why = "Codex 不支援換 profile"
        elif session_run.WORKTREE_MARK in cwds.get(name, ""):
            why = "在 worktree 裡"
        elif own_pane and row.pane == own_pane:
            why = "是 sampan 所在的 pane"
        elif row.profile == profile:
            why = f"已經是 {profile}"
        elif from_profile and row.profile != from_profile:
            why = f"目前不是 {from_profile}"
        elif row.agent_status == "waiting":
            why = "在等人回應"
        elif row.agent_status in BUSY:
            why = "忙碌中"
        else:
            go.append(name)
            continue
        skip.append((name, why))
    return go, skip


def _names(cfg, refs, folder, out):
    """The session names the refs / folder (with its subfolders) stand for, or None after printing why not."""
    if folder is not None:
        path = folder.strip().strip("/")
        if not path:
            out("❌ --folder 要給資料夾路徑（最上層的 session 請直接列名稱）")
            return None
        try:
            tree = folders.load(cfg.state_dir)
        except folders.FoldersError as e:
            out(f"❌ {e}")
            return None
        if path not in tree.folders:
            out(f"❌ 沒有資料夾「{path}」（sampan folder list 看）")
            return None
        names = sorted(folders.sessions_under(tree, path))
        if not names:
            out(f"資料夾「{path}」裡沒有 session")
        return names
    try:
        rows = load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)
    except RegistryError:
        return list(refs)
    names = []
    for ref in refs:
        r = ids.resolve(rows, ref)
        if isinstance(r, str):
            if not r.startswith("找不到"):
                out(f"❌ {r}")
                return None
            names.append(ref)
        else:
            names.append(r.tmux)
    return names


def cmd_switch_many(cfg, run, out, ask, refs, profile, *, folder=None, from_profile=None, dry_run=False, yes=False,
                    own_pane=None, on_prompt=None, switch_fn=session_run.cmd_switch, status_fn=status_rows):
    """ask(prompt) answers the confirmation; None = not interactive (then yes is required). Returns 0 when everything
    switched or was skipped, 1 when a switch failed, 2 for wrong input."""
    if profile not in cfg.profiles:
        out(f"❌ 未知的 profile：{profile}（設定檔裡有：{'、'.join(cfg.profiles)}）")
        return 2
    if from_profile is not None and from_profile not in cfg.profiles:
        out(f"❌ 未知的 profile：{from_profile}（設定檔裡有：{'、'.join(cfg.profiles)}）")
        return 2
    names = _names(cfg, refs, folder, out)
    if names is None:
        return 2
    if not names:
        return 0
    rows = status_fn(cfg, run)
    known = {r.name for r in rows}
    missing = [n for n in names if n not in known]
    if missing:
        out("❌ 找不到：" + "、".join(missing))
        return 2
    go, skip = plan(cfg, rows, names, profile, from_profile=from_profile, own_pane=own_pane)
    out(f"會換 {len(go)} 個：" + ("、".join(go) if go else "（無）"))
    if skip:
        out(f"跳過 {len(skip)} 個：" + "、".join(f"{n}（{why}）" for n, why in skip))
    if not go or dry_run:
        return 0
    if not yes:
        if ask is None:
            out("❌ 要確認請加 --yes")
            return 2
        try:
            answer = ask("開始？ y/N")
        except (EOFError, KeyboardInterrupt):
            answer = None
        if (answer or "").strip() not in ("y", "Y"):
            out("已取消")
            return 0
    label = f"switch {len(go)} sessions → {profile}"
    out = actionlog.tee(cfg, label, out)
    results = [(n, "略過", why) for n, why in skip]
    worst = 0
    for name in go:
        now = next((r for r in status_fn(cfg, run) if r.name == name), None)
        if now is not None and now.agent_status in BUSY:
            results.append((name, "略過", "開始前變忙了"))
            continue
        rc = switch_fn(cfg, run, out, name, profile, own_pane=own_pane, on_prompt=on_prompt, warn_gap=False)
        if rc == 0:
            results.append((name, "已換", profile))
        else:
            results.append((name, "失敗", f"rc {rc}"))
            worst = 1
    out("結果：")
    width = max(len(n) for n, _, _ in results)
    for n, state, note in results:
        out(f"  {n.ljust(width)}  {state}  {note}")
    return worst
