"""Enforcing the remote shell agreement, by editing what mender-connect will do.

Turns mender-connect's Terminal and PortForward features off or on and restarts
it. FileTransfer and the other Mender daemons are deliberately left alone.
See docs/features/remote-access.md#remote-shell-agreement.
"""

import json
import os
import stat
import subprocess
import tempfile

DEFAULT_CONF_PATH = "/etc/mender/mender-connect.conf"
SERVICE = "mender-connect"

#: Only used when writing a config where none existed, which on a node never
#: happens. Matches what the OS image ships.
DEFAULT_CONF_MODE = 0o644

#: The two features the agreement governs. Security invariant: both must go,
#: because a port-forward alone reaches sshd and arrives here as LAN.
#: See docs/features/remote-access.md#enforcement.
GATED_FEATURES = ("Terminal", "PortForward")


class MenderConnect:
    """Reads and writes mender-connect's feature switches."""

    def __init__(self, conf_path=DEFAULT_CONF_PATH, service=SERVICE, dev_mode=False):
        self.conf_path = conf_path
        self.service = service
        self.dev_mode = dev_mode

    # ── reading ──────────────────────────────────────────────────

    def _read(self):
        """The parsed config, or None when it cannot be trusted."""
        try:
            with open(self.conf_path) as f:
                conf = json.load(f)
        except (OSError, ValueError):
            return None
        return conf if isinstance(conf, dict) else None

    def is_shell_enabled(self):
        """True, False, or None when the config cannot be read.

        None ("cannot tell") is deliberately distinct from False ("declined").
        """
        conf = self._read()
        if conf is None:
            return None
        return not any(
            bool((conf.get(feature) or {}).get("Disable"))
            for feature in GATED_FEATURES
        )

    # ── writing ──────────────────────────────────────────────────

    def set_shell_enabled(self, enabled):
        """Apply the agreement. Returns (ok, error).

        Writes both features together and restarts the service. Refuses, rather
        than rebuilding from defaults, when the config is missing or unreadable.
        See docs/features/remote-access.md#refuse-rather-than-repair.
        """
        enabled = bool(enabled)

        if self.dev_mode:
            # Nothing to enforce off-device, but still write the file so the
            # config page reflects the choice during development.
            return self._write({feature: {"Disable": not enabled}
                                for feature in GATED_FEATURES})

        conf = self._read()
        if conf is None:
            return False, (f"{self.conf_path} is missing or unreadable, so the "
                           f"remote shell setting cannot be applied")

        for feature in GATED_FEATURES:
            section = conf.get(feature)
            conf[feature] = {**section, "Disable": not enabled} if isinstance(section, dict) \
                else {"Disable": not enabled}

        ok, error = self._write(conf)
        if not ok:
            return False, error

        try:
            # Restart, not reload: mender-connect reads its config only at startup.
            result = subprocess.run(["systemctl", "restart", self.service],
                                    capture_output=True, timeout=30)
        except subprocess.TimeoutExpired:
            return False, f"Timed out restarting {self.service}"
        except OSError as e:
            return False, f"Could not restart {self.service}: {e}"

        if result.returncode != 0:
            detail = (result.stderr or b"").decode(errors="replace").strip()
            return False, f"Could not restart {self.service}: {detail}"
        return True, None

    def _write(self, conf):
        """Replace the config atomically, keeping the mode it already had.

        DEFAULT_CONF_MODE applies only when no file exists (normally off-device).
        """
        directory = os.path.dirname(self.conf_path) or "."
        try:
            mode = stat.S_IMODE(os.stat(self.conf_path).st_mode)
        except OSError:
            mode = DEFAULT_CONF_MODE

        try:
            os.makedirs(directory, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=directory)
            with os.fdopen(fd, "w") as f:
                json.dump(conf, f, indent=2)
                f.write("\n")
            os.chmod(tmp_path, mode)
            os.rename(tmp_path, self.conf_path)
        except OSError as e:
            return False, f"Could not write {self.conf_path}: {e}"
        return True, None
