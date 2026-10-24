# sampan

English · [繁體中文](README.md)

Manages tmux sessions with one agent per session (Claude Code or Codex).

sampan remembers which conversation each session is running. After an agent exits, tmux closes or the machine reboots,
you can put every session back into its original conversation, one by one. It also keeps the plugins of several Claude
profiles in sync.

## Requirements

- macOS (Linux is experimental, see [Platforms and limits](docs/en/platforms.md))
- tmux
- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- Claude Code (`claude`), required
- Codex CLI (`codex`), optional; skip it if you do not use Codex

No third-party Python dependencies. The package and the command are both `sampan`.

## Install

```
curl -fsSL https://raw.githubusercontent.com/royalroylo/sampan/main/install.sh | sh
```

Or by hand, two lines:

```
uv tool install git+https://github.com/royalroylo/sampan@v0.7.2
sampan init
```

`sampan init` asks questions — run it yourself in a terminal.

For what the one-liner does, reading it first, Linux, a new machine, PATH and what `sampan init` does, see the [install guide](docs/en/install.md).

Tip: sampan is a long name to type, and on macOS `sam` plus Tab collides with `/usr/bin/sample`. Add `alias sp=sampan` to your shell rc (sampan does not ship an `sp` binary).

## What it changes on your machine

- Config goes in `~/.config/sampan/`; the registry and history in `~/.local/state/sampan/`.
- macOS: one launchd job runs `sampan save` every hour. It only saves.
- Claude's `settings.json` is not touched unless you run `sampan init --hook` (or accept repairing a hook installed earlier).
- It types into tmux panes only when you act: open, resume, stop or move.
- Network: while the UI is open it checks sampan's version tags on GitHub at most every 6 hours; `U` checks again right away when no newer version is known yet, and `sampan update` always checks again.
- `sampan init` asks questions and must be run by you in a terminal. It refuses to run without one unless `--yes` is given.

More in [SECURITY.md](SECURITY.md).

## Usage

Type `sampan`. The session list is on the left, an info card for the row under the cursor on the right.

| Key | Action |
|---|---|
| Enter | Go into that session (a stopped one is resumed first) |
| `1`-`9` | Jump to that row (type two digits quickly for row 10 and up) |
| `/` | Search by name (Enter opens, Esc cancels) |
| `n` | Start new work |
| `s` | Put it away (the agent exits normally, the conversation is kept) |
| `R` | Resume several at once |
| `m` | Change working directory |
| `p` | Open plugin management (each profile's plugins and marketplace sources) |
| `X` | Remove a stopped or retired session from the registry (the conversation is not deleted) |
| `U` | Update sampan (updates when a newer version is already known; otherwise checks right then, and when already up to date it only says so; to reinstall use `sampan update --force`) |
| `q` | Quit |

- Starting the day: `sampan`, then Enter on the one you want.
- Putting something away half-way: `s`; to continue, press Enter and the same conversation is resumed.
- After a reboot: `sampan`, press `R` and confirm what to resume. sampan never resumes on its own.
- Scripts or a very narrow terminal: `sampan ui --plain`, or use the individual commands.

The full key reference, the commands and the rules for restore are in the [usage guide](docs/en/usage.md).

## Documentation

- [Install](docs/en/install.md)
- [Usage](docs/en/usage.md)
- [SSH and tmux setup](docs/en/setup.md)
- [Configuration and registry formats](docs/en/reference.md)
- [Platforms and limits](docs/en/platforms.md)
- [SECURITY.md](SECURITY.md)
- [CONTRIBUTING.md](CONTRIBUTING.md)
- [ARCHITECTURE.md](ARCHITECTURE.md)
- [CHANGELOG.md](CHANGELOG.md)

## License

MIT, see [LICENSE](LICENSE).
