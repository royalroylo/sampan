# Security

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting: open the **Security** tab of
[royalroylo/sampan](https://github.com/royalroylo/sampan) and choose
"Report a vulnerability". Please do not open a public issue for security problems.

## What sampan does on your machine

sampan runs as you, with your shell and your agents' permissions. Everything is local: there is no telemetry and
no account. The complete list of what it changes or touches:

**Files it writes**

- `~/.config/sampan/config.toml` (written by `sampan init`, after showing you the content).
- `~/.local/state/sampan/` (override with `SAMPAN_STATE_DIR`): `registry.json` (the session registry), `history/`
  (snapshots of earlier registries), `save.log`, and `hooks/` (one small file per Claude process, only if you enabled the hook).
- Optional: a `SessionStart` hook in each Claude profile's `settings.json` (`sampan hook session-start`). `sampan init`
  asks about it only with `sampan init --hook` (default no); a broken hook from an earlier install is offered for repair.
  It makes a `settings.json.bak-sampan-*` backup before changing the file.
- macOS only: `~/Library/LaunchAgents/io.github.royalroylo.sampan-save.plist`, loaded with `launchctl`. It runs
  `sampan save --quiet` every hour. It only saves; it never restores, starts or moves anything. `sampan init` asks first;
  `--no-launchd` skips it. On other systems no schedule is installed.
- sampan never edits `~/.claude.json` or `~/.codex/config.toml`.

**Keys it types into tmux panes** (only on your command, never from the schedule or a hook)

- `claude --resume <id>` / `codex resume <id>` into a pane (a new session, or an existing shell pane after a single `C-u`
  to clear a half-typed line).
- `/exit` (Claude) or `/quit` (Codex) for `sampan stop` and the first half of `sampan move`.
- Answers to startup prompts (trust this folder, Codex update, hooks review): only the single option you picked in the
  `sampan` screen or an interactive terminal. The one exception is `sampan move`, which presses "trust" when the prompt shows a
  path exactly equal to the destination directory; that grants the agent trust over that directory. Anything else gets no keys.
- `sampan adopt` never presses anything in the original terminal; you exit the agent yourself.

**Commands it runs**

- `claude plugin install/update/uninstall` per profile (`sampan plugins sync`). `--prune` removes plugins that are not
  in your config; use `--dry-run` first.
- `tmux`, `ps`, `lsof`, `git ls-remote` and (macOS) `launchctl`.
- `uv tool upgrade sampan` (or a reinstall pinned to the newest tag) when you press `U` in the UI and confirm.

**Network**

- A check of the repository's version tags on GitHub, at most every 6 hours while the UI is open. No other traffic
  from sampan itself (the agents and `claude plugin` commands do their own).

**Data it reads**

- Process lists and environments (`ps`, `/proc`), the files `lsof` shows for a Codex process, Codex's
  `state_*.sqlite` (read-only), Claude's session files and the tail of transcripts. Nothing is sent anywhere.

## Trust model

The registry holds working directories and conversation ids, and sampan acts on it: anyone who can write
`registry.json` can make sampan type resume commands into your tmux panes. sampan keeps the state directory private
itself: whenever it writes there, the directory (and `history/`, `hooks/`) is set to `700` and the files it writes
(`registry.json`, the history snapshots, `save.log`, the lock, the update-check cache, the hook records) to `600`,
whatever your umask, also fixing files an older version left readable by others. A new `config.toml` written by
`sampan init` is created `600`; sampan never changes the permissions of an existing config file, so keep `config.toml`
(it names the marketplaces `plugins sync` installs from) and the profile `settings.json` files writable only by you.

Safety rules the code keeps (see `ARCHITECTURE.md`): one agent per tmux session; resume never opens a new conversation
in its place; `save` never restores; the registry never deletes a row on its own.
