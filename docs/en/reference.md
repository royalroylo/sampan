# Configuration and registry formats

English · [繁體中文](../reference.md) · [Back to README](../../README.en.md)

For migration or conversion scripts. A version sampan does not understand is never overwritten; it is reported.

## `~/.config/sampan/config.toml` (`version = 1`)

| Field | Meaning |
|---|---|
| `version` | integer, must be `1` |
| `[restore] recent_days` | non-negative integer, default 7; `restore` pre-checks only sessions active within this many days |
| `[ui] start_dir` | path where the directory chooser of `n` (start new work) starts; default `~/Projects` if it exists, else `~` |
| `[ui] detach_key` | string, default `"C-q"`. In a session sampan manages, this key jumps back to sampan: a client attached from outside tmux detaches, and when sampan switched over from inside tmux it switches back to the previous session; in other tmux sessions the key goes to the program as usual. sampan writes the binding into the running tmux server when its screen opens and before attaching (never into `~/.tmux.conf`) and marks its sessions with the `@sampan` session option; if you already bound the key to something else, sampan leaves it and `sampan doctor` says so. `""` binds nothing (and removes a binding sampan wrote). Limits: a session opened outside the `sampan` screen (say `sampan new`, then `tmux attach`) is marked the next time the screen opens or finishes an action; after attaching from outside and switching sessions yourself (`Ctrl-b s`), Ctrl-q switches back to the previous session instead of detaching |
| `[ui] codex_auto_start` | boolean, default `true`. When the `sampan` screen opens and the Codex daemon is not running, run `codex remote-control start` once in the background; `false` never starts it |
| `[mcp] confirm` | boolean, default `true`. The `send`, `send_batch`, `new_session`, `cancel_dispatch` and `folder_move` tools of `sampan mcp serve` are approved every time through the agent's own permission prompt (Claude Code's permission prompt, Codex's `approval_mode = "prompt"`); a client not known to ask a person first is refused, never skipped. An agent running `sampan send`, `sampan send-batch`, `sampan new … --message` or `sampan dispatches cancel` from Bash is refused as well. `false` never asks and just acts (still logged in `actions.log`, still cancellable). The read-only `list_sessions` and `dispatch_result` never ask |
| `[codex] enabled` | boolean, default `true`. With no `agent = "codex"` profile, `[codex]` acts in memory as an implicit Codex profile, converted to `[profiles.<name>]` by the first `sampan profile rename / disable / enable / forget / add --codex`; once any `agent = "codex"` profile exists, all of `[codex]` is ignored; `false` means Codex is off (no Codex profile), and forgetting the last Codex profile writes it |
| `[codex] home` | Codex folder; default `$CODEX_HOME`, else `~/.codex` (only used for the implicit Codex profile) |
| `[marketplaces] <name> = "owner/repo"` | plugin source: GitHub `owner/repo` or a git URL (`https://…`, `ssh://…`, `git@host:path`, e.g. a self-hosted Gitea). Local directories are not supported |
| `[profiles.<name>] config_dir` | required path (`~/` allowed) |
| `[profiles.<name>] default` | exactly one profile is `true` (it starts without `CLAUDE_CONFIG_DIR`) |
| `[profiles.<name>] label` | display text, e.g. an account e-mail |
| `[profiles.<name>] agent` | `"claude"` (default) or `"codex"`; for a Codex profile `config_dir` is its `CODEX_HOME`, and it has no `plugins`. With several Codex profiles, sampan starts Codex with the first enabled one and attributes every running Codex session to it (multi-account is not supported yet) |
| `[profiles.<name>] disabled` | boolean; `true` disables it (hidden, cannot start sessions, skipped by plugins sync); the default profile cannot be disabled |
| `[profiles.<name>] plugins` | list of `plugin@marketplace`; the marketplace must be in `[marketplaces]` |

Overrides: `SAMPAN_CONFIG` (config file, default `~/.config/sampan/config.toml`), `SAMPAN_STATE_DIR` (state folder, default `~/.local/state/sampan/`).

## `registry.json` (`version = 3`, in the state folder)

```json
{
  "version": 3,
  "machine": "…",
  "saved_at": "2026-01-01T09:00:00+00:00",
  "sessions": [
    {"tmux": "work", "agent": "claude", "profile": "alice", "session_id": "…",
     "cwd": "/Users/u/proj", "state": "active", "seen_at": "2026-01-01T09:00:00+00:00",
     "server": "…", "pid": 12345, "id": "…", "alias": ""}
  ]
}
```

| Field | Meaning |
|---|---|
| `tmux` | tmux session name (unique) |
| `agent` | `"claude"` or `"codex"` |
| `profile` | profile name from config.toml; `""` for codex |
| `session_id` | Claude conversation id, or Codex thread id |
| `cwd` | working directory (absolute path) |
| `state` | `"active"` or `"retired"` |
| `seen_at` | when it was last seen |
| `server` | identity of the tmux server at the last sighting (pid and start time), used to tell "the user closed it" from "waiting to be resumed after a reboot" |
| `pid` | the agent process last seen, an integer or `null` |
| `id` | the session's fixed id (`str(uuid.uuid4())`), unchanged by renames, folder moves and profile switches; the screen shows its first 8 characters. Commands and MCP tools take a name, a full id, or a unique prefix of at least 8 characters (when ambiguous they fail and list the candidates: name plus first 8) |
| `alias` | the alias: free text, not empty, no control characters, at most 50 characters; `""` is unset (the name stands in). Changing it touches neither tmux nor the id; it applies to Claude's `--name` and the Remote Control name at the next launch |

Version 1 and 2 files are still read (a missing `agent` counts as `claude`, a missing `pid` as `null`; `id` and `alias` are filled in by `sampan migrate`), and 0.15 always writes 3. Sampan 0.14 and earlier refuse a version 3 file instead of overwriting it; to downgrade, run `sampan migrate --revert` first (below). Before a rewrite the previous file is copied to `history/registry-<time>.json` (newest 20 kept) only when a session really changed (`seen_at`, `pid` and `server` do not count).
Times (`saved_at`, `seen_at`, the history file names) are written in UTC (`+00:00`) and converted to local time only for
display; history files left by older versions may be named in local time.

## `folders.json` (`version = 2`, in the state folder)

Session folders, used only by the screen and `sampan folder`; `save`, `restore` and the reboot resume neither read nor write it, and the registry holds no folders. Version 2 is `{"version": 2, "folders": {"<folder id>": {"name": "A", "parent": "<parent folder id or null>", "collapsed": false}}, "sessions": {"<session id>": "<folder id>"}, "pending": {"<name>": "<folder id>"}}`: a folder has its own id and parent, and a session belongs to a folder by its registry `id`, so renaming a session or a folder, or moving it to another level, never loses the membership (only the one entry touched changes); `pending` holds members with no registry row yet (for example a Codex session `sampan new` placed in a folder), waiting by name. Version 1 is `{"version": 1, "folders": {"客戶/甲": {"collapsed": false}}, "sessions": {"<name>": "客戶/甲"}}` (paths as keys, levels separated by `/`, no empty segment, no control character); it is still read, 0.15 always writes 2, and while a member has no `id` yet (before the registry is upgraded) version 1 is still written. A session not listed sits at the top level. Reads and writes take the registry lock; the file is mode 600. A session the registry does not have is not listed, and parents missing from `folders` are added automatically. If the file cannot be read, is not JSON or its `version` is not 1 or 2, the list shows every session flat, every folder operation exits 2 with an explanation, and the bad file is never overwritten.

| Command | Effect |
|---|---|
| `sampan folder list` | Prints the tree: two spaces per level, a folder as `▸ name/` (folded) or `▾ name/`, with its sessions below |
| `sampan folder add <path>` | Creates the folder (and any missing parents) |
| `sampan folder rename <path> <new name> [--merge]` | Changes the last segment only; a same-named folder at that level needs `--merge` to be merged into, otherwise exit 2 |
| `sampan folder rm <path>` | Deletes the folder; its sessions and subfolders move up one level, no session is stopped or forgotten |
| `sampan folder move <name or path>... <destination｜/> [--merge]` | Moves sessions or folders; `/` is the top level; a source that is a path in `folders.json` is a folder, otherwise it must be a session in the registry, else exit 2 |
| `sampan new … --folder <path>` | Only after the session started puts it in the folder (created if missing): exit 0, or exit 1 with the tmux session there (Codex ready but not registered yet); a failed write is only a warning |

`sampan rename`, `sampan forget` and `sampan profile forget` keep `folders.json` in step; a failed sync only adds a line `⚠ 資料夾設定沒同步：…` and does not affect the action.

## `dispatches.json` (`version = 2`, in the state folder)

Dispatch records: one message sent from a caller session (or the terminal) to a target session. Reads and writes hold `dispatches.lock` in the same folder; the file is 600 and the folder 700. The format is `{"version": 2, "dispatches": [...]}` (version 2 since 0.14; the 0.15 fields ride on 0.14 keeping fields it does not know, so the version did not change), each entry:

| Field | Meaning |
|---|---|
| `id` | four lowercase hex digits |
| `from_name`, `from_profile`, `from_agent` | the caller (for a person at the terminal, `from_name` is "終端機"; for an agent that is not a sampan session, sending through MCP, it is like "未登記的 session（claude）", and it gets no completion notice) |
| `from_kind` | what the caller is: `terminal` (a person at the terminal), `unregistered` (an agent that is not a sampan session), `session` (a sampan session). Only `session` is ever matched by `from_name` (completion notice, MCP results and cancelling). An old record without it: `terminal` when `from_name` is "終端機", else `session` |
| `to_name`, `to_agent`, `to_profile` | the target session |
| `text` | the full text sent, including the source line |
| `state` | one of `queued`, `sent`, `done`, `failed`, `cancelled` |
| `created`, `sent_at`, `done_at` | UTC ISO times; `sent_at` and `done_at` may be `null` |
| `reply`, `reason` | the reply (or `null`); why it failed or was cancelled |
| `notify`, `notified` | whether to notify the caller when done or failed, and whether that happened |
| `thread_id` | conversation identifier, or `null` |
| `raw` | a direct send (`send --raw`, MCP `raw=true`): `text` is the bare message with no source line; default `false` |
| `batch` | batch id (`b` plus 4 lowercase hex), `null` when not sent as a batch; the dispatches of one batch share it |
| `to_id`, `from_id` | the registry `id` of the target and of the caller; after a rename the other side is still found by it, so a rename mid-flight loses neither the reply nor the notice. `""` in old records (found by name instead) |
| `machine` | the id of the machine the dispatch was made on |
| `batch_skipped` | meaningful on the first item of a batch only: how many sessions the batch skipped (for the completion notice); default `0` |
| `startup` | the startup message of `sampan new --message` (the new session may not be registered yet); an old record without it counts as `false` |

A dispatch made by `sampan new --message` starts as `sent` (`sent_at` is when the session was opened; `text` and `outbox/<id>.txt` do not end with a newline, because `$(cat …)` drops trailing newlines and the reply would not match); the conversation id is filled in by later advances from the status rows. While the new session is not recognised yet it is not failed, and `reason` says 「對方 session 還在啟動」: a Codex without a conversation yet (unregistered) is waited for as long as that Codex runs; a tmux session whose agent is not seen yet (a registered Claude shown as 「已停止」, or no agent) for at most 120 seconds; a folder-trust or other startup prompt for as long as it is shown.

How the state moves: `queued` → `sent` (delivered, `sent_at`) → `done` (`reply` is the reply to this message) or `failed` (`reason` says why); `cancelled` comes from `queued` (never sent, `reason` 「已取消」) or from `sent` (stop waiting for the reply; the message is already with the target, `reason` 「不再等回覆」, no notice). `done_at` is when it finished. Every step is taken inside the lock (read the latest, decide, write), so one dispatch is never delivered twice. Before any key is pressed or `codex queue` runs, the record is written as `sent` (`reason` 「送出中」): if sampan is killed in the middle, the dispatch is never sent again (it later fails as 「找不到回覆」), so it is delivered at most once; it goes back to `queued` only when nothing was sent for sure (text in the target's box, an unreadable screen, a failed tmux load…). If Enter fails after the paste (the text is already in the target's box) or `codex queue` times out (Codex may have taken it), it becomes `failed` (「可能已送出，請確認」) and is never sent again (the same for the completion notice: it counts as notified, with the `reason` saying so). A target pane in tmux copy mode gets nothing; the dispatch stays queued.

How a `sent` one ends: when the target is idle and its transcript has an answer after this message → `done`; another message was typed after this one with no answer in between → 「對方沒回這則就換了下一則」; a Codex turn that completed without reply text, or a Claude target idle twice in a row without reply text → 「對方這輪結束了，但沒有回覆文字」; idle twice in a row without the message → 「找不到回覆」 (seeing the target busy in between starts over).

How a direct send (`raw`) ends: after the paste and Enter, as soon as the target's Claude is idle again and the command shows in its transcript (a user record equal to the text or holding `<command-name>/X</command-name>`, or a `system` / `local_command` record) → `done`; the reply is the answer text after it, or 「指令已執行（沒有文字回覆）」 when there is none. If the conversation id changed (`/clear`) → `done` with the reply 「對方換成新對話（first 8 of the id）」. If after the send the screen sits at a menu or prompt (two looks in a row) → `failed` 「指令打開了選單，請自己到那個 session 處理」, and sampan presses no key. A busy target is never judged to be at a menu. Content limits: one line, starting with `/`, at most 500 characters, no control characters, Claude only (to Codex it is refused: 「直接送目前只支援 Claude」).

Batches: every item of a batch is its own dispatch; only sessions that are `running` and idle are sent to, the others are not queued and count as `skipped` (with a reason). When every item has ended (`done`, `failed`, `cancelled`) **one** notice goes out, `〔sampan：批次 <id> 完成：N 完成、M 失敗、K 跳過，用 dispatch_result 看〕`, with no per-item notices; if one item fails or is cancelled the batch still ends and notifies. `sampan dispatches show` does not take a batch id.

Exit codes of `sampan send`: 0 sent or queued (`--wait`: answered), 1 failed, 2 refused or the file cannot be read, 124 `--wait` timed out (the dispatch stays), 130 Ctrl-C (the dispatch stays). `sampan dispatches show <id> [--wait seconds]`: advances it once (or every 2 seconds until it is finished, for at most that long) and prints the state, from whom to whom, the reason and the whole reply; exit 0 answered or still going (without `--wait`), 1 failed or cancelled, 2 no such dispatch or the file cannot be read, 124 `--wait` timed out, 130 Ctrl-C. The completion or failure notice (`notify`) goes to the caller's session through `outbox/<id>-notice.txt`: `〔sampan：<target> 已回覆派工 <id>，用 sampan 工具 dispatch_result 看內容〕` when done, `〔sampan：給 <target> 的派工 <id> 失敗：<reason>〕` when failed.

`sampan send-batch NAME… -m message [--raw] [--wait] [--timeout seconds]` and `sampan send-batch --folder path -m message` (subfolders included; names or `--folder`, not both): exit codes as for `sampan send`; `--wait` waits for the whole batch and prints each reply; skipped ones are listed (the exit code is not affected).

The caller of `sampan send` / `sampan new --message` is also found from the parent processes (as for MCP): run by an agent through its shell tool, it is recorded as that sampan session (or 「未登記的 session」), and sending to itself is refused; only with no claude / codex process above it is it 「終端機」. These two commands send no notice; read the reply with `sampan dispatches show`.

A file that cannot be read is never overwritten. Finished entries (`done`, `failed`, `cancelled`) older than 7 days are dropped, and only the newest 200 are kept. `outbox/<id>.txt` holds the exact text to deliver (mode 600).

## The tools of `sampan mcp serve`

A stdio MCP server (one JSON-RPC 2.0 message per line; protocol version `2025-06-18`, or `2025-03-26` when the client asks for it). Claude Code or Codex starts it; the caller is found from the server's parent processes: the tmux pane of the nearest claude / codex process, then the registry row of that tmux session with the same agent. When that does not match, the caller is an "unregistered session" (the source line and the dispatch record say for example 「未登記的 session（claude）」, or 「未登記的 session」 when no agent process is above the server or ps cannot be read at start; an MCP caller is never 「終端機」): the tools still work, it only gets no completion notice. The caller is looked up again before every tool that acts or reads a dispatch (a Codex session is registered only once its conversation exists, a session may be adopted or renamed); only a lookup that cannot read tmux, ps or the registry keeps the identity already known; a definite miss (no row, another agent) makes the caller unregistered. `dispatch_result` and `cancel_dispatch` only accept this caller's own dispatches (created by this server, or with `from_kind` `session` and the caller's session as `from_name`); any other id gets 「沒有派工 <id>（或不是這個 session 發出的）」. Tool results are JSON text; a refusal (unknown target, a client that does not ask a person first, Codex tools not set to ask every time) is a result with `isError: true`.

| Tool | Arguments | Returns | Approval |
|---|---|---|---|
| `list_sessions` | none | `machine` (`id`, `name`: this machine); `sessions`: each with `name`, `id` (the full id; other tools take a name or an id), `machine` (this machine's id), `alias` (`""` when unset), `agent`, `profile`, `cwd`, `folder` (its folder, `""` is the top level; all `""` when `folders.json` cannot be read, with a `warning` saying so), `state` (`running`, `stopped`…), `activity` (`idle`, `busy`…), `last` (UTC ISO or `null`); plus `warning` when tmux cannot be read | never asks |
| `send` | `session` (name or id), `message`, `raw` (default `false`; direct send: one line, starting with `/`, at most 500 characters, Claude only), `wait` (default `false`) | `dispatch`, `state`, `reply` once done, `reason` while not sent yet. `wait` waits at most 60 seconds; a dispatch not finished by then stays, and a notice goes to the caller's session when it is done or fails | asks |
| `new_session` | `name`, `target` (`claude:<profile>`, `<profile>` or `codex`), `cwd`, `message`, `folder` (optional, as `sampan new --folder`; created when missing) | `dispatch` (the startup-message dispatch, as with `sampan new --message`). A new directory's trust prompt: with `confirm = true` and Claude Code as the caller (its permission prompt shows the whole arguments; not verified for Codex, which gets no automatic trust) you saw the `cwd` in the permission prompt, so sampan trusts for you only the prompt that shows exactly that `cwd`; any other prompt (another directory, an update, an unrecognised menu), and every prompt with `confirm = false`, gets no key: the result is `isError` saying which prompt it stopped at and `tmux attach -t <name>` to handle it, and the startup message starts once it is handled (the dispatch can still be read with `dispatch_result`). A Claude that does not come up in time (for example at a login or theme screen) is an `isError` too; a directory sampan trusted for you is named in the message | asks |
| `send_batch` | `sessions` (a list of names, no duplicates), `message`, `raw` (default `false`), `wait` (default `false`, up to 60 seconds) | `batch` (the batch id), `items` (each `session`, `dispatch`, `state`, `reply` / `reason`), `skipped` (a **list**, each `session`, `reason`). Sends only to sessions that are running and idle, the others are not queued; one notice when the whole batch is done | asks (once) |
| `dispatch_result` | `id` (a dispatch id, or the batch id from `send_batch`), `wait` (0–60 seconds, default 0) | `dispatch`, `state`, `label`, `to`, `reply`, `reason`; given a batch id it returns the whole batch: `batch`, `items`, and `skipped` is a **number** (how many were skipped) | never asks |
| `cancel_dispatch` | `id` (a dispatch id; a batch id is refused, cancel the items one by one) | `dispatch`, `state`; a queued one is cancelled, a sent one is no longer waited for (plus `reason` 「不再等回覆」) | asks |
| `folder_move` | `sessions` (a list of names), `folder` (a path, `/` is the top level; created when missing) | `moved`, `folder`. Like `sampan folder move` it only changes the grouping on the screen; one unknown name, a bad folder name or a broken `folders.json` and nothing changes | asks |

Approval is done by the agent's own permission prompt: with `confirm = true` the tools that act (`send`, `send_batch`, `new_session`, `cancel_dispatch`, `folder_move`) carry `_meta["anthropic/requiresUserInteraction"] = true` in `tools/list`, so Claude Code shows its own permission prompt on every call (in the terminal every time, also in auto and bypass modes, with no "don't ask again"; in Remote Control it is expected to show the full permission prompt with no one-tap approve, which is not yet verified on a phone). The prompt shows the tool name and its whole arguments; to decline say No, and to change the message ask the agent to change it and send again (sampan has no form of its own any more, and no "edit the message" field). A name, target or directory with a newline or control character is refused before anything is sent. `new_session` runs the same checks as `sampan new` (name, disabled profile, Codex turned off, tmux session exists, missing directory). Approving a dispatch approves its completion or failure notice; there is no second question. Once `dispatch_result` has shown a done or failed result, no notice is sent. While the server runs it advances this caller's dispatches every 3 seconds; every program it runs gets `/dev/null` as stdin, so none reads the JSON-RPC input.

The source line says who the dispatch comes from, its id, and tells the receiver to answer in text (`〔sampan 轉送｜來自 <who>｜派工 <id>｜直接回覆文字即可，sampan 會帶回〕`: "just reply in text, sampan brings it back"); completion and failure notices are unchanged.

### Installing, and how approval works

`sampan mcp install` runs `claude mcp add --scope user sampan -- <sampan path> mcp serve` for every enabled Claude profile (with that profile's `CLAUDE_CONFIG_DIR`), and `codex mcp add sampan -- <sampan path> mcp serve` when Codex is enabled (with `CODEX_HOME` when the Codex home is not `~/.codex`); where it is already installed it is skipped. Whether a Claude profile has it is read from its `.claude.json` (`~/.claude.json` for the default profile), `mcpServers.sampan`, without running `claude` just to look (a run can renew the login); only when that file cannot be read or has another shape does it ask `claude mcp get`. `sampan mcp uninstall` does the matching `remove`. Both take `--dry-run`, and `--profile <name>` (repeatable, Codex by its profile name) to handle only those profiles; an unknown name does nothing and exits 2. Each result line is logged in `actions.log` (label `mcp`). Restart the profile's sessions to see the tools. `sampan doctor` shows one line with where it is installed (nothing when it is installed nowhere).

How `[mcp] confirm = true` works (fail closed):

- **Claude Code**: the tools carry `anthropic/requiresUserInteraction`, so every call is asked, even in auto and bypass modes.
- **Codex**: `sampan mcp install` writes `approval_mode = "prompt"` under `[mcp_servers.sampan.tools.<tool>]` in Codex's `config.toml` for `send`, `send_batch`, `new_session`, `cancel_dispatch` and `folder_move`. `send_batch` is new in 0.14: **run `sampan mcp install` again for Codex**; until then Codex refuses that tool and `sampan doctor` says so; with `confirm = false`, or on `uninstall`, it removes only the tables set to `prompt` (user-set `approve` tables are left). Before touching the file it backs it up as `config.toml.bak-sampan-<YYYYmmdd-HHMMSS>`; only sampan's tables change, every other byte (CRLF and comments included) is kept, and the file is read and written as UTF-8. A result that is not valid TOML is not written. If the user picks "always allow" in a Codex prompt, Codex rewrites it to `approve`: `sampan doctor` reports it, and `sampan mcp install` puts `prompt` back.
- **Only clients known to ask a person**: with `confirm = true` these tools run only for `claude-code` and `codex-mcp-client`; any other client gets 「這個 client 不會先問使用者；要用請在 config 設 [mcp] confirm = false」. The direct parent process of `sampan mcp serve` must also be `claude` or `codex` (checked once at start; a failed lookup counts as no): a shell piping JSON-RPC into `sampan mcp serve` and claiming to be `claude-code` is refused, but the name check can be faked (see the limits below). `codex exec` with approval policy `never` cannot ask anyone, so Codex refuses these tools (verified) rather than running them. For Codex the tool must also be `approval_mode = "prompt"` in both the `CODEX_HOME` Codex runs with (else `~/.codex`) and sampan's configured Codex home, else 「Codex 的 sampan 工具沒設成每次詢問（<path>）：跑 sampan mcp install」.
- **`sampan doctor`** checks it: 「Codex 的 sampan 工具沒設成每次詢問（[mcp] confirm = true）：跑 sampan mcp install 補上」, 「…還設著每次詢問（[mcp] confirm = false）：跑 sampan mcp install 拿掉」, 「Codex config.toml 讀不了：…」.
- **Commands run by an agent from Bash**: with `confirm = true`, `sampan send`, `sampan send-batch`, `sampan new … --message` and `sampan dispatches cancel` run by an agent (not a person at the terminal) are refused (exit 2); set `[mcp] confirm = false` to let agents use the commands directly.
- **After changing `[mcp] confirm`**: Claude reads it when its MCP server restarts (a new session, or `/mcp` reconnect); Codex needs `sampan mcp install` again.

Limits:
- Whether a Codex started with `--dangerously-bypass-approvals-and-sandbox` still asks is not documented officially, and was not tested: treat it as unverified.
- This does not stop an agent that types into tmux panes itself (`tmux send-keys`). Claude Code's sandbox can, but sampan does not set it up.
- The parent check looks only at the process name: it stops an agent that simply pipes JSON-RPC into `sampan mcp serve`, but a name can be faked (for example `exec -a claude`, or a link named claude), so, like `tmux send-keys` or editing `[mcp] confirm`, it does not stop an agent that deliberately works around sampan.

## `sampan restart <name…>`

Restarts the named sessions; each result is logged in `actions.log` (label `重開`). Classification: running in the registry with `agent_status` `idle`, and not in the pane sampan itself runs in, then `sampan stop` (90 second timeout) and a background reattach; stopped, then only reattached; busy (`busy`, `working` or `waiting`, reason 「忙」), any other status that is not idle (reason for example 「沒有閒著（unknown）」), retired, state unknown, any other state or not found, then skipped, with a final line "略過：name（reason）…". The reattach is a `restore` without confirmation that does not take you in by itself; an agent that stops at a startup menu (trust this directory and so on) is waited for and its question handed to you, as with `R` (by number in a terminal); in the sampan screen, choosing "go in and do it yourself" takes you into that session. Exit 1 when there is nothing to restart or reattach; otherwise the worst exit code of stop and restore.

## Standard steps after a launch, and the RC reset

After reattach (`restore` / `R` / Enter), restart (`restart` / `S`), `switch`, `move` and `adopt`, once Claude is back at its prompt and when the profile has Remote Control on (`remoteControlAtStartup`), sampan does one RC reset: it types `/remote-control`, presses Enter only when the menu's cursor is verified on `Disconnect this session`, then types `/remote-control <alias, or the name when unset>`. It does this on every launch without judging whether the name is already right, so the phone app's RC list gets a new entry each time and the old one disappears. The only keys sampan presses are text plus Enter, Up, Enter and Escape; if the screen is not what it expects it presses Escape and prints `RC 校正沒做：<reason>`, and the operation and its other steps and exit code are unaffected. `new` does not do it (a new conversation gets the right name directly), and neither can the in-place reattach inside sampan's own pane (sampan exits first). A single Enter on a stopped row in the screen waits for the reattach and the reset before taking you in. By hand it is `/remote-control` → `Disconnect this session` → `/remote-control <name>`. To fix an existing session's name, set the alias with `sampan alias`, then `sampan restart <name>` (an agent may run it from its shell for other sessions; busy ones and its own pane are skipped).

## `sampan alias <name> <alias>`, `sampan alias <name> --clear`

Sets or clears the alias (rules under `alias` in `registry.json`); `L` on the screen. Refused (exit 2) when an agent runs it from Bash while `[mcp] confirm` is on.

## `sampan migrate [--dry-run | --revert]`

Upgrades the data to the 0.15 format: `registry.json` to 3 (filling in `id`) and `folders.json` to 2; `dispatches.json` keeps its version. You normally never run it: the first 0.15 command of any kind does it automatically. Steps: copy the state files that exist into `backup-0.15-<time>/` in the state folder (never deleted; a retry after a failed run with identical content reuses the same backup), write the new format, re-read and compare session count, names, conversation ids and folder membership, and on any difference restore from the backup and report. It can be run again and does nothing when the data is already new. A broken `folders.json` is kept as it is with a warning, and the registry is still upgraded.

- `--dry-run`: tries it on a copy and prints the before/after difference and how many ids would be made; the real files are untouched (read-only).
- `--revert`: writes the registry back as version 2 and the folders as version 1, which 0.14 reads (after a backup). **Run it with 0.15 before downgrading**; ids and aliases are not kept, and the ids are regenerated at the next upgrade. An older sampan refuses version 3 / 2 files instead of damaging them.
- `sampan doctor` shows the data versions of the registry and folders and the last backup.
- After upgrading, a session still running an older `sampan mcp serve` cannot read the new registry and its sampan tools fail until that session is restarted; restart every session once (`sampan restart`). With 0.14 and 0.15 servers mixed, dispatches are still matched by name.

## `machine.toml` and `sampan machine`

`machine.toml` sits in the same folder as `config.toml`: `id` (made on creation) and `name` (editable; default the hostname without `.local`). `sampan machine` prints both; `sampan machine --name X` renames the machine (the id does not change; refused for an agent while `[mcp] confirm` is on). The id is written into the registry and the dispatch records, and MCP `list_sessions` returns it as `machine`. A cross-machine reference is written `<machine id>:<session id>`; only the format is defined so far.

## `sampan switch` (several at once)

`sampan switch <name…> <profile>`: the last word is the profile, before it come session names or ids (several allowed). `--folder <path>` switches everything in that folder (subfolders included) instead, and then only the profile is given. `--from <profile>` switches only the ones currently on that profile and skips the rest; `--dry-run` only lists the plan; `--yes` skips the "start?" question when several are switched. Skipped: busy or waiting sessions, Codex, retired, worktree, the pane sampan itself runs in, ones already on the target profile, ones not on `--from`. One table lists what happened to each session (switched, or skipped with the reason); conversation ids do not change. While `[mcp] confirm` is on, switching several at once (`--folder`, `--from`, or more than one name) is refused for an agent; `--dry-run` is not.
