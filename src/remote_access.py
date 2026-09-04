"""Remote access: the opt-in switch, and the password the node checks visitors against.

One hostname reaches this GUI once a Cloudflare tunnel is up:

    ret4c844c20.retnode.com    the owner, from anywhere. Must present the
                               password below.

Everything else (``owl.local``, ``ret4c844c20.local``, an IP) is the LAN, which
is unauthenticated exactly as it has always been. Being on the network is the
credential there, and nothing in this module changes that.

Offworld staff are not a third case. They reach a node with
``mender-cli port-forward``, which arrives with a Host of ``localhost`` and so
counts as the LAN. Mender has already authenticated them and logged the session,
which is why there is no staff hostname, no Cloudflare Access application and no
fleet-wide staff password anywhere in this design.

## The password never leaves the node

It is generated here, stored here, verified here. Nothing sends it anywhere: the
only thing that leaves this node is a marker file saying remote access is on,
carried up as a Mender inventory attribute. node-infra provisions a tunnel from
that and never learns the password, because it has no reason to. That is the
point of doing this on the node rather than with an identity provider: the owner
decides who gets in, and there is no account to create and no email to collect.

## Stored in the clear, deliberately

This is the phone-hotspot model, not the user-account model: the owner sets
something memorable, looks it up whenever they need to share it, and changes it
when they want. A hash cannot do the middle one, and encrypting instead would be
theatre: any key the node needs to decrypt has to live on the node beside the
ciphertext.

It costs less than it sounds like it does. The only readers of the plaintext are
people who already own the node: the config page shows it on the LAN pathway
alone, and the LAN pathway is unauthenticated anyway, so anyone who can read it
there could already change every setting on the box. Mender's remote terminal is
root, so staff are in the same position with or without this. Nothing is
protected by hashing here that is not already open.

Two consequences worth being deliberate about. Someone briefly on the house
network can note the password down and keep *remote* access after they leave,
which LAN access alone would not have given them. The owner's remedy is to
change it, same as a hotspot. And a memorable password is the kind people reuse,
so the config page says out loud that anyone on the local network can see it.

## Why the device-presence tier exists

Rotating the password has to *mean* something. If a visitor authenticated with a
shared password could add an SSH key, they would keep a way in that outlived the
rotation, and the owner's only remedy would be reinstalling the node. So key
management, password changes and Mender installs are refused on the owner
pathway even when the session is valid. See PRESENCE_REQUIRED_PREFIXES. The LAN
is unaffected, so an owner at home and an engineer on a port-forward both keep
the full interface.
"""

import json
import os
import re
import secrets
import subprocess
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

#: Eight, matching the WPA2 minimum a phone hotspot enforces. This imitates that
#: model, and a floor people already recognise beats one they have to discover.
#: It is short, and deliberately so; the edge rate limit is what makes it safe
#: rather than the length. Anyone wanting real strength can press Generate.
MIN_PASSWORD_LENGTH = 8

#: Where owl-os keeps the connector token, and the unit that consumes it. Read
#: only to report whether the tunnel is actually up; nothing here writes either.
TUNNEL_TOKEN_PATH = "/data/cloudflared/tunnel-token"
CLOUDFLARED_UNIT = "cloudflared.service"

#: The file owl-os's mender-inventory-retina-remote-access script looks for.
#:
#: This is the entire node-to-server channel for this feature. The node states
#: what its owner wants by the presence of this file; Mender carries it up as
#: the `remote_access` inventory attribute on its next poll; node-infra reads
#: that and decides whether a tunnel should exist. Nothing here calls a server.
#:
#: A marker rather than the state file itself because that one is 0600 root and
#: holds the password, and the inventory script is POSIX shell.
#:
#: Presence still means enabled, which is why defaulting on needs the file to
#: appear on a node nobody has touched: the inventory script reads the file, not
#: is_enabled(). publish_marker() at startup is what puts it there.
ENABLED_MARKER = "remote-access-enabled"

#: The remote shell agreement, recorded by its exception rather than its
#: presence, exactly as device_state records cloud services.
#:
#: The two agreements default differently and so are stored differently. Support
#: access defaults OFF, so its marker means "wanted". Shell access defaults ON,
#: because every node in the fleet already has it and defaulting off would
#: silently withdraw something owners already rely on; recording only the
#: exception means an untouched node needs no migration, and a missing or
#: unwritable /data cannot quietly revoke access nobody declined.
SHELL_DISABLED_MARKER = "remote-shell-disabled"

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
        """Whether the owner permits support to reach this node's interface.

        The owner's choice alone. It used to also require a password, from when
        the owner signed in with one; support access is gated by Cloudflare
        Access now and the page no longer offers a way to set one, so that
        condition made the setting impossible to turn on at all.

        Nothing is advertised prematurely by defaulting on. The hostname only
        starts serving once the connector has a token, and node-infra installs
        the Access config before the token precisely so the node can identify
        callers from the first request it answers.

        TEMPORARY: defaults ON, so a node that has never been touched publishes
        `remote_access=true` and gets a tunnel without anyone opting in. That is
        deliberate for now and is meant to be reverted: the setting exists to be
        the owner's choice, and a default of on makes it a choice they have to
        discover in order to decline. Tracked, with the revert, on the ticket in
        Deployment Issues & Improvements.

        Restoring the old behaviour is one word: drop the `True` below.
        """
        return bool(self._read().get("enabled", True))

    def has_password(self):
        return bool(self._read().get("password"))

    # ── the remote shell agreement ───────────────────────────────

    def is_shell_allowed(self):
        """Whether the owner permits interactive access over Mender.

        Independent of everything else in this module. Declining it does not
        affect the support tunnel, and declining support does not affect this.
        """
        return not os.path.exists(
            os.path.join(self.data_dir, SHELL_DISABLED_MARKER))

    def record_shell_allowed(self, allowed):
        """Record the owner's choice. Returns (ok, error).

        Records only. Enforcing it is mender_connect's job, and the caller
        applies that FIRST so this file never claims a state the node is not
        actually in.
        """
        marker = os.path.join(self.data_dir, SHELL_DISABLED_MARKER)
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            if allowed:
                try:
                    os.remove(marker)
                except FileNotFoundError:
                    pass
            else:
                # World readable like the other marker: the inventory scripts
                # run as root and there is nothing here to protect.
                with open(marker, "w") as f:
                    f.write("")
                os.chmod(marker, 0o644)
        except OSError as e:
            return False, f"Could not record the remote shell setting: {e}"
        return True, None

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
            # is_enabled(), not the raw field: the default lives in one place
            # and this is the page that would otherwise disagree with it.
            "enabled": self.is_enabled(),
            "has_password": bool(state.get("password")),
            "shell_allowed": self.is_shell_allowed(),
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
        """Turn support access on or off. Returns (ok, error)."""
        return self._update(enabled=bool(enabled))

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

        self._sync_marker()
        return True, None

    def publish_marker(self):
        """Write the inventory marker to match is_enabled(), at startup.

        The marker is the whole node-to-server channel for this setting, and a
        node that has never been touched has no state file and so has never had
        one written. While the default was off that was consistent: no file, no
        tunnel. With the default on it is not, and the node would keep reporting
        `remote_access=false` until somebody happened to toggle something.
        """
        self._sync_marker()

    def _sync_marker(self):
        """Make the inventory marker agree with is_enabled().

        Recomputed after every write rather than set at the toggle. The marker
        has to reflect the conjunction of two values written by two different
        methods, so deriving it in one of them is how the two drift apart.

        Best effort. A marker that failed to appear costs the owner one Mender
        inventory cycle, whereas refusing the whole save because of it would
        lose a password they just typed.
        """
        marker = os.path.join(self.data_dir, ENABLED_MARKER)
        try:
            if self.is_enabled():
                # World readable on purpose: mender-authd runs the inventory
                # scripts and there is nothing in here to protect.
                with open(marker, "w") as f:
                    f.write("")
                os.chmod(marker, 0o644)
            else:
                try:
                    os.remove(marker)
                except FileNotFoundError:
                    pass
        except OSError:
            pass


# ── is the tunnel actually up ────────────────────────────────────

def tunnel_status(token_path=TUNNEL_TOKEN_PATH, unit=CLOUDFLARED_UNIT):
    """Report the connector's real state, asked of systemd rather than a server.

    Nothing on the node knows whether provisioning succeeded, and under this
    design nothing needs to: node-infra puts a token on the box, owl-os starts
    the connector, and the honest answer to "is it working" is whether that unit
    is up. cloudflared is Type=notify and only signals ready once it has
    connections to the edge, so an active unit means genuinely reachable rather
    than merely launched.

    Returns one of:
      "waiting"  opted in, but no token has arrived yet
      "up"       token installed and the connector is running
      "down"     token installed and the connector is not
      "unknown"  systemctl could not be asked (dev machine, test run)
    """
    try:
        if os.path.getsize(token_path) == 0:
            return "waiting"
    except OSError:
        return "waiting"

    try:
        result = subprocess.run(["systemctl", "is-active", unit],
                                capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return "up" if result.returncode == 0 else "down"


# ── which pathway a request arrived on ───────────────────────────

LAN = "lan"
OWNER = "owner"


#: What a node id looks like. Matched rather than trusting any non-empty string,
#: because read_node_id() reports "Unknown" rather than failing, and that must
#: not be mistaken for an id we could compare a hostname against.
_NODE_ID_RE = re.compile(r"^ret[0-9a-f]{8}$")


def classify_host(host, node_id, domain):
    """Return LAN or OWNER for the Host header of a request.

    Two pathways, distinguished by name alone, because they are the same service
    on the same port and there is nothing else to tell them apart. cloudflared
    forwards the hostname the request arrived on, so a visitor cannot reach the
    tunnel hostname and present themselves as something else.

    There is deliberately no staff pathway. Offworld engineers reach a node
    through Mender port-forward, which lands with a Host of `localhost` and is
    therefore LAN: unauthenticated, exactly like standing in the house. That is
    why no second hostname, Cloudflare Access application or fleet-wide staff
    password is needed anywhere in this design.

    OWNER is **this node's own** support hostname and nothing else. That is the
    single name node-infra provisions, puts an Access application in front of,
    and points at this node; it is the only name this gate has any business
    challenging.

    It used to be every name under the remote domain, to fail closed against a
    stale record or a stray wildcard. That assumed the only name on the domain
    reaching a node's port 80 would be its own. Untrue: hand-built hostnames
    predate this feature and serve exactly that. They were swept into the remote
    pathway, asked for a Cloudflare Access assertion no Access application
    exists to issue, and sent to a password page that cannot be satisfied
    because no password can be set. Locked out with no way back.

    The old reasoning also overstated the danger. A name under the remote domain
    is one *we* created, in our own zone; it is our mistake to make, not an
    attacker's to exploit. Treating an unrecognised one as LAN restores exactly
    the behaviour it had before this feature existed.

    Still fails closed where it genuinely cannot tell: with no node_id there is
    no way to recognise our own hostname, so the old domain-wide rule applies
    rather than waving the real support hostname through unauthenticated.
    """
    host = (host or "").split(":")[0].strip().rstrip(".").lower()
    domain = (domain or "").strip().rstrip(".").lower()
    if not host or not domain:
        return LAN

    # Validated by shape, not merely by being non-empty. read_node_id() returns
    # the string "Unknown" when /data/mender cannot be read, which is truthy: a
    # bare emptiness check would take that as a real id, fail to match the
    # hostname, and hand the actual support hostname out as LAN, unauthenticated.
    node_id = (node_id or "").strip().rstrip(".").lower()
    if not _NODE_ID_RE.match(node_id):
        # Cannot identify our own hostname, so fall back to the blunt rule
        # rather than risk serving the support hostname to anyone who asks.
        return OWNER if host == domain or host.endswith("." + domain) else LAN

    return OWNER if host == f"{node_id}.{domain}" else LAN


def requires_presence(path):
    """True for operations refused on the owner pathway (see the module docstring)."""
    return (path or "").startswith(PRESENCE_REQUIRED_PREFIXES)
