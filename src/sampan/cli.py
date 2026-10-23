import argparse
import shutil
import sys
from pathlib import Path

from . import paths
from .config import ConfigError, load_config
from .update import current_version as version  # the single implementation, shared with doctor and ui


def out(msg=""):
    print(msg, flush=True)


def build_parser():
    p = argparse.ArgumentParser(prog="sampan", description="Claude Code／Codex 的 tmux session 與 plugin 管理（不帶指令 = ui）")
    p.add_argument("--version", action="version", version=f"sampan {version()}")
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    ui = sub.add_parser("ui", help="互動選單：選 session 接上、停掉、接回、接手、retire/activate、開新的")
    ui.add_argument("--plain", action="store_true", help="用輸入編號的純文字選單（不用方向鍵）")
    cl = sub.add_parser("claude", help="以指定 profile 啟動 Claude Code（不給 profile 時列選單）")
    cl.add_argument("rest", nargs=argparse.REMAINDER, help="[profile] [claude 參數…]")
    cx = sub.add_parser("codex", help="啟動 Codex")
    cx.add_argument("rest", nargs=argparse.REMAINDER, help="[codex 參數…]")
    nw = sub.add_parser("new", help="開一個新 session 並登記")
    nw.add_argument("name")
    nw.add_argument("target", help="claude:<profile>、<profile>（當成 claude）或 codex")
    nw.add_argument("cwd")
    nw.add_argument("--dry-run", action="store_true")
    rs = sub.add_parser("restore", help="挑選並逐一接回登記表裡的原對話（不開新對話、不送訊息）")
    rs.add_argument("names", nargs="*", help="只看這些 session（仍會先確認）")
    rs.add_argument("--all", action="store_true", help="勾選所有可接回的（仍會先確認）")
    rs.add_argument("--dry-run", action="store_true")
    ad = sub.add_parser("adopt", help="接手 tmux 外的終端機（例如 iTerm、Terminal）裡的 claude／codex：你自己 /exit 後在 tmux 接回同一段對話")
    ad.add_argument("--dry-run", action="store_true", help="只列出會接手哪些、用什麼指令；不等待、不改任何東西")
    sp = sub.add_parser("stop", help="存檔後讓閒置的 claude／codex 正常結束")
    sp.add_argument("names", nargs="*")
    sp.add_argument("--profile")
    sp.add_argument("--force", action="store_true", help="逾時未結束的送 SIGTERM")
    sp.add_argument("--timeout", type=int, default=90, help="等待秒數（預設 90）")
    sp.add_argument("--keep-tmux", action="store_true", help="agent 結束後保留 tmux session（預設：只剩那個 pane 就關掉）")
    sp.add_argument("--dry-run", action="store_true")
    rn = sub.add_parser("rename", help="改 session 名稱（tmux 與登記表一起改，agent 照常執行）")
    rn.add_argument("name")
    rn.add_argument("new")
    mv = sub.add_parser("move", help="換到另一個目錄繼續同一段對話（自動信任新目錄、更新登記表）")
    mv.add_argument("name")
    mv.add_argument("dest", help="新的工作目錄；不存在會建立")
    mv.add_argument("--timeout", type=int, default=60, help="等 agent 結束／接回的秒數（預設 60）")
    mv.add_argument("--dry-run", action="store_true")
    for verb, help_ in (("retire", "標記為不再重開"), ("activate", "恢復為重開")):
        sv = sub.add_parser(verb, help=help_)
        sv.add_argument("name")
    sub.add_parser("status", help="登記表與實際狀態的差異、最後活動、context 用量")
    sv = sub.add_parser("save", help="記錄每個 tmux session 的 agent、profile 與對話 id")
    sv.add_argument("--quiet", action="store_true", help="沒有警告時不輸出（給 launchd 用）")
    sv.add_argument("--dry-run", action="store_true")
    pl = sub.add_parser("plugins", help="各 profile 的 plugin").add_subparsers(dest="action", required=True)
    pl.add_parser("status", help="profile × plugin 版本對照表")
    sy = pl.add_parser("sync", help="依設定檔安裝／更新各 profile 的 plugin")
    sy.add_argument("--profile")
    sy.add_argument("--dry-run", action="store_true")
    sy.add_argument("--prune", action="store_true",
                    help="也移除清單外的 plugin／marketplace 與 local scope 的重複安裝（先用 --dry-run 看）")
    sub.add_parser("doctor", help="檢查已知的設定問題")
    it = sub.add_parser("init", help="偵測 profile、產生設定檔（已有設定時只提議新增的項目）、hook 與自動存檔排程")
    it.add_argument("--no-launchd", action="store_true", help="不安裝自動存檔排程")
    it.add_argument("--yes", action="store_true", help="所有問題都用預設值，不讀 stdin（沒有終端機時用）")
    it.add_argument("--hook", action="store_true", help="詢問是否安裝 SessionStart hook（預設不問）")
    up = sub.add_parser("update", help="檢查並更新 sampan（不看快取）")
    up.add_argument("--yes", action="store_true", help="不問，直接更新（或 --force 時直接重新安裝）")
    up.add_argument("--force", action="store_true", help="已是最新版也重新安裝")
    pf = sub.add_parser("profile", help="profile 管理").add_subparsers(dest="action", required=True)
    pa = pf.add_parser("add", help="新增 profile：建立 ~/.claude-profiles/<名稱> 並登入；--dir 用既有目錄（不建、不登入）")
    pa.add_argument("name")
    pa.add_argument("--dir", type=Path, help="既有的 Claude 設定目錄")

    hk = sub.add_parser("hook")  # called by Claude Code's SessionStart hook, not listed in the help
    hk.add_argument("event", choices=["session-start"])
    sub._choices_actions = [a for a in sub._choices_actions if a.dest != "hook"]
    return p


def _hook_session_start():
    # swallow every error, print nothing, exit 0: a hook's stdout goes into the conversation and a failure must not block claude from starting
    try:
        import os

        from .clock import now_iso
        from .hook import run_session_start
        from .procs import list_procs
        from .runner import run
        run_session_start(sys.stdin, os.environ, state_dir=paths.state_dir(),
                          procs_fn=lambda: list_procs(run), now=now_iso)
    except BaseException:
        pass
    sys.exit(0)


def _current_tmux_session(run):
    import os
    if not os.environ.get("TMUX"):
        return None
    r = run(["tmux", "-u", "display-message", "-p", "#S"])
    return r.stdout.strip() if r.returncode == 0 else None


def _init(no_launchd, yes=False, hook=False):
    # stdin only: install.sh runs `init <"$TTY"` and its stdout may be a pipe
    if not yes and not (sys.stdin and sys.stdin.isatty()):
        out("sampan init 會問幾個會寫入檔案的問題，請在終端機直接執行。腳本裡要全部用預設值，加 --yes。")
        return 2
    from .doctor import current_sampan
    from .init import cmd_init
    from .runner import run
    try:
        return cmd_init(paths.config_path(), paths.state_dir(), run, out, input, home=Path.home(),
                        sampan_path=current_sampan(), no_launchd=no_launchd, yes=yes, hook=hook)
    except KeyboardInterrupt:
        out("\n已取消")
        return 130


def _update_cmd(yes, force, *, run=None, stream=None, which=None, ask=input, isatty=None, current=None,
                state_dir=None):
    """sampan update: always a fresh check (no cache), then update or, with force, reinstall. Needs no config file.
    Exits after do_update; no restart is exec'd. 0 ok / up to date / cancelled, 1 failed or latest unknown, 2 needs a terminal."""
    from . import update
    run = run or update._run
    stream = stream or update._stream
    which = which or shutil.which
    current = current or version()
    tty = isatty() if isatty is not None else bool(sys.stdin and sys.stdin.isatty())
    latest, _at = update.check(state_dir or paths.state_dir(), run=run, force=True)
    out(f"目前 {current}，最新 {latest or '查不到'}")
    is_newer = update.newer(current, latest)
    if not is_newer and not force:
        if latest is None:
            out("查不到最新版")
            return 1
        out("已是最新版")
        return 0
    question = f"更新到 v{latest}？(y/N): " if is_newer else f"要重新安裝 v{latest or current}？(y/N): "
    if not yes:
        if not tty:
            out("要更新請在終端機執行，或加 --yes")
            return 2
        try:
            answer = ask(question)
        except EOFError:
            answer = ""
        if (answer or "").strip().lower() != "y":
            out("已取消")
            return 0
    ok = update.do_update(out, current=current, stream=stream, run=run, which=which, latest=latest,
                          force=not is_newer, reopen=False)
    return 0 if ok else 1


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv[:2] == ["hook", "session-start"]:
        _hook_session_start()
    if argv[:1] == ["start"]:
        # old command: whatever arguments follow, only show the rename notice
        out("❌ sampan start 已改名為 sampan restore（sampan restore [名稱…] [--all] [--dry-run]）")
        return 2
    if not argv:
        args = argparse.Namespace(cmd="ui", plain=False)
    elif argv[0] in ("claude", "codex") and argv[1:] != ["-h"] and argv[1:] != ["--help"]:
        # argparse's REMAINDER cannot hold back a leading --flag (e.g. --resume x); claude/codex arguments pass through unchanged
        args = argparse.Namespace(cmd=argv[0], rest=argv[1:])
    else:
        args = build_parser().parse_args(argv)
    if args.cmd == "init":
        return _init(args.no_launchd, args.yes, args.hook)
    if args.cmd == "update":
        try:
            return _update_cmd(args.yes, args.force)
        except KeyboardInterrupt:
            out("\n已取消")
            return 130
    try:
        cfg = load_config(paths.config_path(), state_dir=paths.state_dir())
    except ConfigError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2
    try:
        return _dispatch(cfg, args)
    except KeyboardInterrupt:
        out("\n已取消")
        return 130


def _prompt_fn(isatty=None):
    """sampan new/restore in an interactive terminal (stdin and stdout are both a tty): ask by number when an agent stops at a startup menu.
    Returns (on_prompt or None, list of names of sessions where "go in and handle it myself" was chosen)."""
    tty = isatty() if isatty is not None else (sys.stdin.isatty() and sys.stdout.isatty())
    if not tty:
        return None, []
    from .prompts import plain_prompt, recording
    return recording(plain_prompt(out, input))


def _go_in(run, want, rc):
    """"Go in and handle it myself" was chosen: attach to that session (switch-client inside tmux, exec tmux attach outside)."""
    if not want:
        return rc
    import os

    from .ui import _attach
    _attach(run, out, want[-1], _current_tmux_session(run), os.execvp)
    return rc


def _dispatch(cfg, args):
    import os

    from .runner import run

    cmd = args.cmd
    if cmd == "ui":
        from .ui import cmd_ui, curses_ui
        from .update import Checker
        interactive = not args.plain and sys.stdin.isatty() and sys.stdout.isatty()
        where = dict(current_session=_current_tmux_session(run), own_pane=os.environ.get("TMUX_PANE"))
        checker = Checker(version(), cfg.state_dir)
        checker.load_cache()
        argv = sys.argv[1:]  # after a successful update with U, re-run with the same arguments
        if interactive:
            checker.start()  # check for a new version in the background: the list appears first
            return curses_ui(cfg, run, out, **where, updater=checker, argv=argv)
        import subprocess

        from .prompts import plain_prompt
        return cmd_ui(cfg, run, out, **where, picker_fn=None, restore_picker_fn=None, adopt_picker_fn=None, loop=True,
                      attach_fn=subprocess.call, updater=checker, argv=argv, prompt_fn=plain_prompt(out, input))
    if cmd == "claude":
        from .launch import cmd_claude
        rest = list(args.rest)
        profile = rest.pop(0) if rest and not rest[0].startswith("-") else None
        if profile is not None and profile not in cfg.profiles:
            # a word that is not a profile must not be handed to claude as a prompt (a mistyped profile name would start a new conversation in the default profile)
            out(f"❌ 沒有這個 profile：{profile}（可用：{', '.join(cfg.profiles)}）")
            return 2
        return cmd_claude(cfg, run, out, input, profile, rest)
    if cmd == "codex":
        from .launch import cmd_codex
        return cmd_codex(cfg, list(args.rest))
    if cmd == "save":
        from .save import cmd_save
        return cmd_save(cfg, run, out, quiet=args.quiet, dry_run=args.dry_run)
    if cmd == "restore":
        from .restore import cmd_restore
        on_prompt, want = _prompt_fn()
        rc = cmd_restore(cfg, run, out, input, names=tuple(args.names), select_all=args.all, dry_run=args.dry_run,
                         on_prompt=on_prompt)
        return _go_in(run, want, rc)
    if cmd == "adopt":
        from .adopt import cmd_adopt
        from .ui import curses_adopt_checklist
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
        return cmd_adopt(cfg, run, out, input, dry_run=args.dry_run,
                         picker_fn=curses_adopt_checklist if interactive else None)
    if cmd == "status":
        from .status import cmd_status
        return cmd_status(cfg, run, out)
    if cmd in ("new", "stop", "move", "rename", "retire", "activate"):
        from . import session_run
        if cmd == "new":
            on_prompt, want = _prompt_fn()
            rc = session_run.cmd_new(cfg, run, out, args.name, args.target, args.cwd, dry_run=args.dry_run,
                                     on_prompt=on_prompt)
            return _go_in(run, want, rc)
        if cmd == "stop":
            return session_run.cmd_stop(cfg, run, out, names=tuple(args.names), profile=args.profile,
                                        force=args.force, dry_run=args.dry_run, timeout=args.timeout,
                                        keep_tmux=args.keep_tmux,
                                        own_pane=os.environ.get("TMUX_PANE"))
        if cmd == "rename":
            return session_run.cmd_rename(cfg, run, out, args.name, args.new)
        if cmd == "move":
            return session_run.cmd_move(cfg, run, out, args.name, args.dest, dry_run=args.dry_run,
                                        timeout=args.timeout, own_pane=os.environ.get("TMUX_PANE"))
        return session_run.cmd_set_state(cfg, out, args.name, "retired" if cmd == "retire" else "active")
    if cmd == "plugins":
        from . import plugins
        if args.action == "status":
            return plugins.cmd_plugins_status(cfg, out)
        return plugins.cmd_sync(cfg, run, out, profile=args.profile, dry_run=args.dry_run, prune=args.prune)
    if cmd == "profile":
        from .init import cmd_profile_add
        return cmd_profile_add(cfg, run, out, args.name, dir=args.dir, home=cfg.home)
    if cmd == "doctor":
        from .doctor import cmd_doctor
        return cmd_doctor(cfg, run, out)
    return 2
