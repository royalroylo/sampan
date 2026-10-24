# Architecture

sampan manages "one tmux session = one Claude Code or Codex agent". It remembers which conversation each session runs
so it can put it back after the agent exits or the machine reboots. Python 3.11+, standard library only.

## Layers

Checked against the imports: interfaces import actions, actions import detection and the registry, and actions may import
each other (`session_run` uses `save`, `adopt` uses `restore`). Detection is not fully below the registry: `observe.py` and
`status.py` also read `registry.py`, and `observe.py` reads the hook files through `hook.py`, but only to read.
What does not happen: nothing in detection, the registry or the leaf modules imports an action that changes things
(`save`, `restore`, `session_run`, `adopt`, `init`) or an interface (`cli`, `ui`, `doctor`).

1. **Detection** (read-only; turns the outside world into plain data)
   - `tmux.py` (panes), `procs.py` (process tree, environments), `observe.py` (one consistent snapshot of both),
     `claude_state.py` / `codex_state.py` (which conversation a process is in; context use; last activity),
     `screens.py` (every Claude / Codex screen marker and prompt parser lives here and only here),
     `status.py` (the `State` enum and `StatusRow` shown to the user).
2. **Registry** (the only durable state)
   - `registry.py` reads and writes `registry.json`; `config.py` reads `config.toml`; `fsutil.py` does atomic writes;
     `clock.py` stores times in UTC and formats them in local time.
3. **Actions** (change things, always on explicit command)
   - `save.py`, `restore.py`, `session_run.py` (new / stop / move / rename), `adopt.py`, `launch.py`, `prompts.py`
     (answering startup prompts through a `Relay`), `plugins.py` (read-only status and `sync` for scripts), `plugin_view.py` / `marketplace_view.py` (what the plugin screen shows: rows, cards, save previews; pure), `plugin_apply.py` (runs the `claude plugin` commands of a save, backs up and edits the config, restarts the sessions of chosen profiles), `config_edit.py` (byte-preserving edits of `config.toml`), `actionlog.py` (appends action messages to `actions.log`), `profile_ops.py` (profile list / rename / disable / enable / forget / add and the `[codex]` conversion; edits `config.toml` and the registry), `statusline_setup.py` (wraps / unwraps the `statusLine` command in a profile's `settings.json`), `hook.py`, `launchd.py`, `init.py`, `update.py`.
4. **Interfaces**
   - `cli.py` (argparse and dispatch, exit codes), `ui/` (curses screen), `doctor.py` (diagnostics).
   - `usage.py` is the quota reader plus the `sampan statusline` command that Claude Code runs on every refresh (light imports only): it stores the status-line JSON's rate limits and reads Codex's from the newest rollout; the ui shows them as account lines.
   - `ui/` is split into `text`, `state`, `cards`, `render` (pure), `screen` (the thin curses adapter),
     `actions` (the main loop and question flows), `dirpick`, and `plugin_state` / `plugin_actions` (the plugin
     screen's state machines, its question flows including the restart offer, and its `--plain` flow), and `profile_actions` (the accounts screen and the account lines on the main list).

All external programs (`tmux`, `ps`, `lsof`, `claude`, `launchctl`, `git`, `uv`) go through one injected `run(args)`
callable (`runner.py`). That is what makes the suite fast and safe: tests pass a `FakeRunner`.

## Invariants

- One agent per tmux session. A session with more than one agent is not saved, restored, stopped or moved.
- Resume never starts a new conversation in place of the recorded one. If it cannot be found, the session is skipped.
- sampan presses no key the user did not choose. The only automatic answer is `sampan move` pressing "trust" when the
  prompt's path is exactly the destination. A prompt that cannot be read gets no keys.
- `save` (and so the schedule) never restores, starts, moves or stops anything.
- The registry never deletes a row by itself; only `sampan forget` or `X` in the ui (which asks first) removes one.
  A session whose tmux is gone becomes "stopped", not removed. If tmux itself cannot be read, the state is "unknown",
  never "stopped".
- Every write is atomic, and the previous registry is copied to `history/` first. Everything written under the
  state dir is private (files `600`, directories `700`, whatever the umask; best-effort where chmod is refused), and the
  launchd job runs with `Umask` `077` so its logs there are private too.
- No telemetry; the only network use is the version-tag check and what the user asks for: `U` / `sampan update`, and in the plugin screen `r`, `u`, `U` and a confirmed save (opening the screen reads only local files).

## Data files

| Path | Content |
|---|---|
| `~/.config/sampan/config.toml` (`SAMPAN_CONFIG`) | profiles, marketplaces, plugin lists, `[restore]`, `[ui]`, `[codex]` (profiles carry `agent` and `disabled`) |
| `~/.local/state/sampan/` (`SAMPAN_STATE_DIR`) | state directory |
| `registry.json` | sessions: tmux name, agent, profile, conversation id, cwd, state, last seen (UTC) |
| `history/registry-*.json` | previous registries (newest 20 by modification time) |
| `save.log` | one line per `sampan save` run; `sampan doctor` reads it |
| `actions.log` (`.1`, `.lock`) | messages of the plugin, marketplace, restart, forget, switch and profile actions that ran, with time and action; credentials masked (`actionlog.py`) |
| `usage/<profile>.json` | latest Claude 5h / weekly quota written by `sampan statusline` (Codex is read from its own rollout files, never copied here) |
| `hooks/<pid>.json` | written by the optional SessionStart hook |
| `update-check.json` | cached newest version tag (6 hours) |
| `~/Library/LaunchAgents/io.github.royalroylo.sampan-save.plist` | macOS hourly save |

The file formats are documented in [docs/en/reference.md](docs/en/reference.md) (zh-TW: [docs/reference.md](docs/reference.md)).

## Testing

`tests/guard.py` makes any real `tmux`, `claude`, `codex`, `lsof`, `launchctl`, `uv` call fail the test. Platform and
environment are injected (`sys.platform`, `which`, `env`, `home`), never patched globally. `tests/test_scrub.py` keeps
private names out of the repository.
