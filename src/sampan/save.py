import os

from .clock import now_iso
from .fsutil import PRIVATE_FILE, private_dir, try_chmod
from .observe import PROC_ROOT, agent_sessions, observe
from .procs import list_procs
from .registry import RegistryError, load_rows, lock, merge, write_rows
from .tmux import Tmux, unreadable_message


def append_log(cfg, msg):
    private_dir(cfg.save_log.parent)
    with os.fdopen(os.open(cfg.save_log, os.O_WRONLY | os.O_APPEND | os.O_CREAT, PRIVATE_FILE), "a") as fh:
        try_chmod(fh.fileno(), PRIVATE_FILE)
        fh.write(f"{now_iso()} {msg}\n")


def cmd_save(cfg, run, out, *, quiet=False, dry_run=False, proc_root=PROC_ROOT):
    tmux = Tmux(run)
    server = tmux.server_id()
    if server is None:
        append_log(cfg, "skip tmux server 不在，登記表不動")
        if not quiet:
            out("tmux server 沒在跑，登記表不動")
        return 0
    with lock(cfg.state_dir):
        try:
            old = load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)
        except RegistryError as e:
            append_log(cfg, f"error {e}")
            out(f"❌ {e}")
            return 2
        panes = tmux.panes()
        if panes is None:
            msg = f"error {unreadable_message(tmux.unreadable_reason)}，登記表不動"
            append_log(cfg, msg)
            out(f"❌ {msg[6:]}")
            return 1
        if not panes:
            msg = "error tmux server 在，但讀不到任何 pane（可能正在關閉），登記表不動"
            append_log(cfg, msg)
            out(f"❌ {msg[6:]}")
            return 1
        procs = list_procs(run)
        observed, warnings, _skipped = observe(cfg, run, panes, procs, proc_root)
        # there is an agent but it was not recorded (skipped because of several agents, undeterminable, Codex has no thread yet): keep the old row; the reason is already in warnings or is normal
        quiet_keep = agent_sessions(procs, panes) - {ob.tmux for ob in observed}
        now = now_iso()
        res = merge(old, observed, {p.session for p in panes}, now, server, keep_silently=quiet_keep)
        if not dry_run:
            write_rows(cfg.registry_path, res.rows, now, cfg.history_dir)
    warnings += res.warnings
    summary = (f"新增 {len(res.added)}、更新 {len(res.updated)}、改名 {len(res.renamed)}、"
               f"已停止 {len(res.pending)}、警告 {len(warnings)}，共 {len(res.rows)} 列")
    append_log(cfg, f"{'dry-run' if dry_run else 'ok'} {summary}")
    if quiet and not warnings:
        return 0
    out(f"sampan save{'（dry-run，未寫入）' if dry_run else ''}：{summary}")
    for name in res.added:
        out(f"  + {name}")
    for old_name, new_name in res.renamed:
        out(f"  ~ {old_name} → {new_name}")
    for name in res.pending:
        out(f"  … {name}：已停止（sampan restore 接回；不用了就 sampan retire）")
    for w in warnings:
        out(f"  ⚠ {w}")
    return 0
