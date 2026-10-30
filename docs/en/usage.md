# Usage

English · [繁體中文](../usage.md) · [Back to README](../../README.en.md)

## Three ideas

- **One tmux session = one agent.** Each session runs one Claude Code (with some profile) or Codex, for one job, in one
  project directory.
- **sampan keeps a registry** of every session's name, agent, profile, directory and the **conversation it is in**. So after
  the agent ends, tmux closes or the machine reboots, the same conversation can be resumed as if nothing happened.
- **Each session is in one of three states:**

  | State | Meaning | How it gets there |
  |---|---|---|
  | running | the agent is running in tmux | `n`, resume, adopt |
  | stopped | the agent ended; the conversation is kept and can be resumed | `s`, shutdown, reboot |
  | retired | finished; never offered for resuming again | `r` (`a` restores it) |

## Using it (usually just type `sampan`)

`sampan` opens a screen: the session list on the left, an info card for the row under the cursor on the right (state,
directory, conversation, last activity, context use, last message, the keys available for this row), a message area
below. For a session running in tmux the right side shows its live screen (refreshed every second, read-only; press Enter
to go in and type). All questions, progress and results appear on this screen, and after each action you are back at the
list. Press `q` to quit.

| To do this | Press | What happens |
|---|---|---|
| Work in a session | Enter on its row | Goes into its tmux (switches if you are already in tmux). A stopped one is resumed first |
| Jump to a row | `1`-`9` (the number counts the session rows shown; folder rows and sessions hidden in a folded folder have no number) | Moves the cursor there; for 10 and up, type two digits within 1 second, e.g. `1`, `2` is row 12 |
| Filter by state | `Tab` forward, `Shift+Tab` back, through 「全部、閒置、忙碌、已停止、異常」 (all, idle, busy, stopped, odd) | Lists only that kind: idle = running and idle (exactly what `S` restarts); busy = running but not idle (answering, waiting for an approval or a menu, or unknown); stopped; odd = profile / agent mismatch, unregistered, no agent, several agents, unknown, waiting for a trust confirmation. Retired ones only show under 全部. The title shows the filter and the count (`sampan … › 閒置（12）`). Folders list only their matching sessions and count only those; a folder with none is not shown; 「Codex 其他對話」 shows only under 全部. `/` searches within the filtered list, and the digits count only the rows shown. The filter is not remembered: sampan opens on 全部. Checkmarks stay only on sessions the filter shows, and `s` / `S` on a folder row act only on its matching sessions |
| Select all | `*` | Checks every session row shown (not the ones in folded folders, not folder rows); when they are all checked already, it clears every checkmark. Typical use: restart only the running ones → `Tab` to 閒置, `*`, `S`, `y`; resume every stopped one → `Tab` to 已停止, `*`, `S`, `y` (check the session rows; `S` on a folder row still leaves stopped ones stopped). The filter stays after an action returns to the list. When checked sessions leave the filter because their state changed, the checkmarks go, and the next `s` / `S` / `g` / `f` only says to check again instead of acting on the row under the cursor |
| Search by name | `/`, then text | Case-insensitive; rows whose name contains the text stay. Enter opens the row under the cursor, Esc cancels, Backspace on an empty query leaves search. While searching, digits are text and do not jump. Search ignores folding, and a hit inside a folder shows the folder path dim before its name (`客戶/甲 · name`); when search ends and the session under the cursor sits in a folded folder, the folder opens so it stays visible |
| Start new work | `n` | Asks for a name and agent / profile, then a directory (the chooser starts at `[ui] start_dir`, default `~/Projects` if it exists, else `~`); once created you are taken straight in |
| Put one away (free memory, clear the screen) | `s` (space checks several) | Asks to confirm at the bottom of the screen; the agent exits normally and tmux closes too. The conversation is kept; resume with Enter or `R` |
| Restart (stop, then resume the same conversation, without going in) | `S` (space checks several; on a folder row, everything under it) | The bottom of the screen lists what will restart and what is skipped (`將重開：A、B　略過：C（忙）　確定？(y/N)`); y starts it. Idle ones exit normally and are resumed in the background; busy ones, the pane sampan itself runs in and retired ones are skipped and listed with their reason at the end. A stopped one is resumed in the background only when `S` is pressed on its own row (or it is checked); stopped ones reached only through a folder row stay stopped. A confirm line too long for the screen shortens its lists to the first few and 「等 n 個」, so `確定？(y/N)` always shows. Logged in `actions.log` (`[重開]`) |
| Organise into folders | `g` wraps into a new folder, `f` moves into a folder; on a folder row Enter folds / unfolds, `e` renames, `X` deletes | Only `folders.json` changes, no session is touched; see "Folders" below |
| Resume several at once | `R` | Checklist (pre-checked: active in the last 7 days); after resuming, takes you into the first one |
| Bring Claude agents running outside tmux (in any terminal program) into tmux | `A` | Pick which, name each; you type `/exit` in the original terminal, sampan waits, then resumes the same conversation in tmux and takes you in |
| Rename | `e` | tmux and registry together; the agent keeps running |
| Remove an unused session from the registry | `X` (stopped or retired rows only) | Confirms at the bottom of the screen (it will no longer be listed or resumed by `R`); afterwards prints the command to resume it by hand. The conversation files are never deleted; the previous registry stays in `history/` |
| See each account's quota, rename, disable, add, install the status line | `o` | Opens the accounts screen, see "Account management" below. The top of the list has one line per account (`name N running・5h …・week …(time)`) |
| See dispatches, cancel one not yet sent | `D` | Lists dispatches (id, from, to, state, created, reason; newest first); on a queued one press `x`, confirm at the bottom, and it is cancelled; on a sent one waiting for its reply, `x` (after confirming) stops waiting for it (the message cannot be taken back); a finished one only explains why it cannot be. With no dispatches it just says 「沒有派工」. While some are queued or waiting for a reply, a line `派工：N 排隊、M 等回覆（D 看清單）` shows under the list; while the screen is open sampan advances them in the background every 3 seconds (delivering, reading the reply, retrying the completion or failure notice); leaving the screen in the middle of a delivery waits for it to end (about 35 seconds at most, saying 「等派工送完再離開…」) |
| Manage plugins and marketplace sources | `p` | Opens the plugin management screen, see "The plugin management screen" below |
| Continue on another account (e.g. its quota ran out) | `P` (Claude only) | Lists the profiles; after you confirm, claude exits and the same conversation resumes under the new profile in the same directory, trusting that directory automatically the first time (same rule as `m`). A stopped session is reattached under the new profile directly. Plugins the new profile does not list are named first, but do not block the switch. If claude does not exit (e.g. it sits at a quota menu), sampan presses nothing else and asks you to press Esc and type `/exit` yourself, then press `P` again |
| Change working directory | `m` | Directory chooser (starting at its current directory; if that directory is gone, at `[ui] start_dir`, as for `n`); after you confirm, the agent exits and resumes the same conversation in the new directory |
| Mark as done | `r` | Retired; not offered for resuming |
| Save the registry now | `w` | Normally saved every 5 minutes (left out of the key help line at the bottom for lack of room, but it works; so do the digits, the arrows, `Enter`, `R` and `A`) |
| Update sampan | `U` (a line "new version v… (current …), press U to update" appears above the key help when a newer one exists) | Confirms at the bottom of the screen, runs `uv tool upgrade sampan` (if the version does not change, `uv tool install --reinstall git+https://github.com/royalroylo/sampan@v<latest>`), shows the output in the message area; on success sampan reopens itself at the list, on failure it stays in the list. When no newer version is known, `U` first checks again right then (skipping the cache); if it is still up to date (or the check fails) it only says so and asks nothing; to reinstall use `sampan update --force` |

**Directory chooser** (`n`, `m`): folders under the current directory on the left, recently used directories on the right
(directories with more sessions in the registry first), and the sessions already in the directory under the cursor are
listed. With a terminal narrower than 100 columns only the left side is shown.

| Key | Action |
|---|---|
| Up / Down | Move |
| Right or Enter | Go into the folder under the cursor (Enter on `..` = up one level) |
| Left or Backspace | Up one level |
| Space | Pick the current directory (the one shown in the title line) |
| `+` | Create a folder in the current directory and go into it |
| `/` | Type a path directly (`~` works; for `n` the directory must exist, for `m` it is created if missing) |
| Tab | Switch to the "recently used" list on the right; Enter / Space there picks that directory directly |
| `.` | Show / hide folders starting with `.` |
| Esc | Cancel |

Picking the home directory `~` itself asks once more (a project directory is recommended). `sampan ui --plain` is a
numbered, typed menu for scripts and narrow terminals, where you type the path directly.

**Update hint**: when it opens, sampan checks the repository's newest version tag in the background (cached 6 hours,
checked again when it has expired while the screen is open; offline just means no hint) and shows a hint with `U` to
update only when a newer version exists. `--plain` only reads the cache and prints one line at the start; the 5-minute save never checks.

## The plugin management screen

Press `p` in the list (`--plain` has it too, by typing numbers). It manages each profile's plugins and marketplace sources, and writes what you change back to `config.toml`, so the config and what is installed do not drift apart. **Opening the screen never touches the network**; `claude` commands (which reach GitHub and other sources) run only when you press `r`, `u`, `U`, or confirm a save.

**Two layers**: layer 1 is the plugin list (name, state: 最新 up to date / 有新版 newer available / 未裝 not installed / 不一致 mismatched), with an info card on the right (below on a narrow terminal) showing the source and each profile's version and state. When a profile cannot be changed, the card says why (for example "目錄不存在", "來源不同：…"). Enter opens layer 2: one checkbox per profile; space toggles, `s` saves, `q` or `Esc` goes back (with unsaved changes it asks "放棄變更？(y/N)").

| Key | Action |
|---|---|
| ↑↓, `1`-`9`, `/` | Move, jump to that row, search by name |
| Enter | Open layer 2 to choose which profiles get it |
| `r` | Check for newer versions: looks up the newest version at each source (uses the network); plugins with one are then marked 有新版 |
| `u` | Update the plugin under the cursor, without asking: refreshes its marketplace first, then updates it in every profile where it is installed; it installs and removes nothing |
| `U` | Update every plugin in every profile; because it touches every profile it first asks "全部 plugin、全部 profile 都更新？(y/N)" |
| `a` | Add a plugin: pick from the downloaded marketplace catalogs, or press `i` to type `plugin@marketplace` yourself |
| Tab | Switch between the "plugin" and "來源" (sources) tabs (`t` in `--plain`) |
| `q` | Back to the list |

**Saving**: `s` in layer 2 first lists everything it will do under "將會：", one line per change, each naming the profile (for example `+ 安裝到 work（…）`, `− 從 work 移除`), then asks "確定？(y/N)"; nothing runs until you press `y`. Install, uninstall and update use `--scope user`, so only user scope is touched.

- **Config backup**: only when the config file is really rewritten, it is first backed up as `config.toml.bak-<stamp>` (at most one per save); if every profile fails there is no backup. The rest of the file (comments, layout) stays byte for byte the same. The file is re-read from disk at save time, so edits you made by hand while the screen was open survive.
- **Partial failure**: if one profile's command fails the others still finish; the config changes only for the profiles whose commands succeeded, and the failed ones are listed ("… 沒有改（設定檔也沒動那幾個 profile）").
- **A restart is needed to take effect**: after a successful install or removal (also when removing a source on the 來源 tab uninstalled plugins) the screen says which profiles have how many sessions to restart ("要生效需重開 …：按 R 立刻重開，其他鍵稍後"). Only pressing `R` restarts them (stop, then restore); otherwise nothing is touched and the screen reminds you "稍後要生效：sampan stop --profile <p> 後 sampan restore". The pane sampan itself runs in and busy sessions are never restarted; they are listed with their reason under "這些還在跑舊版，處理後再重開：name（reason）…" for you to do later with `sampan stop --profile <p>` and `sampan restore`. When only the config list changed and nothing was really installed or removed, no restart is needed. A session in a worktree is sent `/exit` like any other; if Claude then asks whether to keep the worktree (it asks only when that run created the worktree), sampan does not choose for you and the reason reads "在問要不要保留 worktree": go in, pick "Keep worktree", then `sampan restore`.

**The 來源 (sources) tab** (Tab) lists marketplace sources, each with a state:

| State | Meaning |
|---|---|
| 一致 | The config and every profile have the same source |
| 來源不同 | The same marketplace name points at a different source in some profile; that profile's box cannot be ticked and the card shows the other source |
| 清單外 | A profile has it but `[marketplaces]` in the config does not; it can only be removed |
| 沒用到 | The config has it but no plugin uses it |

Press `a` on this tab to add a source (it asks for `owner/repo` or a git URL); in layer 2 tick the profiles to add it to, and saving also writes it to `[marketplaces]`. `u` runs `marketplace update` for the source under the cursor in every profile that has it; `U` does so for all sources (after asking). Removing a source from a profile is refused while that profile's config list still has plugins from it (the card says "清單還有 N 個 plugin 用它（先在 plugin 頁移掉）"): remove those plugins on the plugin tab first, otherwise one save would silently uninstall them.

**Why a URL must not carry a token**: sampan writes the source into the config file and passes it as is to `claude` (it shows up in command lines and logs). A URL with a user name or token is refused ("網址裡不要放帳號或 token，認證交給 git 的 credential helper"). For a private repository, set up git's credential helper and give a clean URL.

**Action log**: the message area keeps only the last action's result. The messages printed by every action that really ran are in `actions.log` in the state directory (by default `~/.local/state/sampan/actions.log`, moved by `SAMPAN_STATE_DIR`; readable only by you), each line starting with the time and the action, such as `[plugin 存檔 …]`, `[重開]`, `[接回]`, `[接手]`, `[new …]`, `[stop]`, `[move …]`, `[來源 …]`, `[forget …]`, `[switch …]` or `[資料夾]`. Saves, `u` / `U`, `r`, adding, removing and updating sources, every step of an `R` restart (including the "still on the old version" list), `X` / `sampan forget`, `P` / `sampan switch`, `R` / `sampan restore` (first which ones were picked), `A` / `sampan adopt`, `n` / `sampan new`, `s` / `sampan stop` and `m` / `sampan move` are logged, including what you answered to startup prompts (trust and so on) during them (an action run inside another, such as the stop and resume inside a restart, is logged under the outer name); `--dry-run` is not; `sampan doctor` shows where the file is and when its last line was written; previews, the "確定？" question and cancelled actions are not. User names and tokens in URLs, GitHub tokens and `…TOKEN=` style assignments are replaced by `***` first. Past 1 MB the old file is renamed `actions.log.1`.

For scripts and schedules do not use this screen; keep using `sampan plugins sync` (see "Commands").

## Account management

A profile is one account (a Claude config directory, or a Codex `CODEX_HOME`). The top of the list has one line for each enabled account: `name N 個執行中・5h 42%・週 18%（17:20）`, where the time in brackets is when that quota was read; refreshed at most every 60 seconds. Where the quota comes from: for Claude you must install the status line first (below); for Codex it reads the newest rollout file. sampan never reads account credentials and never goes online for quota.

**Quota warnings**: from 80% on, the account line turns into `⚠ 週 85%（10/27 09:10 重置）・5h 40%` (the tightest item first, with its reset time in brackets); used up it reads `⛔ 5h 已用完（08:28 重置）・週 73%`.

**The Codex daemon**: when the Codex daemon (the app-server, started with `codex remote-control start`) is running, sampan asks it, read-only, for Codex's quota, session status and thread list (at most 3 seconds per query, every 30 seconds; the quota is also saved for later). Without it, quota falls back to the newest rollout file, `sampan doctor` says 「Codex 常駐服務沒在執行」, and the main screen shows the one line 「Codex 常駐服務沒在執行（codex remote-control start）」 where the block below would be. When you open the `sampan` screen and the daemon is not running, sampan runs `codex remote-control start` once in the background (turn it off with `[ui] codex_auto_start = false`); the screen shows 「正在啟動 Codex 常駐服務…」 or the failure reason (「找不到 codex 指令」 only shows when the list has a Codex session). It tries once per opening, only in the `sampan` screen (`status`, `doctor` and `restore` never start it). With the daemon:
- A Codex session in tmux gets its live status in the status column (waiting for approval and waiting for input both show `waiting`; also `working`, `idle`, `error`).
- The main screen gets a block 「Codex 其他對話（24 小時內）」, with states 等你核准, 等你輸入, 錯誤, 執行中, 閒置, 未開啟: threads the daemon knows that are not tmux sessions (mostly opened in Codex Desktop), up to 10 rows, view only; Enter just says 「到 Codex Desktop 操作」.

A Codex session's context use (⚠ above 80% of the model's limit) is read from its rollout file and needs no daemon.

Press `o` for the accounts screen:

| Key | Action |
|---|---|
| Up / Down | Move |
| `r` | Rename (config and registry only, the directory stays; refused while the account has running sessions) |
| `d` | Disable / enable. Disabling asks y/N |
| `f` | Forget: remove a disabled account from the config, together with its sessions in the registry |
| `i` | Install the status line for the Claude account under the cursor (does nothing if already installed; to remove it use `sampan statusline uninstall`) |
| `a` | Add an account |
| `q` | Back to the list |

**Commands** (for `--plain` and scripts):

| To do this | Command |
|---|---|
| List all profiles | `sampan profile list` (type, directory, account, enabled / disabled, running sessions) |
| Account management | `sampan profile list｜add｜rename｜disable｜enable｜forget` (`add <name> [--dir D] [--codex]`), see "Account management"; `o` in the ui |
| Let sampan see Claude's quota | `sampan statusline install｜uninstall [--profile P] [--yes]`, see "Account management" |
| Add a Codex account | `sampan profile add <name> --codex` (creates `~/.codex-profiles/<name>` and logs in with `codex login`); `--dir` as above |
| Rename | `sampan profile rename <old> <new>` |
| Disable / enable | `sampan profile disable <name>`, `sampan profile enable <name>` |
| Remove from the config | `sampan profile forget <name>` (disabled ones only; also removes its sessions from the registry) |

**Disabling**: a disabled account is hidden from the list and menus, cannot start sessions, and `sampan plugins sync` skips it; `sampan restore` does not resume rows that belong to it and says why (they come back after `sampan profile enable`). The default profile cannot be disabled. A profile with running sessions cannot be renamed, disabled or forgotten; run `sampan stop` first.

**sampan never deletes a directory.** Forget only removes the account from the config and the registry; the directory and its conversations stay, and the command to resume by hand is printed. To delete an account's directory for real, do it yourself, in this order:

1. `sampan profile disable <name>`, then `sampan profile forget <name>`.
2. Check the directory for links pointing elsewhere (`projects/` and `commands/` are often symlinks to data shared with other profiles): `find <dir> -maxdepth 1 -type l -ls`.
3. Remove only what is inside the directory; for a symlink remove the link itself and never follow it into what it points to. Remove the directory last, once you are sure.

**In the config file**: see [the config format](reference.md). `[profiles.<name>]` gains two keys, `agent = "codex"` and `disabled = true`. An old `[codex]` table is treated in memory as an implicit Codex profile; only the first `sampan profile rename` / `disable` / `enable` / `forget` / `add --codex` (or the same action on the `o` accounts screen) converts it into `[profiles.<name>]` (the message prints its directory); the whole `[codex]` table is removed and a custom `[codex] home` becomes the new profile's `config_dir`. Adding a Claude profile (`sampan profile add` without `--codex`) and `sampan init` do not convert it. Once any `agent = "codex"` profile is in the file, `[codex]` is ignored entirely (`add --codex` also drops a leftover `[codex]`). `[codex] enabled = false` means Codex is off (no Codex profile); forgetting the last Codex profile writes that line.

**Codex multi-account is not supported yet**: you cannot choose a Codex account for a new session or switch between Codex accounts; sampan uses the first enabled Codex profile. With more than one Codex profile, every running Codex session is attributed to the first enabled Codex profile (in the registry and in the account lines' running counts). Claude and Codex conversations also use different formats and cannot be switched into each other (`P` only moves between Claude accounts).

### The status line (how sampan learns Claude's quota)

Claude Code supplies the 5h / weekly quota only in the input of a status-line command, so for sampan to see it, it has to sit on the status line:

- `sampan statusline install [--profile P] [--yes]`: edits the `statusLine` in `settings.json` of every enabled Claude profile (or only the ones named). If you already have a status-line command it is wrapped as `<sampan> statusline -- sh -c '<original>'` and its output is shown as before; if not, sampan prints a line itself (model, directory, context, quota). Before changing anything it shows the change and asks (`--yes` skips the question), backs the file up as `settings.json.bak-<stamp>` and touches only the `statusLine` key; a `settings.json` that is not a valid JSON object is refused. `sampan init` offers it too.
- `sampan statusline uninstall [--profile P] [--yes]`: restores the original command (removes the key if there was none).
- The original command runs under `sh` (on Linux usually dash, not bash), so bash-only syntax (`[[ ]]`, arrays, `<(…)`, …) has to become `bash -c '…'`.
- **A `statusLine` in a project's `.claude/settings.json` / `settings.local.json` overrides the user-level one**, so sessions in that project do not pass through sampan and record no quota. To record there too, wrap the project's command the same way, or remove it.
- Quota is stored in `usage/<profile>.json` in the state directory (readable only by you). `sampan doctor` reports whether each Claude profile's status line is hooked up and its latest reading.

## Common situations

**Starting the day**: type `sampan`, press Enter on the one you want to work in. Press `n` for something new.

**Putting something away half-way**: press `s` on the list. To continue later, move to that row and press Enter; it resumes
the same conversation and takes you in.

**Shutting down or rebooting**: before shutting down you can run `sampan stop` (every idle agent exits normally; busy ones
are left alone and reported). After the reboot type `sampan`, press `R`, confirm the checked ones, and the original
conversations come back one by one. sampan **never resumes on its own**; you always press the key.

**You have several Claude agents running in terminals outside tmux and want them in one place**: type `sampan`, press `A`.

**An agent asks a question as soon as it starts** (Claude's "trust this folder?", Codex's update, trusted directory, hooks
review): with `n` (new) or Enter on a stopped row (resume one), sampan sends the start command and takes you straight into
tmux, where you answer yourself. With `R` (resume several) and `m` (move directory), sampan shows the question and options
in the message area, with the cursor on "go in and do it yourself" by default. Choose an option (Up / Down and Enter, or
its number); sampan sends only the one you chose and moves on to the next question; choosing "go in and do it yourself"
takes you into that session; Esc ("leave it") ends with a list of the sessions left at a prompt. sampan never picks for you.
`sampan new` and `sampan restore` run directly in a terminal ask the same questions by number (non-interactive scripts and
schedules never look at or press anything).

**Getting back to the list while in tmux**: press `Ctrl-q` (`[ui] detach_key`, only in sessions sampan manages) or detach (default `Ctrl-b d`) and you are back at the sampan list; `q` quits sampan.
You can also keep a dedicated sampan session or pane in tmux: what you pick there is switched to, sampan stays where it is,
and when you switch back you still see the list.

**You don't want the full-screen UI** (scripts, a very narrow terminal): `sampan ui --plain` takes numbers, and `/text` searches (one match goes straight in, several are listed with their numbers); every action
also has its own command, see "Commands" below.

## Commands

Every action also has a command, for scripts or when you prefer typing commands (most that touch sessions accept `--dry-run`):

| Situation | Command |
|---|---|
| Look around | `sampan status`, `sampan doctor` (its first line shows the sampan version and the newest one) |
| Update sampan | `sampan update [--yes] [--force]`: checks the newest version right then (no cache) and asks before updating; `--force` reinstalls even when up to date; `--yes` skips the question. Needs no config file and just exits when done |
| Interactive screen | `sampan` (same as `sampan ui`): see "Using it" above. In tmux, what you pick is switched to and sampan stays in its pane; outside tmux it attaches in the foreground and returns to the list after detach. `sampan ui --plain` is the numbered menu, same rules |
| New session | `sampan new <name> <claude:profile｜profile｜codex> <dir>` (no `.`, `:`, newline or control character in the name, and not 「終端機」 or starting with 「未登記的 session」; `rename` and `adopt` follow the same rule) |
| Open claude / codex with an account | `sampan claude [profile] [claude args…]` (without a profile it lists a menu; a first word that is neither a profile nor starts with `-` is an error, not sent as a prompt), `sampan codex` |
| Record each session's conversation id | `sampan save` (the schedule also runs it every 5 minutes) |
| End agents normally (put away; `restore` brings them back) | `sampan stop [name…] [--profile <p>] [--force] [--keep-tmux]`: afterwards, if that tmux session has only this one pane left, it is closed too; with other windows or panes it is kept |
| Restart named sessions | `sampan restart <name…>`: idle ones are stopped and then reattached to the same conversation in the background (you are not taken into them); stopped ones are just reattached. Busy ones, the pane sampan itself runs in and retired ones are skipped and listed with their reason at the end. Exits 1 when nothing could be restarted |
| Resume after a reboot | `sampan restore [name…] [--all] [--dry-run]` |
| Adopt claude / codex running outside tmux (e.g. a terminal program's tab) | `sampan adopt [--dry-run]` (or `A` in the ui): lists claude / codex outside tmux (directory, last activity, last message), you check some and name each; you type `/exit` (Codex: `/quit`) in the original window, and only after it ends does sampan resume the same conversation in a new tmux session and register it. sampan presses no key in the original window; ones whose conversation id or profile cannot be determined cannot be picked (Codex: send the first prompt in a new one, or one message after `codex resume`, so sampan can tell which conversation it is) |
| Rename a session | `sampan rename <name> <new name>` (or `e` in the ui): the tmux session and registry change together, the agent keeps running and no key is sent. No `.` or `:` in the name |
| Continue a session under another profile (account) | `sampan switch <name> <profile> [--dry-run]` (or `P` in the ui): a running session goes through the `sampan move` flow in its own directory under the new profile; a stopped one has its registry row changed and is reattached. When the new profile cannot see the transcript (profiles with separate `projects/`), it is **moved** there, not copied, so there is only ever one copy; if the new profile already has a transcript of that conversation nothing is touched and you decide which to keep. Not for Codex (no profiles) |
| Move a session to another directory | `sampan move <name> <dir>` (or `m` in the ui): ends the agent, resumes the same conversation in the new directory, trusts the new directory automatically (only when the path shown in the prompt is exactly the new directory) and updates the registry. A missing directory is created; busy sessions, worktrees and the pane you are in are left alone |
| Never resume this one again | `sampan retire <name>` (undo: `sampan activate <name>`); `sampan restore <name>` on a retired session says "<name> 是 retired；先 sampan activate <name>" |
| Remove a session that is not running from the registry | `sampan forget <name>… [--dry-run]` (or `X` in the ui; several names are handled one by one, and one failing does not stop the rest): only for a stopped or retired session (run `sampan stop` first if it is still in tmux); the ui asks first. Afterwards it prints the command to resume it by hand; the conversation files are never deleted and the previous registry stays in `history/` |
| Organise sessions in folders | `sampan folder list｜add <path>｜rename <path> <new name>｜rm <path>｜move <name or path>... <destination｜/>`, see "Folders" |
| Send a message to a session | `sampan send <name> <message｜-> [--wait] [--timeout seconds]`, see "Sending a message to a session" |
| List / cancel dispatches | `sampan dispatches [--all]`, `sampan dispatches show <id> [--wait seconds]`, `sampan dispatches cancel <id>` |
| Let an agent send messages through tools | `sampan mcp serve` (an MCP server the agent starts), see "Letting an agent use sampan" |
| Plugin versions per profile | `sampan plugins status` (manage them interactively with `p` in the ui) |
| New plugin version / remove a plugin | edit `config.toml`, then `sampan plugins sync` (kept for scripts); `sampan plugins sync --prune --dry-run` previews what would be removed |
| Account management | `sampan profile list｜add｜rename｜disable｜enable｜forget` (`add <name> [--dir D] [--codex]`), see "Account management"; `o` in the ui |
| Let sampan see Claude's quota | `sampan statusline install｜uninstall [--profile P] [--yes]`, see "Account management" |

After a plugin changes version, restart that profile's Claude sessions completely for it to take effect (stop, then restore).

## Folders

Sessions can be put in folders (levels separated by `/`, such as `客戶/甲`). Folders only organise the display: creating, renaming, deleting or moving a folder never stops, forgets or changes any session, and never touches the registry; the data lives in `folders.json` in the state folder (format in the reference).

**On the `sampan` screen**: each level lists its subfolders first, then its sessions, both by name; sessions in no folder come last, and the 「Codex 其他對話」 block stays at the bottom. A folder row looks like `▾ 客戶 (12)` (the number counts the sessions at every level below it); `▸` when folded, plus `　2 忙` when some inside are busy; sessions are indented by level. With the cursor on a folder row, the info card shows its path, the session count, how many are in each state and the keys; there is no preview.

| Key | On a session row | On a folder row |
|---|---|---|
| Enter | As before: go in (a stopped one is resumed first) | Fold / unfold (remembered in `folders.json`) |
| Right / Left | Left jumps to its folder row | Unfold / fold; Left on a folded one jumps to the parent folder row |
| Space | Check | Check the folder |
| `g` | Wrap the checked sessions and folders (or the row under the cursor) into a new folder: asks for a name, created at their deepest common parent | Same |
| `f` | Move the checked ones (or the row under the cursor) into a folder picked from a list that starts with 「（最外層）」 (top level) and 「新資料夾…」 (new folder) | Same (the whole folder moves; the list leaves out the folder itself and its subfolders) |
| `e` | As before: rename the session | Rename the folder |
| `X` | As before: remove from the registry | Delete the folder: asks y once; its subfolders and sessions move up one level and are not stopped |
| `s`, `S` | As before: stop, restart | Stop, restart every session under it (folded ones too; the confirm line names them all) |
| `n` | The new session goes into the folder of the row under the cursor | The new session goes into this folder |

- A name cannot be empty or contain `/`; when the level already has one of that name, sampan asks 「同一層已經有「X」，合併進去？(y/N)」 (merge into it?).
- Checks are cleared after `g`, `f`, `e` and `X`; each is logged in `actions.log` (`[資料夾]`).
- When `folders.json` is broken (unreadable, not JSON, an unknown version) the list still shows every session, flat, and the bottom of the screen says once 「資料夾設定讀不到：…，先照原樣列出」; the folder keys then only answer 「資料夾設定壞了，修好或刪掉 folders.json 再試」 and the broken file is never overwritten.
- `sampan ui --plain` lists the sessions in the tree's order with the folder path before each name, without folding; `S` asks y there too. Organise folders with `sampan folder …` below.

**Commands**:

- `sampan folder list`: prints the tree, a folder as `▾ name/` (`▸` when folded) with its sessions indented below; a session in no folder is at the top level.
- `sampan folder add 客戶/甲`: creates the folder and any missing parents.
- `sampan folder rename 客戶/甲 乙 [--merge]`: changes the last segment only; a same-named folder at that level is merged into only with `--merge`.
- `sampan folder rm 客戶`: deletes the folder; its sessions and subfolders move up one level (no question, and no session is touched).
- `sampan folder move s1 客戶/甲 開發 [--merge]`: the last argument is the destination (`/` is the top level), the others are session names or folder paths; a folder cannot be moved into itself.
- `sampan new <name> <target> <dir> --folder 開發`: only once the session started, puts it in the folder (created if missing); a Codex session that is up but not registered yet (exit 1) counts as started.
- `sampan rename`, `sampan forget` and `sampan profile forget` update the names in the folders too; when `folders.json` is broken they still finish and only add a warning line. With a broken `folders.json` every folder command is refused (exit 2) and the file is never overwritten; fix or delete it and retry.

## Sending a message to a session

`sampan send <name> <message>` hands one message to a running session (`-` reads it from stdin; several lines, quotes and `$` arrive as written). sampan creates a "dispatch", and the message starts with a source line: `〔sampan 轉送｜來自 終端機｜派工 7f3a｜直接回覆文字即可，sampan 會帶回〕` (it tells the receiving agent to answer in plain text; sampan brings the answer back). When an agent runs `sampan send` through its shell tool, the source is that session (or 「未登記的 session」), never 「終端機」; sending to itself is refused all the same.

- Only a session in the sampan registry that is running gets it; an unknown name, a session that is not running, or sending to yourself is refused (exit code 2), never guessed.
- **Claude**: only when the target is idle (activity idle in `sampan status`) **and its prompt box is empty** does sampan paste the whole message through tmux and press Enter. A busy target, one waiting for your answer, a menu or prompt on screen, half-typed text in the box (「對方輸入框有未送出的字」), or a pane in tmux copy mode (「對方畫面在捲動模式」) → nothing is sent and the dispatch stays queued; someone else's draft is never cleared. Claude's own grey suggestion in the box (for example `/compact` just run, suggested again in grey) counts as empty. After Enter sampan looks at the box a few more times (about 3 seconds, holding the dispatch list's lock meanwhile): only while the prompt line still starts with the text sampan pasted and the pane is not in copy mode does it press Enter once more (it never pastes again); if it still did not go the dispatch fails (「已貼進輸入框但沒按到 Enter…」) for you to check that session. A screen it cannot read, or one that shows something else by now, gets no further key.
- **Queue limit**: a dispatch still queued 30 minutes after it was made, whose last reason is a screen sampan cannot use (a menu or prompt, unsent text in the box, copy mode, not telling whether it is idle) or a `codex queue` error, fails (「排隊 30 分鐘還送不出去（最後一次：…）」), and the session that sent it is told like any failure. Waiting for the target (busy, waiting for its user, not running, no thread yet) has no limit, as before.
- **Codex**: `codex queue` hands it to the conversation, and Codex runs it after the current turn. A `codex queue` that times out (30 seconds) fails as 「可能已送出，請確認」 and is never sent again.
- At most once: the record says sent before any key or `codex queue`, so sampan killed in the middle never sends it again (that dispatch later fails as 「找不到回覆」).
- The reply belongs to **this message**: sampan finds the message in the target's transcript and takes the last answer after it, once the target is idle again. If the target `/clear`s, switches conversation or is stopped meanwhile, the dispatch fails with the reason (for example 「找不到回覆」); text from elsewhere is never passed off as the reply. Another message typed after this one with no answer in between (for example Esc, then typing on) → 「對方沒回這則就換了下一則」; a turn that ended without reply text → 「對方這輪結束了，但沒有回覆文字」.
- Without `--wait`: one attempt, then it prints the dispatch id and exits (exit 0, queued too); a `--wait` command, `sampan dispatches show`, the MCP tools or an open `sampan` screen move it on later (with nobody watching it stays where it is until someone does).
- `sampan dispatches show <id> [--wait seconds]`: advances it once (with `--wait`, waits up to that long) and prints the state, the reason and the **whole reply**. `--wait` works only with `show`; anywhere else it is an error. Use it to read the reply of a send without `--wait`.
- `--wait`: checks every 2 seconds and prints the reply to stdout when the target is done (exit 0); a failure prints the reason (exit 1); past `--timeout` (600 seconds by default) it prints 「還在做，派工 <id>」 (exit 124) and the dispatch stays; Ctrl-C leaves it too (exit 130).
- `sampan dispatches` lists queued dispatches and those waiting for a reply (id, from, to, state, created, reason); `--all` adds the answered, failed and cancelled ones. `sampan dispatches cancel <id>` cancels one not sent yet; for a sent one waiting for its reply it stops waiting (the message is already with the target and cannot be taken back).
- Send one together with a new session: `sampan new <name> <target> <dir> --message <message｜->` (`-` reads stdin). The message is the agent's startup prompt (`claude "$(cat <file>)"`, `codex "$(cat <file>)"`), so startup prompts such as folder trust are still asked first and the message goes in once they are answered; a dispatch already in the "sent" state is recorded, and its reply is read like any other; while the new session is not recognised yet (still starting, at a trust prompt, a Codex without a conversation yet) it is not failed, and the reason says 「對方 session 還在啟動」. The command gets no completion notice; read the reply with `sampan dispatches show`. Without `--message` nothing changes.
- Sending, delivery and cancelling are logged in `actions.log` (`[send]`, `[cancel]`), each line ending with the sampan that wrote it (`〔sampan 0.13.3，pid 12345〕`, the version that process loaded when it started; the screen, each agent's MCP server and commands all advance dispatches, so one still running an old version after an upgrade shows). The scheduled `sampan save` never sends or advances a dispatch.

## Letting an agent use sampan

`sampan mcp serve` is a stdio MCP server: through its tools `list_sessions`, `send`, `new_session`, `dispatch_result`, `cancel_dispatch` and `folder_move` an agent (Claude Code, Codex) sends messages to other sessions, opens new ones, reads replies and puts sessions in folders, with the same rules as `sampan send`. `sampan mcp install` registers it in every enabled Claude profile and in Codex (`sampan mcp uninstall` removes it; both take `--dry-run`, and `--profile <name>`, repeatable, to handle only those profiles); restart a profile's sessions to see the tools.

- By default `[mcp] confirm = true`: the tools that act (`send`, `new_session`, `cancel_dispatch`, `folder_move`) are approved through the agent's own permission prompt, every time. sampan marks these tools as requiring user interaction, so Claude Code shows its own permission prompt on every call (in the terminal every time, also in auto and bypass modes, with no "don't ask again"; in Remote Control it is expected to show the full permission prompt with no one-tap approve, which is not yet verified on a phone); Codex uses `approval_mode = "prompt"`, which `sampan mcp install` writes into Codex's `config.toml` for you (backed up first). The prompt shows the tool and its whole arguments; to decline say No, and to change the message ask the agent to change it and send again. The tools that only look never ask.
- Fail closed: with `confirm = true` the three tools run only for clients known to ask a person first (`claude-code`, `codex-mcp-client`); any other client is refused (「這個 client 不會先問使用者；要用請在 config 設 [mcp] confirm = false」), and so is a `sampan mcp serve` not started directly by `claude` / `codex` (for example JSON-RPC piped in from a shell; this checks only the process name, which can be faked, see below). With approval policy `never`, `codex exec` refuses these three tools (verified) rather than running them. They are refused as well when Codex's sampan tools are not set to ask every time (「Codex 的 sampan 工具沒設成每次詢問（<path>）：跑 sampan mcp install」).
- With `confirm = true`, an agent running `sampan send`, `sampan new … --message` or `sampan dispatches cancel` from Bash is refused (exit 2, 「agent 請改用 sampan MCP 的工具（會先問使用者）；要讓 agent 直接用指令，在 config 設 [mcp] confirm = false」); a person at a terminal is unaffected.
- After changing `[mcp] confirm`: Claude reads it when its MCP server restarts (a new session, or reconnect with `/mcp`); Codex needs `sampan mcp install` again.
- What this cannot do: it does not stop an agent that types into tmux panes itself (`tmux send-keys`). Claude Code's sandbox can, but sampan does not set it up. The parent check looks only at the process name: it stops an agent that simply pipes JSON-RPC into `sampan mcp serve`, but a name can be faked (`exec -a claude`, a link named claude), so, like `tmux send-keys` or editing `[mcp] confirm`, it does not stop an agent that deliberately works around sampan.
- `send` without waiting returns the dispatch id at once; when the target is done, sampan sends one line to the caller's session (`〔sampan：<target> 已回覆派工 <id>，用 sampan 工具 dispatch_result 看內容〕`, with Enter), and one when it fails (`〔sampan：給 <target> 的派工 <id> 失敗：<reason>〕`); the approval of the dispatch covers it. `wait=true` waits at most 60 seconds and returns the reply if it came.
- A caller that is not a session sampan manages (for example one outside tmux) can still use the tools, it only gets no completion notice; it checks with `dispatch_result`.
- Arguments and results of the tools: see [reference](reference.md#the-tools-of-sampan-mcp-serve).

## What restore does

`sampan restore` is the most conservative step in the tool:

- It never runs on its own: no schedule or hook calls it; only you, by hand.
- It lists what can be resumed and asks; pre-checked are active sessions seen within `recent_days`. Retired ones and ones
  untouched for too long are not checked. `--all` checks everything resumable and still asks first.
- It resumes one at a time with the original conversation (`claude --resume <id>`, `codex resume <id>`).
- It sends no message and presses nothing for you at a prompt. At a startup prompt it sends only the one option you picked
  in the `sampan` screen or an interactive terminal (see "Common situations"). The one automatic answer is `sampan move`
  pressing "trust" when the prompt shows exactly the destination path. In an existing shell pane it first sends a single
  `C-u` to clear a half-typed line, then types the resume command.
- At the confirmation, Enter (or `y`) resumes the pre-checked ones; type numbers to choose differently; `q` cancels.
- If the original conversation cannot be found the session is skipped; it never opens a new conversation instead.
- It works even if the tmux server is not up yet: restore creates the session itself; if the registry's name already has a shell pane, that pane is reused.

## Codex notes

- Codex may show an update prompt ("Update now") or "Hooks need review" at startup. sampan never answers these for you; it
  shows them on the sampan screen to choose from (see "Common situations"; non-interactive runs just stop there). To reduce
  the interruptions: choose "Skip until next version" for the update prompt (not "Update now", which runs an npm install
  directly), handle the hooks review once (trust or not); for turning off the startup update check see Codex's own
  documentation (`~/.codex/config.toml`) — this document does not name a setting for it.
- Codex truncates long paths in its trust prompt to the pane width; `sampan move` does not guess and will not auto-trust a
  path it cannot read in full; it asks you or tells you to check the window instead. Widen the window and try again.
- A Codex session has a thread id only after you send the first prompt. `sampan new … codex` warns if it times out without
  recognising the conversation; `sampan save` (or the schedule) registers it later. A Codex session with no thread id yet cannot be resumed.
- Codex conversations are found through `lsof` and Codex's local files. Without `lsof` the conversation is reported as unknown
  (on Linux sampan falls back to Codex's own database, which recognises fewer cases).
