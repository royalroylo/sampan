# The hub: messages between your own machines (0.26)

sampan manages the sessions of one machine. The hub lets a session on **another machine of yours** receive messages and answer them, used the same way: `sampan send laptop/proj-a "…"`, or an agent's MCP `send` and `dispatch_result`.

The hub is a relay: every machine connects to it (HTTPS long polling), so no machine opens an inbound port. **The first phase is only for one person's machines**; other people's messages are not supported yet.

> The hub sees message text in the clear (it is a relay, not end-to-end encrypted), keeps it 14 days after delivery, and expires what was never taken after 7 days. Only each machine's `dispatches.db` has the full history.

## Parts

- **`sampan connect`** (one daemon per machine): standard library only, every request carries `User-Agent: sampan-connect/<version>`, never follows a redirect (it says so when a proxy's login page blocks it).
- **`sampan hub serve`** (on one host only): the separate package `sampan[hub]` (checking tokens needs PyJWT and cryptography); it listens on `127.0.0.1` only, so put a tunnel or reverse proxy in front.
- Authentication: your OIDC identity provider. One OAuth client per machine (client credentials); the hub accepts RS256 tokens only and checks issuer, audience (the array must **contain** it), `azp`, `exp` and `nbf`; 15 minutes is a good token lifetime.
- Messages carry an **HMAC-SHA256 signature**: all your machines share one "owner key" that you put on each of them **by hand**, never through the hub. So even a hub that has been taken over cannot make a message look as if it came from another of your machines (it can still read, drop or delay messages).

## Setup

**Every machine:**
1. On the first one: `sampan connect keygen`. It creates `~/.config/sampan/hub/owner.key` (mode 600) and prints one line; put that line in the same place on your other machines your own way (not in a chat, a memory or the hub), mode 600.
2. Create `~/.config/sampan/hub/credentials.toml` (mode 600, or sampan refuses it):
   ```toml
   hub_url = "https://hub.example.com"
   token_url = "https://idp.example.com/realms/work/protocol/openid-connect/token"
   client_id = "sampan-laptop"
   client_secret = "…"
   # optional: this machine's service token when a Cloudflare Access sits in front
   access_client_id = "…"
   access_client_secret = "…"
   ```
   Keep the credentials out of environment variables and launchd plists.
3. `sampan hub publish <session> [--as alias]`: **no session is visible to other machines by default**; publish explicitly.
4. `sampan connect` (in the foreground; for a daemon use launchd or systemd). `sampan connect status` shows this machine's state and what is missing.

**The hub host:**
- Run the hub as its own OS user (directory 0700), not as the user your agents run as, and keep the hub's directory out of Time Machine, syncing and other backups.
- `sampan hub serve --listen 127.0.0.1:8787 --issuer <issuer> --audience <audience> --jwks-url <jwks>` (`--dev` is for development only and takes test tokens).
- A new machine is **pending** the first time it connects and sends and receives nothing. Its `sampan connect` prints a verification code; on the hub host `sampan hub machines` shows the same code, and only when they match do you `sampan hub approve <name> --code <code>`.
- Revoke: `sampan hub revoke <name>` takes effect at once (it does not wait for the token to run out) and drops what waits for that machine; also disable its OAuth client and service token. `sampan hub audit` shows the audit trail (never message text).

## Use

- `sampan send laptop/proj-a "…"`: "machine/session"; the machine name is the one `sampan machine` shows (the hub requires it to be unique; change it with `sampan machine --name`); a session can be named by its published alias. An agent writes the same in MCP `send`; every message goes through the existing approval gate.
- `sampan hub peers`: the other machines with the sessions they publish and busy or idle. MCP `list_sessions` with `remote: true` shows them too.
- `dispatch_result` gets a `remote` block: `outbox` (not at the hub yet; it stays there while the hub is unreachable) → `at-hub` → `taken` (the other machine has it) → `sent` (pasted into the session) → `replied`; failures say why: refused by the other machine, refused by the hub, or expired because the other machine never came online.
- At the other end the message enters the session like a local one (doorbell, `inbox_take`, `reply`), with "machine/session" as the sender.
- Not supported yet: `restore`, `profile` and `raw` for a remote target, `resend` of a remote dispatch (send a new one), and a remote target in `send_batch` (use `send` one by one).

## Delivery guarantees and limits

- **At least once**: an envelope has a unique id; one the hub has not accepted stays in the outbox and is retried (backoff with jitter, at most every 5 minutes); the hub answers a repeat of an id with the same answer; the receiver tells repeats apart by id, so nothing runs twice.
- Expiry: what nobody took expires after 7 days (the text is deleted at once); after it was taken the text stays 14 more days; the metadata (who sent what to whom, when, how big) stays 90 days.
- Limits: 8,000 characters per message, 200 messages or 1 MB per mailbox by default, a daily limit per sender; going over gives a readable reason.
- Versions: a client that is too old is refused with a request to update; the signature cannot be downgraded (a hub that says "no signing needed" is ignored).
- Clocks: `doctor` warns at a 5 minute difference; past 15 minutes messages are refused.
- There is no read receipt: only `replied` means the other agent answered.
