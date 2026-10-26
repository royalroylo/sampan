# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Session folders, several levels deep, kept in `folders.json` in the state directory (0600, atomic, written under the registry lock). They only organise the display: no folder action stops, forgets or changes a session or writes the registry, and `sampan save`, `sampan restore` and the reattach after a reboot never read or write the file. Reading corrects itself for display only (unknown sessions dropped, sessions not in the file at the top level, missing folders added); a file that cannot be read, is not JSON or has an unknown version gives the flat list with one notice (「資料夾設定讀不到：…，先照原樣列出」) and is never overwritten.
- Folders on the `sampan` screen: subfolders then sessions at each level, both by name, sessions in no folder last; folder rows `▾ 名稱 (n)` / `▸` with 「n 忙」 when folded; sessions indented. Enter (or → / ←) folds and unfolds and the state is remembered; space checks folders too; `g` wraps the checked items (or the row under the cursor) into a new folder at their deepest common parent; `f` moves them into a folder picked from a list (「（最外層）」, 「新資料夾…」, then every folder except the moved ones and what is inside them); `e` / `X` on a folder row rename it or delete it (its contents move up one level); `n` puts the new session in the folder under the cursor. A taken name asks 「同一層已經有「X」，合併進去？(y/N)」. Search lists matching sessions only, with their folder path dim before the name. The card of a folder row shows its path, session count, the count per state and its keys. Folder actions are logged in `actions.log` (`[資料夾]`). `--plain` lists the sessions in the tree's order with the folder path before each name.
- `sampan folder list|add|rename|rm|move` edits the folders from a terminal or a script (`--merge` to merge into a taken name), and `sampan new … --folder <path>` puts a new session in a folder (created when missing) once it started. `sampan rename`, `sampan forget` and `sampan profile forget` keep `folders.json` in step, only warning when that fails.
- `S` on the `sampan` screen restarts without going in: idle running sessions are stopped normally and reattached to the same conversation in the background; a stopped one is reattached when `S` is pressed on its own row (or it is checked). On a folder row `S` (and `s`) acts on every session under it, folded ones too, and leaves stopped ones stopped unless their own row is checked as well. The confirm line names what will restart and what is skipped and why (`將重開：A、B　略過：C（忙）　確定？(y/N)`), and the `s` / `S` confirm lines shorten long lists (「等 n 個」) to fit the screen width, so the question always shows; busy ones, the pane sampan runs in, retired ones and unknown states are skipped and listed with their reason at the end. The info card shows `S 重開` (`S 背景接回` on a stopped row). `--plain` has `S` too, with the same question. The key help line now lists `S 重開` and `g 包資料夾　f 搬進資料夾`; the digits, `w`, `Enter` and `A` are left out of it for room but still work.
- `sampan restart <name>...` does the same from a terminal without asking, prints the skipped ones with their reason and returns the worst exit code.

### Changed
- Approval of `send`, `new_session` and `cancel_dispatch` (`[mcp] confirm = true`, the default) now goes through the agent's own permission prompt instead of sampan's MCP elicitation form. The three tools carry `_meta["anthropic/requiresUserInteraction"] = true`, so Claude Code asks on every call in the terminal (also in auto and bypass modes, no "don't ask again"); in Remote Control it is expected to show the full prompt with no one-tap approve, not yet verified on a phone; Codex asks through `approval_mode = "prompt"`. Fail closed: with `confirm = true` the tools run only for clients `claude-code` and `codex-mcp-client` (others get 「這個 client 不會先問使用者；要用請在 config 設 [mcp] confirm = false」) and only when `sampan mcp serve`'s direct parent process is `claude` or `codex` (JSON-RPC piped in from a shell is refused; the check looks only at the process name, so like `tmux send-keys` or editing `[mcp] confirm` it does not stop an agent that deliberately fakes the name, e.g. `exec -a claude`, or works around sampan), and for Codex only when the tool is `approval_mode = "prompt"` in both the `CODEX_HOME` Codex runs with and sampan's Codex home (else 「Codex 的 sampan 工具沒設成每次詢問（<path>）：跑 sampan mcp install」). `codex exec` with approval policy `never` refuses these tools (verified) rather than running them. Whether Codex under `--dangerously-bypass-approvals-and-sandbox` still asks is undocumented.
- The sampan approval form is gone, including its "edit the message" field: say No in the prompt and ask the agent to change the message.
- With `confirm = true`, an agent (not a person at the terminal) running `sampan send`, `sampan new … --message` or `sampan dispatches cancel` is refused with exit 2 (「agent 請改用 sampan MCP 的工具（會先問使用者）；要讓 agent 直接用指令，在 config 設 [mcp] confirm = false」). A person at a terminal is unaffected. Still not stopped: an agent typing into tmux panes itself (`tmux send-keys`); Claude Code's sandbox can, sampan does not set it up.
- `sampan mcp install` writes `[mcp_servers.sampan.tools.<tool>] approval_mode = "prompt"` for the three tools into Codex's `config.toml` (with `confirm = false`, or on `uninstall`, it removes only the tables set to `prompt`; user-set `approve` tables are left). It backs the file up first as `config.toml.bak-sampan-<YYYYmmdd-HHMMSS>`, changes only sampan's tables (every other byte, CRLF and comments included, is kept), and reads and writes UTF-8. If the user picks "always allow" in Codex, which rewrites it to `approve`, `sampan doctor` reports it and `sampan mcp install` puts `prompt` back. Changing `[mcp] confirm` takes effect for Claude when its MCP server restarts and for Codex after `sampan mcp install`.
- The source line of a dispatch now reads `〔sampan 轉送｜來自 <who>｜派工 <id>｜直接回覆文字即可，sampan 會帶回〕`, telling the receiving agent to answer in text; completion and failure notices are unchanged.

## [0.11.1] - 2026-10-27

### Fixed
- `sampan send` (and completion notices) no longer keep a dispatch queued as 「對方畫面停在選單或提示」 when an idle Claude shows its session title in the top rule of the input box (「──── <title> ─」, Claude Code 2.1.3xx); the box is recognised and its dim suggestion still counts as empty.

## [0.11.0] - 2026-10-26

### Added
- `sampan send <name> <message|-> [--wait] [--timeout s]` hands a message to a running registered session as a dispatch, starting with a source line (「〔sampan 轉送｜來自 終端機｜派工 <id>〕」). Claude gets it pasted from a tmux buffer and Enter, only while it is idle and its prompt box is empty (a draft keeps it queued, never cleared); Codex gets it through `codex queue`. A pane in tmux copy mode keeps it queued too. `--wait` prints the reply to this message (exit 0), the reason of a failure (exit 1), or 「還在做」 after the timeout, 600 s by default (exit 124, the dispatch stays); Ctrl-C leaves it as well (exit 130); without `--wait` a queued one is exit 0 too. A `/clear`, another conversation or a stopped target fails the dispatch with 「找不到回覆」 or the reason; another message typed before any answer fails it with 「對方沒回這則就換了下一則」, a turn that ended without reply text with 「對方這輪結束了，但沒有回覆文字」; text from elsewhere is never returned. Delivery is at most once: the record is written as `sent` (「送出中」) before any key or `codex queue`, so a sampan killed mid-delivery never sends it again, and a `codex queue` timeout fails as 「可能已送出，請確認」 instead of being retried. Run by an agent through its shell tool, `sampan send` is labelled as that session (or 「未登記的 session」), not 「終端機」, and sending to itself is refused.
- `sampan new <name> <target> <dir> --message <message|->` sends the first message as the agent's startup prompt (`claude "$(cat <outbox file>)"`, `codex "$(cat …)"`), after any trust prompt, and records a dispatch that starts as `sent`; it is not failed while the new session is still starting (a Codex without a conversation yet, a Claude not seen yet, a trust prompt): its reason says 「對方 session 還在啟動」. Without `--message` nothing changes.
- `sampan dispatches [--all]` lists dispatches (columns aligned by display width); `sampan dispatches show <id> [--wait N]` advances one and prints its state, reason and whole reply (exit 124 when still going after `--wait`); `sampan dispatches cancel <id>` cancels one still queued, or stops waiting for the reply of a sent one (「不再等回覆」). Sending, delivery and cancelling are logged in `actions.log`.
- `sampan mcp serve`: a stdio MCP server (standard library only) with the tools `list_sessions`, `send` (`wait` up to 60 s), `new_session`, `dispatch_result` (`wait` up to 60 s) and `cancel_dispatch`. The caller is found from the server's parent processes (the tmux pane of the nearest claude / codex and its registry row); it is looked up again before each tool that acts or reads a dispatch. An unregistered caller can use the tools but gets no completion notice, and is recorded as 「未登記的 session（<agent>）」 (or 「未登記的 session」 when no agent or no process list is found), never 「終端機」. `dispatch_result` and `cancel_dispatch` only accept the caller's own dispatches. `new_session` runs the same checks as `sampan new` before asking; names with newlines or control characters, and the reserved names 「終端機」 and 「未登記的 session…」, are refused by `sampan new`, `rename` and `adopt` too. Dispatch records carry `from_kind` (`terminal` / `unregistered` / `session`; inferred for old records), and only a `session` is matched by name for notices and MCP ownership. While it runs, the server advances its caller's dispatches every 3 seconds; a dispatch not waited for sends the caller a completion notice (with Enter) when it is done, or a failure notice 「〔sampan：給 <target> 的派工 <id> 失敗：<reason>〕」 when it fails (none once `dispatch_result` has shown the result); programs it runs get stdin from `/dev/null`, never the JSON-RPC stream. `cancel_dispatch` also stops waiting for a sent dispatch.
- `[mcp] confirm` (default `true`): `send`, `new_session` and `cancel_dispatch` ask through an MCP elicitation form every time (approve, decline, or edit the message; the field is optional and an empty one sends the original, since a client need not prefill it); the agent's message is shown below a fixed separator line, declining creates no dispatch, and a client that does not declare elicitation is refused with 「這個 client 不支援核准表單；要用請在 config 設 [mcp] confirm = false」. `list_sessions` and `dispatch_result` never ask.
- `sampan mcp install` / `uninstall [--dry-run]`: registers `sampan mcp serve` with every enabled Claude profile (`claude mcp add --scope user`, with that profile's `CLAUDE_CONFIG_DIR`) and with Codex (`codex mcp add`, `CODEX_HOME` when not `~/.codex`); already-installed ones are skipped and results are logged in `actions.log`. `sampan doctor` shows where the sampan MCP is installed (nothing when nowhere). The docs record that Codex supports elicitation forms but auto-approves them under `--dangerously-bypass-approvals-and-sandbox`.
- The `sampan` screen watches dispatches: while it is open a background thread advances every pending dispatch every 3 seconds (never on the ui thread; its commands run with stdin closed), and a line `派工：N 排隊、M 等回覆（D 看清單）` shows under the list when some are queued or waiting. `D` opens the dispatch list (id, from, to, state, created, reason); `x` on a queued one asks, then cancels it; on a sent one it asks, then stops waiting for its reply; a finished one says why it cannot. Leaving the screen in the middle of a delivery waits for it to end (about 35 s at most). `D` with no dispatches says 「沒有派工」.

### Changed
- The Docker live smoke (`scripts/linux-smoke`) covers `sampan send`: the fake claude draws a Claude-style prompt box, takes a bracketed paste plus Enter, records what it receives and answers in its transcript. It checks that one multi-line CJK prompt with quotes, `$HOME` and backticks arrives byte for byte, that `--wait` prints the reply, and that a draft in the box keeps the dispatch queued with nothing pasted.
- The launchd auto-save now runs every 5 minutes (minute 0, 5, ... 55) instead of every hour. An hourly plist counts as an old schedule: `sampan doctor` warns and `sampan init` offers to reinstall it.
- A `history/` snapshot of the registry is kept only when a session really changed; a save that differs only in `seen_at`, `pid` or `server` no longer adds one (the registry itself is still written every time), so the newest 20 snapshots span much more than 20 saves.

## [0.10.0] - 2026-10-25

### Added
- Opening the `sampan` screen now starts the Codex daemon (`codex remote-control start`) in the background when it is not running, once per opening and never on the ui thread (15 s limit). The main screen shows 「正在啟動 Codex 常駐服務…」 meanwhile, or 「Codex 常駐服務啟動失敗：<reason>」 if it failed (no `codex` command, a non-zero exit, a timeout); 「找不到 codex 指令」 only shows when the list has a Codex session. `status`, `doctor` and `restore` never start it. Turn it off with `[ui] codex_auto_start = false` in `config.toml`.

## [0.9.1] - 2026-10-25

### Fixed
- tmux Codex sessions no longer lose their thread id when the Codex TUI is attached to the shared daemon (Codex 0.164+): the thread is stored as source `vscode` with originator `codex-tui`, and the daemon (not the TUI process) holds the rollout open. The threads table match now accepts `codex-tui` threads (Codex Desktop threads stay excluded), and a pane still unresolved asks the daemon for the single live `codex-tui` thread in its directory (several → the usual 「無法判斷」 warning). This also stops the 「Codex 其他對話」 block from listing a tmux session's own thread again.
- The 「Codex 常駐服務沒在執行」 note on the main screen now shows only when the list has a Codex session; it no longer appears for Claude-only use.
- The last-message line (「最後一句話」) skips Claude's compaction summary, and an injected leading self-closing tag such as `<artifact-content-authored-by-others/>` is treated as injected text.

## [0.9.0] - 2026-10-25

### Added
- Quota warnings: the account line shows `⚠` from 80% and `⛔` when a quota is used up, with the reset time (Claude and Codex).
- Read-only monitoring of the Codex daemon (`codex remote-control start`): Codex quota (saved to the state directory), tmux Codex session status, a 「Codex 其他對話（24 小時內）」 block on the main screen for threads that are not tmux sessions, and a `sampan doctor` line. Codex context use (⚠ above 80% of the model limit, read from the rollout file, no daemon needed) is shown for Codex sessions too. The ui reads the daemon in the background every 30 seconds (3 s per query); without the daemon quota falls back to the newest rollout file and the main screen shows a one-line 「Codex 常駐服務沒在執行」 note instead of the block.

## [0.8.1] - 2026-10-25

### Fixed
- The profile screen (`o`) no longer cuts a two-digit running count: 「12 個執行中」 showed as 「12 個執行」. The column now widens to its widest row.
- `sampan plugins sync` lists only plugins whose version or commit changed (a GitHub plugin that moved to a new commit without bumping its version shows `1.0.0（abc1234 → def5678）`), prints 「沒有變動」 for a profile where nothing changed and nothing failed, and shows the restart hint only when something changed or was removed.

## [0.8.0] - 2026-10-25

### Added
- Profile management: `sampan profile list | rename | disable | enable | forget`, and `profile add [--dir D] [--codex]` now also adds Codex accounts. Disabled profiles are hidden, cannot start sessions and are skipped by `plugins sync`; `restore` explains why their rows are not resumed. sampan still never deletes a profile directory; the usage guide says how to remove one by hand.
- `[profiles.X]` accepts `agent = "codex"` and `disabled = true`. An old `[codex]` table acts as an implicit Codex profile and is converted to `[profiles.<name>]` by the first `sampan profile rename / disable / enable / forget / add --codex`; once any Codex profile is in the file `[codex]` is ignored, and `[codex] enabled = false` means Codex is off.
- `sampan statusline install | uninstall [--profile P] [--yes]` (also offered by `sampan init`): wraps the `statusLine` command so sampan can record Claude's 5h / weekly quota in `usage/` in the state directory. `sampan doctor` reports it.
- Account lines at the top of the main list (running sessions, 5h and weekly quota; Codex quota is read from its newest rollout), and an accounts screen on `o` (rename, disable / enable, forget, install the status line, add).

### Changed
- Documentation: release pins move to v0.8.0.
- Codex is now a profile like Claude's. Multi-account Codex (choosing the account for a new session, switching between Codex accounts) is not supported yet: sampan uses the first enabled Codex profile.
- `actions.log` also records profile actions.
- `sampan codex` refuses to start when there is no enabled Codex profile, instead of running `codex` anyway.

## [0.7.3] - 2026-10-24

### Changed
- Documentation: release pins move to v0.7.3.

### Added
- `actions.log` also records the answer to each startup prompt (trust, update, hooks review) during a restart or
  `switch`, and `sampan doctor` shows where the log is and when it was last written.

## [0.7.2] - 2026-10-24

### Changed
- Documentation: release pins move to v0.7.2.
- `sampan stop` (and `R` restarts) send `/exit` to sessions in a `.claude/worktrees/` directory too, instead of always
  skipping them. Claude asks Keep / Remove worktree only when that run created the worktree; when the question shows up,
  sampan presses nothing and leaves it to you. A resumed session in a worktree now restarts like any other. A worktree
  session already showing the question, or whose screen can't be read, gets no keys at all, and `--force` never sends
  SIGTERM to a worktree session.
- The "still on the old version" list after a restart shows each session's reason (busy, asking about the worktree,
  sampan's own pane…), since the lines explaining it may have scrolled away.

## [0.7.1] - 2026-10-24

### Added
- `actions.log` in the state directory: the messages printed by plugin saves, `u` / `U`, `r`, marketplace changes,
  restarts (each step, and the sessions left on the old version), `forget` and `switch`, one line each with the time
  and the action (previews and cancelled actions are not logged). User names and tokens in URLs, GitHub tokens and
  `…TOKEN=` style assignments are masked; the file is 0600 and rotates to `actions.log.1` at 1 MB.

### Changed
- Documentation: release pins move to v0.7.1.

### Fixed
- The restart question after a plugin save was shown twice.
- After a save the restart question was asked over the layer-2 screen of before the save; it is now asked on the
  redrawn plugin list.

## [0.7.0] - 2026-10-24

### Added
- Plugin management screen: `p` in the ui. Two layers (a plugin list with an info card, then per-profile checkboxes), `r`
  checks for newer versions, `u` / `U` update, `a` adds a plugin, `/` and digits search and jump. Saving shows a preview
  of every change per profile and asks first; the config is backed up as `config.toml.bak-<stamp>` only when it is
  really rewritten, a failure in one profile leaves the other profiles' changes in place and changes the config only for
  profiles that succeeded, and a restart hint offers `R` (busy sessions and sampan's own pane are never restarted).
  Opening the screen never touches the network. `sampan plugins sync` remains for scripts.
- Marketplace sources tab (Tab; `t` in `--plain`) with the states 一致 / 來源不同 / 清單外 / 沒用到: add a source (`a`;
  URLs carrying a user name or token are refused, use git's credential helper), remove it from profiles (refused
  while that profile's config still lists plugins from it), and update one or all (`u` / `U`). Rows that cannot be
  changed show their reason in the card.
- `sampan forget <name> [--dry-run]` and `X` in the ui: remove a stopped or retired session from the registry after
  confirming. It prints the command to resume it by hand; conversation files are never deleted and the previous registry
  is kept in `history/`.

### Changed
- Documentation: release pins move to v0.7.0.
- `U` in the ui no longer offers to reinstall when nothing newer is found; it only says so. Reinstalling is
  `sampan update --force`.
- `sampan restore <name>` on a retired session now says `<name> 是 retired；先 sampan activate <name>`.
- Long messages in the ui now wrap instead of being cut off.
- ARCHITECTURE: the network invariant also lists what is pressed in the plugin screen (`r`, `u`, `U`, save); new modules
  are listed under Layers.

## [0.6.5] - 2026-10-24

### Added
- Switch a Claude session to another profile (another account, e.g. when one has used up its quota) and continue the
  same conversation: `P` in the ui, `sampan switch <name> <profile> [--dry-run]` on the command line. A running session
  exits and is reattached under the new profile in its own directory (trusting it automatically, as `move` does); a
  stopped one is reattached under the new profile directly. Plugins the new profile does not list are named first. If
  the new profile cannot see the transcript, it is moved there (never copied, so there is one copy only); an existing
  transcript of the same conversation in the new profile is never overwritten.

### Changed
- Documentation: release pins move to v0.6.5.

## [0.6.4] - 2026-10-23

### Fixed
- A Claude or Codex started as the pane's own command (`tmux new-session -s x claude`, no shell in between) is now
  seen: before, the session showed "沒有 agent", `save` warned `session 檔指向 … 不在那個 pane 裡` and never
  recorded its conversation, and `stop` could not stop it.
  `stop` reports such a session as closed (it closes with its agent), and `move` refuses it with a hint
  (`sampan stop` + `sampan restore` first), because no shell is left in the pane to relaunch in. `move` also checks that the pane it
  types the relaunch into is the one that went back to the shell.

### Changed
- AGENTS.md: agents open sessions with `sampan new` rather than raw `tmux new-session`.
- Documentation: release pins move to v0.6.4.

## [0.6.3] - 2026-10-23

### Security
- The state directory (and its `history/` and `hooks/`) is kept at `700` and every file sampan writes there (registry, history
  snapshots, `save.log`, lock, update-check cache, hook records) at `600`, whatever the umask; looser files left by an older
  version are fixed on the next write. The hourly launchd job runs with umask `077`, so its logs are private too (takes effect
  when the plist is rewritten, e.g. by `sampan init`). Tightening is best-effort: where chmod is refused (a state dir you do
  not own, some filesystems) the write still succeeds. A new `config.toml` from `sampan init` is created `600` (an existing one
  is never changed). SECURITY.md no longer asks you to `chmod` by hand.

### Fixed
- An action that fails in the ui no longer closes it: the message area shows `❌ <action> 失敗：<error>` and the list stays.
- `sampan stop` prints the "tmux unreadable" warning once instead of twice.

### Changed
- CI: Dependabot keeps the GitHub Actions and the Linux smoke base images current; the Linux smoke image pins uv (0.11.3) instead of `latest`.
- Tests: the guard also blocks network / mutating `git` subcommands inside shell strings.
- Documentation: the update check is described the same way in README and SECURITY.md (`U` checks again when no newer
  version is known yet, `sampan update` always checks again); the registry reference says times
  are stored in UTC; the usage guide says `m` starts at `[ui] start_dir` when the session's directory is gone; release pins
  move to v0.6.3.

## [0.6.2] - 2026-10-23

### Fixed
- `U` in the ui no longer trusts a stale update cache: when no newer version is known it checks again right then, and if it is
  still up to date (or the check fails) it offers to reinstall the current version.

### Added
- `sampan update [--yes] [--force]`: checks the newest version right then (no cache) and updates after asking; `--force` reinstalls
  even when up to date, `--yes` skips the question. It needs no config file.

### Changed
- Documentation: README notes that `sampan init` asks questions and should be run by you in a terminal; release pins move to v0.6.2.

## [0.6.1] - 2026-10-23

### Changed
- `sampan init` refuses to run when stdin is not a terminal (exit code 2, nothing written); `--yes` takes every default instead.
- The SessionStart hook is asked about only with `sampan init --hook`; repairing a broken hook is still offered. `sampan doctor` no longer reports an absent hook.
- `install.sh --yes` now runs `sampan init --yes` (only when there is a terminal; with no terminal init is skipped), and a failing `sampan init` no longer aborts the installer (it prints a note and carries on).
- `~/.claude-profiles/*.retired.*` directories are skipped by `sampan init` and `sampan doctor`.
- Documentation: README says that Claude Code is required and Codex CLI is optional, and the README has a short
  "What it changes on your machine" section plus a tip to alias `sampan` to `sp`.

### Added
- Session list: each row has a number (its place in the full list). Type `1`-`9` to jump to it; two digits typed within 1 second make
  one number (`1`, `2` is row 12). `/` searches by name (case-insensitive substring); Enter opens the selection, Esc cancels,
  Backspace on an empty query leaves search. Digits typed while searching go into the query.
- Plain menu (`sampan ui --plain`): `/text` searches; one match attaches, several are listed with their numbers and you pick one right away (Enter goes back).
- `AGENTS.md`: guidance for AI coding agents (what to tell a user, what to run, what never to run).

## [0.6.0] - 2026-10-23

First public release. Versions up to 0.5.3 were released privately under the name "berth"; their entries below are kept as history.

### Added
- `install.sh`, a one-line installer for macOS and Linux (`curl -fsSL https://raw.githubusercontent.com/royalroylo/sampan/main/install.sh | sh`):
  offers to install uv, tmux (and on Linux ps, lsof, git), installs `sampan`,
  prints the PATH line if needed, and runs `sampan init` (skipped, with a message, when Claude Code is missing or there is no terminal).
  Tested on Ubuntu 24.04 in Docker (`scripts/linux-smoke/install-test.sh`); not yet on macOS.
- Linux install section in the docs (apt/dnf commands, cron and systemd-timer examples for auto-save) and a "Testing on Linux" section in `CONTRIBUTING.md`.
- English README (`README.en.md`), documentation pages in `docs/` (zh-TW) and `docs/en/` (English), `LICENSE` (MIT), `SECURITY.md`, `CONTRIBUTING.md`, `ARCHITECTURE.md`, an issue template
  and package metadata (license, URLs, classifiers).
- `[ui] start_dir` config key: where the directory chooser of `n` starts.
- Platform guard: on systems other than macOS, `sampan init` skips the launchd schedule and says so, `sampan doctor` skips the
  Homebrew and launchd checks, and the tmux install hint is generic. Linux is experimental.
- `python -m sampan` works like the `sampan` command.
- `lsof` is looked up on `PATH` and then in the usual locations; when there is none, Codex conversation detection reports
  "unknown" instead of failing.
- The test suite refuses to run real `tmux`, `claude`, `codex`, `launchctl` or `uv`, and a test keeps private names out
  of the repository.

### Changed
- The command and package are `sampan` (`uv tool install git+https://github.com/royalroylo/sampan@v0.6.0`),
  the project lives at <https://github.com/royalroylo/sampan>, config is `~/.config/sampan/config.toml`, state is
  `~/.local/state/sampan/`, the environment variables are `SAMPAN_CONFIG` / `SAMPAN_STATE_DIR`, and the launchd
  label is `io.github.royalroylo.sampan-save`.
- Timestamps in the registry are stored in UTC and shown in local time. Registries written by older versions are read
  as before, and their times are shown unchanged.
- Self-update pins the newest version tag instead of reinstalling from the default branch; wording that assumed a
  particular terminal application or project directory is now neutral.
- `sampan move` run from the command line no longer sends keys blindly: it answers a trust prompt only when the path shown is
  exactly the destination, and every other prompt gets no keys. All prompt handling goes through one relay shared with the UI.
- All Claude and Codex screen markers and parsers live in one module (`screens.py`); session states are an enum
  (`State`) with a `StatusRow` record instead of display strings and dicts; the UI is split into a package
  (`sampan.ui.*`); code comments and docstrings are in English.

### Fixed
- When tmux cannot be read, sessions are shown as "unknown" instead of "stopped" with a resume offer.
- Registry history snapshots are pruned by modification time rather than file name.
- `sampan status` exits 1 when tmux cannot be read.
- Setting a session to an invalid state is rejected, and an unknown UI action raises instead of being ignored.

## [0.5.3] - 2026-10-21

### Fixed
- Pressing Enter on the session berth itself is running in now closes berth and resumes the conversation in that pane.

## [0.5.2] - 2026-10-21

### Fixed
- A stopped row that cannot be resumed but whose tmux session still exists (for example because a different program holds
  the pane) now opens that session on Enter.

## [0.5.1] - 2026-10-21

### Changed
- `n` and Enter on a stopped row go straight into tmux once the start command is sent, so a startup menu can be answered
  there. `R` and `m` still forward such menus to the berth screen.

## [0.5.0] - 2026-10-21

### Added
- Background check for a newer version. When one exists, the list shows a hint, and `U` updates with `uv` after an in-place
  confirmation and reopens berth.

## [0.4.5] - 2026-10-21

### Changed
- Documented that detaching from tmux returns to the berth list.

## [0.4.4] - 2026-10-21

### Added
- Colour in the UI: states, idle / busy / waiting, card field names and keys, and messages by outcome.

## [0.4.3] - 2026-10-21

### Added
- `stop`, `move` and `restore` report progress as they run (exit sent, who finished, who is still awaited, who resumed).

## [0.4.2] - 2026-10-21

### Fixed
- Sessions closed by `berth stop` stay in the registry; the next `save` no longer mistakes them for sessions the user
  closed and removes them.

## [0.4.1] - 2026-10-21

### Added
- Documentation for opening berth automatically on SSH login.

## [0.4.0] - 2026-10-21

### Changed
- README reorganised: three core ideas, "how to use", common situations, and a command reference.

## [0.3.2] - 2026-10-20

### Added
- `w` in the UI saves the registry immediately (same as `berth save`).

## [0.3.1] - 2026-10-20

### Added
- `berth rename`: renames the tmux session and the registry row together; the agent keeps running.

## [0.3.0] - 2026-10-20

### Added
- `berth adopt`: take over Claude / Codex agents running outside tmux and resume them in tmux.

## [0.2.1] - 2026-10-20

### Changed
- The automatic save now runs on the hour instead of at a fixed interval.

## [0.2.0] - 2026-10-20

### Added
- Marketplaces may be given as git URLs.
- `berth --version`.

### Security
- Credentials in git URLs are no longer shown in output.

[Unreleased]: https://github.com/royalroylo/sampan/compare/v0.11.1...HEAD
[0.11.1]: https://github.com/royalroylo/sampan/releases/tag/v0.11.1
[0.11.0]: https://github.com/royalroylo/sampan/releases/tag/v0.11.0
[0.10.0]: https://github.com/royalroylo/sampan/releases/tag/v0.10.0
[0.9.1]: https://github.com/royalroylo/sampan/releases/tag/v0.9.1
[0.9.0]: https://github.com/royalroylo/sampan/releases/tag/v0.9.0
[0.8.1]: https://github.com/royalroylo/sampan/releases/tag/v0.8.1
[0.8.0]: https://github.com/royalroylo/sampan/releases/tag/v0.8.0
[0.7.3]: https://github.com/royalroylo/sampan/releases/tag/v0.7.3
[0.7.2]: https://github.com/royalroylo/sampan/releases/tag/v0.7.2
[0.7.1]: https://github.com/royalroylo/sampan/releases/tag/v0.7.1
[0.7.0]: https://github.com/royalroylo/sampan/releases/tag/v0.7.0
[0.6.5]: https://github.com/royalroylo/sampan/releases/tag/v0.6.5
[0.6.4]: https://github.com/royalroylo/sampan/releases/tag/v0.6.4
[0.6.3]: https://github.com/royalroylo/sampan/releases/tag/v0.6.3
[0.6.2]: https://github.com/royalroylo/sampan/releases/tag/v0.6.2
[0.6.1]: https://github.com/royalroylo/sampan/releases/tag/v0.6.1
[0.6.0]: https://github.com/royalroylo/sampan/releases/tag/v0.6.0
