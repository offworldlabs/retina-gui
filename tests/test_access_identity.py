"""Tests for verifying Cloudflare Access assertions.

Uses a real RSA keypair and real signed tokens rather than mocking the
verification, because the failures that matter here are the ones where a token
verifies and should not have. A mocked verifier would pass all of them.
"""

import json
import logging
import time

import jwt
import pytest
import requests
from cryptography.hazmat.primitives.asymmetric import rsa

from access_identity import AccessIdentity

TEAM = "offworldlab.cloudflareaccess.com"
ISSUER = f"https://{TEAM}"
AUD = "b62aeb13c198cd2118bd5d92b350f2af5c42830c703baeab42aad5fa3e01f29a"
OTHER_AUD = "0000000000000000000000000000000000000000000000000000000000000000"
EMAIL = "jehan@offworldlab.com"


@pytest.fixture(scope="module")
def keys():
    """One keypair for the suite. Generating RSA is slow enough to matter."""
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


def _jwk(public_key, kid):
    data = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public_key))
    data.update({"kid": kid, "alg": "RS256", "use": "sig"})
    return data


class FakeHTTP:
    """Stands in for requests, counting fetches so caching can be asserted."""

    def __init__(self, jwks, fail=False):
        self.jwks = jwks
        self.fail = fail
        self.calls = 0

    def get(self, url, timeout=None):
        self.calls += 1
        if self.fail:
            raise requests.RequestException("unreachable")
        payload = self.jwks

        class Response:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return payload

        return Response()


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "access.json"
    path.write_text(json.dumps({"team_domain": TEAM, "aud": AUD}))
    return path


@pytest.fixture
def verifier(config, keys):
    _, public = keys
    http = FakeHTTP({"keys": [_jwk(public, "kid-1")]})
    v = AccessIdentity(config_path=str(config), http=http)
    v.http_fake = http
    return v


def token(keys, *, aud=AUD, iss=ISSUER, email=EMAIL, kid="kid-1",
          exp_delta=300, key=None, **extra):
    private, _ = keys
    claims = {"aud": aud, "iss": iss, "email": email,
              "exp": int(time.time()) + exp_delta,
              "iat": int(time.time()) - 10, **extra}
    return jwt.encode(claims, key or private, algorithm="RS256",
                      headers={"kid": kid})


# ── the happy path ───────────────────────────────────────────────

def test_a_valid_assertion_yields_the_email(verifier, keys):
    assert verifier.identity(token(keys)) == EMAIL


def test_the_key_set_is_cached(verifier, keys):
    for _ in range(5):
        verifier.identity(token(keys))
    assert verifier.http_fake.calls == 1


# ── the refusals that matter ─────────────────────────────────────

def test_a_token_for_another_application_is_refused(verifier, keys):
    """The one most easily left out, because such a token is perfectly signed.

    This team already runs Access on other hostnames, so without the audience
    check anyone holding a valid session for those would be admitted here.
    """
    assert verifier.identity(token(keys, aud=OTHER_AUD)) is None


def test_a_token_from_another_team_is_refused(verifier, keys):
    assert verifier.identity(token(keys, iss="https://someone-else.cloudflareaccess.com")) is None


def test_an_expired_assertion_is_refused(verifier, keys):
    assert verifier.identity(token(keys, exp_delta=-600)) is None


def test_a_token_signed_by_someone_else_is_refused(verifier, keys):
    """Right shape, right claims, wrong key. The signature is the whole point."""
    impostor = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert verifier.identity(token(keys, key=impostor)) is None


def test_an_unsigned_token_is_refused(verifier):
    """alg=none is the classic way to walk past a verifier that trusts the
    header's own claim about how it was signed."""
    forged = jwt.encode({"aud": AUD, "iss": ISSUER, "email": EMAIL,
                         "exp": int(time.time()) + 300},
                        key="", algorithm="none")
    assert verifier.identity(forged) is None


def test_a_token_naming_nobody_is_refused(verifier, keys):
    """Verifies, but authenticates no one. Returning something truthy would let
    a caller believe it had identified a person."""
    assert verifier.identity(token(keys, email="")) is None


def test_a_token_with_no_audience_claim_is_refused(verifier, keys):
    private, _ = keys
    claims = {"iss": ISSUER, "email": EMAIL, "exp": int(time.time()) + 300}
    naked = jwt.encode(claims, private, algorithm="RS256", headers={"kid": "kid-1"})
    assert verifier.identity(naked) is None


def test_rubbish_is_refused(verifier):
    for value in ("", None, "not-a-token", "a.b.c"):
        assert verifier.identity(value) is None


# ── configuration ────────────────────────────────────────────────

def test_no_config_means_no_identity(tmp_path, keys):
    """Fails closed. Before node-infra delivers the audience there is nothing to
    check against, and admitting anyone in the meantime would be the worst
    possible default."""
    v = AccessIdentity(config_path=str(tmp_path / "absent.json"))
    assert v.is_configured() is False
    assert v.identity(token(keys)) is None


def test_a_partial_config_is_treated_as_absent(tmp_path, keys):
    path = tmp_path / "access.json"
    path.write_text(json.dumps({"team_domain": TEAM}))
    v = AccessIdentity(config_path=str(path))
    assert v.is_configured() is False
    assert v.identity(token(keys)) is None


def test_a_corrupt_config_is_treated_as_absent(tmp_path, keys):
    path = tmp_path / "access.json"
    path.write_text("{ not json")
    assert AccessIdentity(config_path=str(path)).identity(token(keys)) is None


# ── key rotation and outages ─────────────────────────────────────

def test_an_unknown_key_id_triggers_one_refetch(config, keys):
    """A key id we have not seen is either a rotation or a forgery, and one
    refetch tells them apart."""
    private, public = keys
    http = FakeHTTP({"keys": [_jwk(public, "old-kid")]})
    v = AccessIdentity(config_path=str(config), http=http)

    assert v.identity(token(keys, kid="old-kid")) == EMAIL
    assert http.calls == 1

    # Cloudflare rotates; the token now carries a kid we have never seen.
    http.jwks = {"keys": [_jwk(public, "new-kid")]}
    assert v.identity(token(keys, kid="new-kid")) == EMAIL
    assert http.calls == 2


def test_a_kid_that_never_appears_is_refused_and_does_not_loop(config, keys):
    private, public = keys
    http = FakeHTTP({"keys": [_jwk(public, "real-kid")]})
    v = AccessIdentity(config_path=str(config), http=http)

    assert v.identity(token(keys, kid="invented")) is None
    assert http.calls <= 2, "must not refetch endlessly for a forged kid"


def test_cloudflare_being_unreachable_refuses_rather_than_admits(config, keys):
    http = FakeHTTP({}, fail=True)
    v = AccessIdentity(config_path=str(config), http=http)
    assert v.identity(token(keys)) is None


# ── clock drift ──────────────────────────────────────────────────

def test_small_clock_drift_is_tolerated(verifier, keys):
    """Nodes keep time with chrony but drift while offline. Refusing a token
    that expired two seconds ago would be a confusing way to fail."""
    assert verifier.identity(token(keys, exp_delta=-5)) == EMAIL


def test_large_drift_is_not_tolerated(verifier, keys):
    assert verifier.identity(token(keys, exp_delta=-120)) is None


# ── explaining refusals ──────────────────────────────────────────
#
# Every refusal returns a bare None, which is right for the caller and useless
# for whoever has to work out why a node stopped admitting anyone. These assert
# the log makes up the difference, and that it does so without handing out the
# credential it is describing.

def test_every_refusal_says_why(verifier, keys, caplog):
    caplog.set_level(logging.WARNING)
    cases = {
        "wrong audience": token(keys, aud=OTHER_AUD),
        "wrong team": token(keys, iss="https://someone-else.cloudflareaccess.com"),
        "expired": token(keys, exp_delta=-600),
        "no email": token(keys, email=""),
        "unknown kid": token(keys, kid="never-published"),
    }
    for label, bad in cases.items():
        caplog.clear()
        assert verifier.identity(bad) is None, label
        assert caplog.records, f"{label} was refused silently"
        assert all(r.levelno >= logging.WARNING for r in caplog.records), label


def test_the_token_is_never_logged(verifier, keys, caplog):
    """A refused assertion is still a live bearer credential for its session.
    A log that quotes it hands that session to anyone who can read logs, and
    these lines exist to be read by people debugging."""
    caplog.set_level(logging.DEBUG)
    for bad in (token(keys, aud=OTHER_AUD),
                token(keys, exp_delta=-600),
                token(keys, kid="never-published")):
        caplog.clear()
        verifier.identity(bad)
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert bad not in logged
        # Not even a substantial slice of it. Signatures are the long tail.
        assert bad.split(".")[2][:24] not in logged


def test_a_wrong_audience_names_both(verifier, keys, caplog):
    """The failure a misconfiguration actually produces, and the one that is
    invisible from outside: the hostname is up and refuses everybody."""
    caplog.set_level(logging.WARNING)
    verifier.identity(token(keys, aud=OTHER_AUD))
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert OTHER_AUD[:12] in logged, "does not say what the token claimed"
    assert AUD[:12] in logged, "does not say what this node expects"


def test_unreachable_cloudflare_is_distinguishable_from_a_bad_token(config, keys,
                                                                    caplog):
    """An outage and a forgery both return None. Confusing the two sends
    somebody hunting an attacker during a network problem."""
    caplog.set_level(logging.WARNING)
    v = AccessIdentity(config_path=str(config), http=FakeHTTP({}, fail=True))
    assert v.identity(token(keys)) is None
    logged = " ".join(r.getMessage() for r in caplog.records).lower()
    assert "could not reach" in logged


def test_missing_config_is_reported_as_such(tmp_path, keys, caplog):
    caplog.set_level(logging.WARNING)
    v = AccessIdentity(config_path=str(tmp_path / "absent.json"))
    assert v.identity(token(keys)) is None
    logged = " ".join(r.getMessage() for r in caplog.records)
    assert "absent.json" in logged


def test_no_token_at_all_is_not_logged(verifier, caplog):
    """Unauthenticated requests are ordinary on a public hostname. Logging each
    one would bury the refusals that mean something."""
    caplog.set_level(logging.WARNING)
    assert verifier.identity(None) is None
    assert verifier.identity("") is None
    assert not caplog.records


def test_a_success_is_not_logged_as_a_refusal(verifier, keys, caplog):
    caplog.set_level(logging.WARNING)
    assert verifier.identity(token(keys)) == EMAIL
    assert not caplog.records
