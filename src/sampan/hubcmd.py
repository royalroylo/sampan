"""The commands of the hub: `sampan connect` (this machine's daemon and its keys), `sampan hub publish / unpublish / peers /
status` (this machine's side) and, on the hub host, `sampan hub serve / machines / approve / revoke / audit`.

Everything that changes something is refused to an agent while `[mcp] confirm` is on (the CLI wrapper checks that); the
reading commands are free.
"""
import fcntl
import os
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import actionlog, machine, remote
from . import hubproto as hp
from .connect import KEY_KID, Connector, TransportError, file_problem, load_keys, write_key
from .hubhttp import load_credentials, transport_from_credentials
from .update import current_version

HUB_CONFIG = "hub.toml"
MAINTENANCE_EVERY = 3600  # seconds


def _utcnow():
    return datetime.now(timezone.utc)


# ---------- this machine ----------

def cmd_keygen(cfg, out):
    """Make the owner key (the one every machine of the owner shares) and say what to do with it."""
    path = remote.key_path(cfg)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        line = write_key(path)
    except FileExistsError:
        out(f"❌ 已經有 {path}，不覆蓋。要在這台用同一把金鑰，把另一台的那一行貼進這個檔案（權限 600）")
        return 2
    out(f"✔ 已建立 {path}（權限 600）")
    out("把下面這一行用你自己的方式（不要經過 hub、不要貼進對話或記憶）放到你其他每台機器的同一個位置，權限 600：")
    out(line)
    return 0


def cmd_publish(cfg, out, session, alias=""):
    from .registry import RegistryError, load_rows
    try:
        names = {r.tmux for r in load_rows(cfg.registry_path, codex_profile=cfg.codex.profile)}
    except RegistryError as e:
        out(f"❌ {e}")
        return 2
    if session not in names:
        out(f"❌ 沒有登記的 session「{session}」（sampan status 看名稱）")
        return 2
    if alias and not hp.valid_name(alias):
        out("❌ 別名不可含斜線、控制字元，最多 64 字")
        return 2
    store = remote.open_store(cfg)
    store.publish(session, alias)
    store.close()
    shown = f"{session}（別名 {alias}）" if alias else session
    out(f"✔ 已發布 {shown}：你其他的機器可以用「{machine.load_or_create(cfg.path).name}/{alias or session}」送訊息給它")
    out("   hub 下次 hello（每分鐘）就會看到；sampan connect 要在跑")
    actionlog.append(cfg, "hub", [f"發布 {session}" + (f"（別名 {alias}）" if alias else "")])
    return 0


def cmd_unpublish(cfg, out, session):
    store = remote.open_store(cfg)
    done = store.unpublish(session)
    store.close()
    if not done:
        out(f"沒有發布「{session}」")
        return 1
    out(f"✔ 不再發布 {session}")
    actionlog.append(cfg, "hub", [f"取消發布 {session}"])
    return 0


def cmd_peers(cfg, out):
    store = remote.open_store(cfg)
    peers = store.peers_get()
    store.close()
    if not peers:
        out("還沒有其他機器的清單（sampan connect 沒在跑、這台還沒被 hub 核准，或你只有這一台）")
        return 0
    for p in peers:
        out(f"{p['name']}")
        for s in p["sessions"]:
            alias = f"（別名 {s['alias']}）" if s.get("alias") else ""
            out(f"  {p['name']}/{s['session']}{alias}　{s.get('status') or '?'}")
        if not p["sessions"]:
            out("  （沒有發布任何 session）")
    return 0


def cmd_status(cfg, out):
    """What this machine's side of the hub is doing: the state the hub gave it, the outbox, the files that must stay private."""
    own = machine.load_or_create(cfg.path)
    if not (Path(cfg.state_dir) / "remote.db").exists():  # reading must not create anything
        out(f"這台機器：{own.name}（id {own.id[:8] or '?'}）")
        out("hub 狀態：還沒連過 hub")
        for what, path in (("owner key", remote.key_path(cfg)), ("credentials", remote.credentials_path(cfg))):
            if not path.exists():
                out(f"⚠ 沒有 {what}（{path}）")
        return 1
    store = remote.open_store(cfg)
    out(f"這台機器：{own.name}（id {own.id[:8] or '?'}）")
    state = store.get("machine_state", "還沒連過 hub")
    out(f"hub 狀態：{state}" + (f"（驗證碼 {store.get('verification_code')}）" if state == "pending" and store.get("verification_code") else ""))
    pending = store.outbox_pending()
    out(f"待送出：{len(pending)} 則" + ("（hub 連不上或還沒核准時會留在這裡）" if pending else ""))
    out(f"已發布：{('、'.join(p['session'] for p in store.published())) or '沒有'}")
    out(f"其他機器：{('、'.join(p['name'] for p in store.peers_get())) or '沒有'}")
    store.close()
    problems = []
    for what, path in (("owner key", remote.key_path(cfg)), ("credentials", remote.credentials_path(cfg))):
        if not path.exists():
            problems.append(f"沒有 {what}（{path}）")
        elif (p := file_problem(path)):
            problems.append(p)
    for p in problems:
        out(f"⚠ {p}")
    return 1 if problems else 0


def _lock(cfg):
    """One sampan connect per state folder. The file object (keep it alive), or None when another one holds it."""
    from .fsutil import PRIVATE_FILE, private_dir
    private_dir(cfg.state_dir)
    fh = os.fdopen(os.open(Path(cfg.state_dir) / "connect.lock", os.O_WRONLY | os.O_CREAT, PRIVATE_FILE), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def build_connector(cfg, run, store, *, clock=_utcnow, transport=None, version=None, status_fn=None):
    """(Connector, None), or (None, why not): credentials, keys and machine id are all there."""
    version = version or current_version()
    creds = None
    if transport is None:
        creds = load_credentials(remote.credentials_path(cfg))
        if isinstance(creds, str):
            return None, creds
    keys = load_keys(remote.key_path(cfg))
    if not keys:
        return None, f"沒有 owner key（{remote.key_path(cfg)}）：sampan connect keygen，再把那一行放到你其他的機器"
    problem = file_problem(remote.key_path(cfg))
    if problem:
        return None, problem
    own = machine.load_or_create(cfg.path)
    if not own.id:
        return None, "讀不到或寫不了 machine.toml，沒有機器 id"
    transport = transport or transport_from_credentials(creds, clock=clock, version=version)

    def log(line):
        actionlog.append(cfg, "hub", [line])
    hooks = remote.make_hooks(cfg, run, store, log=log, now_fn=clock, **({"status_fn": status_fn} if status_fn else {}))
    return Connector(store, transport, hooks, machine_id=own.id, machine_name=own.name, version=version, keys=keys,
                     kid=KEY_KID if KEY_KID in keys else sorted(keys)[0], clock=clock), None


def run_loop(cfg, run, conn, store, *, stop, once=False, wait=hp.POLL_WAIT, sleep=time.sleep, clock=_utcnow):
    """The daemon: a pass of the connector, then what the receiving side owes (advance what came in, queue its receipts and
    replies), then the cancels; over and over. A pass with an error waits (growing, at most a minute) before the next."""
    failures, last_maintenance = 0, clock()
    while not stop.is_set():
        out = conn.tick(wait=0 if once else wait)
        try:
            remote.pump_inbound(cfg, run, store, conn, sleep=sleep, now_fn=clock)
            if conn.state == "active" and store.outbox_pending():
                conn.send_outbox()
            remote.process_cancels(store, conn.transport)
        except TransportError as e:
            out["error"] = f"hub 連不上：{e}"
        except Exception as e:  # noqa: BLE001  the daemon must not die of one bad pass; the reason goes to the log
            out["error"] = f"{type(e).__name__}: {e}"
            conn.hooks.log(f"一輪失敗：{out['error']}")
        if (clock() - last_maintenance).total_seconds() >= MAINTENANCE_EVERY:
            conn.maintenance()
            last_maintenance = clock()
        if once:
            return out
        if out.get("error") or conn.state != "active":
            failures += 1
            stop.wait(min(60, 2 ** min(failures, 6)) if conn.state == "active" else 15)
        else:
            failures = 0
    return None


def cmd_connect(cfg, run, out, *, once=False, wait=hp.POLL_WAIT):
    store = remote.open_store(cfg)
    conn, problem = build_connector(cfg, run, store)
    if conn is None:
        out(f"❌ {problem}")
        store.close()
        return 2
    lock = _lock(cfg)
    if lock is None:
        out("❌ 已經有 sampan connect 在跑（同一個狀態資料夾只能一個）")
        store.close()
        return 2
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    conn.hooks.log("sampan connect 開始")
    try:
        result = run_loop(cfg, run, conn, store, stop=stop, once=once, wait=wait)
    finally:
        conn.hooks.log("sampan connect 結束")
        store.close()
        lock.close()
    if once:
        out(f"機器狀態：{conn.state}" + (f"（驗證碼 {conn.code}）" if conn.state == "pending" and conn.code else ""))
        if result.get("error"):
            out(f"⚠ {result['error']}")
        return 1 if result.get("error") else 0
    return 0


# ---------- the hub host ----------

def _hub_db(args, env=os.environ):
    return Path(args.db or env.get("SAMPAN_HUB_DB") or Path.home() / ".local" / "state" / "sampan-hub" / "hub.db")


def _open_core(args):
    from .hub.core import HubCore
    path = _hub_db(args)
    if not path.exists():
        return None, f"找不到 hub 資料庫 {path}（sampan hub serve 跑過、或用 --db 指定）"
    return HubCore(path), None


def cmd_machines(args, out):
    core, problem = _open_core(args)
    if core is None:
        out(f"❌ {problem}")
        return 2
    rows = core.machines()
    if not rows:
        out("還沒有機器連過來")
    for m in rows:
        extra = f"　驗證碼 {m['code']}（跟那台機器 sampan connect 印的比對）" if m["state"] == "pending" else ""
        out(f"{m['name']}　{m['state']}　最後連線 {m['last_seen'] or '-'}　版本 {m['version'] or '-'}{extra}")
    core.close()
    return 0


def cmd_approve(args, out):
    """Approve a pending machine. The verification code (the one that machine's `sampan connect` printed) is required:
    approving by name alone would let a stranger's request, or a stolen client, in without anyone having compared anything."""
    if not args.code:
        out("❌ 要給 --code：那台機器 sampan connect 印的驗證碼（sampan hub machines 會列出等核准的機器與它們的碼，跟那台機器上的比對相同才核准）")
        return 2
    core, problem = _open_core(args)
    if core is None:
        out(f"❌ {problem}")
        return 2
    try:
        core.approve(args.name, actor="admin", code=args.code)
    except hp.ProtoError as e:
        out(f"❌ {e.message}")
        return 2
    finally:
        core.close()
    out(f"✔ 已核准 {args.name}")
    return 0


def cmd_revoke(args, out):
    core, problem = _open_core(args)
    if core is None:
        out(f"❌ {problem}")
        return 2
    try:
        core.revoke(args.name, actor="admin")
    except hp.ProtoError as e:
        out(f"❌ {e.message}")
        return 2
    finally:
        core.close()
    out(f"✔ 已撤銷 {args.name}：立刻生效；也請停用它的 Keycloak client 與 Access service token")
    return 0


def cmd_audit(args, out):
    core, problem = _open_core(args)
    if core is None:
        out(f"❌ {problem}")
        return 2
    for a in reversed(core.audit_log(args.limit)):
        out(f"{a['at']}　{a['actor']}　{a['action']}　{a['subject'] or ''}　{a['detail'] or ''}".rstrip())
    core.close()
    return 0


def cmd_hub_status(args, out):
    core, problem = _open_core(args)
    if core is None:
        out(f"❌ {problem}")
        return 2
    st = core.status()
    out(f"機器：{st['machines'] or '沒有'}")
    out(f"信封：{st['envelopes'] or '沒有'}")
    out(f"協定：{st['proto']}　最低客戶端協定：{st['min_client_proto']}")
    core.close()
    return 0


def cmd_serve(args, out):
    """Run the hub: http.server on the given loopback address. Tokens: --dev takes the stub identity provider's (never for
    real use); otherwise OIDC (RS256) with --issuer, --audience and --jwks-url, which needs the sampan[hub] extra."""
    from .hub.auth import OidcVerifier, StubVerifier, fetch_json
    from .hub.core import HubCore
    from .hub.server import HubServer
    host, _, port = (args.listen or "127.0.0.1:8787").rpartition(":")
    if host not in ("127.0.0.1", "::1", "localhost"):
        out("❌ hub 只聽本機（127.0.0.1）；對外請走 Tunnel／反向代理")
        return 2
    if args.dev:
        verifier = StubVerifier()
        out("⚠ --dev：用測試用的 token 驗證，只能在開發時用")
    else:
        if not (args.issuer and args.audience and args.jwks_url):
            out("❌ 要給 --issuer、--audience 與 --jwks-url（或 --dev 做開發測試）")
            return 2
        try:
            ua = hp.user_agent(current_version())
            verifier = OidcVerifier(args.issuer, args.audience, args.jwks_url, fetch=lambda url: fetch_json(url, ua=ua))
        except RuntimeError as e:
            out(f"❌ {e}")
            return 2
    path = _hub_db(args)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    core = HubCore(path)
    server = HubServer((host, int(port)), core, verifier, log=lambda line: print(line, flush=True))
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    server.start()
    out(f"hub 在 {server.url} 上（資料庫 {path}）")

    def janitor():
        while not stop.wait(60):
            try:
                core.tick()
            except Exception as e:  # noqa: BLE001
                print(f"tick 失敗：{type(e).__name__}", flush=True)
    t = threading.Thread(target=janitor, daemon=True)
    t.start()
    try:
        stop.wait()
    finally:
        server.stop()
        core.close()
    return 0
