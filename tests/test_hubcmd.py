import dataclasses
import io
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sampan import cli, hubcmd, machine, remote
from sampan.connect import write_key
from sampan.hub.core import HubCore, Principal
from sampan.send import TERMINAL, Caller
from tests.helpers import make_env, register
from tests.hub_helpers import Clock, LocalTransport, hello_payload


def lines(fn, *a, **kw):
    out = []
    rc = fn(*a, out.append, **kw) if "out" not in kw else fn(*a, **kw)
    return rc, out


class Case(unittest.TestCase):
    def setUp(self):
        self.cfg = make_env(Path(tempfile.mkdtemp()))
        register(self.cfg, "proj-a", "proj-b")

    def out(self):
        self.lines = []
        return self.lines.append


class KeyTest(Case):
    def test_keygen_makes_a_private_file_and_prints_the_line_once(self):
        o = self.out()
        self.assertEqual(hubcmd.cmd_keygen(self.cfg, o), 0)
        path = remote.key_path(self.cfg)
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        self.assertEqual(oct(os.stat(path.parent).st_mode & 0o777), "0o700")
        line = path.read_text().strip()
        self.assertIn(line, self.lines)
        self.assertTrue(line.startswith("k1:"))
        o = self.out()
        self.assertEqual(hubcmd.cmd_keygen(self.cfg, o), 2)  # never overwrites
        self.assertEqual(path.read_text().strip(), line)
        self.assertNotIn(line, self.lines)


class PublishTest(Case):
    def test_publish_unpublish_and_the_list(self):
        o = self.out()
        self.assertEqual(hubcmd.cmd_publish(self.cfg, o, "proj-a", "main"), 0)
        self.assertIn(f"{machine.load_or_create(self.cfg.path).name}/main", "".join(self.lines))
        store = remote.open_store(self.cfg)
        self.assertEqual(store.published(), [{"session": "proj-a", "alias": "main"}])
        store.close()
        self.assertEqual(hubcmd.cmd_unpublish(self.cfg, self.out(), "proj-a"), 0)
        self.assertEqual(hubcmd.cmd_unpublish(self.cfg, self.out(), "proj-a"), 1)

    def test_publish_refuses_an_alias_that_is_another_sessions_name_or_alias(self):
        self.assertEqual(hubcmd.cmd_publish(self.cfg, self.out(), "proj-a", "proj-b"), 2)  # proj-b is a session of this machine
        self.assertIn("另一個 session 的名稱", "".join(self.lines))
        self.assertEqual(hubcmd.cmd_publish(self.cfg, self.out(), "proj-a", "main"), 0)
        self.assertEqual(hubcmd.cmd_publish(self.cfg, self.out(), "proj-b", "main"), 2)  # the alias is taken by proj-a
        self.assertIn("已發布", "".join(self.lines))
        self.assertEqual(hubcmd.cmd_publish(self.cfg, self.out(), "proj-a", "proj-a"), 0)  # an alias equal to its own name is fine

    def test_publish_refuses_unknown_sessions_and_bad_aliases(self):
        self.assertEqual(hubcmd.cmd_publish(self.cfg, self.out(), "ghost"), 2)
        self.assertEqual(hubcmd.cmd_publish(self.cfg, self.out(), "proj-a", "a/b"), 2)
        self.assertFalse((Path(self.cfg.state_dir) / "remote.db").exists())  # nothing was created for a refused publish

    def test_peers_lists_what_the_hub_told_us(self):
        self.assertIn("還沒有", "".join(lines(hubcmd.cmd_peers, self.cfg)[1]))
        store = remote.open_store(self.cfg)
        store.peers_set([{"machine_id": "m1", "name": "desktop", "sessions": [{"session": "proj-a", "alias": "main", "status": "idle"}]},
                         {"machine_id": "m2", "name": "nas", "sessions": []}])
        store.close()
        text = "\n".join(lines(hubcmd.cmd_peers, self.cfg)[1])
        self.assertIn("desktop/proj-a（別名 main）　idle", text)
        self.assertIn("nas", text)
        self.assertIn("沒有發布", text)


class StatusTest(Case):
    def test_status_before_anything_creates_nothing(self):
        rc, out = lines(hubcmd.cmd_status, self.cfg)
        self.assertEqual(rc, 1)
        self.assertIn("還沒連過 hub", "".join(out))
        self.assertFalse((Path(self.cfg.state_dir) / "remote.db").exists())

    def test_status_names_the_state_the_outbox_and_loose_files(self):
        store = remote.open_store(self.cfg)
        store.put("machine_state", "pending")
        store.put("verification_code", "AB1-23C")
        store.publish("proj-a")
        store.close()
        path = remote.key_path(self.cfg)
        path.parent.mkdir(parents=True)
        write_key(path)
        os.chmod(path, 0o644)
        rc, out = lines(hubcmd.cmd_status, self.cfg)
        text = "\n".join(out)
        self.assertEqual(rc, 1)
        self.assertIn("pending（驗證碼 AB1-23C）", text)
        self.assertIn("已發布：proj-a", text)
        self.assertIn("644", text)
        self.assertIn("沒有 credentials", text)


class ConnectorTest(Case):
    def test_each_missing_piece_is_named(self):
        store = remote.open_store(self.cfg)
        conn, why = hubcmd.build_connector(self.cfg, None, store)
        self.assertIsNone(conn)
        self.assertIn("credentials", why)
        remote.hub_dir(self.cfg).mkdir(parents=True)
        conn, why = hubcmd.build_connector(self.cfg, None, store, transport=object())
        self.assertIn("owner key", why)
        write_key(remote.key_path(self.cfg))
        os.chmod(remote.key_path(self.cfg), 0o666)
        conn, why = hubcmd.build_connector(self.cfg, None, store, transport=object())
        self.assertIn("666", why)
        os.chmod(remote.key_path(self.cfg), 0o600)
        conn, why = hubcmd.build_connector(self.cfg, None, store, transport=object())
        self.assertIsNone(why)
        self.assertEqual(conn.machine_name, machine.load_or_create(self.cfg.path).name)

    def test_the_credentials_file_has_to_be_private_too(self):
        remote.hub_dir(self.cfg).mkdir(parents=True)
        path = remote.credentials_path(self.cfg)
        path.write_text('hub_url = "https://hub.example.com"\ntoken_url = "https://idp.example.com/t"\nclient_id = "c"\nclient_secret = "s"\n')
        os.chmod(path, 0o644)
        conn, why = hubcmd.build_connector(self.cfg, None, remote.open_store(self.cfg))
        self.assertIsNone(conn)
        self.assertIn("644", why)
        self.assertNotIn('client_secret', why)


class LoopTest(Case):
    """run_loop with the in-process hub: one machine, one pass at a time."""

    def setUp(self):
        super().setUp()
        self.clock = Clock()
        self.hub = HubCore(Path(tempfile.mkdtemp()) / "hub.db", clock=self.clock)
        remote.hub_dir(self.cfg).mkdir(parents=True)
        write_key(remote.key_path(self.cfg))
        self.store = remote.open_store(self.cfg, clock=self.clock)
        self.transport = LocalTransport(self.hub, Principal("client-x", "sub-x"))
        self.conn, why = hubcmd.build_connector(self.cfg, None, self.store, clock=self.clock, transport=self.transport)
        self.assertIsNone(why)

    def test_a_pass_says_hello_and_waits_for_approval(self):
        out = hubcmd.run_loop(self.cfg, None, self.conn, self.store, stop=threading.Event(), once=True, clock=self.clock)
        self.assertEqual(out["state"], "pending")
        self.assertTrue(self.hub.machines()[0]["code"])
        self.hub.approve(machine.load_or_create(self.cfg.path).name)
        out = hubcmd.run_loop(self.cfg, None, self.conn, self.store, stop=threading.Event(), once=True, clock=self.clock)
        self.assertEqual(out["state"], "active")

    def test_a_hub_that_is_down_is_a_reported_pass_not_a_crash(self):
        self.transport.fail("hello", 1)
        out = hubcmd.run_loop(self.cfg, None, self.conn, self.store, stop=threading.Event(), once=True, clock=self.clock)
        self.assertIn("hub 連不上", out["error"])

    def test_a_stopped_loop_returns_at_once(self):
        stop = threading.Event()
        stop.set()
        self.assertIsNone(hubcmd.run_loop(self.cfg, None, self.conn, self.store, stop=stop, clock=self.clock))

    def test_a_pass_that_raises_is_logged_and_survives(self):
        self.hub.approve(machine.load_or_create(self.cfg.path).name) if False else None
        with mock.patch.object(remote, "pump_inbound", side_effect=RuntimeError("boom")):
            out = hubcmd.run_loop(self.cfg, None, self.conn, self.store, stop=threading.Event(), once=True, clock=self.clock)
        self.assertIn("RuntimeError", out["error"])

    def test_one_connect_per_state_folder(self):
        first = hubcmd._lock(self.cfg)
        self.assertIsNotNone(first)
        self.assertIsNone(hubcmd._lock(self.cfg))
        first.close()
        again = hubcmd._lock(self.cfg)
        self.assertIsNotNone(again)
        again.close()


class HostTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.path = Path(tempfile.mkdtemp()) / "hub.db"
        self.hub = HubCore(self.path, clock=self.clock)
        self.hub.hello(Principal("client-laptop", "s"), hello_payload("laptop"))
        self.args = SimpleNamespace(db=str(self.path), name="laptop", code=None, limit=10)

    def run_cmd(self, fn, **kw):
        out = []
        for k, v in kw.items():
            setattr(self.args, k, v)
        rc = fn(self.args, out.append)
        return rc, "\n".join(out)

    def test_machines_shows_the_pending_one_with_its_code(self):
        rc, text = self.run_cmd(hubcmd.cmd_machines)
        self.assertEqual(rc, 0)
        self.assertIn("laptop　pending", text)
        self.assertIn(self.hub.machines()[0]["code"], text)

    def test_approve_without_a_code_does_nothing(self):
        rc, text = self.run_cmd(hubcmd.cmd_approve, code=None)
        self.assertEqual(rc, 2)
        self.assertIn("--code", text)
        self.assertEqual(self.hub.machines()[0]["state"], "pending")
        rc, text = self.run_cmd(hubcmd.cmd_approve, code="")
        self.assertEqual((rc, self.hub.machines()[0]["state"]), (2, "pending"))

    def test_approve_with_the_wrong_code_does_nothing_then_the_right_one_works(self):
        rc, text = self.run_cmd(hubcmd.cmd_approve, code="000-000")
        self.assertEqual(rc, 2)
        self.assertEqual(self.hub.machines()[0]["state"], "pending")
        rc, _ = self.run_cmd(hubcmd.cmd_approve, code=self.hub.machines()[0]["code"])
        self.assertEqual((rc, self.hub.machines()[0]["state"]), (0, "active"))
        rc, text = self.run_cmd(hubcmd.cmd_approve, name="ghost", code=None)
        self.assertEqual(rc, 2)

    def test_revoke_and_audit(self):
        self.run_cmd(hubcmd.cmd_approve, code=self.hub.machines()[0]["code"])
        rc, text = self.run_cmd(hubcmd.cmd_revoke)
        self.assertEqual(rc, 0)
        self.assertIn("Keycloak", text)
        self.assertEqual(self.hub.machines()[0]["state"], "revoked")
        rc, text = self.run_cmd(hubcmd.cmd_audit)
        self.assertIn("machine-revoked", text)
        self.assertIn("machine-approved", text)

    def test_status_and_a_missing_database(self):
        rc, text = self.run_cmd(hubcmd.cmd_hub_status)
        self.assertEqual(rc, 0)
        self.assertIn("pending", text)
        rc, text = self.run_cmd(hubcmd.cmd_machines, db=str(self.path) + ".none")
        self.assertEqual(rc, 2)
        self.assertIn("找不到", text)

    def test_serve_needs_the_list_of_allowed_clients(self):
        rc, text = self.run_cmd(hubcmd.cmd_serve, listen="127.0.0.1:0", dev=False, issuer="https://idp.example.com", audience="a",
                                jwks_url="https://idp.example.com/certs", client=None)
        self.assertEqual(rc, 2)
        self.assertIn("--client", text)

    def test_serve_refuses_a_public_address_and_a_missing_issuer(self):
        args = SimpleNamespace(db=str(self.path), listen="0.0.0.0:8787", dev=True, issuer=None, audience=None, jwks_url=None)
        rc, text = self.run_cmd(hubcmd.cmd_serve, listen="0.0.0.0:8787", dev=True, issuer=None, audience=None, jwks_url=None)
        self.assertEqual(rc, 2)
        self.assertIn("只聽本機", text)
        rc, text = self.run_cmd(hubcmd.cmd_serve, listen="127.0.0.1:0", dev=False, issuer=None, audience=None, jwks_url=None)
        self.assertEqual(rc, 2)
        self.assertIn("--issuer", text)
        self.assertEqual(args.listen, "0.0.0.0:8787")


class CliGateTest(unittest.TestCase):
    def setUp(self):
        self.cfg = dataclasses.replace(make_env(Path(tempfile.mkdtemp())), mcp_confirm=True)
        register(self.cfg, "proj-a")
        p = mock.patch("sampan.cli.load_config", return_value=self.cfg)
        p.start()
        self.addCleanup(p.stop)

    def run_cli(self, argv, caller):
        buf = io.StringIO()
        with redirect_stdout(buf), mock.patch("sampan.send.command_caller", return_value=caller):
            rc = cli.main(argv)
        return rc, buf.getvalue()

    def test_an_agent_cannot_change_anything_through_the_cli(self):
        agent = Caller("a", "alice", "claude", "%1")
        for argv in (["connect", "keygen"], ["connect"], ["hub", "publish", "proj-a"], ["hub", "unpublish", "proj-a"],
                     ["hub", "approve", "laptop"], ["hub", "revoke", "laptop"], ["hub", "serve", "--dev"]):
            rc, _ = self.run_cli(argv, agent)
            self.assertEqual(rc, 2, argv)
        self.assertFalse(remote.key_path(self.cfg).exists())
        self.assertFalse((Path(self.cfg.state_dir) / "remote.db").exists())

    def test_an_agent_can_read(self):
        agent = Caller("a", "alice", "claude", "%1")
        rc, text = self.run_cli(["hub", "peers"], agent)
        self.assertEqual(rc, 0)
        rc, text = self.run_cli(["connect", "status"], agent)
        self.assertEqual(rc, 1)  # nothing set up yet: it says what is missing
        self.assertIn("沒有 owner key", text)

    def test_a_person_at_the_terminal_can_keygen_and_publish(self):
        rc, text = self.run_cli(["connect", "keygen"], TERMINAL)
        self.assertEqual(rc, 0)
        rc, text = self.run_cli(["hub", "publish", "proj-a", "--as", "main"], TERMINAL)
        self.assertEqual(rc, 0)
        self.assertIn("main", text)
        rc, text = self.run_cli(["hub", "publish"], TERMINAL)
        self.assertEqual(rc, 2)

    def test_hub_host_commands_run_for_the_terminal(self):
        path = Path(tempfile.mkdtemp()) / "hub.db"
        HubCore(path, clock=Clock()).close()
        rc, text = self.run_cli(["hub", "machines", "--db", str(path)], TERMINAL)
        self.assertEqual(rc, 0)
        rc, text = self.run_cli(["hub", "status", "--host", "--db", str(path)], TERMINAL)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
