#!/bin/bash
# Installer test, run INSIDE the container (see run.sh): install.sh on a bare Ubuntu 24.04 (/bin/sh is dash).
# The GitHub repo may be private, so SAMPAN_INSTALL_SPEC points `uv tool install` at the local copy of the repo.
# `sampan init` is interactive; install.sh reads its answers from SAMPAN_INSTALL_TTY (here: a file of newlines, i.e. the defaults)
# instead of /dev/tty, which a container has no use for.
# Version note: the copy has no .git, so hatch-vcs reports its fallback version (0.0.0+unknown) instead of 0.6.0.
[ -f /.dockerenv ] || { echo "install-test.sh only runs inside the container: use scripts/linux-smoke/run.sh" >&2; exit 1; }
set -uo pipefail
SRC="$(cd "$(dirname "$0")/../.." && pwd)"
fails=0
step() { printf '\n=== %s\n' "$*"; }
expect() { local what="$1"; shift; if "$@"; then echo "  ok   $what"; else echo "  FAIL $what"; fails=$((fails + 1)); fi; }
has() { grep -q -- "$1" <<<"$2"; }
printf '\n\n\n\n\n\n' > /tmp/answers
export SAMPAN_INSTALL_SPEC="$SRC" SAMPAN_INSTALL_TTY=/tmp/answers

step "bare machine"
expect "/bin/sh is dash" test "$(readlink -f /bin/sh)" = /usr/bin/dash
for tool in uv tmux git lsof ps; do expect "$tool is not installed yet" bash -c "! command -v $tool"; done
mkdir -p ~/.claude  # what "run claude once and log in" leaves behind; sampan init refuses to run without it

step "A: fresh machine, fake claude on PATH, --yes (installs uv, tmux, ps, lsof, git; sampan; runs init)"
export PATH="/usr/local/fake-bin:$PATH"
out=$(sh "$SRC/install.sh" --yes --version v0.6.0 2>&1); rc=$?; echo "$out" | tail -25
expect "exit 0" test "$rc" = 0
expect "tmux installed" command -v tmux
expect "git, ps, lsof installed" bash -c 'command -v git && command -v ps && command -v lsof'
expect "uv installed in ~/.local/bin" test -x "$HOME/.local/bin/uv"
expect "sampan installed" test -x "$HOME/.local/bin/sampan"
expect "init ran and wrote the config" test -s ~/.config/sampan/config.toml
expect "init output seen" has "sampan init" "$out"
expect "--yes was passed through to init" has "→ 預設（--yes）" "$out"
expect "installer's own PATH hint printed (~/.local/bin was not on PATH)" has "$HOME/.local/bin is not on your PATH. Add it" "$out"
expect "sampan init's own PATH hint printed too" has "不在 PATH" "$out"
expect "sampan --version printed" has "sampan" "$(tail -1 <<<"$out")"
echo "  version line: $(tail -1 <<<"$out")"

step "B: run it again (idempotent: upgrades in place, init re-run changes nothing)"
cp ~/.config/sampan/config.toml /tmp/config.before
out=$(sh "$SRC/install.sh" --yes --version v0.6.0 2>&1); rc=$?; echo "$out" | tail -8
expect "exit 0" test "$rc" = 0
expect "--yes was passed through to init" has "→ 預設（--yes）" "$out"
expect "config unchanged" cmp -s /tmp/config.before ~/.config/sampan/config.toml
expect "nothing to install the second time" bash -c '! grep -q "^Missing:" <<<"$0"' "$out"

step "C: claude missing (fresh HOME, tools already present): sampan installed, init skipped with a message"
export HOME=/tmp/home-c; mkdir -p "$HOME"
export PATH="${PATH//\/usr\/local\/fake-bin:/}"
expect "claude is not on PATH" bash -c '! command -v claude'
out=$(sh "$SRC/install.sh" --yes 2>&1); rc=$?; echo "$out" | tail -12
expect "exit 0" test "$rc" = 0
expect "sampan installed" test -x "$HOME/.local/bin/sampan"
expect "prints the Claude Code install command" has "claude.ai/install.sh" "$out"
expect "says init was skipped" has "Skipped 'sampan init': Claude Code" "$out"
expect "no config written" test ! -e "$HOME/.config/sampan/config.toml"

step "D: no terminal and no --yes: every question is answered no, so it stops at uv (missing in this fresh HOME) and installs nothing"
export HOME=/tmp/home-d; mkdir -p "$HOME"; export PATH="/usr/local/fake-bin:$PATH"
out=$(SAMPAN_INSTALL_TTY=/nonexistent sh "$SRC/install.sh" --version v0.6.0 </dev/null 2>&1); rc=$?; echo "$out" | tail -8
expect "exit 1 with the uv message" test "$rc" = 1
expect "says uv is required" has "uv is required" "$out"
expect "uv was not installed" test ! -e "$HOME/.local/bin/uv"
expect "asked about uv and answered no" has "no terminal to ask on" "$out"

step "E: bad option / --help"
expect "--help exits 0" sh "$SRC/install.sh" --help
expect "unknown option exits 1" bash -c '! sh "$0" --nope >/dev/null 2>&1' "$SRC/install.sh"

step "shellcheck install.sh (installed only now, so it cannot affect the bare machine above)"
sudo apt-get update -qq >/dev/null && sudo apt-get install -y -qq shellcheck >/dev/null
expect "shellcheck (sh) clean" shellcheck -s sh "$SRC/install.sh"
expect "shellcheck (dash) clean" shellcheck -s dash "$SRC/install.sh"
expect "bash --posix parses it" bash --posix -n "$SRC/install.sh"

echo
if [ "$fails" = 0 ]; then echo "INSTALL TEST PASSED"; else echo "INSTALL TEST FAILED: $fails check(s)"; exit 1; fi
