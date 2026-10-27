# Platforms and limits

English · [繁體中文](../platforms.md) · [Back to README](../../README.en.md)

## Supported platforms

- **macOS**: fully supported, including the 5-minute auto-save schedule.
- **Linux**: experimental. Commands and the screen work, but there is no auto-save schedule: `sampan init` skips launchd and
  says so, so run `sampan save` yourself (or from cron or a systemd timer). `sampan doctor` does not check Homebrew or launchd.
  Checked on Ubuntu 24.04 (Docker) with fake claude/codex scripts: init, new, save, stop, restore and the ui ([`scripts/linux-smoke/`](../../scripts/linux-smoke/)),
  and the unit tests on Python 3.11-3.13. Also tried on Windows WSL2 (Ubuntu, systemd on) with the real Claude Code: init, doctor, new, the screen, save, stop, and restore back into the same conversation after `wsl.exe --shutdown` (see [Install: WSL](install.md#wsl)); Codex is not yet tried on Linux. Known limits: needs `ps`
  (procps); Codex conversations are identified through `lsof` (without it sampan falls back to Codex's own database, which
  recognises fewer cases); no launchd, so no auto-save.
- **Windows**: not supported.

Installing on Linux (the one-liner, cron and systemd save examples): [Install: Linux](install.md#linux). How it is tested: [Testing on Linux](../../CONTRIBUTING.md#testing-on-linux).

## Windows

sampan depends on Unix process tools and tmux, so Windows is not supported. On Windows use [herdr](https://github.com/herdrdev/herdr),
and consider turning off its `resume_agents_on_restore` so it does not resume every agent at boot.

## Limits

- One agent per tmux session; if more than one is detected in a session, the whole session is not saved, resumed, stopped or moved, and sampan asks you to split it.
- Finding a conversation relies on Claude Code's undocumented session files (`<config_dir>/sessions/`) and transcript
  format, and on Codex's rollout files. They may change, and sampan may need updating with them; `sampan doctor` flags known problems.
- macOS is the only fully supported platform; Linux is experimental (no auto-save schedule); Windows is not supported.
