"""Verifying that a request really was authenticated by Cloudflare Access.

Turns the `Cf-Access-Jwt-Assertion` header into a verified email address, or
None. Security invariant: signature, audience, issuer and expiry must ALL be
checked, and a missing or unreadable config refuses everything.
See docs/features/remote-access.md#cloudflare-access-verification.
"""

import json
import logging
import threading
import time

import jwt
import requests
from jwt import PyJWKSet

log = logging.getLogger(__name__)

#: Written by node-infra alongside the connector token and installed by owl-os.
#: {"team_domain": "<team>.cloudflareaccess.com", "aud": "<64 hex chars>"}
DEFAULT_CONFIG_PATH = "/data/cloudflared/access.json"

#: A ceiling on key-set staleness. Rotation itself is handled by refetching on
#: an unrecognised key id. See docs/features/remote-access.md#signing-keys.
JWKS_TTL_SECONDS = 3600

#: Nodes can drift while offline; do not reject a fresh token over seconds.
CLOCK_LEEWAY_SECONDS = 30


def _short(value):
    """Enough of an identifier to match against, not enough to fill a line."""
    value = str(value or "")
    return value if len(value) <= 12 else value[:12] + "..."


def _kid(token):
    """The key id a token claims, for the log only.

    Read without verifying anything, which is safe here precisely because the
    caller has already decided to refuse: this only ever describes a rejection.
    """
    try:
        return _short(jwt.get_unverified_header(token).get("kid"))
    except Exception:
        return "unreadable"


def _claim(token, name):
    """One unverified claim, for explaining a rejection in the log.

    Never use this to decide anything.
    """
    try:
        value = jwt.decode(token, options={"verify_signature": False}).get(name)
    except Exception:
        return "unreadable"
    if isinstance(value, list):
        return [_short(v) for v in value]
    return _short(value)


class AccessIdentity:
    """Turns a Cloudflare Access assertion into a verified email address."""

    def __init__(self, config_path=DEFAULT_CONFIG_PATH, ttl=JWKS_TTL_SECONDS,
                 http=requests):
        self.config_path = config_path
        self.ttl = ttl
        self.http = http
        self._jwks = None
        self._jwks_domain = None
        self._fetched_at = 0.0
        # Requests are served by threads; guards the cached key set.
        self._lock = threading.Lock()

    # ── configuration ────────────────────────────────────────────

    def config(self):
        """(team_domain, audience), or (None, None) when it cannot be read."""
        try:
            with open(self.config_path) as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None, None
        if not isinstance(data, dict):
            return None, None
        domain = (data.get("team_domain") or "").strip()
        audience = (data.get("aud") or "").strip()
        if not domain or not audience:
            return None, None
        return domain, audience

    def is_configured(self):
        return self.config() != (None, None)

    # ── signing keys ─────────────────────────────────────────────

    def _fetch_jwks(self, team_domain):
        url = f"https://{team_domain}/cdn-cgi/access/certs"
        response = self.http.get(url, timeout=10)
        response.raise_for_status()
        return PyJWKSet.from_dict(response.json())

    def _signing_key(self, token, team_domain):
        """The key this token was signed with, refetching once if it is new.

        A key id still unknown after a fresh fetch is not one of Cloudflare's.
        """
        kid = jwt.get_unverified_header(token).get("kid")
        if not kid:
            return None

        with self._lock:
            stale = (self._jwks is None
                     or self._jwks_domain != team_domain
                     or time.time() - self._fetched_at > self.ttl)
            if stale:
                self._jwks = self._fetch_jwks(team_domain)
                self._jwks_domain = team_domain
                self._fetched_at = time.time()

            try:
                return self._jwks[kid]
            except KeyError:
                pass

            self._jwks = self._fetch_jwks(team_domain)
            self._jwks_domain = team_domain
            self._fetched_at = time.time()
            try:
                return self._jwks[kid]
            except KeyError:
                return None

    # ── the answer ───────────────────────────────────────────────

    def identity(self, token):
        """The verified email address, or None.

        None for every failure, deliberately indistinguishable to the caller.
        Each is logged at WARNING with its reason. Never log the token itself:
        it is a bearer credential.
        See docs/features/remote-access.md#failure-reporting.
        """
        if not token:
            return None

        team_domain, audience = self.config()
        if not team_domain:
            log.warning(
                "Refusing an Access assertion: no usable configuration at %s. "
                "Every request on the support hostname will be refused until "
                "node-infra delivers it.", self.config_path)
            return None

        try:
            key = self._signing_key(token, team_domain)
            if key is None:
                log.warning(
                    "Refusing an Access assertion: signed with key id %s, which "
                    "is not one of %s's published keys.",
                    _kid(token), team_domain)
                return None
            claims = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                audience=audience,
                issuer=f"https://{team_domain}",
                leeway=CLOCK_LEEWAY_SECONDS,
                options={"require": ["exp", "aud", "iss"]},
            )
        except jwt.InvalidAudienceError:
            # A token for another of the team's Access applications, or a
            # stale access.json: a config fix, so it gets its own message.
            log.warning(
                "Refusing an Access assertion: it names audience %r, but this "
                "node's application is %s. Either it was issued for a different "
                "application, or access.json is stale.",
                _claim(token, "aud"), _short(audience))
            return None
        except jwt.InvalidTokenError as exc:
            log.warning("Refusing an Access assertion: %s: %s",
                        type(exc).__name__, exc)
            return None
        except requests.RequestException as exc:
            # Cannot judge the token, rather than a bad token.
            log.warning(
                "Refusing an Access assertion: could not reach %s for signing "
                "keys: %s", team_domain, exc)
            return None
        except (ValueError, KeyError) as exc:
            log.warning("Refusing an Access assertion: malformed: %s: %s",
                        type(exc).__name__, exc)
            return None

        # A token that verifies but names nobody is not an identity.
        email = (claims.get("email") or "").strip()
        if not email:
            log.warning("Refusing an Access assertion: it verifies, but carries "
                        "no email claim, so it identifies nobody.")
            return None
        return email
