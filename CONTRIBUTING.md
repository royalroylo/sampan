# Contributing

Issues and pull requests are welcome. Write in English or Traditional Chinese (zh-TW), whichever you prefer.

## Ground rules

- **Standard library only.** sampan has no runtime dependencies and needs Python 3.11 or newer. Do not add one.
  (Linters and similar tools for development or CI are fine.)
- User-facing text is Traditional Chinese (zh-TW); code, comments and docstrings are English. The exception is `install.sh`, which strangers run: its messages are short English.
- Keep the safety rules in `ARCHITECTURE.md`: one agent per session, resume never starts a new conversation, no keys
  the user did not choose (except the exact-path trust prompt of `sampan move`), `save` never restores, no automatic
  registry row deletion.

## Running the tests

```
uv run python -m unittest discover -s tests -t .
```

**Tests must never touch a real tmux server, `claude`, `codex`, `launchctl` or `uv`.** `tests/guard.py` blocks them
(and `tests/test_isolation.py` checks it); use `FakeRunner` from `tests/fakes.py` and temporary directories. Anything that
needs a real tmux must use a private socket (`tmux -L <name>`) and be run by hand, never in the suite.

## Testing on Linux

```
scripts/linux-smoke/run.sh [git-ref]
```

Needs Docker. It builds images from a `git archive` of the ref (default `HEAD`, so commit first; nothing on your machine is mounted)
and runs, inside containers named `zz-sampan-linux-*`: the unit tests on Python 3.11, 3.12 and 3.13; the live smoke
(`smoke.sh`: a real tmux with fake `claude`/`codex` in `scripts/linux-smoke/fake-bin/`: init, new, save, stop, restore, ui);
and the installer test (`install-test.sh` on a bare Ubuntu 24.04 where `/bin/sh` is dash: `install.sh` has to bring uv, tmux,
ps, lsof and git itself, plus shellcheck and the "claude missing" / rerun / no-terminal cases). The GitHub repo may be private,
so the installer test points `SAMPAN_INSTALL_SPEC` at the local copy and feeds `sampan init` its answers from a file through
`SAMPAN_INSTALL_TTY`; because the copy has no `.git`, sampan reports version `0.0.0+unknown` there.

Never run `smoke.sh` or `install-test.sh` directly: they refuse outside a container, because they start tmux servers and install packages.
CI (`.github/workflows/test.yml`) runs the unit tests on Ubuntu and macOS (Python 3.11-3.13) and ruff; the Docker scenarios are run by hand.

## The scrub test

`tests/test_scrub.py` scans every tracked file for a list of private words (kept as hashes) so that internal names do
not leak into the repository. As a contributor you can ignore it unless it fails; if it does, rename the offending
string in your change to something neutral (`alice`, `proj-a`, `/Users/u`).

## Commits

Use [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`, `docs:`, `refactor:`, `test:`,
`chore:`, with an optional scope (`fix(ui): ...`). Keep each commit passing the full suite. Add a line to
`CHANGELOG.md` under "Unreleased" for user-visible changes.
