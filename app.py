from flask import Flask, render_template, request, redirect, url_for
import os
import re
import tempfile

app = Flask(__name__)

DATA_DIR = "/data/retina-gui"
AUTH_KEYS_FILE = os.path.join(DATA_DIR, "authorized_keys")

# Valid SSH key types (exact match to prevent prefix tricks)
VALID_KEY_TYPES = (
    'ssh-rsa', 'ssh-ed25519', 'ssh-dss',
    'ecdsa-sha2-nistp256', 'ecdsa-sha2-nistp384', 'ecdsa-sha2-nistp521',
    'sk-ssh-ed25519@openssh.com', 'sk-ecdsa-sha2-nistp256@openssh.com'
)


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


def get_ssh_keys():
    """Read current SSH keys from file."""
    if not os.path.exists(AUTH_KEYS_FILE):
        return []
    with open(AUTH_KEYS_FILE) as f:
        return [line.strip() for line in f if line.strip()]

def add_ssh_key(key):
    """Add SSH key to file (atomic write)."""
    os.makedirs(DATA_DIR, exist_ok=True)

    # Read existing keys
    keys = get_ssh_keys()
    if key in keys:
        return  # Already exists

    keys.append(key)

    # Atomic write: write to temp file, then rename
    fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR)
    with os.fdopen(fd, 'w') as f:
        f.write('\n'.join(keys) + '\n')
    os.chmod(tmp_path, 0o644)  # World-readable so sshd can read for any user
    os.rename(tmp_path, AUTH_KEYS_FILE)


def remove_ssh_key(key_to_remove):
    """Remove SSH key from file (atomic write)."""
    keys = [k for k in get_ssh_keys() if k != key_to_remove]

    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR)
    with os.fdopen(fd, 'w') as f:
        if keys:
            f.write('\n'.join(keys) + '\n')
    os.chmod(tmp_path, 0o644)
    os.rename(tmp_path, AUTH_KEYS_FILE)


@app.route("/")
def index():
    keys = get_ssh_keys()
    return render_template("index.html", ssh_keys=keys)

@app.route("/ssh-keys", methods=["POST"])
def add_key():
    key = request.form.get("ssh_key", "").strip()
    if key and is_valid_ssh_key(key):
        add_ssh_key(key)
        return redirect(url_for("index"))
    else:
        keys = get_ssh_keys()
        return render_template("index.html", ssh_keys=keys,
                               error="Invalid SSH key format")


@app.route("/ssh-keys/delete", methods=["POST"])
def delete_key():
    key = request.form.get("ssh_key", "")
    if key:
        remove_ssh_key(key)
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80, debug=False)
