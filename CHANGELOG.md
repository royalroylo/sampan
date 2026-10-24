# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[Unreleased]: https://github.com/royalroylo/sampan/compare/v0.6.5...HEAD
[0.6.5]: https://github.com/royalroylo/sampan/releases/tag/v0.6.5
[0.6.4]: https://github.com/royalroylo/sampan/releases/tag/v0.6.4
[0.6.3]: https://github.com/royalroylo/sampan/releases/tag/v0.6.3
[0.6.2]: https://github.com/royalroylo/sampan/releases/tag/v0.6.2
[0.6.1]: https://github.com/royalroylo/sampan/releases/tag/v0.6.1
[0.6.0]: https://github.com/royalroylo/sampan/releases/tag/v0.6.0
