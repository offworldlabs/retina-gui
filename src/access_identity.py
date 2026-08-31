"""Verifying that a request really was authenticated by Cloudflare Access.

Cloudflare puts a signed assertion in `Cf-Access-Jwt-Assertion` on every request
it lets through. This turns that into an email address, or into nothing.

## Why verify at all, when Access sits in front

In normal operation nothing unauthenticated reaches the node: the tunnel serves
one hostname, and Access intercepts by hostname. This is for the case where that
stops being true. An Access application deleted, renamed or misconfigured leaves
the hostname open and the node happily serving it, and node-infra's
reconciliation would notice eventually rather than immediately.

So this is the node's own answer to "did Cloudflare actually vouch for you",
independent of anything upstream continuing to be configured correctly.

## What is checked, and why each one matters

    signature   against the team's published keys. Without it the header is a
                claim anyone can type.
    audience    the tag of *this node's* Access application. Without it, a token
                minted for any other application in the same team is accepted
                here, and the team already runs Access on other hostnames.
    issuer      the team domain, so a valid token from some other Cloudflare
                team is not enough.
    expiry      with a little leeway for clock drift, which these nodes have.

Missing any one of those turns verification into decoration. The audience is the
one most easily left out, because a token that fails it still has a perfectly
good signature.

## Configuration

Both values arrive from node-infra in a small file beside the tunnel token,
because the audience is generated per Access application and so differs per
node. Absent or unreadable means no identity can be established, and the caller
refuses. Failing closed is the only safe reading of "we cannot tell who this is".
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
#: {"team_domain": "offworldlab.cloudflareaccess.com", "aud": "<64 hex chars>"}
DEFAULT_CONFIG_PATH = "/data/cloudflared/access.json"

#: Cloudflare rotates signing keys, so a cached set goes stale. An hour is well
#: inside their rotation window, and an unrecognised key id refetches
#: immediately regardless, so this is a ceiling on staleness rather than the
#: mechanism that handles rotation.
JWKS_TTL_SECONDS = 3600

#: Nodes keep time with chrony but can drift while offline, and rejecting a
#: freshly issued token because the node is three seconds behind would be a
#: confusing way to fail.
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
    """One unverified claim, for explaining a rejection.

    Never use this to decide anything. It exists so a log line can say which
    audience a refused token named, which is the difference between a two minute
    fix and an afternoon on a live node.
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
        # Requests are served by threads; without this two arriving together
        # could each fetch, and one could install a key set for a team domain
        # the other had just changed away from.
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

        An unrecognised key id means either a rotation we have not seen or a
        forged header. Refetching once distinguishes them: after a fresh fetch a
        key that still does not exist is not one of Cloudflare's.
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

        None covers every way this can fail: no configuration, no token, a bad
        signature, the wrong audience, the wrong team, an expired assertion, or
        Cloudflare being unreachable. The caller cannot act differently on any
        of them, and distinguishing them in a return value would invite somebody
        to treat one as good enough.

        The *log* does distinguish them, at WARNING, because operationally they
        could not be more different: a wrong audience is a misconfiguration
        nobody will spot from the outside, and an unreachable Cloudflare is an
        outage. Returning a bare None for both once cost an afternoon of
        instrumenting a live node to find out which had happened, since a
        refusal left no trace anywhere.

        The token is never logged. It is a bearer credential for its session,
        and a log that quotes it hands that session to anyone who can read logs.
        The claims are, because they are what the decision turned on.
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
            # Worth its own message. This is what a token minted for one of the
            # team's other Access applications looks like, and the fix is a
            # configuration change rather than anything the caller did wrong.
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
            # Not a refusal of *this* token so much as an inability to judge it.
            # Distinct from the above because nothing is wrong with the request.
            log.warning(
                "Refusing an Access assertion: could not reach %s for signing "
                "keys: %s", team_domain, exc)
            return None
        except (ValueError, KeyError) as exc:
            log.warning("Refusing an Access assertion: malformed: %s: %s",
                        type(exc).__name__, exc)
            return None

        # Cloudflare puts the address in `email`. A token that verifies but
        # names nobody is not an identity, and returning something truthy for it
        # would let a caller believe it had authenticated a person.
        email = (claims.get("email") or "").strip()
        if not email:
            log.warning("Refusing an Access assertion: it verifies, but carries "
                        "no email claim, so it identifies nobody.")
            return None
        return email
