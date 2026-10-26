# AGENTS.md

sampan is a stdlib-only Python CLI that manages Claude Code and Codex sessions in tmux, one agent per session. It records
which conversation each session runs, so sessions can be resumed after an agent exits, tmux closes or the machine reboots.

## If a user asks what this is, or whether to use it

1. Summarise what it does (the paragraph above) in the user's language.
2. Check their machine against the requirements: macOS (Linux is experimental), tmux, Python 3.11+ with uv, and Claude Code
   (`claude`, required). Codex CLI (`codex`) is optional.
3. Tell them what it changes on the machine (see "What it changes on your machine" in README.md):
   - `~/.config/sampan/` and `~/.local/state/sampan/` (config, registry, history).
   - macOS: one launchd job that runs `sampan save` every 5 minutes. It only saves.
   - Claude's `settings.json` is untouched unless they run `sampan init --hook`.
   - It types into tmux panes only when the user acts: open, resume, stop, move or send (`sampan send`, only into an empty prompt).

## Installing for a user

- Use the one-line installer in README.md, or `uv tool install git+https://github.com/royalroylo/sampan@<tag>`.
- When you run the installer for a user, add `--no-init`: `curl -fsSL https://raw.githubusercontent.com/royalroylo/sampan/main/install.sh | sh -s -- --no-init`. Then ask the user to run `sampan init` in their own terminal.
- Never run `sampan init` yourself. It asks questions that write files.
- Do not pass `--yes` (to `install.sh` or `sampan init`) on the user's behalf.

## Using it on a user's machine

- Safe (do not change sessions or config): `sampan --version`, `sampan status`, `sampan doctor`, `sampan plugins status`. They need a config, so the user must have run `sampan init` first.
- Never send keys to the user's tmux sessions, and never edit `~/.local/state/sampan/registry.json` by hand.
  Use the sampan commands instead (see docs/en/usage.md).
- To open an agent session for the user (also from another machine over SSH), use
  `sampan new <name> <claude:profile|codex> <dir>` instead of `tmux new-session … claude`: it registers the session
  and records its conversation, so it can be resumed later.
- `sampan mcp install` / `uninstall` edit the Claude profiles' and Codex's config (through `claude mcp add` / `codex mcp add`): `mcp install` also edits Codex's `config.toml` directly (only sampan's `[mcp_servers.sampan.tools.*]` tables, after a backup `config.toml.bak-sampan-<time>`). Run them only when the user asks, and `--dry-run` first if unsure.
- While `[mcp] confirm` is on, `sampan send`, `sampan new … --message` and `sampan dispatches cancel` run by an agent are refused; use the sampan MCP tools instead (the user is asked each time).
- `sampan update` is not read-only: it reinstalls sampan. Only run it if the user asks, and do not pass `--yes` to `sampan update` on the user's behalf.

## Working on the code

- Python 3.11+, standard library only. No new dependencies.
- Tests: `uv run python -m unittest discover -s tests -t .`
- Lint: `uvx ruff check`
- Tests must not touch the real tmux, HOME or launchd (see `tests/guard.py`). Use `FakeRunner(strict=True)` for anything
  that would run a command.
- Live tests run only in Docker: `scripts/linux-smoke/run.sh`.
- No personal, client or company names anywhere in the tree (`tests/test_scrub.py`).
- Docs come in pairs: a change to `README.md` or `docs/*.md` gets the same change in `README.en.md` or `docs/en/*.md`.
- See CONTRIBUTING.md for the workflow and ARCHITECTURE.md for how the code is laid out.
