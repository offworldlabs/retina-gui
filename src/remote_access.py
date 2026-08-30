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

## Stored in the clear, deliberately

This is the phone-hotspot model, not the user-account model: the owner sets
something memorable, looks it up whenever they need to share it, and changes it
when they want. A hash cannot do the middle one, and encrypting instead would be
theatre — any key the node needs to decrypt has to live on the node beside the
ciphertext.

It costs less than it sounds like it does. The only readers of the plaintext are
people who already own the node: the config page shows it on the LAN pathway
alone, and the LAN pathway is unauthenticated anyway, so anyone who can read it
there could already change every setting on the box. Mender's remote terminal is
root, so staff are in the same position with or without this. Nothing is
protected by hashing here that is not already open.

Two consequences worth being deliberate about. Someone briefly on the house
network can note the password down and keep *remote* access after they leave,
which LAN access alone would not have given them — the owner's remedy is to
change it, same as a hotspot. And a memorable password is the kind people reuse,
so the config page says out loud that anyone on the local network can see it.

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

# Comparison is constant-time, which is the one thing a plaintext store still has
# to get right: a plain == leaks the password a character at a time through how
# long the mismatch took to find.
#
# Note what is *not* here any more. Hashing used to cost ~145 ms per attempt on a
# pi5-v3, which incidentally throttled guessing. A string compare is free, so the
# rate limit at Cloudflare's edge is now the only thing standing between this and
# an offline-speed guessing run. It is not optional.

#: Eight, matching the WPA2 minimum a phone hotspot enforces — this imitates that
#: model, and a floor people already recognise beats one they have to discover.
#: It is short, and deliberately so; the edge rate limit is what makes it safe
#: rather than the length. Anyone wanting real strength can press Generate.
MIN_PASSWORD_LENGTH = 8

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
        return bool(state.get("enabled")) and bool(state.get("password"))

    def has_password(self):
        return bool(self._read().get("password"))

    def get_password(self):
        """The password itself, for display. "" when none is set.

        Separate from status() on purpose. status() is handed to the config
        template on every pathway and returned by the toggle endpoint as JSON;
        this is only ever read where the pathway allows it (see routes/config.py).
        Keeping them apart means the password cannot reach a template or a
        response by being carried along inside something else.
        """
        return self._read().get("password") or ""

    def status(self):
        """What the config page needs to render the section. Never the password."""
        state = self._read()
        return {
            "enabled": bool(state.get("enabled")) and bool(state.get("password")),
            "requested": bool(state.get("enabled")),
            "has_password": bool(state.get("password")),
            "updated_at": state.get("updated_at"),
        }

    # ── verifying ────────────────────────────────────────────────

    def verify(self, password):
        """Check a submitted password. False whenever no password is set.

        Encoded before comparing because compare_digest refuses non-ASCII str,
        and nothing stops an owner picking a password with an accent in it.
        """
        stored = self._read().get("password")
        if not stored or not password:
            return False
        return secrets.compare_digest(stored.encode("utf-8"),
                                      password.encode("utf-8"))

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
        return self._update(password=password)

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
            # 0600 from creation rather than chmod-ed afterwards, so the
            # password is never briefly world-readable. It matters more now that
            # the file holds the password itself rather than a hash of it.
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
