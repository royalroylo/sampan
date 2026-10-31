# Install

English · [繁體中文](../install.md) · [Back to README](../../README.en.md)

## One-line install

Works on macOS and Linux:

```
curl -fsSL https://raw.githubusercontent.com/royalroylo/sampan/main/install.sh | sh
```

What it does (it asks before installing anything; `--yes` answers yes to everything):

- Checks the system: macOS or Linux; anything else stops with a message.
- If uv is missing: asks, then runs uv's official installer (`curl -LsSf https://astral.sh/uv/install.sh | sh`). It does not edit your shell startup files.
- If tmux is missing: on macOS with Homebrew it asks to run `brew install tmux`; without Homebrew it only prints the Homebrew install command and stops (it never installs Homebrew). On Linux it uses apt, dnf, pacman or zypper (with sudo unless you are root); for any other package manager it lists what to install and stops.
- On Linux it also makes sure `ps`, `lsof` and `git` exist, asking once for everything that is missing.
- Runs `uv tool install --reinstall git+https://github.com/royalroylo/sampan@<version>` (default: the latest `v*` tag, or `v0.14.2` if it cannot be looked up). If sampan is already installed this upgrades it in place.
- If Claude Code (`claude`) is missing it does **not** install it: it prints `curl -fsSL https://claude.ai/install.sh | bash` and tells you to run `claude` once and log in. sampan is still installed, but `sampan init` is skipped (init stops without claude).
- If `~/.local/bin` is not on your PATH it prints the exact line to add for your shell (zsh, bash or fish); it never edits rc files.
- Finally it runs `sampan init` (interactive; answers are read from `/dev/tty`, because under `curl | sh` stdin is the pipe. With no terminal it skips init and tells you to run it) and prints `sampan --version`.

Options go after `sh -s --`, for example `curl ... | sh -s -- --yes --no-init`: `--yes` (also runs `sampan init --yes` when there is a terminal, so init takes its defaults; with no terminal init is skipped; if init fails the installer only says so and carries on), `--no-init`, `--version vX.Y.Z`, `--help`.

**Read it first**: if you would rather not pipe a script from the internet into a shell, download it, read it, then run it:

```
curl -fsSL https://raw.githubusercontent.com/royalroylo/sampan/main/install.sh -o install.sh
less install.sh
sh install.sh
```

The whole script sits in a `main` function that is called on the last line, so a truncated download runs nothing. The only
thing it pipes into a shell is uv's official installer; Claude Code is only printed.

## Manual install

Without the script, on a new Mac, top to bottom (skip what you already have; Linux is in the next section):

```
# 1. Homebrew
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
# 2. tmux and uv
brew install tmux uv
# 3. Claude Code (run claude once afterwards and log in)
curl -fsSL https://claude.ai/install.sh | bash
# 4. sampan
uv tool install git+https://github.com/royalroylo/sampan@v0.14.2
sampan init
```

The package and the command it installs are both named `sampan`.
`uv tool install` puts `sampan` in `~/.local/bin`; if that is not on your PATH, add
`export PATH="$HOME/.local/bin:$PATH"` to your shell's startup file (for zsh, `~/.zshrc`).
Upgrade: press `U` in the `sampan` screen, run `sampan update [--yes] [--force]`, or `uv tool install --reinstall git+https://github.com/royalroylo/sampan@v<new version>`
(`sampan --version` shows the current one).

## Linux

Linux is experimental (see [Platforms and limits](platforms.md)). The installer does the following for you; by hand:

```
# Debian / Ubuntu
sudo apt-get update && sudo apt-get install -y tmux procps lsof git
# Fedora / RHEL
sudo dnf install -y tmux procps-ng lsof git
# uv, then sampan
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install git+https://github.com/royalroylo/sampan@v0.14.2
sampan init
```

Install Claude Code the same way as above (`curl -fsSL https://claude.ai/install.sh | bash`, then run `claude` once and log in).

**Auto-save**: on Linux `sampan init` skips the auto-save schedule and says so. Set one up yourself if you want it, for example cron (`crontab -e`):

```
*/5 * * * * $HOME/.local/bin/sampan save --quiet
```

or a systemd user timer (tried on WSL2: the timer comes back after WSL restarts, and `Persistent=true` runs a missed save at boot; save as `~/.config/systemd/user/sampan-save.service` and `sampan-save.timer`, then `systemctl --user enable --now sampan-save.timer`):

```
# sampan-save.service
[Service]
Type=oneshot
ExecStart=%h/.local/bin/sampan save --quiet

# sampan-save.timer
[Timer]
OnCalendar=*:0/5
Persistent=true
[Install]
WantedBy=timers.target
```

Like the macOS schedule, it only saves; it never restores, starts or moves anything. How Linux is tested: [CONTRIBUTING.md](../../CONTRIBUTING.md#testing-on-linux).

### WSL

On Windows, install and use sampan inside WSL (for example Ubuntu), as for Linux above:

- Run the installer in a WSL terminal. In the Windows command prompt or Git Bash it stops (`unsupported system MINGW…`).
- Claude Code in WSL is a separate install (the Linux one) with its own login: WSL's `~/.claude` is not Windows'.
- Keep sessions in WSL's own directories (under `~`); `/mnt/c/…` is slow.
- Ubuntu's tmux is often older than Homebrew's (3.4, say). A `~/.tmux.conf` brought from a Mac that uses newer options (3.5's `extended-keys-format`, for example) shows an error screen on every attach; `sampan doctor` names the lines. The error screen goes away only after the tmux server restarts.
- For auto-save use the systemd user timer above: first add `[boot]` `systemd=true` to `/etc/wsl.conf`, then restart WSL.
- Restart WSL with `wsl.exe --shutdown` (`wsl --shutdown` typed inside WSL is not found). Then open a new terminal tab, enter with `wsl ~` and run `sampan restore`; going back into WSL in the old tab can garble every key in some terminals (for example WezTerm).
- When Windows restarts or WSL shuts down, tmux and every session end; run `sampan restore` yourself to bring them back.
- Plugins: put the marketplaces and plugins you use on Windows into `config.toml`, then `sampan plugins sync`. A private repo needs git credentials set up inside WSL first, or its marketplace cannot be added (`sampan doctor` reports 「清單上的 marketplace … 沒有加進來」): a self-hosted Git server can use `git config --global credential.helper store`; for GitHub you can borrow Windows' Git Credential Manager:
  ```sh
  git config --global credential.helper ""   # clear other helpers first
  git config --global --add credential.helper "/mnt/c/Program\ Files/Git/mingw64/bin/git-credential-manager.exe"
  git config --global credential.https://github.com.username <your GitHub user>
  ```
- User-level MCP servers (`claude mcp add --scope user`) do not come with the plugins; add them again in WSL. For ones that need Node, install the Linux Node in WSL first (for example from NodeSource): WSL's PATH carries the Windows paths by default, and `npx` may resolve to the Windows one under `/mnt/c/…`.
- Moving a Claude conversation that is under way on Windows into WSL (by hand):
  1. Note the conversation id (`/status` shows it) and `/exit` on the Windows side.
  2. Copy `%USERPROFILE%\.claude\projects\C--Users-…\<id>.jsonl` (and the folder of the same name, if there is one) to WSL's `~/.claude/projects/-mnt-c-Users-…/` (the folder name is the working directory inside WSL with every character other than letters and digits, `/`, `.`, `_` and spaces included, turned into `-`: `/mnt/c/Users/a.b/my_app` → `-mnt-c-Users-a-b-my-app`).
  3. In WSL `cd` to the same directory (`/mnt/c/…`), run `claude --resume <id>`, and once it is back hand it to sampan with `sampan adopt`.
  A Codex conversation records Windows paths (`C:\…`); there is no way to move it yet.

## What sampan init does

`sampan init` asks questions that write files, so it refuses to run (exit code 2, nothing written) when stdin is not a terminal. In a script, pass `--yes` to take every default (each question is still printed, followed by `→ 預設（--yes）`). **An agent must not run init for the user**; ask the user to run it in a terminal. Directories under `~/.claude-profiles/` whose name contains `.retired.` are treated as retired and skipped.

`sampan init` first checks for Homebrew (macOS), tmux, claude, codex (optional) and your PATH. If tmux is missing and
Homebrew exists it asks before running `brew install tmux`; for anything else it only prints the install command for you
to run (the Homebrew installer asks for your password). It stops without writing anything if tmux or claude is missing.
`sampan doctor` runs the same checks but only reports.

It then detects `~/.claude`, `~/.claude-profiles/*/` and the `CLAUDE_CONFIG_DIR` of running claude processes, asks for
profile names and writes `~/.config/sampan/config.toml` (showing it first). Running it again only proposes new items and
never touches a config file it cannot read. `init` also:

- optionally installs a SessionStart hook in each profile's `settings.json`. It is opt-in: init neither asks nor installs unless you run `sampan init --hook`. A hook that is installed but broken (it points to a sampan that no longer exists) is still offered for repair without `--hook`;
- on macOS, offers a 5-minute auto-save schedule (default yes; `sampan init --no-launchd` skips it):
  `~/Library/LaunchAgents/io.github.royalroylo.sampan-save.plist` runs `sampan save --quiet` every 5 minutes (a run missed
  during sleep is made up on wake). If `SAMPAN_CONFIG` / `SAMPAN_STATE_DIR` were set when you ran `init`, the schedule uses
  the same values. **The schedule only saves; it never restores, starts or moves anything.**

An example configuration is in [config.example.toml](../../config.example.toml); the format is described in [reference.md](reference.md).
