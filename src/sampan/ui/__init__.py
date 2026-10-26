"""sampan ui: an interactive menu that stays on one screen until you quit.

Package layout (the names below are re-exported here so `sampan.ui.<name>` keeps working):
  text     shared text helpers and key constants: display width (CJK takes two cells), pad/truncate, wrapping, pane capture cleanup
  state    pure state machines: Picker (arrow-key menu), Checklist (restore/adopt), and the display conversion of state labels
  cards    the info card: per-row data (gather_cards), card contents, key hints and style decisions; pure data
  render   pure functions turning state, card and messages into lines of [(text, style), ...]
  tree     pure: the session rows and folders.json's folders as one (depth, item) list (FolderItem rows)
  screen   CursesScreen, the thin curses adapter (keys, redraw, preview capture), plus curses_ui / curses_adopt_checklist
  actions  cmd_ui: the main loop and dispatch, the question flows of the n/s/S/m/e/U actions, the --plain menu
  folder_actions  the question flows of the folder keys (g, f, e / X on a folder row) and saving a fold
  dirpick  DirPicker, the directory chooser state machine (used by n and m)

In a terminal (curses): on the left is the session list (name, agent, profile, state, claude state; the name column is only two cells wider than the
longest name and the other columns follow right after), on the right is the info card of the row under the cursor (state, agent / profile, directory,
conversation id, last activity, context, number of tmux windows / panes, last message, the keys available for this row); when the terminal is narrower than
100 columns the card goes under the list. When the row under the cursor has a tmux session, the card is replaced by a compact header plus a live preview
of that session's agent pane (tmux capture-pane, read-only, no keys sent): captured as soon as the cursor moves there, re-captured every second while
waiting for a key, and the list is re-read every 5 seconds (cursor and checkmarks are kept); question input and checklists are not affected. Under the list one
line per enabled profile (sessions running, 5h／週額度); at the bottom are the message area (the result of the last action, live progress of long actions, the input line for questions such as y/N and names) and the key help.

Keys: up/down move, space checks, Enter attaches (a stopped session is reattached first), s stops (confirmed in place), S restarts (confirmed
in place: idle ones are stopped and reattached in the background, a stopped one on its own row is reattached, never going in), m moves to another
directory, e renames, R reattaches, A adopts claude/codex running outside tmux, r retire, a activate, n new session (into the folder under the
cursor), w save now, U update sampan, p opens the plugin screen, o opens the profile (帳號) screen, X removes a stopped or retired session from
the registry, Tab / Shift+Tab filter the list by state (全部 閒置 忙碌 已停止 異常; the title shows the filter and its count),
* checks every session row shown (again: clears all), q / Esc quit. The key help line leaves the digits, the arrows, w, Enter, R and A
out for room; they still work.

Folders (folders.json, display only; no folder key touches a session or the registry): each level lists subfolders, then sessions, by name;
a folder row is "▾ name (n)" ("▸" when folded, plus "n 忙" when folded with busy ones inside). On a folder row Enter / right / left fold and
unfold (saved at once, the list stays), space checks it, e renames it, X deletes it (its contents move up one level), s / S act on every session
under it (folded ones too). g wraps the checked items (else the cursor row) into a new folder, f moves them into a folder chosen from a list
(CursesScreen.choose_folder). Search lists matching sessions only, each with its folder path dim before the name. A folders.json that cannot be
trusted gives the flat list and one notice, and the folder keys refuse. --plain lists the tree's order with folder paths, has S, and points to
`sampan folder` for organising.

When a newer version exists (update.Checker checks in the background, cached for 6 hours, and re-checks in the idle time while waiting for a key) one more
line appears above the key help saying a new version is available and to press U; U updates after an in-place confirmation, the output is shown live in the
message area; on success curses is closed and sampan is exec'd with the same arguments, on failure it stays on the list.
When no newer version is known, U re-checks right away (skipping the cache) and, if still nothing newer, only says so (reinstalling is `sampan update --force`).

The working directory of n and the new directory of m use the directory chooser (dirpick.DirPicker + render_dirpick): subdirectories on the left, recently
used ones (from the registry) and the sessions already in the directory under the cursor on the right; up/down move, right / Enter enter, left / Backspace
go up, space picks the current directory, + creates a folder, / types a path, Tab switches to recently used, . shows hidden, Esc cancels; picking the home
directory itself asks for one more confirmation. --plain still types the path.

When an agent stops at a menu on startup (Codex update, trusted directory, hooks review; Claude trust the folder): while n, Enter / R reattach, and m move
wait for the agent, the question and options are shown under the message area (prompts.Relay -> CursesScreen.prompt_choice) and sampan does not choose for
you; the cursor defaults to "go in and handle it myself", up/down choose, Enter sends, number keys pick directly, Esc leaves it for now. Only after an option
is chosen is that key sent to the pane; choosing "go in and handle it myself" attaches to that session when the action ends. --plain asks the same options by number.

Rules: from entering until q you stay inside sampan's screen; after an action finishes you return to the list (the registry and tmux are re-read) and the
result stays in the message area; actions that open a tmux session (Enter reattach, R, A, n) finally go straight into that session: inside tmux the ui
ends after switch-client, outside tmux the terminal is first restored, curses is left, and then it execs tmux attach-session. When not on a terminal (or
with --plain) numbers are typed instead; the rules are the same.

Every action goes through the existing subcommands (stop, move, restore, adopt...), so stop's protections (a busy session is not stopped, /exit is not
sent to a worktree) apply unchanged. The screen has three layers: Picker / Checklist are pure state machines; render() turns state, card and messages
into lines of text (pure functions); CursesScreen is a very thin curses adapter providing pick / checklist / choose_dir / ask / out to cmd_ui.
"""
from .actions import _attach, cmd_ui, dir_context
from .cards import Keys, Wrap, _label, card_lines, gather_cards, preview_header, row_keys
from .render import _list_cells, frame_text, list_cols, preview_lines, render, render_dirpick, screen_title
from .screen import NO_SCREEN, SELF_SCREEN, CursesScreen, curses_adopt_checklist, curses_ui
from .state import Checklist, Picker, list_state, row_text, show_state
from .text import clean_capture, pad, text_width

__all__ = [
    "NO_SCREEN", "SELF_SCREEN", "Checklist", "CursesScreen", "Keys", "Picker", "Wrap", "_attach",
    "_label", "_list_cells", "card_lines", "clean_capture", "cmd_ui", "curses_adopt_checklist", "curses_ui",
    "dir_context", "frame_text", "gather_cards", "list_cols", "list_state", "pad", "preview_header",
    "preview_lines", "render", "render_dirpick", "row_keys", "row_text", "screen_title", "show_state", "text_width",
]
