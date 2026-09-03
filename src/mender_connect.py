"""Enforcing the remote shell agreement, by editing what mender-connect will do.

The owner can decline interactive access without declining anything else. That
is enforced here, on the node, by turning off two of mender-connect's features
and restarting it:

    Terminal      the remote shell Mender's dashboard offers
    PortForward   forwarding a local port to one on the node

Both have to go. Disabling only the terminal leaves `port-forward 2222:localhost:22`
reaching sshd directly, and a port-forward also arrives at this GUI with a Host
of `localhost`, which classifies as the local network and so bypasses the
restrictions that stop the support pathway granting SSH keys.

## What is deliberately left alone

`FileTransfer` stays enabled. It is how node-infra delivers and clears the
Cloudflare tunnel token, so gating it here would make the two agreements
dependent on each other: a node whose owner had declined the shell could never
receive a support tunnel. It cannot be used to obtain a shell, because sshd
trusts only /data/retina-gui/authorized_keys, which is root-owned and outside
the chroot Mender file transfer writes into.

`MenderClient` stays enabled, and mender-authd and mender-updated are untouched
entirely. Enrolment, OTA updates and inventory reporting continue whatever the
owner decides. They live in different daemons that share no process or
dependency with mender-connect, which is what makes that promise structural
rather than a matter of care.

## Why this refuses rather than repairs

A missing or unparseable config is not rewritten from defaults. The file
carries file-transfer limits, the shell user and session caps that are not ours
to reconstruct, and writing a plausible-looking replacement could silently widen
what a session may do. Refusing leaves the node exactly as it was, which is the
safer failure.
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

#: The two features the agreement governs. Order is not significant; both are
#: written together so the file can never describe a half-applied state.
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

        None is a distinct answer on purpose. "We cannot tell" and "the owner
        declined" look the same to a boolean and mean very different things to
        anyone deciding whether the agreement is being honoured.
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

        Writes both features together and restarts the service, so the running
        daemon and the file on disk always agree.
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
            # Restarted rather than reloaded: mender-connect reads its config at
            # startup only, so a reload would leave the daemon serving the
            # previous answer while the file claimed otherwise.
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

        Carried over rather than chosen: the image ships this file 0644 root,
        and tightening it here would be an unrelated change smuggled in behind
        a setting the owner toggled. DEFAULT_CONF_MODE only applies when there
        is no existing file to copy, which off-device is the normal case.
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
