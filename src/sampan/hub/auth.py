"""Who is calling: a bearer token turned into a Principal, or ProtoError("unauthorized").

StubVerifier is for tests and local development (opaque tokens from the stub identity provider). OidcVerifier checks a
real OIDC access token (RS256 only; issuer, audience, expiry, not-before, and the OAuth client in `azp`); it needs the
`sampan[hub]` extra (PyJWT with its crypto support) and says so when it is missing.
"""
import json
import threading
import time

from .. import hubproto as hp
from .core import Principal

JWKS_TTL = 3600  # seconds a fetched key set is trusted without asking again
JWKS_STALE = 24 * 3600  # how long the last key set is still used when asking again fails
JWKS_MIN_GAP = 30  # seconds between two fetches triggered by an unknown key id
LEEWAY = 60  # seconds of clock difference tolerated for exp and nbf
ALGORITHMS = ("RS256",)  # nothing else: not "none", and never HS256 (the public key must not be usable as a secret)


class StubVerifier:
    """Tokens of the form `stub.<client id>.<sub>` (what the stub identity provider hands out), or an explicit table."""

    def __init__(self, tokens=None):
        self.tokens = dict(tokens or {})

    def __call__(self, token):
        if token in self.tokens:
            return self.tokens[token]
        parts = token.split(".", 2) if isinstance(token, str) else []
        if len(parts) == 3 and parts[0] == "stub" and parts[1]:
            return Principal(parts[1], parts[2])
        raise hp.ProtoError("unauthorized")


class OidcVerifier:
    def __init__(self, issuer, audience, jwks_url, *, fetch, clock=time.time, allowed_clients=None):
        try:
            import jwt  # noqa: F401  PyJWT; with `sampan[hub]`
            from jwt import PyJWK
        except ImportError as e:
            raise RuntimeError("驗證 OIDC token 需要 sampan[hub]（PyJWT 與 cryptography）：uv tool install 'sampan[hub]'") from e
        self._jwt, self._PyJWK = jwt, PyJWK
        self.issuer, self.audience, self.url, self.fetch, self.clock = issuer, audience, jwks_url, fetch, clock
        self.allowed_clients = allowed_clients  # None: any client the hub has a machine row for
        self._keys, self._fetched, self._attempt = {}, 0.0, 0.0
        self._lock = threading.Lock()

    def _load(self):
        """Fetch the key set (no lock held: a slow identity provider must not stop requests that can be answered from the
        keys we have). Returns {kid: key}; raises when it cannot be had."""
        data = self.fetch(self.url)
        keys = {}
        for k in (data or {}).get("keys", []):
            if isinstance(k, dict) and k.get("kid") and k.get("kty") == "RSA" and k.get("use") != "enc":
                keys[k["kid"]] = self._PyJWK(k).key
        if not keys:
            raise ValueError("JWKS 沒有 RSA 簽章公鑰")
        return keys

    def _key(self, kid):
        with self._lock:
            now = self.clock()
            stale = now - self._fetched > JWKS_TTL
            unknown = kid not in self._keys
            # every fetch, failed or not, waits JWKS_MIN_GAP before the next: a down identity provider is asked every
            # 30 seconds, not by every request
            fetch = (stale or unknown or not self._keys) and now - self._attempt >= JWKS_MIN_GAP
            if fetch:
                self._attempt = now
            have = dict(self._keys)
            fetched = self._fetched
        if fetch:
            try:
                keys = self._load()
            except Exception:  # noqa: BLE001  keep serving from the last set for a while
                keys = None
            with self._lock:
                if keys is not None:
                    self._keys, self._fetched = keys, now
                    have = keys
                elif not have or now - fetched > JWKS_STALE:
                    raise hp.ProtoError("unauthorized", "拿不到公鑰")
        elif not have:
            raise hp.ProtoError("unauthorized", "拿不到公鑰")
        key = have.get(kid)
        if key is None:
            raise hp.ProtoError("unauthorized")
        return key

    def __call__(self, token):
        jwt = self._jwt
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in ALGORITHMS or not header.get("kid"):
                raise hp.ProtoError("unauthorized")
            claims = jwt.decode(token, self._key(header["kid"]), algorithms=list(ALGORITHMS), audience=self.audience,
                                issuer=self.issuer, leeway=LEEWAY, options={"require": ["exp", "iss", "aud", "sub"]})
        except hp.ProtoError:
            raise
        except Exception:  # noqa: BLE001  expired, wrong audience or issuer, bad signature, garbage: all just "no"
            raise hp.ProtoError("unauthorized") from None
        client = claims.get("azp") or claims.get("client_id")
        if not isinstance(client, str) or not client:
            raise hp.ProtoError("unauthorized", "token 沒有 azp")
        if self.allowed_clients is not None and client not in self.allowed_clients:
            raise hp.ProtoError("unauthorized")
        return Principal(client, str(claims["sub"]))


def fetch_json(url, *, ua, timeout=15):
    """The JSON at url (the key set), with the custom User-Agent. Plain http only for loopback."""
    from ..hubhttp import check_url, request_json
    status, data = request_json(check_url(url, what="jwks"), headers={"User-Agent": ua, "Accept": "application/json"}, timeout=timeout)
    if status != 200 or data is None:
        raise ValueError(f"HTTP {status}")
    return data


__all__ = ["OidcVerifier", "StubVerifier", "fetch_json", "json"]
