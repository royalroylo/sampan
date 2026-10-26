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
    nw.add_argument("--folder", help="放進這個資料夾（沒有就建；用 / 分層）；開成功才記")
    nw.add_argument("--message", help="開場訊息，當作 agent 啟動時的第一個 prompt（會建一筆派工）；- 從 stdin 讀")
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
    rt = sub.add_parser("restart", help="重開這些 session：閒置的先停再背景接回原對話，已停止的直接接回，忙的略過")
    rt.add_argument("names", nargs="+", help="session 名稱")
    rn = sub.add_parser("rename", help="改 session 名稱（tmux 與登記表一起改，agent 照常執行）")
    rn.add_argument("name")
    rn.add_argument("new")
    mv = sub.add_parser("move", help="換到另一個目錄繼續同一段對話（自動信任新目錄、更新登記表）")
    mv.add_argument("name")
    mv.add_argument("dest", help="新的工作目錄；不存在會建立")
    mv.add_argument("--timeout", type=int, default=60, help="等 agent 結束／接回的秒數（預設 60）")
    mv.add_argument("--dry-run", action="store_true")
    sw = sub.add_parser("switch", help="換 profile（換帳號）繼續同一段對話，例如額度用完時；已停止的直接用新 profile 接回")
    sw.add_argument("name")
    sw.add_argument("profile")
    sw.add_argument("--timeout", type=int, default=60, help="等 claude 結束／接回的秒數（預設 60）")
    sw.add_argument("--dry-run", action="store_true")
    fg = sub.add_parser("forget", help="從登記表移除一個沒在跑的 session（對話紀錄不刪，會印出手動接回的指令）")
    fg.add_argument("name")
    fg.add_argument("--dry-run", action="store_true")
    for verb, help_ in (("retire", "標記為不再重開"), ("activate", "恢復為重開")):
        sv = sub.add_parser(verb, help=help_)
        sv.add_argument("name")
    sd = sub.add_parser("send", help="傳訊息給一個執行中的 session（對方閒著才送，忙就排隊）")
    sd.add_argument("name")
    sd.add_argument("message", help="訊息；- 從 stdin 讀")
    sd.add_argument("--wait", action="store_true", help="等對方回覆，把回覆印出來")
    sd.add_argument("--timeout", type=int, default=600, help="--wait 最多等幾秒（預設 600；逾時結束碼 124，派工照樣留著）")
    ds = sub.add_parser("dispatches", usage="sampan dispatches [--all] | sampan dispatches show <編號> [--wait 秒] | sampan dispatches cancel <編號>",
                        help="列派工（預設只列排隊中與等回覆的）；show <編號> 看狀態與完整回覆；cancel <編號> 取消排隊中的、或不再等已送出的")
    ds.add_argument("action", nargs="?", choices=["cancel", "show"], help=argparse.SUPPRESS)
    ds.add_argument("id", nargs="?", help=argparse.SUPPRESS)
    ds.add_argument("--all", action="store_true", help="也列已完成、失敗、取消的")
    ds.add_argument("--wait", type=int, default=0, metavar="秒", help="show：最多等幾秒等它做完（逾時結束碼 124）")
    fd = sub.add_parser("folder", usage="sampan folder list | add <路徑> | rename <路徑> <新名稱> | rm <路徑> | move <名稱或路徑>... <目的|/>",
                        help="session 資料夾：list 看樹、add 建、rename 改名、rm 刪（裡面的搬上一層）、move 搬（/ 是最外層）；只動 folders.json，不動任何 session")
    fd.add_argument("action", choices=["list", "add", "rename", "rm", "move"])
    fd.add_argument("args", nargs="*", help=argparse.SUPPRESS)
    fd.add_argument("--merge", action="store_true", help="同名的資料夾已存在時合併進去")
    mc = sub.add_parser("mcp", help="MCP server：讓 agent 用工具傳訊息給其他 session").add_subparsers(dest="action", required=True)
    mc.add_parser("serve", help="stdio MCP server（由 Claude Code／Codex 啟動，不是給人直接跑的）")
    for verb, help_ in (("install", "把 sampan MCP 裝進各啟用的 Claude profile 與 Codex"), ("uninstall", "從各 profile 與 Codex 移除 sampan MCP")):
        mi = mc.add_parser(verb, help=help_)
        mi.add_argument("--dry-run", action="store_true")
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
    pa = pf.add_parser("add", help="新增 profile：建立 ~/.claude-profiles/<名稱>（--codex 則是 ~/.codex-profiles/<名稱>）並登入；--dir 用既有目錄（不建、不登入）")
    pa.add_argument("name")
    pa.add_argument("--dir", type=Path, help="既有的設定目錄（Claude 或 Codex）")
    pa.add_argument("--codex", action="store_true", help="新增 Codex profile（用 codex login 登入）")
    pf.add_parser("list", help="列出每個 profile：類型、目錄、帳號、使用中／停用、執行中 session 數")
    pr = pf.add_parser("rename", help="改名（目錄不動；有 session 在跑時拒絕）")
    pr.add_argument("old")
    pr.add_argument("new")
    pd = pf.add_parser("disable", help="停用（目錄保留；預設 profile 不能停用；有 session 在跑時拒絕）")
    pd.add_argument("name")
    pe = pf.add_parser("enable", help="恢復停用的 profile")
    pe.add_argument("name")
    pg = pf.add_parser("forget", help="從設定檔拿掉已停用的 profile，連同登記表裡它的 session（目錄不動，會印出手動接回的指令）")
    pg.add_argument("name")

    sl = sub.add_parser("statusline", help="Claude Code 的狀態列：記下 5h／週額度，再交給 -- 後面原本的狀態列指令（沒有就自己印一行）")
    sl.add_argument("rest", nargs=argparse.REMAINDER, help="[-- 原本的狀態列指令…]")

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


def _statusline(rest):
    """sampan statusline [-- cmd…]: run by Claude Code on every status-line refresh. Light imports only; a config
    that does not load still runs the wrapped command (only the 額度 is not recorded). The wrapped command's stdout
    goes to stdout unchanged; sampan's own errors only ever go to stderr."""
    import os

    if rest[:1] in (["install"], ["uninstall"]):
        return _statusline_setup(rest[0], rest[1:])
    from .usage import cmd_statusline
    if rest[:1] == ["--"]:
        rest = rest[1:]
    try:
        cfg = load_config(paths.config_path(), state_dir=paths.state_dir())
    except Exception:
        cfg = None
    try:
        stdin = sys.stdin.buffer.read() if sys.stdin is not None else b""
    except Exception:
        stdin = b""
    return cmd_statusline(cfg, out, list(rest), stdin=stdin, env=os.environ)


def _statusline_setup(action, rest):
    """sampan statusline install|uninstall [--profile P] [--yes]: interactive, so normal imports are fine."""
    from . import statusline_setup
    from .doctor import current_sampan
    ap = argparse.ArgumentParser(prog=f"sampan statusline {action}")
    ap.add_argument("--profile", action="append", default=[], help="只處理這個 profile（可重複）；預設全部使用中的 Claude profile")
    ap.add_argument("--yes", "-y", action="store_true", help="不再問確定")
    args = ap.parse_args(rest)
    try:
        cfg = load_config(paths.config_path(), state_dir=paths.state_dir())
    except ConfigError as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2
    unknown = [n for n in args.profile if n not in cfg.profiles]
    if unknown:
        out(f"❌ 找不到使用中的 Claude profile：{', '.join(unknown)}")
        return 2
    profiles = [cfg.profiles[n] for n in args.profile] or list(cfg.profiles.values())

    def ask(prompt):
        try:
            return input(prompt)
        except EOFError:
            return ""
    try:
        if action == "install":
            return statusline_setup.install(cfg, out, ask, profiles, current_sampan(), yes=args.yes)
        return statusline_setup.uninstall(cfg, out, ask, profiles, yes=args.yes)
    except KeyboardInterrupt:
        out("\n已取消")
        return 130


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
    pin = latest
    if update.base_version(current) is None:  # "unknown" (or unparseable): never offer an upgrade by comparison
        if not force:
            out("讀不到目前版本；要重新安裝請加 --force")
            return 1
        if latest is None:
            out("查不到最新版（離線或連不到 GitHub）")
            return 1
        question = f"要重新安裝 v{latest}？(y/N): "
    elif is_newer:
        question = f"更新到 v{latest}？(y/N): "
    elif latest is None:
        if not force:
            out("查不到最新版")
            return 1
        pin = update.clean_release(current)
        if pin is None:  # a dev build and no known release: nothing sensible to reinstall
            out("查不到最新版（離線或連不到 GitHub）")
            return 1
        question = f"要重新安裝 v{pin}？(y/N): "
    elif update.newer(latest, current):  # a dev build ahead of the newest release
        if not force:
            out(f"目前版本比最新版新（v{latest}）")
            return 0
        question = f"目前版本比最新版新；要改裝 v{latest} 嗎？(y/N): "
    else:
        if not force:
            out("已是最新版")
            return 0
        question = f"要重新安裝 v{latest}？(y/N): "
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
    ok = update.do_update(out, current=current, stream=stream, run=run, which=which, latest=pin,
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
    if argv[:1] == ["statusline"] and argv[1:] not in (["-h"], ["--help"]):
        # handled before argparse: the wrapped command's own --flags pass through unchanged
        return _statusline(argv[1:])
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


def _agent_refused(cfg, caller, out):
    """While [mcp] confirm is on, an agent (not a person at the terminal) may not run an acting command through its
    shell: print why and return True. caller None = not looked up (confirm off)."""
    from .send import AGENT_REFUSED, caller_kind
    if not cfg.mcp_confirm or caller is None or caller_kind(caller) == "terminal":
        return False
    out("❌ " + AGENT_REFUSED)
    return True


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
            from .ui.daemon_watch import make_watcher
            from .ui.dispatch_watch import make_dispatch_watcher
            return curses_ui(cfg, run, out, **where, updater=checker, argv=argv, watcher=make_watcher(cfg, run),
                             dispatch_watcher=make_dispatch_watcher(cfg, run))
        import subprocess

        from .prompts import plain_prompt
        return cmd_ui(cfg, run, out, **where, picker_fn=None, restore_picker_fn=None, adopt_picker_fn=None, loop=True,
                      attach_fn=subprocess.call, updater=checker, argv=argv, prompt_fn=plain_prompt(out, input))
    if cmd == "claude":
        from .launch import cmd_claude, disabled_problem
        rest = list(args.rest)
        profile = rest.pop(0) if rest and not rest[0].startswith("-") else None
        if profile is not None and disabled_problem(cfg, profile):
            out(disabled_problem(cfg, profile))
            return 2
        if profile is not None and profile not in cfg.profiles:
            # a word that is not a profile must not be handed to claude as a prompt (a mistyped profile name would start a new conversation in the default profile)
            out(f"❌ 沒有這個 profile：{profile}（可用：{', '.join(cfg.profiles)}）")
            return 2
        return cmd_claude(cfg, run, out, input, profile, rest)
    if cmd == "codex":
        from .launch import cmd_codex
        return cmd_codex(cfg, list(args.rest), out=out)
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
    if cmd == "send":
        from .send import cmd_send, command_caller
        # an agent running this through its shell tool is that session (or unregistered), not a person at the terminal
        caller = command_caller(cfg, run, os.getpid())
        if _agent_refused(cfg, caller, out):
            return 2
        message = sys.stdin.read() if args.message == "-" else args.message
        return cmd_send(cfg, run, out, args.name, message, wait=args.wait, timeout=args.timeout, caller=caller)
    if cmd == "dispatches":
        from .send import cmd_cancel, cmd_dispatches, cmd_show
        if args.action in ("cancel", "show"):
            if not args.id:
                out(f"❌ 用法：sampan dispatches {args.action} <編號>")
                return 2
            if args.action == "show":
                return cmd_show(cfg, run, out, args.id, wait=args.wait)
            from .send import command_caller
            if _agent_refused(cfg, command_caller(cfg, run, os.getpid()) if cfg.mcp_confirm else None, out):
                return 2
            return cmd_cancel(cfg, out, args.id)
        return cmd_dispatches(cfg, out, all_=args.all)
    if cmd == "folder":
        from .folder_cmd import cmd_folder
        return cmd_folder(cfg, out, args.action, list(args.args), merge=args.merge)
    if cmd == "mcp" and args.action in ("install", "uninstall"):
        from . import mcp_install
        from .doctor import current_sampan
        from .runner import run
        if args.action == "install":
            return mcp_install.install(cfg, run, out, sampan_path=current_sampan(), dry_run=args.dry_run)
        return mcp_install.uninstall(cfg, run, out, dry_run=args.dry_run)
    if cmd == "mcp":
        from . import mcp_server
        from .runner import no_stdin
        run = no_stdin(run)  # a child must never read the JSON-RPC stream on our stdin
        ppid = os.getppid()
        caller = mcp_server.identify_caller(cfg, run, ppid)
        proto = sys.stdout
        sys.stdout = sys.stderr  # stdout carries only protocol messages: anything printed by accident goes to stderr
        try:
            return mcp_server.serve(cfg, run, sys.stdin.buffer, proto, caller=caller,
                                    resolve=lambda: mcp_server.lookup_caller(cfg, run, ppid))
        finally:
            sys.stdout = proto
    if cmd == "status":
        from .status import cmd_status
        return cmd_status(cfg, run, out)
    if cmd == "restart":
        from . import restart, status
        own = os.environ.get("TMUX_PANE")
        plan = restart.plan_restart(status.status_rows(cfg, run), args.names, resume_stopped=True, own_pane=own)
        if not plan.restart and not plan.resume:
            out("沒有可重開的 session。" + (restart.skip_text(plan.skip) if plan.skip else ""))
            return 1
        on_prompt, _want = _prompt_fn()
        return restart.cmd_restart(cfg, run, out, input, plan, own_pane=own, on_prompt=on_prompt)
    if cmd in ("new", "stop", "move", "switch", "rename", "forget", "retire", "activate"):
        from . import session_run
        if cmd == "new":
            on_prompt, want = _prompt_fn()
            extra = {}
            if args.folder is not None:
                extra["folder"] = args.folder
            if args.message is not None:
                from .send import command_caller
                caller = command_caller(cfg, run, os.getpid())
                if _agent_refused(cfg, caller, out):
                    return 2
                extra["message"] = sys.stdin.read() if args.message == "-" else args.message
                extra.update(caller=caller, notify=False)
            rc = session_run.cmd_new(cfg, run, out, args.name, args.target, args.cwd, dry_run=args.dry_run,
                                     on_prompt=on_prompt, **extra)
            return _go_in(run, want, rc)
        if cmd == "stop":
            return session_run.cmd_stop(cfg, run, out, names=tuple(args.names), profile=args.profile,
                                        force=args.force, dry_run=args.dry_run, timeout=args.timeout,
                                        keep_tmux=args.keep_tmux,
                                        own_pane=os.environ.get("TMUX_PANE"))
        if cmd == "rename":
            return session_run.cmd_rename(cfg, run, out, args.name, args.new)
        if cmd == "forget":
            return session_run.cmd_forget(cfg, run, out, args.name, dry_run=args.dry_run)
        if cmd == "switch":
            on_prompt, want = _prompt_fn()
            rc = session_run.cmd_switch(cfg, run, out, args.name, args.profile, dry_run=args.dry_run,
                                        timeout=args.timeout, own_pane=os.environ.get("TMUX_PANE"),
                                        on_prompt=on_prompt)
            return _go_in(run, want, rc)
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
        from . import profile_ops
        if args.action == "list":
            return profile_ops.cmd_profile_list(cfg, run, out)
        if args.action == "rename":
            return profile_ops.cmd_profile_rename(cfg, run, out, args.old, args.new)
        if args.action == "disable":
            return profile_ops.cmd_profile_disable(cfg, run, out, args.name)
        if args.action == "enable":
            return profile_ops.cmd_profile_enable(cfg, run, out, args.name)
        if args.action == "forget":
            return profile_ops.cmd_profile_forget(cfg, run, out, input, args.name)
        from .init import cmd_profile_add
        return cmd_profile_add(cfg, run, out, args.name, dir=args.dir, home=cfg.home, codex=args.codex)
    if cmd == "doctor":
        from .doctor import cmd_doctor
        return cmd_doctor(cfg, run, out)
    return 2
