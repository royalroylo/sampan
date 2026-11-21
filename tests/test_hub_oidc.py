"""The real token check. Needs `sampan[hub]` (PyJWT with cryptography): without it these tests are skipped, and the rest of the
suite uses the stub verifier."""
import importlib.util
import time
import unittest

from sampan import hubproto as hp
from sampan.hub.auth import ALGORITHMS, OidcVerifier

HAVE = importlib.util.find_spec("jwt") is not None and importlib.util.find_spec("cryptography") is not None
ISS, AUD, JWKS = "https://idp.example.com/realms/work", "sampan-hub", "https://idp.example.com/certs"


@unittest.skipUnless(HAVE, "needs sampan[hub] (PyJWT with cryptography)")
class OidcTest(unittest.TestCase):
    def setUp(self):
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        self.jwt = jwt
        self.now = [time.time()]  # the verifier's clock (key-set caching); PyJWT checks exp and nbf against the real one
        self.keys = {kid: rsa.generate_private_key(public_exponent=65537, key_size=2048) for kid in ("k1", "k2")}
        self.fetches = []
        self.fail_fetch = False
        self.published = ["k1"]

    def jwks(self, url):
        self.fetches.append(url)
        if self.fail_fetch:
            raise OSError("down")
        out = []
        for kid in self.published:
            data = self.jwt.algorithms.RSAAlgorithm.to_jwk(self.keys[kid].public_key(), as_dict=True)
            out.append({**data, "kid": kid, "use": "sig"})
        return {"keys": out}

    def verifier(self, **kw):
        return OidcVerifier(ISS, AUD, JWKS, fetch=self.jwks, clock=lambda: self.now[0], **kw)

    def token(self, kid="k1", alg="RS256", key=None, **claims):
        real = time.time()
        base = {"iss": ISS, "aud": [AUD, "account"], "sub": "svc-laptop", "azp": "client-laptop", "exp": real + 900, "iat": real - 5}
        base.update(claims)
        base = {k: v for k, v in base.items() if v is not None}
        return self.jwt.encode(base, key or self.keys[kid], algorithm=alg, headers={"kid": kid})

    def code(self, verifier, token):
        try:
            verifier(token)
        except hp.ProtoError as e:
            return e.code
        return None

    def test_a_good_token_gives_the_client_and_subject(self):
        who = self.verifier()(self.token())
        self.assertEqual((who.client_id, who.sub), ("client-laptop", "svc-laptop"))

    def test_audience_is_a_list_and_must_contain_ours(self):
        v = self.verifier()
        self.assertIsNone(self.code(v, self.token(aud=[AUD])))
        self.assertIsNone(self.code(v, self.token(aud=["account", AUD])))
        self.assertEqual(self.code(v, self.token(aud=["account"])), "unauthorized")
        self.assertEqual(self.code(v, self.token(aud="other")), "unauthorized")
        self.assertEqual(self.code(v, self.token(aud=None)), "unauthorized")

    def test_issuer_expiry_not_before_and_leeway(self):
        v = self.verifier()
        self.assertEqual(self.code(v, self.token(iss="https://evil.example.com")), "unauthorized")
        self.assertEqual(self.code(v, self.token(exp=time.time() - 120)), "unauthorized")
        self.assertIsNone(self.code(v, self.token(exp=time.time() - 30)))  # within the 60 s leeway
        self.assertEqual(self.code(v, self.token(exp=None)), "unauthorized")
        self.assertEqual(self.code(v, self.token(nbf=time.time() + 600)), "unauthorized")
        self.assertEqual(self.code(v, self.token(sub=None)), "unauthorized")

    def test_the_client_comes_from_azp_and_may_be_limited(self):
        self.assertEqual(self.code(self.verifier(), self.token(azp=None)), "unauthorized")
        v = self.verifier(allowed_clients={"client-desktop"})
        self.assertEqual(self.code(v, self.token()), "unauthorized")
        self.assertIsNone(self.code(v, self.token(azp="client-desktop")))

    def test_only_rs256_none_and_hs256_are_refused(self):
        self.assertEqual(ALGORITHMS, ("RS256",))
        v = self.verifier()
        v(self.token())  # load the keys
        none = self.jwt.encode({"iss": ISS, "aud": AUD, "sub": "x", "azp": "client-laptop", "exp": time.time() + 900},
                               key=None, algorithm="none", headers={"kid": "k1"})
        self.assertEqual(self.code(v, none), "unauthorized")
        # HS256 signed with the public key as the secret (the classic confusion)
        from cryptography.hazmat.primitives import serialization
        pub = self.keys["k1"].public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        import hashlib
        import hmac
        import json
        from base64 import urlsafe_b64encode

        def b64(b):
            return urlsafe_b64encode(b).rstrip(b"=")
        head = b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": "k1"}).encode())
        body = b64(json.dumps({"iss": ISS, "aud": AUD, "sub": "x", "azp": "client-laptop", "exp": time.time() + 900}).encode())
        sig = b64(hmac.new(pub, head + b"." + body, hashlib.sha256).digest())
        self.assertEqual(self.code(v, (head + b"." + body + b"." + sig).decode()), "unauthorized")

    def test_a_token_signed_by_another_key_or_with_no_kid_is_refused(self):
        v = self.verifier()
        self.assertEqual(self.code(v, self.token(kid="k1", key=self.keys["k2"])), "unauthorized")  # k2's signature under k1's id
        no_kid = self.jwt.encode({"iss": ISS, "aud": AUD, "sub": "x", "azp": "c", "exp": time.time() + 900}, self.keys["k1"], algorithm="RS256")
        self.assertEqual(self.code(v, no_kid), "unauthorized")
        for junk in ("", "abc", "a.b.c", None, 5):
            self.assertEqual(self.code(v, junk), "unauthorized", junk)

    def test_key_set_is_cached_and_an_unknown_kid_refetches_at_most_every_30_seconds(self):
        v = self.verifier()
        v(self.token())
        v(self.token())
        self.assertEqual(len(self.fetches), 1)
        self.assertEqual(self.code(v, self.token(kid="k2")), "unauthorized")  # just fetched: no refetch for an unknown kid yet
        self.assertEqual(len(self.fetches), 1)
        self.now[0] += 31
        self.assertEqual(self.code(v, self.token(kid="k2")), "unauthorized")  # k2 not published yet: one refetch
        self.assertEqual(len(self.fetches), 2)
        self.assertEqual(self.code(v, self.token(kid="k2")), "unauthorized")
        self.assertEqual(len(self.fetches), 2)  # not again so soon
        self.published = ["k1", "k2"]
        self.now[0] += 31
        self.assertIsNone(self.code(v, self.token(kid="k2")))  # rotated in
        self.assertEqual(len(self.fetches), 3)

    def test_the_key_set_is_refreshed_after_an_hour_and_the_old_one_serves_for_a_day_if_it_cannot(self):
        v = self.verifier()
        v(self.token())
        self.now[0] += 3601
        self.fail_fetch = True
        self.assertIsNone(self.code(v, self.token()))  # fetch fails: the last set still serves
        self.now[0] += 24 * 3600
        self.assertEqual(self.code(v, self.token()), "unauthorized")  # but not forever

    def test_no_key_set_at_all_is_unauthorized(self):
        self.fail_fetch = True
        self.assertEqual(self.code(self.verifier(), self.token()), "unauthorized")

    def test_a_key_set_without_rsa_keys_is_not_trusted(self):
        v = OidcVerifier(ISS, AUD, JWKS, fetch=lambda url: {"keys": [{"kid": "x", "kty": "oct", "k": "AAAA"}]}, clock=lambda: self.now[0])
        self.assertEqual(self.code(v, self.token()), "unauthorized")


class WithoutTheExtraTest(unittest.TestCase):
    @unittest.skipIf(HAVE, "the extra is installed")
    def test_says_how_to_install_it(self):
        with self.assertRaises(RuntimeError) as e:
            OidcVerifier(ISS, AUD, JWKS, fetch=lambda u: {})
        self.assertIn("sampan[hub]", str(e.exception))


if __name__ == "__main__":
    unittest.main()
