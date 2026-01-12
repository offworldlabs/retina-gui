from flask import Flask, render_template, request, redirect, url_for
import os
import tempfile

app = Flask(__name__)

DATA_DIR = "/data/retina-gui"
AUTH_KEYS_FILE = os.path.join(DATA_DIR, "authorized_keys")

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

@app.route("/")
def index():
    keys = get_ssh_keys()
    return render_template("index.html", ssh_keys=keys)

@app.route("/ssh-keys", methods=["POST"])
def add_key():
    key = request.form.get("ssh_key", "").strip()
    if key and key.startswith(("ssh-", "ecdsa-", "sk-")):
        add_ssh_key(key)
    return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80, debug=False)
