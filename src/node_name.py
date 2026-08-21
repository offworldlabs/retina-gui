"""The node's friendly name — what an operator calls it, rather than its id.

`ret4c844c20` is stable and unambiguous, which is exactly why it is the mDNS
name, but a fleet page listing several of them is not something anyone can
navigate. This is the label shown on the card instead, with the id underneath.

Stored on /data so it survives an OS update, alongside the SSH keys and the
telemetry node_ref cache, and advertised to the other nodes in the
`_owl-node._tcp` TXT record.

Rewriting that advertisement is deliberately delegated to owl-mdns-identity
(owl-os) rather than done here. It is the same script that writes the file at
boot, so there is one place that knows the format and one place doing the XML
escaping; running it again is how a rename reaches the network. avahi-daemon
watches /etc/avahi/services and reloads on its own, so nothing has to be
restarted and the new name is live within a second or two.
"""

import os
import re
import subprocess
import tempfile

# Kept short because it has to fit on a card and inside a TXT record, and
# because a name long enough to need scrolling is not doing its job.
MAX_LENGTH = 48

# Anything printable, but nothing that could break out of the line-oriented
# name file or the XML the identity script builds around it. Control
# characters, newlines and angle brackets are all refused here rather than
# escaped, because there is no legitimate node name that needs them — the
# escaping in owl-mdns-identity is the second line of defence, for a file
# edited by hand over SSH.
_FORBIDDEN = re.compile(r'[\x00-\x1f\x7f<>&"\']')

IDENTITY_SCRIPT = "/usr/local/sbin/owl-mdns-identity"


class NodeName:
    """Reads and writes the operator-assigned name for this node."""

    def __init__(self, name_file, dev_mode=False,
                 identity_script=IDENTITY_SCRIPT):
        self.name_file = name_file
        self.data_dir = os.path.dirname(name_file)
        self.dev_mode = dev_mode
        self.identity_script = identity_script

    def get(self):
        """The current name, or "" when the operator has not set one."""
        try:
            with open(self.name_file) as f:
                return f.read().strip()
        except OSError:
            return ""

    @staticmethod
    def validate(name):
        """Return (ok, error). An empty name is valid — it means "unset"."""
        name = (name or "").strip()
        if len(name) > MAX_LENGTH:
            return False, f"Name must be {MAX_LENGTH} characters or fewer"
        if _FORBIDDEN.search(name):
            return False, "Name cannot contain control characters or < > & \" '"
        return True, None

    def set(self, name):
        """Store the name and re-advertise it. Returns (ok, error)."""
        name = (name or "").strip()
        ok, error = self.validate(name)
        if not ok:
            return False, error

        try:
            os.makedirs(self.data_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=self.data_dir)
            with os.fdopen(fd, "w") as f:
                f.write(name)
            os.chmod(tmp_path, 0o644)
            os.rename(tmp_path, self.name_file)
        except OSError as e:
            return False, f"Could not save the name: {e}"

        self._republish()
        return True, None

    def _republish(self):
        """Ask owl-mdns-identity to rewrite the DNS-SD advertisement.

        Best-effort. The name is already saved at this point, and the boot run
        of the same script will pick it up regardless, so a failure here delays
        the rename reaching other nodes rather than losing it.
        """
        if self.dev_mode or not os.path.exists(self.identity_script):
            return
        try:
            subprocess.run([self.identity_script], capture_output=True,
                           timeout=30, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"node_name: could not re-advertise: {e}", flush=True)
