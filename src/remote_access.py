"""Remote access: the support-access toggle, the shell agreement, and pathway classification.

The LAN is unauthenticated. The tunnel hostname is the only other pathway, and
the gate in app.py challenges it. See docs/features/remote-access.md.
"""

import json
import os
import re
import secrets
import subprocess
import tempfile
import time

# The password is stored in plaintext and compared in constant time. There is no
# rate limit in this process: the one at Cloudflare's edge is not optional.
# See docs/features/remote-access.md#owner-password.

#: The WPA2 hotspot minimum. Short on purpose; the edge rate limit is what makes
#: it safe. See docs/features/remote-access.md#rules.
MIN_PASSWORD_LENGTH = 8

#: Where owl-os keeps the connector token, and the unit that consumes it. Read
#: only to report whether the tunnel is actually up; nothing here writes either.
TUNNEL_TOKEN_PATH = "/data/cloudflared/tunnel-token"
CLOUDFLARED_UNIT = "cloudflared.service"

#: The file owl-os's mender-inventory-retina-remote-access script looks for.
#: Matched pair with owl-os: presence means enabled, and it is the whole
#: node-to-server channel for this feature.
#: See docs/features/remote-access.md#the-node-to-server-channel.
ENABLED_MARKER = "remote-access-enabled"

#: The remote shell agreement, recorded by its exception: presence means
#: declined, so a missing or unwritable /data never revokes access.
#: See docs/features/remote-access.md#recorded-by-exception.
SHELL_DISABLED_MARKER = "remote-shell-disabled"

#: No 0/O, 1/l/I: generated passwords get read aloud and copied off screens.
_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"

#: Four groups of four, hyphenated: about 79 bits.
_GENERATED_GROUPS = 4
_GENERATED_GROUP_LEN = 4

#: Refused on the owner (tunnel) pathway even with a valid session.
#: Prefix-matched. Security invariant: a remote visitor must not be able to
#: outlive their access (an SSH key), change the owner's remote access controls,
#: or push firmware. See docs/features/remote-access.md#device-presence-tier.
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

        TEMPORARY: defaults ON, to be reverted (drop the `True` below).
        See docs/features/remote-access.md#defaults-to-on-temporary.
        """
        return bool(self._read().get("enabled", True))

    def has_password(self):
        return bool(self._read().get("password"))

    # ── the remote shell agreement ───────────────────────────────

    def is_shell_allowed(self):
        """Whether the owner permits interactive access over Mender.

        Independent of the support access toggle.
        """
        return not os.path.exists(
            os.path.join(self.data_dir, SHELL_DISABLED_MARKER))

    def record_shell_allowed(self, allowed):
        """Record the owner's choice. Returns (ok, error).

        Records only. The caller must enforce via mender_connect FIRST so this
        never claims a state the node is not in.
        See docs/features/remote-access.md#order-enforce-then-record.
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
                # World readable: read by the inventory scripts, holds nothing.
                with open(marker, "w") as f:
                    f.write("")
                os.chmod(marker, 0o644)
        except OSError as e:
            return False, f"Could not record the remote shell setting: {e}"
        return True, None

    def get_password(self):
        """The password itself, for display. "" when none is set.

        Kept out of status() on purpose, so the password cannot ride along into
        a template or JSON response on the remote pathway.
        """
        return self._read().get("password") or ""

    def status(self):
        """What the config page needs to render the section. Never the password."""
        state = self._read()
        return {
            # is_enabled(), not the raw field, so the default lives in one place.
            "enabled": self.is_enabled(),
            "has_password": bool(state.get("password")),
            "shell_allowed": self.is_shell_allowed(),
            "updated_at": state.get("updated_at"),
        }

    # ── verifying ────────────────────────────────────────────────

    def verify(self, password):
        """Check a submitted password, in constant time. False when none is set.

        Encoded first because compare_digest refuses non-ASCII str.
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
            # 0600 before any content is written, so the plaintext password is
            # never world-readable.
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

        An untouched node has no state file and so never had a marker written.
        See docs/features/remote-access.md#defaults-to-on-temporary.
        """
        self._sync_marker()

    def _sync_marker(self):
        """Make the inventory marker agree with is_enabled().

        Called after every write, not just the toggle. Best effort: a missing
        marker costs one inventory cycle, a refused save loses the owner's input.
        """
        marker = os.path.join(self.data_dir, ENABLED_MARKER)
        try:
            if self.is_enabled():
                # World readable: read by the inventory scripts, holds nothing.
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

    cloudflared is Type=notify, so an active unit means connected to the edge.
    See docs/features/remote-access.md#tunnel-status.

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


#: What a node id looks like. read_node_id() can return "Unknown", which must not
#: pass as an id. See docs/features/remote-access.md#why-the-node-id-is-validated-by-shape.
_NODE_ID_RE = re.compile(r"^ret[0-9a-f]{8}$")


def classify_host(host, node_id, domain):
    """Return LAN or OWNER for the Host header of a request.

    OWNER is exactly this node's own tunnel hostname (ret<node_id>.<domain>);
    everything else is LAN, including Mender port-forwards (Host: localhost).
    Fails closed to the whole domain when node_id is not a valid id.
    See docs/features/remote-access.md#classifying-a-request.
    """
    host = (host or "").split(":")[0].strip().rstrip(".").lower()
    domain = (domain or "").strip().rstrip(".").lower()
    if not host or not domain:
        return LAN

    # Security invariant: validate by shape, never by non-emptiness. "Unknown"
    # would otherwise serve the real support hostname as LAN, unauthenticated.
    node_id = (node_id or "").strip().rstrip(".").lower()
    if not _NODE_ID_RE.match(node_id):
        # Cannot identify our own hostname, so fall back to the blunt rule
        # rather than risk serving the support hostname to anyone who asks.
        return OWNER if host == domain or host.endswith("." + domain) else LAN

    return OWNER if host == f"{node_id}.{domain}" else LAN


def requires_presence(path):
    """True for operations refused on the owner pathway (see PRESENCE_REQUIRED_PREFIXES)."""
    return (path or "").startswith(PRESENCE_REQUIRED_PREFIXES)
