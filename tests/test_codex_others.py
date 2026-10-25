"""ui.codex_others: which daemon threads the「Codex 其他對話」block lists, their state roll-up, rows and card."""
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sampan.codex_daemon import CodexThread, DaemonSnapshot
from sampan.ui.cards import agent_style
from sampan.ui.codex_others import (
    GO_DESKTOP,
    OTHERS_MAX,
    DAEMON_DOWN,
    START_FAILED,
    STARTING,
    OtherRow,
    ListNote,
    block_left,
    other_card,
    other_cells,
    other_rows,
    rel_time,
)
from tests.helpers import status_row

NOW = datetime(2026, 10, 25, 9, 0, tzinfo=timezone.utc)
ROOTS = ("/tmp", "/private/tmp", "/var/folders/xy/T")


def th(tid, *, hours=1.0, status="idle", flags=(), name=None, cwd="/w/proj", originator="Codex Desktop",
       parent=None, path=None):
    return CodexThread(tid, name if name is not None else f"t-{tid}", cwd, originator, "vscode", parent, status,
                       tuple(flags), NOW - timedelta(hours=hours), path)


def snap(*threads, loaded=()):
    return DaemonSnapshot(Path("/h"), None, tuple(threads), frozenset(loaded), NOW)


def ids(found):
    return [o.thread.id for o in found[0]]


class OtherRowsTest(unittest.TestCase):
    def test_filters(self):
        rows = [status_row("cx", agent="codex", sid="in-tmux")]
        s = snap(th("in-tmux"), th("claude-bg", originator="Claude Code"), th("tmpwork", cwd="/private/tmp/x"),
                 th("tmpdir", cwd="/var/folders/xy/T/abc"), th("old", hours=25), th("desk", hours=1),
                 th("tmpfoo", hours=1.5, cwd="/tmpfoo/a", originator="codex-tui"))
        self.assertEqual(ids(other_rows(s, rows, NOW, tmp_roots=ROOTS)), ["desk", "tmpfoo"])  # /tmpfoo is not /tmp

    def test_none_snapshot_lists_nothing(self):
        self.assertEqual(other_rows(None, [], NOW, tmp_roots=ROOTS), ([], 0))

    def test_child_status_rolls_up(self):
        s = snap(th("p", status="idle", hours=3), th("c", status="active", flags=["waitingOnApproval"], hours=2,
                                                    parent="p", name="sub-a"))
        (o,), extra = other_rows(s, [], NOW, tmp_roots=ROOTS)
        self.assertEqual((o.thread.id, o.state, o.via, o.children, extra), ("p", "approval", "sub-a", 1, 0))
        self.assertEqual(o.updated, NOW - timedelta(hours=2))
        s = snap(th("p", hours=30), th("c", hours=1, parent="p"))
        (o,), _ = other_rows(s, [], NOW, tmp_roots=ROOTS)
        self.assertEqual((o.thread.id, o.state, o.via), ("p", "idle", None))  # parent itself is as urgent: no via

    def test_children_of_a_tmux_session_are_not_listed(self):
        rows = [status_row("cx", agent="codex", sid="p")]
        s = snap(th("p"), th("c", parent="p", status="active"))
        self.assertEqual(other_rows(s, rows, NOW, tmp_roots=ROOTS), ([], 0))

    def test_orphan_child_listed_alone(self):
        s = snap(th("c", parent="gone"))
        (o,), _ = other_rows(s, [], NOW, tmp_roots=ROOTS)
        self.assertEqual((o.thread.id, o.children, o.via), ("c", 0, None))

    def test_loaded_only_threads_not_listed(self):
        self.assertEqual(other_rows(snap(loaded={"x"}), [], NOW, tmp_roots=ROOTS), ([], 0))

    def test_sorted_by_severity_then_newest(self):
        s = snap(th("idle-new", hours=0.1), th("work", status="active", hours=5), th("idle-old", hours=4),
                 th("err", status="systemError", hours=6), th("ask", status="active", flags=["waitingOnUserInput"]),
                 th("nl", status="notLoaded", hours=0.01))
        self.assertEqual(ids(other_rows(s, [], NOW, tmp_roots=ROOTS)),
                         ["ask", "err", "work", "idle-new", "idle-old", "nl"])

    def test_limit_and_overflow_count(self):
        s = snap(*[th(f"t{i:02}", hours=i / 10) for i in range(12)])
        found, extra = other_rows(s, [], NOW, tmp_roots=ROOTS)
        self.assertEqual((len(found), extra), (OTHERS_MAX, 2))
        self.assertEqual(found[0].thread.id, "t00")


class OtherDisplayTest(unittest.TestCase):
    def test_key_and_cells(self):
        o = OtherRow(th("abc", status="active", flags=["waitingOnApproval"], cwd="/w/proj/"), "approval", None, 0)
        self.assertEqual(o.name, "codex:abc")
        name, source, folder, state, when = other_cells(o, NOW)
        self.assertIn("t-abc", name)
        self.assertEqual((source, folder, state, when), ("Desktop", "proj", ("等你核准", "attn"), "1h"))
        cli = OtherRow(th("x", originator="codex-tui", status="weird"), "unknown", None, 0)
        self.assertEqual(other_cells(cli, NOW)[1:4], ("CLI", "proj", ("weird", "muted")))
        self.assertEqual(other_cells(OtherRow(th("e"), "error", None, 0), NOW)[3], ("錯誤", "err"))
        self.assertEqual(other_cells(OtherRow(th("n"), "notloaded", None, 0), NOW)[3], ("未開啟", "muted"))

    def test_rel_time(self):
        for delta, want in ((timedelta(seconds=10), "<1m"), (timedelta(minutes=5), "5m"), (timedelta(hours=23), "23h"),
                            (timedelta(days=2), "2d"), (timedelta(minutes=-3), "<1m")):
            self.assertEqual(rel_time(NOW - delta, NOW), want)

    def test_card(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rollout.jsonl"
            info = {"last_token_usage": {"input_tokens": 120_000}, "model_context_window": 258_400}
            path.write_text(json.dumps({"payload": {"type": "token_count", "info": info}}) + "\n")
            parent = th("p", path=str(path), cwd="/home/alice/w/proj")
            o = OtherRow(parent, "approval", "sub-a", 2, NOW - timedelta(hours=2))
            card = other_card(o, NOW, home="/home/alice")
        text = ["".join(t for t, _s in line) if isinstance(line, tuple) else line for line in card]
        self.assertEqual(text[0], "t-p")
        joined = "\n".join(text)
        for want in ("~/w/proj", "Desktop", "等你核准（子對話：sub-a）", "120k", "2 小時前", "2"):
            self.assertIn(want, joined)
        self.assertEqual(card[-1], GO_DESKTOP)

    def test_card_without_rollout(self):
        card = other_card(OtherRow(th("p"), "idle", None, 0), NOW)
        self.assertEqual(card[-1], GO_DESKTOP)
        self.assertIn("-", "\n".join("".join(t for t, _s in x) for x in card if isinstance(x, tuple)))


class AgentStyleTest(unittest.TestCase):
    def test_error_is_red(self):
        self.assertEqual(agent_style("error"), "err")


if __name__ == "__main__":
    unittest.main()


class OtherRowKeysTest(unittest.TestCase):
    """With no tmux row the cursor sits on the block, yet global keys keep working; only row actions are refused."""

    def picker(self):
        from sampan.ui import Picker
        return Picker([], others=[OtherRow(th("abc"), "idle", None, 0)])

    def test_global_keys_work_on_the_block(self):
        for key, action in (("n", "new"), ("R", "restore"), ("A", "adopt"), ("w", "save"), ("U", "update"),
                            ("p", "plugins"), ("o", "profiles")):
            with self.subTest(key=key):
                p = self.picker()
                self.assertTrue(p.on_other())
                self.assertEqual(p.key(key), (action, []))

    def test_row_actions_on_the_block_are_refused(self):
        for key in ("\n", "s", "m", "P", "e", "X", "r", "a"):
            with self.subTest(key=key):
                self.assertEqual(self.picker().key(key), ("desktop", []))
        p = self.picker()
        self.assertIsNone(p.key(" "))
        self.assertEqual(p.marked, set())


class TmpRootsTest(unittest.TestCase):
    def test_raw_and_resolved_temp_dirs_are_both_listed(self):
        import os
        from unittest import mock

        from sampan.ui.screen import tmp_roots
        link = Path(tempfile.mkdtemp(dir="/tmp"))
        self.addCleanup(link.rmdir)
        real = Path(os.path.realpath(link))
        with mock.patch.dict(os.environ, {"TMPDIR": str(link) + "/."}):
            roots = tmp_roots()
        self.assertIn(str(link) + "/.", roots)
        self.assertIn(str(real), roots)


class BlockLeftStartTest(unittest.TestCase):
    def test_block_left_starting(self):
        self.assertEqual(block_left([], 0, down=False, starting=True), [ListNote(STARTING, "muted")])

    def test_block_left_failed_shows_reason(self):
        out = block_left([], 0, down=True, failed="找不到 codex 指令")
        self.assertEqual(out, [ListNote("Codex 常駐服務啟動失敗：找不到 codex 指令（codex remote-control start）", "warn")])
        self.assertEqual((out[0], out[0].style), (START_FAILED.format(reason="找不到 codex 指令"), "warn"))

    def test_block_left_starting_beats_down(self):
        self.assertEqual(block_left([], 0, down=True, starting=True, failed="x"), [ListNote(STARTING, "muted")])

    def test_block_left_defaults_unchanged(self):
        self.assertEqual(block_left([], 0, down=True), [ListNote(DAEMON_DOWN, "muted")])
        self.assertEqual(block_left([], 0, down=False), [])
