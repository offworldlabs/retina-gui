"""The node's friendly name: what an operator calls it, rather than its id.

A display label only, stored on /data and advertised to other nodes in the
`_owl-node._tcp` TXT record. Re-advertising is delegated to owl-mdns-identity
(owl-os), the script that writes the service file at boot.
See docs/features/fleet-and-naming.md#friendly-names.
"""

import os
import re
import subprocess
import tempfile

# Has to fit on a card and inside a TXT record.
MAX_LENGTH = 48

# Refused rather than escaped: nothing that could break out of the
# line-oriented name file or the identity script's XML. owl-mdns-identity's own
# escaping is the second line of defence, for a file edited by hand.
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
        """Return (ok, error). An empty name is valid: it means "unset"."""
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

        Best-effort: the name is already saved and the boot run picks it up,
        so a failure only delays the rename reaching other nodes.
        """
        if self.dev_mode or not os.path.exists(self.identity_script):
            return
        try:
            subprocess.run([self.identity_script], capture_output=True,
                           timeout=30, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"node_name: could not re-advertise: {e}", flush=True)
