"""SSH key management for retina-gui.

Handles validation, storage, and retrieval of SSH public keys.
Keys are stored in a flat file (one per line), written atomically.
"""

import os
import re
import tempfile


# Valid SSH key types (exact match to prevent prefix tricks)
VALID_KEY_TYPES = (
    'ssh-rsa', 'ssh-ed25519', 'ssh-dss',
    'ecdsa-sha2-nistp256', 'ecdsa-sha2-nistp384', 'ecdsa-sha2-nistp521',
    'sk-ssh-ed25519@openssh.com', 'sk-ecdsa-sha2-nistp256@openssh.com'
)


class SSHKeyManager:
    """Manages SSH public keys in an authorized_keys file."""

    def __init__(self, keys_file):
        self.keys_file = keys_file
        self.data_dir = os.path.dirname(keys_file)

    @staticmethod
    def is_valid_ssh_key(key):
        """Validate SSH public key format."""
        # No newlines (prevents injection of extra keys)
        if '\n' in key or '\r' in key:
            return False

        # Length limit (RSA 4096 is ~750 chars, give headroom for comments)
        if len(key) > 2000:
            return False

        # Reject shell metacharacters anywhere (paranoid but safe)
        shell_chars = ['|', ';', '&', '$', '`', '(', ')', '{', '}', '<', '>', '!', '#']
        if any(c in key for c in shell_chars):
            return False

        # Must have: type base64 [comment]
        parts = key.split()
        if len(parts) < 2:
            return False

        key_type = parts[0]
        key_data = parts[1]

        # Whitelist valid key types
        if key_type not in VALID_KEY_TYPES:
            return False

        # Key data must be valid base64 (alphanumeric + / + = padding)
        if not re.match(r'^[A-Za-z0-9+/]+=*$', key_data):
            return False

        return True

    def get_keys(self):
        """Read current SSH keys from file."""
        if not os.path.exists(self.keys_file):
            return []
        with open(self.keys_file) as f:
            return [line.strip() for line in f if line.strip()]

    def add_key(self, key):
        """Add SSH key to file (atomic write)."""
        os.makedirs(self.data_dir, exist_ok=True)

        keys = self.get_keys()
        if key in keys:
            return  # Already exists

        keys.append(key)

        # Atomic write: write to temp file, then rename
        fd, tmp_path = tempfile.mkstemp(dir=self.data_dir)
        with os.fdopen(fd, 'w') as f:
            f.write('\n'.join(keys) + '\n')
        os.chmod(tmp_path, 0o644)  # World-readable so sshd can read for any user
        os.rename(tmp_path, self.keys_file)

    def remove_key(self, key_to_remove):
        """Remove SSH key from file (atomic write)."""
        keys = [k for k in self.get_keys() if k != key_to_remove]

        os.makedirs(self.data_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=self.data_dir)
        with os.fdopen(fd, 'w') as f:
            if keys:
                f.write('\n'.join(keys) + '\n')
        os.chmod(tmp_path, 0o644)
        os.rename(tmp_path, self.keys_file)
