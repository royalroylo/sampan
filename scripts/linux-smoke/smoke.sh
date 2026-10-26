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

step "sampan init --yes (every default, no hook)"
out=$(sampan init --yes 2>&1 </dev/null); rc=$?; echo "$out" | tail -4
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

step "sampan send to a Claude session (fake claude draws the prompt box and records what it receives)"
recv=~/.fake-claude-received
received() { find "$recv" -type f 2>/dev/null | wc -l; }
wait_for() {  # wait_for <seconds> <command...>
    local t="$1"; shift
    for _ in $(seq $((t * 4))); do "$@" && return 0; sleep 0.25; done
    return 1
}
IFS= read -r -d '' msg <<'EOT'
第一行：你好，世界「引號」 "double" 'single'
second line with $HOME and `echo hi` and a backslash \n and $(whoami)

  indented 日本語のテキスト 한국어
last line
EOT
tmux ls
expect "nothing received yet" test "$(received)" = 0
out=$(printf %s "$msg" | sampan send c1 - 2>&1); rc=$?; echo "$out"
expect "send exits 0" test "$rc" = 0
expect "send says sent" has "已送出" "$out"
expect "the fake received exactly one prompt" wait_for 10 test "$(received)" = 1
got=$(cat "$recv"/*; printf x); got=${got%x}
first=${got%%$'\n'*}; body=${got#*$'\n'}
expect "source line first (from the terminal)" bash -c '[[ "$0" =~ ^〔sampan\ 轉送｜來自\ 終端機｜派工\ [^〕]+〕$ ]]' "$first"
expect "message arrives byte for byte (multi-line, CJK, quotes, \$HOME, backticks)" test "$body" = "$msg"
out=$(sampan dispatches); echo "$out"
expect "dispatches lists it" has "c1" "$out"

step "sampan send --wait: waits for the idle box, prints the reply"
out=$(sampan send c1 "say OK" --wait --timeout 60 2>&1); rc=$?; echo "$out"
expect "wait exits 0" test "$rc" = 0
expect "the reply is printed" has "OK 2" "$out"
expect "second prompt received" test "$(received)" = 2
out=$(sampan dispatches --all); echo "$out"
expect "both dispatches listed, the last one answered" test "$(grep -c '已回覆' <<<"$out")" -ge 1

step "sampan send into a box with a draft: queued, nothing pasted, draft kept"
tmux send-keys -t c1 -l 'half typed'
expect "the draft shows in the box" wait_for 10 bash -c 'tmux capture-pane -p -t c1 | grep -q "half typed"'
out=$(sampan send c1 "must wait" 2>&1); rc=$?; echo "$out"
expect "send exits 0" test "$rc" = 0
expect "dispatch stays queued" has "排隊中" "$out"
did=$(sed -En 's/^派工 ([^：]+)：.*/\1/p' <<<"$out" | head -1)
sleep 2
expect "nothing new received" test "$(received)" = 2
expect "the draft is untouched" bash -c 'tmux capture-pane -p -t c1 | grep -q "❯.half typed$"'
expect "the queued text never reached the box" bash -c '! tmux capture-pane -p -t c1 | grep -q "must wait"'
out=$(sampan dispatches cancel "$did" 2>&1); rc=$?; echo "$out"
expect "cancel exits 0" test "$rc" = 0
tmux send-keys -t c1 C-u
expect "draft cleared" wait_for 10 bash -c 'tmux capture-pane -p -t c1 | grep -q "write a test"'
out=$(sampan send c1 "after the draft" 2>&1); echo "$out"
expect "an empty box takes the next message" wait_for 10 test "$(received)" = 3

step "cleanup (sessions of this container only)"
sampan stop --timeout 30 >/dev/null

echo
if [ "$fails" = 0 ]; then echo "SMOKE PASSED (Python $PY)"; else echo "SMOKE FAILED: $fails check(s)"; exit 1; fi
