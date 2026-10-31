"""Moving the state files to the 0.15 format (registry.json version 3 with session ids, folders.json version 2) and back.

`ensure` runs before every command that reads or writes state. When the files are already new (or there is no registry
yet) it does nothing. Otherwise it backs the files up byte for byte into state/backup-0.15-<stamp>/ (never deleted),
writes the new formats and reads them back: the sessions (names, conversation ids, agent, profile, state), each session's
folder and the folders themselves must be what they were, or both files are restored from the backup. A run cut short
(registry already version 3, folders still version 1) only does the folders step, so ids never change twice.

`cmd_migrate` is `sampan migrate`: the same step by hand, `--dry-run` (a rehearsal on copies, nothing written) and
`--revert` (registry version 2 and folders version 1 again, for a downgrade to 0.14; the ids are dropped).
"""
import json
import os
import shutil
import tempfile
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from . import folders, registry
from .clock import now_iso
from .fsutil import PRIVATE_DIR, PRIVATE_FILE, atomic_write, private_file, write_target
from .machine import registry_machine

FILES = ("registry.json", folders.FOLDERS_FILE, "dispatches.json")  # backed up (those that exist)
RESTORED = ("registry.json", folders.FOLDERS_FILE)  # what a migration writes, so what a failure puts back
BACKUP_PREFIX = "backup-0.15-"
REVERT_NOTE = "降版前請先跑這個；再升級會產生新的 id"
_V14_DROPPED = ("id", "alias")  # Row fields 0.14 does not know: its Row(**s) would refuse the file


def _version(path):
    """The file's "version", or None when it is missing or unreadable (then it is not ours to migrate)."""
    try:
        v = json.loads(Path(path).read_text()).get("version")
    except (OSError, ValueError, AttributeError):
        return None
    return v if type(v) is int else None


def _folders_path(cfg):
    return Path(cfg.state_dir) / folders.FOLDERS_FILE


def needed(cfg):
    """True when the registry is older than version 3 or folders.json is version 1. No registry yet: False."""
    reg = _version(cfg.registry_path)
    if reg is None:
        return False
    return reg < registry.VERSION or _version(_folders_path(cfg)) == 1


def _snap(rows, tree):
    return {"sessions": sorted((r.tmux, r.session_id, r.agent, r.profile, r.state) for r in rows),
            "membership": sorted((n, p) for n, p in tree.sessions.items() if p),
            "folders": sorted(tree.folders.items())}


def _read(cfg):
    rows = registry.load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)
    return rows, folders.load(cfg.state_dir, rows)


def snapshot(cfg):
    """What a migration must keep: sessions (name, conversation id, agent, profile, state), each session's folder and
    the folders with their collapsed state. Never ids. Raises RegistryError / FoldersError for an unreadable file."""
    return _snap(*_read(cfg))


def _diff(before, after):
    labels = {"sessions": "session", "membership": "資料夾歸屬", "folders": "資料夾"}
    lines = []
    for key, label in labels.items():
        b, a = set(before.get(key, ())), set(after.get(key, ()))
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


def _backup(state_dir, now):
    """Copy the state files that exist into a new state/backup-0.15-<stamp>/ (never an existing one) and return it."""
    state_dir = Path(state_dir)
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
        src = state_dir / name
        if src.exists():
            _write_bytes(d / name, src.read_bytes())
    return d


def _restore(state_dir, backup):
    for name in RESTORED:
        saved, target = Path(backup) / name, Path(state_dir) / name
        if saved.exists():
            _write_bytes(target, saved.read_bytes())
        elif target.exists():  # it was not there before the migration
            target.unlink()


def _check(cfg, before, *, want):
    """Re-read both files: why they are not right (a list of lines), or [] when they are."""
    try:
        after = snapshot(cfg)
    except (registry.RegistryError, folders.FoldersError) as e:
        return [f"寫完讀不回來：{e}"]
    versions = (_version(cfg.registry_path), _version(_folders_path(cfg)))
    problems = []
    if versions[0] != want[0] or versions[1] not in (want[1], None):
        problems.append(f"寫完的版本是 登記表 {versions[0]}、資料夾 {versions[1]}，應該是 {want[0]}、{want[1]}")
    diff = _diff(before, after)
    if diff:
        problems += ["寫完前後不一致："] + diff
    return problems


def _fail(cfg, out, backup, why):
    try:
        _restore(cfg.state_dir, backup)
        out(f"❌ 資料升級失敗，已從備份還原 registry.json 與 folders.json（備份在 {backup}）")
    except OSError as e:
        out(f"❌ 資料升級失敗，而且還原也失敗（{e}）：請手動把 {backup} 裡的檔案複製回 {cfg.state_dir}")
    _say(out, why)
    return 2


def _upgrade(cfg, *, machine, now, backup=True):
    """The migration steps. Returns (status, backup dir, snapshot before, message): status "skip" (nothing left to do),
    "refused" (a file is unreadable, nothing written), "failed" (an error after the backup) or "written"."""
    with registry.lock(cfg.state_dir):
        if not needed(cfg):  # another sampan did it while we waited for the lock
            return "skip", None, None, ""
        try:
            rows, tree = _read(cfg)  # both read before anything is written: an unreadable file stops here
        except (registry.RegistryError, folders.FoldersError) as e:
            return "refused", None, None, str(e)
        before = _snap(rows, tree)
        bk = _backup(cfg.state_dir, now) if backup else None
        try:
            if _version(cfg.registry_path) < registry.VERSION:
                registry.write_rows(cfg.registry_path, rows, now_iso(), cfg.history_dir, machine=machine)  # fills the ids
                rows = registry.load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)
        except Exception as e:
            return "failed", bk, before, f"{type(e).__name__}: {e}"
    try:
        if _version(_folders_path(cfg)) == 1:  # outside the lock: the registry lock is not reentrant
            folders.save(cfg.state_dir, folders.load(cfg.state_dir, rows), rows)
    except Exception as e:
        return "failed", bk, before, f"{type(e).__name__}: {e}"
    return "written", bk, before, ""


def _say(out, lines):
    for line in lines:
        out(line if line.startswith("  ") else f"  {line}")


def ensure(cfg, out, *, now=None):
    """Migrate the state when needed (see the module docstring). 0 = nothing to do or done; 2 = not done, files as
    they were (the reason is printed)."""
    if not needed(cfg):
        return 0
    try:
        status, bk, before, msg = _upgrade(cfg, machine=registry_machine(cfg), now=now or datetime.now())
    except Exception as e:  # before the backup (the lock, the backup itself): nothing was written
        out(f"❌ 資料沒有升級（檔案都沒動）：{type(e).__name__}: {e}")
        return 2
    if status == "skip":
        return 0
    if status == "refused":
        out(f"❌ 資料沒有升級（檔案都沒動）：{msg}")
        out("  修好或把檔案移開後再跑 sampan migrate")
        return 2
    problems = [msg] if status == "failed" else _check(cfg, before, want=(registry.VERSION, folders.VERSION))
    if problems:
        return _fail(cfg, out, bk, problems)
    out(f"已把資料升到新格式（備份在 {bk}）")
    return 0


def last_backup(state_dir):
    """The newest state/backup-0.15-* directory, or None."""
    def key(p):  # backup-0.15-YYYYMMDD-HHMMSS[-n]
        rest = p.name[len(BACKUP_PREFIX):]
        return rest[:15], int(rest[16:]) if rest[16:].isdigit() else 1

    found = [p for p in Path(state_dir).glob(BACKUP_PREFIX + "*") if p.is_dir()]
    return max(found, key=key) if found else None


def versions(cfg):
    """(registry version, folders version), None for a missing or unreadable file."""
    return _version(cfg.registry_path), _version(_folders_path(cfg))


def _dry_run(cfg, out):
    try:
        rows, _tree = _read(cfg)
        before = _snap(rows, _tree)
    except (registry.RegistryError, folders.FoldersError) as e:
        out(f"❌ 升級會失敗（檔案讀不到）：{e}")
        return 2
    reg, fol = versions(cfg)
    out("sampan migrate --dry-run（在複本上試做，原檔不動）")
    out(f"  登記表 v{reg} → v{registry.VERSION}、資料夾 v{fol if fol is not None else '（沒有）'} → v{folders.VERSION}；"
        f"會產生 {sum(1 for r in rows if not r.id)} 個 session id")
    with tempfile.TemporaryDirectory() as tmp:
        for name in FILES:
            src = Path(cfg.state_dir) / name
            if src.exists():
                shutil.copyfile(src, Path(tmp) / name)
        trial = replace(cfg, state_dir=Path(tmp))
        try:
            status, _bk, _before, msg = _upgrade(trial, machine="", now=datetime.now(), backup=False)
            problems = [msg] if status in ("failed", "refused") else _check(trial, before, want=(registry.VERSION, folders.VERSION))
        except Exception as e:
            problems = [f"{type(e).__name__}: {e}"]
    if problems:
        out("  ❌ 試做的結果不對，真的升級會還原並停下：")
        _say(out, problems)
        return 1
    out("  前後比對：沒有差異")
    return 0


def _revert(cfg, out, now):
    if not cfg.registry_path.exists():
        out("沒有登記表，不用寫回")
        return 0
    with registry.lock(cfg.state_dir):
        try:
            rows, tree = _read(cfg)
        except (registry.RegistryError, folders.FoldersError) as e:
            out(f"❌ 沒有寫回（檔案都沒動）：{e}")
            return 2
        before = _snap(rows, tree)
        bk = _backup(cfg.state_dir, now)
        try:
            sessions = [{k: v for k, v in asdict(r).items() if k not in _V14_DROPPED} for r in rows]
            body = {"version": 2, "saved_at": now_iso(), "sessions": sessions}
            atomic_write(cfg.registry_path, json.dumps(body, ensure_ascii=False, indent=2) + "\n", mode=PRIVATE_FILE)
        except Exception as e:
            return _fail(cfg, out, bk, [f"{type(e).__name__}: {e}"])
    try:
        if _folders_path(cfg).exists() or tree.folders or tree.sessions:
            data = {"version": 1, "folders": {p: {"collapsed": c} for p, c in tree.folders.items()},
                    "sessions": {n: p for n, p in tree.sessions.items() if p}}  # pending members too, by name
            atomic_write(_folders_path(cfg), json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n",
                         mode=PRIVATE_FILE)
    except Exception as e:
        return _fail(cfg, out, bk, [f"{type(e).__name__}: {e}"])
    problems = _check(cfg, before, want=(2, 1))
    if problems:
        return _fail(cfg, out, bk, problems)
    out(f"已把資料寫回 0.14 的格式：登記表 v2、資料夾 v1（備份在 {bk}）")
    if any(r.alias for r in rows):
        out("  0.14 沒有別名：別名沒有寫回（備份裡還有）")
    out(REVERT_NOTE)
    return 0


def cmd_migrate(cfg, out, *, dry_run=False, revert=False, now=None):
    """sampan migrate [--dry-run | --revert]."""
    if dry_run and revert:
        out("❌ --dry-run 和 --revert 只能選一個")
        return 2
    now = now or datetime.now()
    if revert:
        return _revert(cfg, out, now)
    if not needed(cfg):
        out("資料已是新格式（或還沒有資料），不用升級")
        return 0
    if dry_run:
        return _dry_run(cfg, out)
    return ensure(cfg, out, now=now)
