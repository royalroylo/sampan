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
next write. The previous file is copied to `history/registry-<time>.json` before each rewrite (newest 20 kept).
Times (`saved_at`, `seen_at`, the history file names) are written in UTC (`+00:00`) and converted to local time only for
display; history files left by older versions may be named in local time.
