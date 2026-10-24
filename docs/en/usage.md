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
| Jump to a row | `1`-`9` (the number is the row's place in the full list) | Moves the cursor there; for 10 and up, type two digits within 1 second, e.g. `1`, `2` is row 12 |
| Search by name | `/`, then text | Case-insensitive; rows whose name contains the text stay. Enter opens the row under the cursor, Esc cancels, Backspace on an empty query leaves search. While searching, digits are text and do not jump |
| Start new work | `n` | Asks for a name and agent / profile, then a directory (the chooser starts at `[ui] start_dir`, default `~/Projects` if it exists, else `~`); once created you are taken straight in |
| Put one away (free memory, clear the screen) | `s` (space checks several) | Asks to confirm at the bottom of the screen; the agent exits normally and tmux closes too. The conversation is kept; resume with Enter or `R` |
| Resume several at once | `R` | Checklist (pre-checked: active in the last 7 days); after resuming, takes you into the first one |
| Bring Claude agents running outside tmux (in any terminal program) into tmux | `A` | Pick which, name each; you type `/exit` in the original terminal, sampan waits, then resumes the same conversation in tmux and takes you in |
| Rename | `e` | tmux and registry together; the agent keeps running |
| Remove an unused session from the registry | `X` (stopped or retired rows only) | Confirms at the bottom of the screen (it will no longer be listed or resumed by `R`); afterwards prints the command to resume it by hand. The conversation files are never deleted; the previous registry stays in `history/` |
| Manage plugins and marketplace sources | `p` | Opens the plugin management screen, see "The plugin management screen" below |
| Continue on another account (e.g. its quota ran out) | `P` (Claude only) | Lists the profiles; after you confirm, claude exits and the same conversation resumes under the new profile in the same directory, trusting that directory automatically the first time (same rule as `m`). A stopped session is reattached under the new profile directly. Plugins the new profile does not list are named first, but do not block the switch. If claude does not exit (e.g. it sits at a quota menu), sampan presses nothing else and asks you to press Esc and type `/exit` yourself, then press `P` again |
| Change working directory | `m` | Directory chooser (starting at its current directory; if that directory is gone, at `[ui] start_dir`, as for `n`); after you confirm, the agent exits and resumes the same conversation in the new directory |
| Mark as done | `r` | Retired; not offered for resuming |
| Save the registry now | `w` | Normally saved on the hour |
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
update only when a newer version exists. `--plain` only reads the cache and prints one line at the start; the hourly save never checks.

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

**Action log**: the message area keeps only the last action's result. The messages printed by every action that really ran are in `actions.log` in the state directory (by default `~/.local/state/sampan/actions.log`, moved by `SAMPAN_STATE_DIR`; readable only by you), each line starting with the time and the action, such as `[plugin 存檔 …]`, `[重開]`, `[來源 …]`, `[forget …]` or `[switch …]`. Saves, `u` / `U`, `r`, adding, removing and updating sources, every step of an `R` restart (including the "still on the old version" list), `X` / `sampan forget` and `P` / `sampan switch` are logged, including what you answered to startup prompts (trust and so on) during a restart or switch; `sampan doctor` shows where the file is and when its last line was written; previews, the "確定？" question and cancelled actions are not. User names and tokens in URLs, GitHub tokens and `…TOKEN=` style assignments are replaced by `***` first. Past 1 MB the old file is renamed `actions.log.1`.

For scripts and schedules do not use this screen; keep using `sampan plugins sync` (see "Commands").

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

**Getting back to the list while in tmux**: detach (default `Ctrl-b d`) and you are back at the sampan list; `q` quits sampan.
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
| New session | `sampan new <name> <claude:profile｜profile｜codex> <dir>` (no `.` or `:` in the name) |
| Open claude / codex with an account | `sampan claude [profile] [claude args…]` (without a profile it lists a menu; a first word that is neither a profile nor starts with `-` is an error, not sent as a prompt), `sampan codex` |
| Record each session's conversation id | `sampan save` (the schedule also runs it on the hour) |
| End agents normally (put away; `restore` brings them back) | `sampan stop [name…] [--profile <p>] [--force] [--keep-tmux]`: afterwards, if that tmux session has only this one pane left, it is closed too; with other windows or panes it is kept |
| Resume after a reboot | `sampan restore [name…] [--all] [--dry-run]` |
| Adopt claude / codex running outside tmux (e.g. a terminal program's tab) | `sampan adopt [--dry-run]` (or `A` in the ui): lists claude / codex outside tmux (directory, last activity, last message), you check some and name each; you type `/exit` (Codex: `/quit`) in the original window, and only after it ends does sampan resume the same conversation in a new tmux session and register it. sampan presses no key in the original window; ones whose conversation id or profile cannot be determined cannot be picked |
| Rename a session | `sampan rename <name> <new name>` (or `e` in the ui): the tmux session and registry change together, the agent keeps running and no key is sent. No `.` or `:` in the name |
| Continue a session under another profile (account) | `sampan switch <name> <profile> [--dry-run]` (or `P` in the ui): a running session goes through the `sampan move` flow in its own directory under the new profile; a stopped one has its registry row changed and is reattached. When the new profile cannot see the transcript (profiles with separate `projects/`), it is **moved** there, not copied, so there is only ever one copy; if the new profile already has a transcript of that conversation nothing is touched and you decide which to keep. Not for Codex (no profiles) |
| Move a session to another directory | `sampan move <name> <dir>` (or `m` in the ui): ends the agent, resumes the same conversation in the new directory, trusts the new directory automatically (only when the path shown in the prompt is exactly the new directory) and updates the registry. A missing directory is created; busy sessions, worktrees and the pane you are in are left alone |
| Never resume this one again | `sampan retire <name>` (undo: `sampan activate <name>`); `sampan restore <name>` on a retired session says "<name> 是 retired；先 sampan activate <name>" |
| Remove a session that is not running from the registry | `sampan forget <name> [--dry-run]` (or `X` in the ui): only for a stopped or retired session (run `sampan stop` first if it is still in tmux); the ui asks first. Afterwards it prints the command to resume it by hand; the conversation files are never deleted and the previous registry stays in `history/` |
| Plugin versions per profile | `sampan plugins status` (manage them interactively with `p` in the ui) |
| New plugin version / remove a plugin | edit `config.toml`, then `sampan plugins sync` (kept for scripts); `sampan plugins sync --prune --dry-run` previews what would be removed |
| Add a Claude account | `sampan profile add <name>` (creates `~/.claude-profiles/<name>` and logs in); `--dir <existing dir>` uses an existing directory (no creation, no login) |

After a plugin changes version, restart that profile's Claude sessions completely for it to take effect (stop, then restore).

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
