"""The session list's context and model columns: same values as `sampan status`, hidden from the right on narrow
screens (model first, then context) without ever squeezing the name, a warning colour exactly where the ⚠ is, and no
line wider than the screen at any width (with and without the info card)."""
import unittest

from sampan.status import State, _fmt_tokens
from sampan.textwidth import text_width
from sampan.ui import _list_cells, frame_text, list_cols, render
from tests.helpers import status_row

CARD = ["alpha", "─" * 80, "狀態     執行中", "context  312k ⚠"]


def row(name, tokens=93_000, model="", agent="claude", window=None, alias="", **kw):
    r = status_row(name, State.RUNNING, agent=agent, profile="alice" if agent == "claude" else "codex",
                   agent_status="idle", tokens=tokens, alias=alias, **kw)
    r.model = model
    r.context_window = window
    return r


def lines(rows, width, height=12, card=()):
    left = [_list_cells(r, set(), None) for r in rows]
    return frame_text(render(title="sampan", left=left, cursor=0, card=list(card), cols=list_cols(rows),
                             width=width, height=height))


def style_of(r):
    return _list_cells(r, set(), None)[5][1]


class Values(unittest.TestCase):
    def test_context_is_the_text_status_prints_and_model_the_registry_value(self):
        r = row("a", tokens=312_000, model="sonnet[1m]")
        cells = _list_cells(r, set(), None)
        self.assertEqual(cells[5][0], _fmt_tokens(312_000, None))
        self.assertEqual(cells[6][0], "sonnet[1m]")
        self.assertEqual(_list_cells(row("b", tokens=None), set(), None)[5][0], "-")
        self.assertEqual(_list_cells(row("b"), set(), None)[6][0], "-")  # unset

    def test_warning_colour_is_where_the_warning_sign_is(self):
        for r in (row("a", tokens=250_000), row("a", tokens=1_234_000),
                  row("x", tokens=90_000, agent="codex", window=100_000)):  # Codex: 80% of its own window
            self.assertIn("⚠", _list_cells(r, set(), None)[5][0])
            self.assertEqual(style_of(r), "warn", r.tokens)
        for r in (row("a", tokens=249_000), row("a", tokens=0), row("a", tokens=None),
                  row("x", tokens=90_000, agent="codex", window=1_000_000)):  # 250k is not Codex's rule
            self.assertNotIn("⚠", _list_cells(r, set(), None)[5][0])
            self.assertNotEqual(style_of(r), "warn", r.tokens)

    def test_model_column_only_when_somebody_sets_one(self):
        self.assertEqual(len(list_cols([row("a")])), 5)
        self.assertEqual(list_cols([row("a")]).optional, 1)
        cols = list_cols([row("a", model="opus"), row("b")])
        self.assertEqual((len(cols), cols[-1], cols.optional), (6, 4, 2))
        self.assertEqual(list_cols([row("a", model="x" * 40)])[-1], 18)

    def test_context_column_is_wide_enough_for_the_warning_sign(self):
        for tokens, text in ((1_234_000, "1234k ⚠"), (0, "0k"), (None, "-"), (93_000, "93k")):
            rows = [row("a", tokens=tokens)]
            self.assertGreaterEqual(list_cols(rows)[4], text_width(text), text)
            self.assertIn(text, lines(rows, 100, 4)[1])
        self.assertEqual(list_cols([row("a", tokens=312_000)])[4], 6)  # nothing widens it needlessly


class Widths(unittest.TestCase):
    ROWS = [row("alpha-session", model="sonnet[1m]", tokens=312_000), row("beta", tokens=40_000)]

    def shown(self, width, rows=None, card=()):
        text = "\n".join(lines(rows or self.ROWS, width, card=card))
        return {"name": "alpha-session" in text, "context": "312k ⚠" in text, "model": "sonnet[1m]" in text}

    def test_all_at_160_and_120(self):
        for w in (160, 120):
            for card in ((), CARD):
                self.assertEqual(self.shown(w, card=card), {"name": True, "context": True, "model": True}, (w, bool(card)))

    def test_model_goes_first_then_context_and_the_name_always_stays(self):
        for card in ((), CARD):  # 100-112: the list beside the card is that narrow (as before 0.28.2), the name is cut
            seen = [self.shown(w, card=card) for w in [*range(160, 112, -1), *range(99, 49, -1)]]
            for s in seen:
                self.assertTrue(s["name"], s)
                self.assertFalse(s["model"] and not s["context"], s)  # never the model without the context
            self.assertTrue(any(s["context"] and not s["model"] for s in seen))
        self.assertTrue(any(not s["context"] and not s["model"] for s in (self.shown(w) for w in range(70, 49, -1))))

    def test_the_card_narrows_only_when_both_columns_then_fit(self):
        def card_width(width):
            first = lines(self.ROWS, width, card=CARD)[1]
            return width - first.index("│") - 3  # the card starts 3 cells after the list's right edge
        for w in range(100, 161):
            narrowed = card_width(w) < max(36, min(64, w // 2))
            if narrowed:
                self.assertTrue(self.shown(w, card=CARD)["model"], w)  # it gave way, so the columns are there
            self.assertGreaterEqual(card_width(w), 36, w)

    def test_no_line_is_wider_than_the_screen(self):
        rows = [row("中文名稱的 session", tokens=1_234_000, model="sonnet[1m]", alias="別名 · 甲"),
                row("alpha", tokens=0, alias="a·b"), row("beta", tokens=None),
                row("cx", agent="codex", tokens=90_000, window=100_000, model="gpt")]
        for card in ((), CARD):
            for w in range(30, 201):
                for line in lines(rows, w, 10, card=card):
                    self.assertLessEqual(text_width(line), w, (w, line))

    def test_name_with_alias_is_whole_where_the_list_has_room(self):
        rows = [row("中文名稱", model="sonnet", alias="別名 · 甲"), row("cx", agent="codex", tokens=1_500_000)]
        for w in [*range(70, 100), *range(113, 201)]:  # 100-112: the list beside the card is that narrow (as before 0.28.2)
            for card in ((), CARD):
                self.assertIn("中文名稱 · 別名 · 甲", "\n".join(lines(rows, w, 6, card=card)), (w, bool(card)))

    def test_example_screens(self):
        for w in (80, 120, 160):
            out = lines(self.ROWS, w, 6, card=CARD if w >= 100 else ())
            self.assertIn("alpha-session", out[1])
            self.assertIn("312k ⚠", out[1])
            self.assertIn("sonnet[1m]", out[1])
            self.assertIn("40k", out[2])


if __name__ == "__main__":
    unittest.main()
