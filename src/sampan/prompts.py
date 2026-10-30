"""Menus an agent stops at on startup (Codex: update, trusted directory, hooks review; Claude: trust the folder), relayed to sampan's own screen for the user to choose.

sampan never answers for the user: parse_prompt reads the menu at the bottom of the screen into a Prompt, Relay hands it to on_prompt (the ui or a terminal
question) to ask the user, and only the key of the chosen option is sent to the pane; "go in and handle it myself" and "leave it for now" are always offered. Used only
on panes this sampan action has just started (the waits of new, restore, move); save/status/previews never use it.

Keys (confirmed on real codex-cli 0.147 and Claude Code 2.1.287): in Codex's menu, sending the number selects and advances (no Enter needed);
Claude's options have no numbers and the ❯ cursor starts on "No, exit", so move to the target with Down/Up and press Enter, sending each key separately with a 0.3 s pause.
An arrow key sent the moment the menu draws can be lost (seen 2026-10-30: Enter then landed on "No, exit"), so after moving
the screen is read again and Enter is pressed only with the cursor on the chosen option; otherwise the move is sent again,
and after MOVE_TRIES no Enter is pressed at all.
"""
from dataclasses import replace

from .screens import Prompt, parse_prompt  # noqa: F401  (re-exported: callers import Prompt/parse_prompt from here)

ATTACH = "attach"  # go in and handle it myself
SKIP = "skip"  # leave it for now, deal with it later
ANSWERED = "answered"
MOVE_TRIES = 3


class Relay:
    """Prompt relay within one action (new/restore/move): asks on_prompt(name, agent, prompt); the answer is an option number (0-based),
    ATTACH or SKIP. About 1 second after sending keys it re-captures the screen: if the next menu is there it asks again; if the same menu is still there it says so and offers only go in / leave it."""

    def __init__(self, on_prompt, tmux, out, sleep, *, settle=1, gap=0.3, auto=None):
        """auto(prompt): for prompts the caller answers itself (move trusting the new directory) return the option number, otherwise None (ask the user)."""
        self.on_prompt, self.tmux, self.out, self.sleep = on_prompt, tmux, out, sleep
        self.settle, self.gap, self.auto = settle, gap, auto
        self.skipped = []  # sessions where "leave it" was chosen: not asked again within this action
        self.attach = None  # session where "go in and handle it myself" was chosen

    def send(self, pane_id, p, choice):
        """Keys to pick option number choice; each group is sent separately with a gap-second pause between. A menu
        moved with arrows (Claude) is read again before Enter: False when the cursor never reached choice (no Enter)."""
        groups = p.keys_for(choice)
        if groups[-1] != ["Enter"]:  # Codex: the number selects by itself
            for k, keys in enumerate(groups):
                if k:
                    self.sleep(self.gap)
                self.tmux.send_keys(pane_id, *keys)
            return True
        now = p
        for _ in range(MOVE_TRIES):
            moves = now.keys_for(choice)[:-1]
            for k, keys in enumerate(moves):
                if k:
                    self.sleep(self.gap)
                self.tmux.send_keys(pane_id, *keys)
            self.sleep(self.gap)
            now = parse_prompt(self.tmux.capture(pane_id), p.agent) if moves else now
            if now is None or not now.same(p):
                return False  # the menu changed under us: press nothing more
            if now.current == choice:
                self.tmux.send_keys(pane_id, "Enter")
                return True
        return False

    def check(self, name, agent, pane_id, screen, prompt=None):
        """None: no prompt (or this session was already set to leave it); ANSWERED: answered and the screen is no longer a menu; SKIP; ATTACH."""
        if name in self.skipped or self.attach is not None:
            return None
        p = prompt or parse_prompt(screen, agent)
        if p is None:
            return None
        unchanged = False  # the same menu survived our keys; say so only if nobody else is going to explain the wait
        while True:
            choice = self.auto(p) if self.auto is not None else None
            if unchanged and choice != SKIP:  # auto answering SKIP = the caller keeps waiting for the redraw and reports it itself
                self.out(f"⚠ {name}：送出後畫面沒變，請進去處理")
            unchanged = False
            if choice is None:
                choice = self.on_prompt(name, agent, p)
            if choice == ATTACH:
                self.attach = name
                return ATTACH
            if isinstance(choice, bool) or not isinstance(choice, int) or not 0 <= choice < len(p.options):
                self.skipped.append(name)
                return SKIP
            if not self.send(pane_id, p, choice):
                self.out(f"⚠ {name}：游標沒有移到「{p.options[choice]}」，沒有按 Enter，請進去處理")
                self.skipped.append(name)
                return SKIP
            self.sleep(self.settle)
            nxt = parse_prompt(self.tmux.capture(pane_id), agent)
            if nxt is None:
                return ANSWERED
            if nxt.same(p):
                unchanged = True
                nxt = replace(nxt, options=(), current=None, numbers=())
            p = nxt


def recording(fn):
    """Wrap on_prompt: record the sessions where "go in and handle it myself" was chosen (the caller attaches to it afterwards). Returns (wrapped function, list of names)."""
    want = []

    def wrapped(name, agent, p):
        choice = fn(name, agent, p)
        if choice == ATTACH:
            want.append(name)
        return choice
    return wrapped, want


def prompt_lines(name, agent, p):
    """Title and extra text of the question (shared by the ui and the terminal)."""
    if not p.options:
        head = f"? {name}（{agent}）停在提示，sampan 認不出選項；畫面最後幾行："
    else:
        head = f"? {name}（{agent}）在問：{p.question}"
    return [head] + [f"  {line}" for line in p.detail]


def prompt_choices(p):
    """[(display text, answer)]: "go in and handle it myself" is always on top (and the default), "leave it" last."""
    return ([("進去自己操作", ATTACH)] + [(f"{k + 1}. {o}", k) for k, o in enumerate(p.options)]
            + [("先不管，之後再處理", SKIP)])


def plain_prompt(out, ask):
    """Question for the terminal (--plain, sampan new/restore): a/Enter go in, a number picks an option, s leaves it; EOF or Ctrl-C counts as leave it."""
    def on_prompt(name, agent, p):
        for line in prompt_lines(name, agent, p):
            out(line)
        out("  a. 進去自己操作（Enter）")
        for k, o in enumerate(p.options):
            out(f"  {k + 1}. {o}")
        out("  s. 先不管，之後再處理")
        while True:
            try:
                raw = ask("選擇（Enter = 進去自己操作）: ")
            except (EOFError, KeyboardInterrupt):
                return SKIP
            if raw is None:
                return SKIP
            raw = raw.strip().lower()
            if raw in ("", "a"):
                return ATTACH
            if raw == "s":
                return SKIP
            if raw.isdigit() and 1 <= int(raw) <= len(p.options):
                return int(raw) - 1
            out(f"無效選擇：{raw}")
    return on_prompt
