"""Remote access: the opt-in switch, and the password the node checks visitors against.

Two hostnames reach this GUI once a Cloudflare tunnel is up, and they are told
apart by nothing except the ``Host`` header:

    ret4c844c20.retnode.com          the owner. Must present the password below.
    ret4c844c20.admin.retnode.com    Offworld staff. Gated by Cloudflare Access
                                     at the edge; this GUI asks them for nothing.

Everything else — ``owl.local``, ``ret4c844c20.local``, an IP — is the LAN, which
is unauthenticated exactly as it has always been. Being on the network is the
credential there, and nothing in this module changes that.

## The password never leaves the node

It is generated here, stored here, verified here. It is not in the tunnel token,
not in the telemetry registration payload, and the server that provisions the
tunnel neither receives it nor could ask for it. That is the point of doing this
on the node rather than with an identity provider: the owner decides who gets in,
and there is no account to create and no email to collect.

Stored hashed, so a lost password is replaced rather than recovered. That is a
deliberate departure from the phone-hotspot model this otherwise imitates — a
hotspot shows you its password whenever you ask, and this cannot. Recovery is
setting a new one from the LAN, which needs physical presence, which is the same
authority a factory reset would need.

## Why the device-presence tier exists

Rotating the password has to *mean* something. If a visitor authenticated with a
shared password could add an SSH key, they would keep a way in that outlived the
rotation, and the owner's only remedy would be reinstalling the node. So key
management, password changes and Mender installs are refused on the owner
pathway even when the session is valid — see PRESENCE_REQUIRED_PREFIXES. The LAN
and the admin hostname are unaffected.
"""

import json
import os
import secrets
import tempfile
import time

from werkzeug.security import check_password_hash, generate_password_hash

# Hashing method is werkzeug's default, whatever that is for the version
# installed. Measured on a live pi5-v3 node (werkzeug 2.2.2, which predates
# scrypt and so picks pbkdf2:sha256:260000): ~147 ms to hash, ~143 ms to verify.
#
# That cost is the point on a login form, but it is also why the edge rate limit
# is not optional: at 143 ms of CPU per attempt, an unthrottled guessing run is a
# load problem on a board already busy running the radar, quite apart from
# eventually finding the password.

#: Twelve is the floor for anything the owner types. Short enough to be typed on
#: a phone, long enough that the edge rate limit is a backstop rather than the
#: only defence. The generated default below is much stronger and is what most
#: nodes will actually run with.
MIN_PASSWORD_LENGTH = 12

#: No 0/O, 1/l/I. A password read aloud across a room or copied off a screen is
#: the normal case here, and character pairs nobody can distinguish turn that
#: into a support conversation.
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"

#: Four groups of four, hyphenated: 16 characters from a 31-symbol alphabet is
#: about 79 bits, which is not brute-forceable at any online rate.
_GENERATED_GROUPS = 4
_GENERATED_GROUP_LEN = 4

#: Refused on the owner pathway even with a valid session. Prefix-matched.
#:
#: These are the operations that would let a visitor outlive the password they
#: were given, or take the node away from its owner:
#:
#:   /ssh-keys        an added key survives every future password rotation
#:   /remote-access   changing the password locks the owner out of their own node
#:   /mender/install  pushes new firmware, and can leave the node unreachable
#:
#: Everything else on the GUI is fair game for whoever the owner shared with.
PRESENCE_REQUIRED_PREFIXES = (
    "/ssh-keys",
    "/remote-access",
    "/mender/install",
)


def generate_password():
    """A strong default, in the shape of a thing a person will retype."""
    groups = [
        "".join(secrets.choice(_ALPHABET) for _ in range(_GENERATED_GROUP_LEN))
        for _ in range(_GENERATED_GROUPS)
    ]
    return "-".join(groups)


class RemoteAccess:
    """The node's own record of whether remote access is on, and its password."""

    def __init__(self, state_file):
        self.state_file = state_file
        self.data_dir = os.path.dirname(state_file)

    # ── reading ──────────────────────────────────────────────────

    def _read(self):
        try:
            with open(self.state_file) as f:
                state = json.load(f)
        except (OSError, ValueError):
            return {}
        return state if isinstance(state, dict) else {}

    def is_enabled(self):
        """True only when the owner turned it on *and* set a password.

        Both halves are required deliberately. A node advertising a hostname it
        will refuse every visitor on is worse than one that never advertised it,
        because the owner has no way to tell the difference from outside.
        """
        state = self._read()
        return bool(state.get("enabled")) and bool(state.get("password_hash"))

    def has_password(self):
        return bool(self._read().get("password_hash"))

    def status(self):
        """What the config page needs to render the section."""
        state = self._read()
        return {
            "enabled": bool(state.get("enabled")) and bool(state.get("password_hash")),
            "requested": bool(state.get("enabled")),
            "has_password": bool(state.get("password_hash")),
            "updated_at": state.get("updated_at"),
        }

    # ── verifying ────────────────────────────────────────────────

    def verify(self, password):
        """Check a submitted password. False whenever no password is set.

        ``check_password_hash`` compares in constant time, so this does not leak
        the password through how long it took to reject it.
        """
        password_hash = self._read().get("password_hash")
        if not password_hash or not password:
            return False
        try:
            return check_password_hash(password_hash, password)
        except (ValueError, TypeError):
            # A hash written by a future format, or a corrupted file. Refusing
            # is the only safe reading of "we cannot tell".
            return False

    # ── writing ──────────────────────────────────────────────────

    @staticmethod
    def validate_password(password):
        """Return (ok, error) for a password the owner typed."""
        password = password or ""
        if len(password) < MIN_PASSWORD_LENGTH:
            return False, (f"Password must be at least {MIN_PASSWORD_LENGTH} "
                           f"characters")
        if password.strip() != password:
            return False, "Password cannot start or end with a space"
        return True, None

    def set_password(self, password):
        """Store a new password. Returns (ok, error)."""
        ok, error = self.validate_password(password)
        if not ok:
            return False, error
        return self._update(password_hash=generate_password_hash(password))

    def set_enabled(self, enabled):
        """Turn remote access on or off. Returns (ok, error).

        Turning it on without a password is refused rather than silently
        half-applied: see is_enabled() for why an advertised-but-unusable
        hostname is the worst of the three states.
        """
        enabled = bool(enabled)
        if enabled and not self.has_password():
            return False, "Set a password before turning on remote access"
        return self._update(enabled=enabled)

    def _update(self, **changes):
        state = self._read()
        state.update(changes)
        state["updated_at"] = int(time.time())
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            # 0600 from creation rather than chmod-ed afterwards, so the hash is
            # never briefly world-readable. Same reasoning as the telemetry
            # bearer token's handling in retina-telemetry's state.py.
            fd, tmp_path = tempfile.mkstemp(dir=self.data_dir)
            os.chmod(tmp_path, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(state, f)
            os.rename(tmp_path, self.state_file)
        except OSError as e:
            return False, f"Could not save remote access settings: {e}"
        return True, None


# ── which pathway a request arrived on ───────────────────────────

LAN = "lan"
OWNER = "owner"
ADMIN = "admin"


def classify_host(host, node_id, domain):
    """Return LAN, OWNER or ADMIN for the Host header of a request.

    The three pathways are distinguished by name alone, because they are the
    same service on the same port and there is nothing else to distinguish them
    by. cloudflared forwards the hostname the request arrived on, so a visitor
    cannot reach the owner hostname and present themselves as the admin one.

    Fails closed on the remote domain. Anything under it that is not recognised
    as the admin name is treated as OWNER — so a hostname this node does not
    expect (a stale DNS record, a provisioning bug, a domain-wide wildcard
    someone added) demands a password rather than being mistaken for the LAN and
    waved through. Only names with no relationship to the remote domain are LAN.
    """
    host = (host or "").split(":")[0].strip().rstrip(".").lower()
    domain = (domain or "").strip().rstrip(".").lower()
    if not host or not domain:
        return LAN

    suffix = "." + domain
    if not (host == domain or host.endswith(suffix)):
        return LAN

    node_id = (node_id or "").strip().lower()
    if node_id and host == f"{node_id}.admin{suffix}":
        return ADMIN
    return OWNER


def requires_presence(path):
    """True for operations refused on the owner pathway (see the module docstring)."""
    return (path or "").startswith(PRESENCE_REQUIRED_PREFIXES)
