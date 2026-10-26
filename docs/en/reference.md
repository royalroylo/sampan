# Configuration and registry formats

English · [繁體中文](../reference.md) · [Back to README](../../README.en.md)

For migration or conversion scripts. A version sampan does not understand is never overwritten; it is reported.

## `~/.config/sampan/config.toml` (`version = 1`)

| Field | Meaning |
|---|---|
| `version` | integer, must be `1` |
| `[restore] recent_days` | non-negative integer, default 7; `restore` pre-checks only sessions active within this many days |
| `[ui] start_dir` | path where the directory chooser of `n` (start new work) starts; default `~/Projects` if it exists, else `~` |
| `[ui] codex_auto_start` | boolean, default `true`. When the `sampan` screen opens and the Codex daemon is not running, run `codex remote-control start` once in the background; `false` never starts it |
| `[mcp] confirm` | boolean, default `true`. The `send`, `new_session` and `cancel_dispatch` tools of `sampan mcp serve` ask you every time through an MCP approval form (elicitation): approve, decline, or edit the message first; a client that does not declare elicitation is refused, never skipped. `false` never asks and just acts (still logged in `actions.log`, still cancellable). The read-only `list_sessions` and `dispatch_result` never ask |
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

## `registry.json` (`version = 2`, in the state folder)

```json
{
  "version": 2,
  "saved_at": "2026-01-01T09:00:00+00:00",
  "sessions": [
    {"tmux": "work", "agent": "claude", "profile": "alice", "session_id": "…",
     "cwd": "/Users/u/proj", "state": "active", "seen_at": "2026-01-01T09:00:00+00:00",
     "server": "…", "pid": 12345}
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

Version 1 files are still read (a missing `agent` counts as `claude`, a missing `pid` as `null`) and become version 2 on the
next write. Before a rewrite the previous file is copied to `history/registry-<time>.json` (newest 20 kept) only when a session really changed (`seen_at`, `pid` and `server` do not count).
Times (`saved_at`, `seen_at`, the history file names) are written in UTC (`+00:00`) and converted to local time only for
display; history files left by older versions may be named in local time.

## `dispatches.json` (`version = 1`, in the state folder)

Dispatch records: one message sent from a caller session (or the terminal) to a target session. Reads and writes hold `dispatches.lock` in the same folder; the file is 600 and the folder 700. The format is `{"version": 1, "dispatches": [...]}`, each entry:

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
| `startup` | the startup message of `sampan new --message` (the new session may not be registered yet); an old record without it counts as `false` |

A dispatch made by `sampan new --message` starts as `sent` (`sent_at` is when the session was opened; `text` and `outbox/<id>.txt` do not end with a newline, because `$(cat …)` drops trailing newlines and the reply would not match); the conversation id is filled in by later advances from the status rows. While the new session is not recognised yet it is not failed, and `reason` says 「對方 session 還在啟動」: a Codex without a conversation yet (unregistered) is waited for as long as that Codex runs; a tmux session whose agent is not seen yet (a registered Claude shown as 「該跑沒跑」, or no agent) for at most 120 seconds; a folder-trust or other startup prompt for as long as it is shown.

How the state moves: `queued` → `sent` (delivered, `sent_at`) → `done` (`reply` is the reply to this message) or `failed` (`reason` says why); `cancelled` comes from `queued` (never sent, `reason` 「已取消」) or from `sent` (stop waiting for the reply; the message is already with the target, `reason` 「不再等回覆」, no notice). `done_at` is when it finished. Every step is taken inside the lock (read the latest, decide, write), so one dispatch is never delivered twice. Before any key is pressed or `codex queue` runs, the record is written as `sent` (`reason` 「送出中」): if sampan is killed in the middle, the dispatch is never sent again (it later fails as 「找不到回覆」), so it is delivered at most once; it goes back to `queued` only when nothing was sent for sure (text in the target's box, an unreadable screen, a failed tmux load…). If Enter fails after the paste (the text is already in the target's box) or `codex queue` times out (Codex may have taken it), it becomes `failed` (「可能已送出，請確認」) and is never sent again (the same for the completion notice: it counts as notified, with the `reason` saying so). A target pane in tmux copy mode gets nothing; the dispatch stays queued.

How a `sent` one ends: when the target is idle and its transcript has an answer after this message → `done`; another message was typed after this one with no answer in between → 「對方沒回這則就換了下一則」; a Codex turn that completed without reply text, or a Claude target idle twice in a row without reply text → 「對方這輪結束了，但沒有回覆文字」; idle twice in a row without the message → 「找不到回覆」 (seeing the target busy in between starts over).

Exit codes of `sampan send`: 0 sent or queued (`--wait`: answered), 1 failed, 2 refused or the file cannot be read, 124 `--wait` timed out (the dispatch stays), 130 Ctrl-C (the dispatch stays). `sampan dispatches show <id> [--wait seconds]`: advances it once (or every 2 seconds until it is finished, for at most that long) and prints the state, from whom to whom, the reason and the whole reply; exit 0 answered or still going (without `--wait`), 1 failed or cancelled, 2 no such dispatch or the file cannot be read, 124 `--wait` timed out, 130 Ctrl-C. The completion or failure notice (`notify`) goes to the caller's session through `outbox/<id>-notice.txt`: `〔sampan：<target> 已回覆派工 <id>，用 sampan 工具 dispatch_result 看內容〕` when done, `〔sampan：給 <target> 的派工 <id> 失敗：<reason>〕` when failed.

The caller of `sampan send` / `sampan new --message` is also found from the parent processes (as for MCP): run by an agent through its shell tool, it is recorded as that sampan session (or 「未登記的 session」), and sending to itself is refused; only with no claude / codex process above it is it 「終端機」. These two commands send no notice; read the reply with `sampan dispatches show`.

A file that cannot be read is never overwritten. Finished entries (`done`, `failed`, `cancelled`) older than 7 days are dropped, and only the newest 200 are kept. `outbox/<id>.txt` holds the exact text to deliver (mode 600).

## The tools of `sampan mcp serve`

A stdio MCP server (one JSON-RPC 2.0 message per line; protocol version `2025-06-18`, or `2025-03-26` when the client asks for it). Claude Code or Codex starts it; the caller is found from the server's parent processes: the tmux pane of the nearest claude / codex process, then the registry row of that tmux session with the same agent. When that does not match, the caller is an "unregistered session" (the source line and the dispatch record say for example 「未登記的 session（claude）」, or 「未登記的 session」 when no agent process is above the server or ps cannot be read at start; an MCP caller is never 「終端機」): the tools still work, it only gets no completion notice. The caller is looked up again before every tool that acts or reads a dispatch (a Codex session is registered only once its conversation exists, a session may be adopted or renamed); only a lookup that cannot read tmux, ps or the registry keeps the identity already known; a definite miss (no row, another agent) makes the caller unregistered. `dispatch_result` and `cancel_dispatch` only accept this caller's own dispatches (created by this server, or with `from_kind` `session` and the caller's session as `from_name`); any other id gets 「沒有派工 <id>（或不是這個 session 發出的）」. Tool results are JSON text; a refusal (unknown target, declined by the user, a client without approval forms) is a result with `isError: true`.

| Tool | Arguments | Returns | Approval |
|---|---|---|---|
| `list_sessions` | none | `sessions`: each with `name`, `agent`, `profile`, `cwd`, `state` (`running`, `stopped`…), `activity` (`idle`, `busy`…), `last` (UTC ISO or `null`); plus `warning` when tmux cannot be read | never asks |
| `send` | `session`, `message`, `wait` (default `false`) | `dispatch`, `state`, `reply` once done, `reason` while not sent yet. `wait` waits at most 60 seconds; a dispatch not finished by then stays, and a notice goes to the caller's session when it is done or fails | asks |
| `new_session` | `name`, `target` (`claude:<profile>`, `<profile>` or `codex`), `cwd`, `message` | `dispatch` (the startup-message dispatch, as with `sampan new --message`) | asks |
| `dispatch_result` | `id`, `wait` (0–60 seconds, default 0) | `dispatch`, `state`, `label`, `to`, `reply`, `reason` | never asks |
| `cancel_dispatch` | `id` | `dispatch`, `state`; a queued one is cancelled, a sent one is no longer waited for (plus `reason` 「不再等回覆」) | asks |

The approval form shows who it goes to (name, profile, Claude / Codex), which session it comes from and the whole message, plus 「完成或失敗後會在你的 session 送出一則通知」 (a notice will be sent to your session when it is done or fails) when the caller is a sampan session; the agent's message always sits below the fixed separator 「──── 訊息內容（由 agent 提供）────」, and everything above it is written by sampan; a name, target or directory with a newline or control character is refused before asking, and the directory shown is the resolved path. Its one optional field `message` defaults to the original message (a client need not prefill it): empty or absent sends the original, a filled-in one is what gets sent; after approval the target is checked again (it may have stopped while the form was open). `new_session` runs the same checks as `sampan new` (name, disabled profile, Codex turned off, tmux session exists, missing directory), all before asking. Approving a dispatch approves its completion or failure notice; there is no second question. Once `dispatch_result` has shown a done or failed result, no notice is sent. While the server runs it advances this caller's dispatches every 3 seconds; every program it runs gets `/dev/null` as stdin, so none reads the JSON-RPC input.

### Installing, and what the approval form relies on

`sampan mcp install` runs `claude mcp add --scope user sampan -- <sampan path> mcp serve` for every enabled Claude profile (with that profile's `CLAUDE_CONFIG_DIR`), and `codex mcp add sampan -- <sampan path> mcp serve` when Codex is enabled (with `CODEX_HOME` when the Codex home is not `~/.codex`); where it is already installed it is skipped. `sampan mcp uninstall` does the matching `remove`. Both take `--dry-run`, and each result line is logged in `actions.log` (label `mcp`). Restart the profile's sessions to see the tools. `sampan doctor` shows one line with where it is installed (nothing when it is installed nowhere).

`[mcp] confirm = true` relies on the calling client actually showing the form to a person. What was checked (2026-10-26):

- **Codex CLI supports elicitation forms.** The official configuration reference lists `approval_policy = { granular = { mcp_elicitations = … } }`: "when `true`, MCP elicitation prompts are allowed to surface instead of being auto-rejected", so `false` auto-rejects them (`send` and the others then fail; they are not bypassed). Third-party release-note write-ups say support began around v0.119 / v0.120 and that under `danger-full-access` (`--dangerously-bypass-approvals-and-sandbox`) Codex **auto-approves** every elicitation. The local `codex-cli 0.164.0` `codex mcp --help` shows no elicitation option (only `-c`).
- **So a Codex started with `--dangerously-bypass-approvals-and-sandbox` has no approval form in practice.** `confirm = true` cannot stop such a caller; avoid that mode, or do not install the sampan MCP for that agent.
- **Claude Code**: the official MCP docs describe an elicitation dialog and an `Elicitation` hook, and say a non-interactive (headless) session asks the server to cancel a request that needs input. They do **not** say whether `bypassPermissions` / `--dangerously-skip-permissions` auto-answers elicitations, and this was not tested: treat it as unverified. An `Elicitation` hook can answer on the person's behalf, so a profile with such a hook bypasses the person too.

Sources: official [Codex configuration reference](https://developers.openai.com/codex/config-reference) (redirects to learn.chatgpt.com/docs/config-file/config-reference); third-party [MCP Server Elicitations in Codex CLI](https://codex.danielvaughan.com/2026/04/10/mcp-elicitation-codex-cli/); Claude Code [MCP docs](https://code.claude.com/docs/en/mcp). The official Codex MCP page does not mention elicitation; version details come from the third-party source only.
