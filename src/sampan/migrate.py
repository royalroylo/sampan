"""Moving the state files to the 0.15 format (registry.json version 3 with session ids, folders.json version 2) and back.

`ensure` runs before every command that reads or writes state. When the files are already new (or there is no registry
yet) it does nothing. Otherwise it backs the files up byte for byte into state/backup-0.15-<stamp>/ (never deleted),
writes the new formats and reads them back: the sessions (names, conversation ids, agent, profile, state), each session's
folder and the folders themselves must be what they were, or both files are restored from the backup. Everything from
the backup to the restore happens under the registry lock. A run cut short (registry already version 3, folders still
version 1) only does the folders step, so ids never change twice. folders.json is display-only: one that cannot be read
is kept as it is (a warning, the registry still migrates) and migrated by a later run once it reads again.

Then `ensure` runs the 0.17 step (migrate_017.py): dispatches.json into dispatches.db. Its revert mark keeps it from
running again; while the mark is there only the commands that use dispatches stop.

`cmd_migrate` is `sampan migrate`: both steps by hand (also after a revert: the mark is removed first), `--dry-run` (a
rehearsal on copies, nothing written) and `--revert` (dispatches back into a dispatches.json 0.16 reads, for a downgrade
to 0.16; see migrate_017.revert). To go further back, revert to 0.16 first and run 0.16's `sampan migrate --revert`
there.
"""
import json
import os
import shutil
import tempfile
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from . import folders, registry
from .clock import now_iso
from .fsutil import PRIVATE_DIR, PRIVATE_FILE, private_file, write_target
from .machine import registry_machine

FILES = ("registry.json", folders.FOLDERS_FILE, "dispatches.json")  # backed up (those that exist)
# What a migration writes, so what a failure puts back. Folders first: a restore cut short then leaves registry v3 with
# folders v1, the state a later run resumes from, never folders keyed by ids the registry no longer has.
RESTORED = (folders.FOLDERS_FILE, "registry.json")
BACKUP_PREFIX = "backup-0.15-"


def _version(path):
    """The file's "version", or None when it is missing or unreadable (then it is not ours to migrate)."""
    try:
        v = json.loads(Path(path).read_text()).get("version")
    except (OSError, ValueError, AttributeError):
        return None
    return v if type(v) is int else None


def _folders_path(cfg):
    return Path(cfg.state_dir) / folders.FOLDERS_FILE


def _folders_readable(cfg):
    try:
        folders.load(cfg.state_dir, rows=[])  # version 1 needs no rows
    except folders.FoldersError:
        return False
    return True


def needed(cfg):
    """True when the registry is older than version 3, or folders.json is a readable version 1. No registry yet: False.
    A folders.json that cannot be read never makes this True by itself (it is kept as it is until it reads again)."""
    reg = _version(cfg.registry_path)
    if reg is None:
        return False
    return reg < registry.VERSION or (_version(_folders_path(cfg)) == 1 and _folders_readable(cfg))


def _snap(rows, tree):
    """tree None (folders.json unreadable): only the sessions are compared."""
    return {"sessions": sorted((r.tmux, r.session_id, r.agent, r.profile, r.state) for r in rows),
            "membership": None if tree is None else sorted((n, p) for n, p in tree.sessions.items() if p),
            "folders": None if tree is None else sorted(tree.folders.items())}


def _read(cfg):
    """(rows, tree or None, why folders.json cannot be read or ""). An unreadable registry raises RegistryError."""
    rows = registry.load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)
    try:
        return rows, folders.load(cfg.state_dir, rows), ""
    except folders.FoldersError as e:
        return rows, None, str(e)


def snapshot(cfg):
    """What a migration must keep: sessions (name, conversation id, agent, profile, state), each session's folder and
    the folders with their collapsed state. Never ids. Raises RegistryError / FoldersError for an unreadable file."""
    rows = registry.load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)
    return _snap(rows, folders.load(cfg.state_dir, rows))


def _diff(before, after):
    labels = {"sessions": "session", "membership": "資料夾歸屬", "folders": "資料夾"}
    lines = []
    for key, label in labels.items():
        b, a = set(before.get(key) or ()), set(after.get(key) or ())
        lines += [f"  - {label}：{' / '.join(map(str, item))}" for item in sorted(b - a)]
        lines += [f"  + {label}：{' / '.join(map(str, item))}" for item in sorted(a - b)]
    return lines


def _write_bytes(path, data):
    """Write data atomically at write_target(path), mode 0600."""
    target = write_target(path)
    tmp = target.with_name(f".{target.name}.tmp-{os.getpid()}")
    try:
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_FILE), "wb") as fh:
            fh.write(data)
        private_file(tmp)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def _raw(path):
    path = Path(path)
    return path.read_bytes() if path.exists() else None


def _holds_current(backup, state_dir):
    """backup has exactly the state files as they are now (same files present, same bytes)."""
    return all(_raw(Path(backup) / n) == _raw(Path(state_dir) / n) for n in FILES)


def _backup(state_dir, now):
    """Copy the state files that exist into a new state/backup-0.15-<stamp>/ (never an existing one) and return it.
    When the newest backup already holds these very bytes (a failed run retried), that one is returned instead."""
    state_dir = Path(state_dir)
    latest = last_backup(state_dir)
    if latest is not None and _holds_current(latest, state_dir):
        return latest
    base = f"{BACKUP_PREFIX}{now:%Y%m%d-%H%M%S}"
    n = 1
    while True:
        d = state_dir / (base if n == 1 else f"{base}-{n}")
        try:
            d.mkdir(mode=PRIVATE_DIR)
            break
        except FileExistsError:
            n += 1
    for name in FILES:
        data = _raw(state_dir / name)
        if data is not None:
            _write_bytes(d / name, data)
    return d


def last_backup(state_dir):
    """The newest state/backup-0.15-* directory, or None."""
    def key(p):  # backup-0.15-YYYYMMDD-HHMMSS[-n]
        rest = p.name[len(BACKUP_PREFIX):]
        return rest[:15], int(rest[16:]) if rest[16:].isdigit() else 1

    found = [p for p in Path(state_dir).glob(BACKUP_PREFIX + "*") if p.is_dir()]
    return max(found, key=key) if found else None


def _restore(state_dir, backup):
    for name in RESTORED:
        saved, target = Path(backup) / name, Path(state_dir) / name
        if saved.exists():
            _write_bytes(target, saved.read_bytes())
        elif target.exists():  # it was not there before the migration
            target.unlink()


def _check(cfg, before, *, want, kept=False, kept_bytes=None):
    """Re-read both files: why they are not right (a list of lines), or [] when they are. want: (registry version,
    folders version). kept: folders.json was left alone, so it must still be kept_bytes."""
    try:
        rows, tree, why = _read(cfg)
    except registry.RegistryError as e:
        return [f"寫完讀不回來：{e}"]
    problems = []
    reg = _version(cfg.registry_path)
    if reg != want[0]:
        problems.append(f"寫完的登記表是版本 {reg}，應該是 {want[0]}")
    if kept:
        if _raw(_folders_path(cfg)) != kept_bytes:
            problems.append("讀不到的 folders.json 應該原封不動，卻被改了")
    elif tree is None:
        problems.append(f"寫完讀不回來：{why}")
    elif _version(_folders_path(cfg)) not in (want[1], None):
        problems.append(f"寫完的資料夾是版本 {_version(_folders_path(cfg))}，應該是 {want[1]}")
    diff = _diff(before, _snap(rows, None if kept else tree))
    if diff:
        problems += ["寫完前後不一致："] + diff
    return problems


def _say(out, lines):
    for line in lines:
        out(line if line.startswith("  ") else f"  {line}")


def _restore_or_say(cfg, bk):
    """Put the files back from bk (under the caller's lock). The line to print."""
    if bk is None:
        return "❌ 試做的結果不對"
    try:
        _restore(cfg.state_dir, bk)
        return f"❌ 資料升級失敗，已從備份還原 folders.json 與 registry.json（備份在 {bk}）"
    except OSError as e:
        return f"❌ 資料升級失敗，而且還原也失敗（{e}）：請手動把 {bk} 裡的檔案複製回 {cfg.state_dir}"


def _upgrade(cfg, *, machine, now, backup=True):
    """The migration, all of it under the registry lock (folders.load/save with rows take no lock). Returns
    (rc, lines to print, backup dir, why folders.json was kept or ""). A failure after the backup is restored here."""
    with registry.lock(cfg.state_dir):
        if not needed(cfg):  # another sampan did it while we waited for the lock
            return 0, [], None, ""
        try:
            rows, tree, broken = _read(cfg)  # read before anything is written: an unreadable registry stops here
        except registry.RegistryError as e:
            return 2, [f"❌ 資料沒有升級（檔案都沒動）：{e}", "  修好或把檔案移開後再跑 sampan migrate"], None, ""
        before = _snap(rows, tree)
        kept_bytes = _raw(_folders_path(cfg))
        bk = _backup(cfg.state_dir, now) if backup else None
        try:
            if _version(cfg.registry_path) < registry.VERSION:
                registry.write_rows(cfg.registry_path, rows, now_iso(), cfg.history_dir, machine=machine)  # fills the ids
                rows = registry.load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)
            if tree is not None and _version(_folders_path(cfg)) == 1:
                folders.save(cfg.state_dir, folders.load(cfg.state_dir, rows), rows)
            problems = _check(cfg, before, want=(registry.VERSION, folders.VERSION), kept=tree is None,
                              kept_bytes=kept_bytes)
        except Exception as e:
            problems = [f"{type(e).__name__}: {e}"]
        if problems:
            return 2, [_restore_or_say(cfg, bk)] + problems, bk, broken
    return 0, [], bk, broken


def ensure(cfg, out, *, now=None, touches_dispatches=False):
    """Migrate the state when needed (see the module docstring): the 0.15 step, then the 0.17 one. 0 = nothing to do or
    done (folders.json may have been kept as it was, with a warning); 2 = not done, files as they were (the reason is
    printed). touches_dispatches: the command reads or writes dispatches; only such a command stops (2) when the
    dispatches are not migrated (failed, or reverted to 0.16), the others go on."""
    rc = _ensure_015(cfg, out, now=now)
    if rc:
        return rc
    return _ensure_017(cfg, out, now=now, touches_dispatches=touches_dispatches)


def _ensure_017(cfg, out, *, now, touches_dispatches):
    from . import migrate_017
    if migrate_017.reverted(cfg):
        if touches_dispatches:
            out(migrate_017.REVERTED_NOTE)
            return 2
        return 0
    if not migrate_017.needed(cfg):
        return 0
    try:
        lines = migrate_017.upgrade(cfg, now=now or datetime.now())
    except Exception as e:
        why = str(e) if isinstance(e, migrate_017.MigrateError) else f"{type(e).__name__}: {e}"
        out(f"❌ 派工資料沒有升級到 0.17（dispatches.json 沒動）：{why}")
        return 2 if touches_dispatches else 0
    for line in lines:
        out(line)
    if lines:  # migrated just now: this is the first 0.17 run after an update made by 0.16's code
        from .update import mark_upgrade_notice
        mark_upgrade_notice(cfg.state_dir)
    return 0


def _ensure_015(cfg, out, *, now=None):
    if not needed(cfg):
        return 0
    try:
        rc, lines, bk, broken = _upgrade(cfg, machine=registry_machine(cfg), now=now or datetime.now())
    except Exception as e:  # before the backup (the lock, the backup itself): nothing was written
        out(f"❌ 資料沒有升級（檔案都沒動）：{type(e).__name__}: {e}")
        return 2
    if lines:
        out(lines[0])
        _say(out, lines[1:])
    if rc == 0 and bk is not None:
        out(f"已把資料升到新格式（備份在 {bk}）")
        if broken:
            out(f"⚠ folders.json 讀不到，原檔保留沒動、也沒有升級（備份在 {bk}）；修好後跑 sampan migrate：{broken}")
    return rc


def versions(cfg):
    """(registry version, folders version, dispatches.json version), None for a missing or unreadable file (dispatches
    version 3 is the stub 0.17 leaves: the dispatches are in dispatches.db)."""
    return _version(cfg.registry_path), _version(_folders_path(cfg)), _version(Path(cfg.state_dir) / "dispatches.json")


def _dry_run(cfg, out):
    try:
        rows, _tree, broken = _read(cfg)
    except registry.RegistryError as e:
        out(f"❌ 升級會失敗（登記表讀不到）：{e}")
        return 2
    reg, fol, _dis = versions(cfg)
    out("sampan migrate --dry-run（在複本上試做，原檔不動）")
    out(f"  登記表 v{reg} → v{registry.VERSION}、資料夾{_v(fol)} → v{folders.VERSION}；"
        f"會產生 {sum(1 for r in rows if not r.id)} 個 session id")
    if broken:
        out(f"  ⚠ folders.json 讀不到，會原封不動保留：{broken}")
    with tempfile.TemporaryDirectory() as tmp:
        for name in FILES:
            src = Path(cfg.state_dir) / name
            if src.exists():
                shutil.copyfile(src, Path(tmp) / name)
        trial = replace(cfg, state_dir=Path(tmp))
        try:
            rc, lines, _bk, _broken = _upgrade(trial, machine="", now=datetime.now(), backup=False)
        except Exception as e:
            rc, lines = 2, ["", f"{type(e).__name__}: {e}"]
    if rc:
        out("  ❌ 試做的結果不對，真的升級會還原並停下：")
        _say(out, lines[1:])
        return 2
    out("  前後比對：沒有差異")
    return 0


def _v(version):
    return "（沒有）" if version is None else f" v{version}"


def cmd_migrate(cfg, out, *, dry_run=False, revert=False, now=None):
    """sampan migrate [--dry-run | --revert]."""
    from . import migrate_017
    if dry_run and revert:
        out("❌ --dry-run 和 --revert 只能選一個")
        return 2
    now = now or datetime.now()
    if revert:
        try:
            lines = migrate_017.revert(cfg, now=now)
        except Exception as e:
            why = str(e) if isinstance(e, migrate_017.MigrateError) else f"{type(e).__name__}: {e}"
            out(f"❌ {why}")
            return 2
        for line in lines:
            out(line)
        return 0
    if not needed(cfg) and not migrate_017.pending(cfg) and not migrate_017.reverted(cfg):
        out("資料已是新格式（或還沒有資料），不用升級")
        return 0
    if dry_run:
        return _dry_run_all(cfg, out)
    if migrate_017.reverted(cfg):
        if (Path(cfg.state_dir) / "dispatches.db").exists():
            out("❌ 上次降版沒做完（dispatches.db 還在）：先跑 sampan migrate --revert 做完，再跑 sampan migrate")
            return 2
        migrate_017.unmark(cfg)
        out("已移除降版標記，重新升級派工資料")
    return ensure(cfg, out, now=now, touches_dispatches=True)


def _dry_run_all(cfg, out):
    from . import migrate_017
    rc = _dry_run(cfg, out) if needed(cfg) else 0
    if rc or not migrate_017.pending(cfg):
        return rc
    if not needed(cfg):
        out("sampan migrate --dry-run（在複本上試做，原檔不動）")
    try:
        lines = migrate_017.dry_run(cfg)
    except Exception as e:
        out(f"  ❌ 派工試做失敗，真的升級會停下、dispatches.json 不動：{e}")
        return 2
    _say(out, lines)
    if migrate_017.reverted(cfg):
        out("  現在有降版標記：sampan migrate 會先移除它")
    return 0
