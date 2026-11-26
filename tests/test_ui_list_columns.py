"""The session list's context and model columns: same values as `sampan status`, hidden from the right on narrow
screens (model first, then context) without ever squeezing the name, a warning colour from CONTEXT_WARN."""
import unittest

from sampan.status import State, _fmt_tokens
from sampan.ui import _list_cells, frame_text, list_cols, render
from sampan.ui.render import CONTEXT_WARN
from tests.helpers import status_row


def row(name, tokens=93_000, model="", **kw):
    r = status_row(name, State.RUNNING, agent="claude", profile="alice", agent_status="idle", tokens=tokens, **kw)
    r.model = model
    return r


def lines(rows, width, height=12):
    left = [_list_cells(r, set(), None) for r in rows]
    return frame_text(render(title="sampan", left=left, cursor=0, card=[], cols=list_cols(rows), width=width, height=height))


class Values(unittest.TestCase):
    def test_context_is_the_text_status_prints_and_model_the_registry_value(self):
        r = row("a", tokens=312_000, model="sonnet[1m]")
        cells = _list_cells(r, set(), None)
        self.assertEqual(cells[5][0], _fmt_tokens(312_000, None))
        self.assertEqual(cells[6][0], "sonnet[1m]")
        self.assertEqual(_list_cells(row("b", tokens=None), set(), None)[5][0], "-")
        self.assertEqual(_list_cells(row("b"), set(), None)[6][0], "-")  # unset

    def test_warning_colour_from_the_constant(self):
        self.assertEqual(CONTEXT_WARN, 250_000)
        self.assertEqual(_list_cells(row("a", tokens=CONTEXT_WARN), set(), None)[5][1], "warn")
        self.assertEqual(_list_cells(row("a", tokens=CONTEXT_WARN - 1_000), set(), None)[5][1], "")

    def test_model_column_only_when_somebody_sets_one(self):
        self.assertEqual(len(list_cols([row("a")])), 5)
        self.assertEqual(list_cols([row("a")]).optional, 1)
        cols = list_cols([row("a", model="opus"), row("b")])
        self.assertEqual((len(cols), cols[-1], cols.optional), (6, 4, 2))
        self.assertEqual(list_cols([row("a", model="x" * 40)])[-1], 18)


class Widths(unittest.TestCase):
    ROWS = [row("alpha-session", model="sonnet[1m]", tokens=312_000), row("beta", tokens=40_000)]

    def shown(self, width):
        text = "\n".join(lines(self.ROWS, width))
        return {"name": "alpha-session" in text, "context": "312k" in text, "model": "sonnet[1m]" in text}

    def test_all_at_160_and_120(self):
        for w in (160, 120):
            self.assertEqual(self.shown(w), {"name": True, "context": True, "model": True}, w)

    def test_model_goes_first_then_context_and_the_name_always_stays(self):
        seen = [self.shown(w) for w in range(120, 49, -1)]
        for s in seen:
            self.assertTrue(s["name"], s)
            self.assertFalse(s["model"] and not s["context"], s)  # never the model without the context
        self.assertTrue(any(s["context"] and not s["model"] for s in seen))
        self.assertTrue(any(not s["context"] and not s["model"] for s in seen))

    def test_example_screens(self):
        for w in (80, 120, 160):
            out = lines(self.ROWS, w, 6)
            print(f"\n--- {w} columns ---\n" + "\n".join(x.rstrip() for x in out[:4]))


if __name__ == "__main__":
    unittest.main()
