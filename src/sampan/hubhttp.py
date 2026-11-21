"""The client's HTTPS transport to the hub, and where its token comes from. Standard library only (urllib).

Every request carries the custom User-Agent (Cloudflare answers 403 to Python's default one), never follows a redirect (a
Cloudflare Access login page is not the hub), and goes over plain http only to a loopback address (tests, a hub on the same
machine). Network trouble and server errors are TransportError (the caller tries again later); an error the hub answered
with is the hubproto.ProtoError it names.
"""
import json
import socket
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta
from pathlib import Path

from . import hubproto as hp
from .connect import TransportError, file_problem

CREDENTIALS_FILE = "credentials.toml"
LOOPBACK = ("127.0.0.1", "::1", "localhost")
TOKEN_MARGIN = timedelta(minutes=2)  # a token with less left than this is renewed


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kw):
        return None


def _opener():
    return urllib.request.build_opener(_NoRedirect)


def check_url(url, *, what="hub"):
    """The URL, or ValueError: https, or http only for a loopback host."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return url
    if parts.scheme == "http" and parts.hostname in LOOPBACK:
        return url
    raise ValueError(f"{what} 的網址要是 https（只有 127.0.0.1／localhost 可以用 http）：{url}")


def request_json(url, *, method="GET", body=None, headers=None, timeout=40, opener=None):
    """(status, parsed JSON or None). TransportError for a network failure, a redirect, a 5xx or an answer that is not JSON
    (what a proxy or a login page sends)."""
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        if isinstance(body, (dict, list)):
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        else:
            data = body if isinstance(body, bytes) else str(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with (opener or _opener()).open(req, timeout=timeout) as resp:
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except (urllib.error.URLError, OSError, socket.timeout) as e:
        raise TransportError(f"{type(e).__name__}: {getattr(e, 'reason', e)}") from e
    if status in (301, 302, 303, 307, 308):
        raise TransportError(f"被導向別處（HTTP {status}），像是 Cloudflare Access 的登入頁：檢查 service token")
    if status >= 500:
        raise TransportError(f"HTTP {status}")
    if status == 204 or not raw:
        return status, None
    try:
        return status, json.loads(raw)
    except ValueError as e:
        raise TransportError(f"HTTP {status}，不是 JSON（被代理或登入頁擋住？）") from e


class TokenSource:
    """An access token from the identity provider's token endpoint (OAuth client credentials), renewed before it runs out.
    The client secret is read from the credentials file and is never printed, logged or put in the environment."""

    def __init__(self, token_url, client_id, client_secret, *, clock, ua, extra_headers=None, timeout=20, opener=None):
        self.url = check_url(token_url, what="token")
        self.client_id, self.secret, self.clock, self.ua = client_id, client_secret, clock, ua
        self.extra, self.timeout, self.opener = dict(extra_headers or {}), timeout, opener
        self._token, self._until = None, None

    def __call__(self):
        now = self.clock()
        if self._token and self._until and now + TOKEN_MARGIN < self._until:
            return self._token
        form = urllib.parse.urlencode({"grant_type": "client_credentials", "client_id": self.client_id,
                                       "client_secret": self.secret}).encode("ascii")
        status, data = request_json(self.url, method="POST", body=form, timeout=self.timeout, opener=self.opener,
                                    headers={**self.extra, "User-Agent": self.ua, "Accept": "application/json",
                                             "Content-Type": "application/x-www-form-urlencoded"})
        if status != 200 or not isinstance(data, dict) or not data.get("access_token"):
            raise TransportError(f"拿不到 token（HTTP {status}）")
        self._token = data["access_token"]
        self._until = now + timedelta(seconds=int(data.get("expires_in") or 300))
        return self._token


class HttpTransport:
    """The hub's endpoints as methods (the same ones the in-process test transport has)."""

    def __init__(self, base_url, token_source, *, ua, extra_headers=None, timeout=15, opener=None, poll_extra=10):
        self.base = check_url(base_url).rstrip("/")
        self.token, self.ua, self.extra = token_source, ua, dict(extra_headers or {})
        self.timeout, self.opener, self.poll_extra = timeout, opener, poll_extra

    def _call(self, method, path, body=None, *, timeout=None):
        headers = {**self.extra, "User-Agent": self.ua, "Accept": "application/json",
                   "Authorization": f"Bearer {self.token()}", "X-Sampan-Proto": str(hp.PROTO)}
        status, data = request_json(self.base + path, method=method, body=body, headers=headers,
                                    timeout=timeout or self.timeout, opener=self.opener)
        if status in (200, 202, 204):
            return data
        code = data.get("error") if isinstance(data, dict) else None
        if code in hp.ERRORS:
            raise hp.ProtoError(code, str(data.get("detail") or ""))
        if status == 401:
            raise hp.ProtoError("unauthorized")
        raise TransportError(f"HTTP {status}")

    def hello(self, payload):
        return self._call("POST", "/v1/hello", payload)

    def send(self, env):
        return self._call("POST", "/v1/send", env)

    def poll(self, wait=0):
        data = self._call("GET", f"/v1/poll?wait={int(wait)}", timeout=self.timeout + int(wait) + self.poll_extra)
        return (data or {}).get("items", [])

    def ack(self, ids, result, reason=""):
        return self._call("POST", "/v1/ack", {"ids": ids, "result": result, "reason": reason})

    def cancel(self, env_id):
        return self._call("POST", "/v1/cancel", {"id": env_id})

    def peers(self):
        return (self._call("GET", "/v1/peers") or {}).get("peers", [])


def load_credentials(path):
    """The credentials file (TOML) as a dict, or a problem string. Required: hub_url, token_url, client_id, client_secret;
    optional: access_client_id and access_client_secret (a Cloudflare Access service token for this machine)."""
    problem = file_problem(path)
    if problem:
        return problem
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return f"讀不到 {path}"
    except tomllib.TOMLDecodeError as e:
        return f"{path} 格式不對：{e}"
    missing = [k for k in ("hub_url", "token_url", "client_id", "client_secret") if not isinstance(data.get(k), str) or not data[k]]
    if missing:
        return f"{path} 缺少：{'、'.join(missing)}"
    return data


def access_headers(creds):
    """The Cloudflare Access service token headers for this machine (none when the file has no such token)."""
    if creds.get("access_client_id") and creds.get("access_client_secret"):
        return {"CF-Access-Client-Id": creds["access_client_id"], "CF-Access-Client-Secret": creds["access_client_secret"]}
    return {}


def transport_from_credentials(creds, *, clock, version, opener=None):
    ua = hp.user_agent(version)
    extra = access_headers(creds)
    token = TokenSource(creds["token_url"], creds["client_id"], creds["client_secret"], clock=clock, ua=ua,
                        extra_headers=extra, opener=opener)
    return HttpTransport(creds["hub_url"], token, ua=ua, extra_headers=extra, opener=opener)
