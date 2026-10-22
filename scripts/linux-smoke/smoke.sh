#!/bin/bash
# Live smoke of sampan on Linux, run INSIDE the container (see run.sh). Uses the container's own tmux server and
# fake claude/codex (fake-bin/). Needs a fresh container: it writes ~/.config/sampan and ~/.local/state/sampan.
# Env: SMOKE_PY=3.12 picks the Python sampan is installed with.
[ -f /.dockerenv ] || { echo "smoke.sh only runs inside the container: use scripts/linux-smoke/run.sh" >&2; exit 1; }
set -uo pipefail
export PATH="$HOME/.local/bin:/usr/local/fake-bin:$PATH"
PY="${SMOKE_PY:-3.12}"
fails=0
step() { printf '\n=== %s\n' "$*"; }
expect() {  # expect <description> <command...>: the command must succeed
    local what="$1"; shift
    if "$@"; then echo "  ok   $what"; else echo "  FAIL $what"; fails=$((fails + 1)); fi
}
has() { grep -q -- "$1" <<<"$2"; }
agents() { ps -A -o comm= | grep -cE '^(claude|codex)$'; }
strip_ansi() { sed 's/\x1b\[[0-9;?]*[a-zA-Z]//g; s/\x1b[()][A-Z0-9]//g' | tr -d '\r'; }

step "install sampan (Python $PY)"
uv tool install --python "$PY" "$(dirname "$0")/../.." >/dev/null 2>&1 || { echo "FAIL install"; exit 1; }
mkdir -p ~/.claude ~/work/proj
sampan --version

step "sampan init (non-interactive: accept the name, write the config, decline the hook)"
out=$(printf '\n\n\n\n\n' | sampan init 2>&1); rc=$?; echo "$out" | tail -4
expect "init exits 0" test "$rc" = 0
expect "config written" test -s ~/.config/sampan/config.toml
expect "init says there is no schedule on Linux" has "不是 macOS" "$out"
expect "init does not mention launchd or Homebrew" bash -c '! grep -qiE "launchd|homebrew|brew " <<<"$0"' "$out"

step "sampan doctor"
out=$(sampan doctor 2>&1); rc=$?; echo "$out"
expect "doctor exits 0" test "$rc" = 0
expect "doctor has no Homebrew complaint" bash -c '! grep -qi homebrew <<<"$0"' "$out"
expect "doctor explains there is no auto-save" has "沒有自動存檔排程" "$out"

step "sampan new (one claude, one codex)"
sampan new c1 claude:fake ~/work/proj; expect "new claude exits 0" test "$?" = 0
sampan new x1 codex ~/work/proj; expect "new codex exits 0" test "$?" = 0
expect "two fake agents running" test "$(agents)" = 2

step "sampan status (before save)"
out=$(sampan status); echo "$out"
expect "2 rows" test "$(grep -c '執行中' <<<"$out")" = 2

step "sampan save"
out=$(sampan save); echo "$out"
expect "registry has 2 rows" has "共 2 列" "$out"

step "sampan stop"
out=$(sampan stop --timeout 30); echo "$out"
expect "both ended" has "已結束 2" "$out"
expect "no agent left" test "$(agents)" = 0
expect "tmux sessions closed" bash -c '! tmux ls 2>/dev/null | grep -q .'

step "sampan status (stopped)"
out=$(sampan status); echo "$out"
expect "2 rows, both should-be-running-but-not" test "$(grep -c '該跑沒跑' <<<"$out")" = 2

step "sampan restore --all"
out=$(printf 'y\n' | sampan restore --all 2>&1); echo "$out"
expect "2 reattached" has "接回 2" "$out"
expect "agents running again" test "$(agents)" = 2
expect "resumed the same conversations (ids in process args)" test "$(ps -A -o args= | grep -cE 'fake-bin/(claude --resume|codex resume) ')" = 2

step "sampan status (after restore)"
out=$(sampan status); echo "$out"
expect "2 rows running" test "$(grep -c '執行中' <<<"$out")" = 2

step "sampan ui outside tmux (pty via script, q to quit)"
script -qec "stty rows 30 cols 120; timeout --foreground 10 sampan" /dev/null < <(sleep 3; printf q; sleep 3) 2>&1 | strip_ansi > /tmp/ui.out
echo "  captured $(wc -c </tmp/ui.out) bytes"
expect "ui drew the session list" grep -q 'c1' /tmp/ui.out
expect "ui drew the key hints" grep -q '離開' /tmp/ui.out

step "sampan ui inside tmux (send-keys q)"
tmux new-session -d -s zz-sampan-ui -x 120 -y 30 sampan
sleep 3
screen=$(tmux capture-pane -p -t zz-sampan-ui)
echo "$screen" | head -4
expect "ui drew the session list" has "x1" "$screen"
tmux send-keys -t zz-sampan-ui q
sleep 1
expect "q leaves the ui" bash -c '! tmux has-session -t zz-sampan-ui 2>/dev/null'

step "cleanup (sessions of this container only)"
sampan stop --timeout 30 >/dev/null

echo
if [ "$fails" = 0 ]; then echo "SMOKE PASSED (Python $PY)"; else echo "SMOKE FAILED: $fails check(s)"; exit 1; fi
