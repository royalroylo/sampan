"""`send --restore`: a dispatch to a stopped session reattaches it first (send_restore, send._restoring, restore.start_one)."""
import unittest
from unittest import mock

from sampan import dispatch, send, send_restore
from sampan.registry import Row
from sampan.send_restore import decide
from sampan.status import State
from tests.fakes import FakeRunner
from tests.helpers import status_row
from tests.test_send import DRAFT, ID_B, NOW, SID_B, Base, later, tmux_runner

ID_D, ID_E, ID_F = ("dddddddd-0000-4000-8000-00000000000%d" % n for n in (4, 5, 6))
SID_D2, SID_E, SID_F = ("5555555%d-6666-7777-8888-999999999999" % n for n in (1, 2, 3))


def stopped(name, id, sid, *, alive=False):
    return status_row(name, State.STOPPED, sid=sid, agent_status="-", id=id, tmux_alive=alive)


class DecideTest(unittest.TestCase):
    """The pure judging: one stopped target's next step from its shared phase."""

    def row(self, state=State.STOPPED, status="-", alive=True):
        return status_row("b", state, agent_status=status, tmux_alive=alive)

    def step(self, row, phase, age=0, others=0, rc=False):
        return decide(row, phase, age, others_active=others, rc=rc)

    def test_a_stopped_target_is_launched_unless_the_places_are_taken(self):
        self.assertEqual(self.step(self.row(), "").kind, "launch")
        self.assertEqual(self.step(self.row(), "", others=send_restore.RESTORE_AT_ONCE - 1).kind, "launch")
        full = self.step(self.row(), "", others=send_restore.RESTORE_AT_ONCE)
        self.assertEqual((full.kind, full.reason), ("wait", "排隊等接回（同時最多 2 個）"))

    def test_launching_waits_and_a_claim_that_never_finished_fails_without_launching_again(self):
        self.assertEqual(self.step(self.row(), "launching", age=send_restore.LAUNCH_STALE_S).kind, "wait")
        s = self.step(self.row(), "launching", age=send_restore.LAUNCH_STALE_S + 1)
        self.assertEqual(s.kind, "fail")
        self.assertIn("不重複開", s.reason)

    def test_starting_follows_the_target(self):
        up = self.row(State.RUNNING, "idle")
        self.assertEqual(self.step(up, "starting").kind, "ready")
        self.assertEqual(self.step(up, "starting", rc=True).kind, "rc")
        for status in ("busy", "-", "waiting"):
            self.assertEqual(self.step(self.row(State.RUNNING, status), "starting").kind, "wait", status)
        self.assertEqual(self.step(self.row(State.RUNNING, "waiting"), "starting",
                                   age=send_restore.START_LIMIT_S + 1).kind, "fail")
        self.assertEqual(self.step(self.row(State.STOPPED), "starting").kind, "wait")  # claude is still coming up
        self.assertEqual(self.step(self.row(State.STOPPED, alive=False), "starting",
                                   age=send_restore.DIED_GRACE_S).kind, "wait")  # tmux may not show it yet
        self.assertEqual(self.step(self.row(State.STOPPED, alive=False), "starting",
                                   age=send_restore.DIED_GRACE_S + 1).kind, "fail")  # it died
        slow = self.step(self.row(State.STOPPED), "starting", age=send_restore.START_LIMIT_S + 1)
        self.assertEqual(slow.kind, "fail")
        self.assertIn("接回逾時", slow.reason)

    def test_a_trust_prompt_fails_and_says_sampan_does_not_press_it(self):
        s = self.step(self.row(State.AWAIT_TRUST), "starting")
        self.assertEqual(s.kind, "fail")
        self.assertIn("信任提示", s.reason)
        self.assertIn("不替你按", s.reason)

    def test_other_states_after_the_launch_fail(self):
        self.assertEqual(self.step(self.row(State.MULTI), "starting").kind, "fail")

    def test_rc_phase_waits_then_goes_on_even_when_it_never_finished(self):
        up = self.row(State.RUNNING, "idle")
        self.assertEqual(self.step(up, "rc").kind, "wait")
        self.assertEqual(self.step(up, "rc", age=send_restore.RC_STALE_S + 1).kind, "ready")
        self.assertEqual(self.step(self.row(), "rc").kind, "ready")
        self.assertEqual(self.step(up, "ready").kind, "ready")

    def test_waiting_reasons_are_recognised(self):
        for r in (send_restore.REASON_NOT_CLAIMED, send_restore.REASON_RESTORING, send_restore.REASON_STARTING,
                  send_restore.REASON_QUEUE.format(n=2), send_restore.REASON_RC):
            self.assertTrue(send_restore.waiting(r), r)
        self.assertFalse(send_restore.waiting("對方正在忙"))
        self.assertFalse(send_restore.waiting(""))


class TargetProblemTest(unittest.TestCase):
    def test_restore_lets_a_stopped_target_through_and_retired_stays_refused(self):
        stop = status_row("b", State.STOPPED)
        self.assertIn("不是執行中", send.target_problem(stop, "b"))
        self.assertIsNone(send.target_problem(stop, "b", restore=True))
        retired = status_row("b", State.RETIRED)
        self.assertNotIn("activate", send.target_problem(retired, "b"))  # as before without the flag
        self.assertIn("sampan activate b", send.target_problem(retired, "b", restore=True))
        for state in (State.AWAIT_TRUST, State.UNKNOWN, State.MULTI):
            self.assertIn("不是執行中", send.target_problem(status_row("b", state), "b", restore=True), state)
        self.assertIn("找不到", send.target_problem(None, "b", restore=True))


class RestoreBase(Base):
    def setUp(self):
        super().setUp()
        self.set_row("b", state=State.STOPPED, agent_status="-", pane=None, tmux_alive=False)
        self.rows["d"] = stopped("d", ID_D, SID_D2)
        self.rows["e"] = stopped("e", ID_E, SID_E)
        self.rows["f"] = stopped("f", ID_F, SID_F)
        self.start = self.patch("sampan.send.restore_mod.start_one", return_value=None)
        self.rc = self.patch("sampan.send.steps.rc_step")
        self.rc_wanted = self.patch("sampan.send.steps.rc_wanted", return_value=False)
        self.patch("sampan.send.load_rows", return_value=[Row("b", "claude", "alice", SID_B, "/tmp/proj-b")])

    def patch(self, target, **kw):
        p = mock.patch(target, **kw)
        self.addCleanup(p.stop)
        return p.start()

    def up(self, name="b", pane="%2"):
        self.set_row(name, state=State.RUNNING, agent_status="idle", pane=pane, tmux_alive=True)

    def phase(self, did):
        return self.stored(did).extra.get(send_restore.PHASE)

    def restore_submit(self, to="b", **kw):
        d = self.submit(to=to, restore=True, **kw)
        self.assertNotIsInstance(d, str, d)
        return d


class SubmitRestoreTest(RestoreBase):
    def test_a_stopped_target_is_refused_without_restore_and_queued_with_it(self):
        self.assertIn("不是執行中", self.submit())
        d = self.restore_submit()
        self.assertEqual((d.state, d.to_id), ("queued", ID_B))
        self.assertIs(self.stored(d.id).extra[send_restore.RESTORE], True)
        self.up()
        self.assertEqual(self.submit().extra, {})  # a plain send carries no mark

    def test_retired_is_refused_with_a_hint(self):
        self.set_row("b", state=State.RETIRED)
        self.assertIn("sampan activate b", self.submit(restore=True))
        self.assertEqual(dispatch.load(self.cfg.state_dir), [])

    def test_raw_and_restore_do_not_mix(self):
        self.assertEqual(self.submit(restore=True, raw=True, message="/compact"), send.REASON_RESTORE_RAW)


class RestoreFlowTest(RestoreBase):
    def test_the_whole_way_from_stopped_to_delivered(self):
        self.rc_wanted.return_value = True
        d = self.restore_submit(notify=True)
        d = self.advance(FakeRunner(strict=True), d.id)  # claim, launch, record
        self.start.assert_called_once()
        self.assertEqual(self.start.call_args.args[2], "b")
        self.assertEqual((d.state, d.reason, self.phase(d.id)), ("queued", send_restore.REASON_STARTING, "starting"))
        # claude is coming up: the registry row has no agent yet, its tmux session exists
        self.set_row("b", tmux_alive=True)
        d = self.advance(FakeRunner(strict=True), d.id, later(5))
        self.assertEqual((d.state, d.reason), ("queued", send_restore.REASON_STARTING))
        self.start.assert_called_once()
        # up and idle: the Remote Control reset runs once, outside the lock, and then the message goes
        self.up()
        d = self.advance(tmux_runner(), d.id, later(10))  # the reset, then the same look delivers (the paste below)
        self.rc.assert_called_once()
        self.assertEqual((self.rc.call_args.args[2].tmux, self.rc.call_args.args[3]), ("b", "%2"))
        self.assertEqual(self.phase(d.id), "ready")
        self.assertEqual(d.state, "sent")
        self.start.assert_called_once()
        self.rc.assert_called_once()

    def test_without_remote_control_there_is_no_reset(self):
        d = self.restore_submit()
        self.advance(FakeRunner(strict=True), d.id)
        self.up()
        d = self.advance(tmux_runner(), d.id, later(5))
        self.assertEqual(d.state, "sent")  # ready and delivered in the same look
        self.rc.assert_not_called()

    def test_a_look_that_may_not_start_things_only_waits(self):
        d = self.restore_submit()
        d = send.advance(self.cfg, FakeRunner(strict=True), d.id, sleep=self.sleeps.append, status_fn=self.status_fn,
                         now=NOW, restore=False)  # what an MCP tool's result does: it must return at once
        self.start.assert_not_called()
        self.assertEqual((d.state, d.reason), ("queued", send_restore.REASON_NOT_CLAIMED))
        self.assertIsNone(self.phase(d.id))

    def test_a_dispatch_without_the_mark_never_starts_anything(self):
        self.up()
        d = self.submit()  # sent while it ran, so no mark
        self.set_row("b", state=State.STOPPED, agent_status="-", pane=None, tmux_alive=False)  # then it stopped
        d = self.advance(FakeRunner(strict=True), d.id)
        self.assertEqual((d.state, d.reason), ("queued", send.REASON_NOT_RUNNING))
        self.assertIsNone(self.phase(d.id))
        self.start.assert_not_called()

    def test_two_dispatches_to_one_stopped_target_launch_it_once(self):
        a, b = self.restore_submit(message="one"), self.restore_submit(message="two")
        self.advance(FakeRunner(strict=True), a.id)
        self.advance(FakeRunner(strict=True), b.id, later(1))
        self.start.assert_called_once()
        self.assertEqual((self.phase(a.id), self.phase(b.id)), ("starting", "starting"))
        self.up()
        self.advance(tmux_runner(), a.id, later(5))
        self.advance(tmux_runner(), b.id, later(6))
        self.assertEqual({self.stored(a.id).state, self.stored(b.id).state}, {"sent"})
        self.start.assert_called_once()

    def test_a_target_that_went_down_again_before_the_message_left_is_reattached_again(self):
        d = self.restore_submit()
        self.advance(FakeRunner(strict=True), d.id)
        self.up()
        d = self.advance(tmux_runner(DRAFT), d.id, later(5))  # ready, but someone is typing: the message waits
        self.assertEqual((d.state, self.phase(d.id)), ("queued", "ready"))
        self.set_row("b", state=State.STOPPED, agent_status="-", pane=None, tmux_alive=False)  # it died meanwhile
        d = self.advance(FakeRunner(strict=True), d.id, later(6))
        self.assertEqual(self.start.call_count, 2)
        self.assertEqual(self.phase(d.id), "starting")

    def test_a_target_that_is_up_by_itself_is_just_delivered(self):
        d = self.restore_submit()
        self.up()  # somebody reattached it meanwhile
        d = self.advance(tmux_runner(), d.id)
        self.assertEqual(d.state, "sent")
        self.start.assert_not_called()


class RestoreLimitTest(RestoreBase):
    def test_only_a_few_targets_are_reattached_at_once_and_the_rest_wait_without_the_queue_limit(self):
        ds = {n: self.restore_submit(to=n) for n in "bde"}
        for n in "bde":
            self.advance(FakeRunner(strict=True), ds[n].id)
        self.assertEqual(self.start.call_count, send_restore.RESTORE_AT_ONCE)
        waiting = self.stored(ds["e"].id)
        self.assertEqual((waiting.state, waiting.reason), ("queued", "排隊等接回（同時最多 2 個）"))
        # a long wait for a place is not a 30-minute queue failure
        waiting = self.advance(FakeRunner(strict=True), waiting.id, later(send.QUEUE_LIMIT_S * 2))
        self.assertEqual(waiting.state, "queued")
        # one of the two is up and delivered: its place frees up and the waiting one is launched
        self.up("b")
        self.advance(tmux_runner(), ds["b"].id, later(send.QUEUE_LIMIT_S * 2 + 1))
        self.set_row("e", tmux_alive=False)
        self.advance(FakeRunner(strict=True), ds["e"].id, later(send.QUEUE_LIMIT_S * 2 + 2))
        self.assertEqual(self.start.call_count, 3)
        self.assertEqual(self.start.call_args.args[2], "e")

    def test_a_batch_of_stopped_sessions_is_queued_and_reattached_a_few_at_a_time(self):
        self.set_row("b", state=State.RUNNING, agent_status="idle", pane="%2", tmux_alive=True)
        b = send.submit_batch(self.cfg, FakeRunner(strict=True), send.TERMINAL, ["b", "d", "e", "f"], "hi", raw=False,
                              notify=False, now=NOW, status_fn=self.status_fn, restore=True)
        self.assertEqual([d.to_name for d in b.items], ["b", "d", "e", "f"])
        self.assertEqual(b.skipped, [])
        for d in b.items:
            self.advance(tmux_runner(), d.id)
        self.assertEqual(self.start.call_count, send_restore.RESTORE_AT_ONCE)
        states = {d.to_name: self.stored(d.id) for d in b.items}
        self.assertEqual(states["b"].state, "sent")  # the one that was running was not held up
        self.assertEqual(states["f"].reason, "排隊等接回（同時最多 2 個）")


class RestoreFailureTest(RestoreBase):
    def test_a_launch_that_cannot_be_done_fails_every_dispatch_of_that_target_with_the_reason(self):
        self.start.return_value = "找不到逐字稿 aaaaaaaa，不改開新對話"
        a, b = self.restore_submit(notify=True), self.restore_submit()
        d = self.advance(tmux_runner(), a.id)  # the sender is told at once: its pane gets the notice
        self.assertEqual(d.state, "failed")
        self.assertEqual(d.reason, "接回失敗：找不到逐字稿 aaaaaaaa，不改開新對話")
        self.assertEqual(self.stored(b.id).state, "failed")
        self.assertTrue(self.stored(a.id).notified)

    def test_a_launch_that_raises_fails_the_dispatch(self):
        self.start.side_effect = OSError("tmux 壞了")
        d = self.advance(FakeRunner(strict=True), self.restore_submit().id)
        self.assertEqual(d.state, "failed")
        self.assertIn("tmux 壞了", d.reason)

    def test_a_trust_prompt_fails_it_and_nothing_is_pressed(self):
        a, b = self.restore_submit(), self.restore_submit()
        self.advance(FakeRunner(strict=True), a.id)
        self.set_row("b", state=State.AWAIT_TRUST, tmux_alive=True)
        run = FakeRunner(strict=True)
        d = self.advance(run, a.id, later(3))
        self.assertEqual(d.state, "failed")
        self.assertIn("信任提示", d.reason)
        self.assertEqual(self.stored(b.id).state, "failed")
        self.assertEqual(run.calls, [])

    def test_too_slow_fails(self):
        d = self.restore_submit()
        self.advance(FakeRunner(strict=True), d.id)
        self.set_row("b", tmux_alive=True)
        d = self.advance(FakeRunner(strict=True), d.id, later(send_restore.START_LIMIT_S + 1))
        self.assertEqual(d.state, "failed")
        self.assertIn("接回逾時", d.reason)

    def test_an_agent_that_died_after_the_launch_fails(self):
        d = self.restore_submit()
        self.advance(FakeRunner(strict=True), d.id)
        d = self.advance(FakeRunner(strict=True), d.id, later(send_restore.DIED_GRACE_S + 1))  # no tmux session any more
        self.assertEqual(d.state, "failed")
        self.assertIn("又結束了", d.reason)

    def test_a_crash_between_the_claim_and_the_result_is_never_launched_again(self):
        d = self.restore_submit()
        with dispatch.transaction(self.cfg.state_dir, ids=[d.id]) as items:  # what a killed process left behind
            x = next(i for i in items if i.id == d.id)
            x.extra = {**x.extra, send_restore.PHASE: "launching", send_restore.PHASE_AT: NOW.isoformat()}
        d = self.advance(FakeRunner(strict=True), d.id, later(10))
        self.assertEqual((d.state, d.reason), ("queued", send_restore.REASON_RESTORING))
        d = self.advance(FakeRunner(strict=True), d.id, later(send_restore.LAUNCH_STALE_S + 1))
        self.assertEqual(d.state, "failed")
        self.assertIn("不重複開", d.reason)
        self.start.assert_not_called()

    def test_resend_starts_the_reattach_over(self):
        self.start.return_value = "目錄不存在 /gone"
        d = self.advance(FakeRunner(strict=True), self.restore_submit().id)
        self.assertEqual(d.state, "failed")
        self.start.return_value = None
        r = send.resend(self.cfg, d.id, now=later(20))
        self.assertNotIn(send_restore.PHASE, r.extra)
        self.assertIs(r.extra[send_restore.RESTORE], True)
        d = self.advance(FakeRunner(strict=True), d.id, later(21))
        self.assertEqual(self.start.call_count, 2)
        self.assertEqual(self.phase(d.id), "starting")


class CmdSendRestoreTest(RestoreBase):
    def clock(self):
        t = iter(range(0, 10_000, 2))
        return lambda: next(t)

    def test_a_terminal_send_stays_until_the_message_is_out_and_says_what_it_waits_for(self):
        lines = []
        calls = []

        def status(cfg, run, snapshot_fn=None):
            calls.append(1)
            if len(calls) == 4:  # a few looks later claude is up
                self.up()
            return list(self.rows.values())
        rc = send.cmd_send(self.cfg, tmux_runner(), lines.append, "b", "hi", wait=False, sleep=self.sleeps.append,
                           clock=self.clock(), status_fn=status, now_fn=lambda: NOW, restore=True)
        self.assertEqual(rc, 0)
        self.assertIn(f"… {send_restore.REASON_STARTING}", lines)
        self.assertEqual(self.start.call_count, 1)
        self.assertRegex(lines[-1], r"已送出、等回覆")

    def test_a_failed_reattach_is_reported_with_its_reason(self):
        self.start.return_value = "目錄不存在 /gone"
        lines = []
        rc = send.cmd_send(self.cfg, FakeRunner(strict=True), lines.append, "b", "hi", wait=False,
                           sleep=self.sleeps.append, clock=self.clock(), status_fn=self.status_fn, now_fn=lambda: NOW,
                           restore=True)
        self.assertEqual(rc, 0)
        self.assertRegex(lines[-1], "失敗.*接回失敗：目錄不存在 /gone")

    def test_it_gives_up_waiting_after_the_limit_and_leaves_the_dispatch(self):
        lines = []
        self.start.return_value = None
        t = iter(range(0, 100_000, 100))
        send.cmd_send(self.cfg, FakeRunner(strict=True), lines.append, "b", "hi", wait=False, sleep=self.sleeps.append,
                      clock=lambda: next(t), status_fn=self.status_fn, now_fn=lambda: NOW, restore=True)
        self.assertEqual(self.start.call_count, 1)
        self.assertRegex(lines[-1], "排隊中")

    def test_raw_with_restore_is_refused_before_anything_is_recorded(self):
        lines = []
        rc = send.cmd_send(self.cfg, FakeRunner(strict=True), lines.append, "b", "/compact", wait=False, raw=True,
                           restore=True, status_fn=self.status_fn, now_fn=lambda: NOW)
        self.assertEqual((rc, lines), (2, [f"❌ {send.REASON_RESTORE_RAW}"]))

    def test_batch_without_restore_skips_a_stopped_session_as_before(self):
        b = send.submit_batch(self.cfg, FakeRunner(strict=True), send.TERMINAL, ["d", "b"], "hi", raw=False,
                              notify=False, now=NOW, status_fn=self.status_fn)
        self.assertIsInstance(b, str)  # nobody to send to
        self.assertIn("不是執行中", b)

    def test_batch_with_restore_still_refuses_retired_and_raw(self):
        self.set_row("e", state=State.RETIRED)
        b = send.submit_batch(self.cfg, FakeRunner(strict=True), send.TERMINAL, ["d", "e"], "hi", raw=False,
                              notify=False, now=NOW, status_fn=self.status_fn, restore=True)
        self.assertEqual([d.to_name for d in b.items], ["d"])
        self.assertEqual([n for n, _ in b.skipped], ["e"])
        self.assertIn("sampan activate e", b.skipped[0][1])
        self.assertEqual(send.submit_batch(self.cfg, FakeRunner(strict=True), send.TERMINAL, ["d"], "/x", raw=True,
                                           notify=False, now=NOW, status_fn=self.status_fn, restore=True),
                         send.REASON_RESTORE_RAW)


if __name__ == "__main__":
    unittest.main()
