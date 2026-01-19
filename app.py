from flask import Flask, render_template, request, redirect, url_for
import os
import re
import tempfile
import yaml

from config_schema import CaptureConfig
from form_utils import schema_to_form_fields

app = Flask(__name__)

# Configurable paths - override via environment for local dev
DATA_DIR = os.environ.get('DATA_DIR', '/data/retina-gui')
USER_CONFIG_PATH = os.environ.get('USER_CONFIG_PATH', '/data/retina-node/config/user.yml')
RETINA_NODE_PATH = os.environ.get('RETINA_NODE_PATH', '/data/mender-app/retina-node/manifests')

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


# ============================================================================
# Config Management
# ============================================================================

def is_retina_node_installed():
    """Check if retina-node stack is deployed."""
    return os.path.exists(os.path.join(RETINA_NODE_PATH, 'docker-compose.yaml'))


def load_user_config():
    """Load user config from YAML file."""
    if not os.path.exists(USER_CONFIG_PATH):
        return {}
    with open(USER_CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def save_user_config(config):
    """Save user config to YAML file (atomic write)."""
    config_dir = os.path.dirname(USER_CONFIG_PATH)
    os.makedirs(config_dir, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=config_dir)
    with os.fdopen(fd, 'w') as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
    os.chmod(tmp_path, 0o644)
    os.rename(tmp_path, USER_CONFIG_PATH)


def parse_form_to_nested_dict(form_data):
    """
    Convert flat form data with dot notation to nested dict.
    e.g. {'capture.fs': '2000000', 'capture.device.type': 'RspDuo'}
    becomes {'capture': {'fs': 2000000, 'device': {'type': 'RspDuo'}}}
    """
    result = {}
    for key, value in form_data.items():
        parts = key.split('.')
        current = result
        for part in parts[:-1]:
            if part not in current:
                current[part] = {}
            current = current[part]

        # Convert types
        final_key = parts[-1]
        if value == '':
            continue  # Skip empty values
        elif value.isdigit() or (value.startswith('-') and value[1:].isdigit()):
            current[final_key] = int(value)
        elif value.lower() in ('true', 'false', 'on'):
            current[final_key] = value.lower() in ('true', 'on')
        else:
            current[final_key] = value

    return result


@app.route("/")
def index():
    keys = get_ssh_keys()
    config = load_user_config()
    retina_installed = is_retina_node_installed()

    # Generate form fields from Pydantic schema
    capture_values = config.get('capture', {}) or {}
    capture_fields = schema_to_form_fields(CaptureConfig, capture_values)

    return render_template("index.html",
                           ssh_keys=keys,
                           config=config,
                           retina_installed=retina_installed,
                           capture_fields=capture_fields)

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


@app.route("/config", methods=["POST"])
def save_config():
    """Save config form data to user.yml."""
    # Parse form data to nested dict
    form_dict = parse_form_to_nested_dict(request.form.to_dict())

    # Handle unchecked checkboxes (they don't get submitted)
    # We need to explicitly set them to False
    if 'capture' in form_dict and 'device' in form_dict['capture']:
        device = form_dict['capture']['device']
        if 'dabNotch' not in device:
            device['dabNotch'] = False
        if 'rfNotch' not in device:
            device['rfNotch'] = False

    # Load existing config and merge (preserves fields not in form)
    existing = load_user_config()
    if 'capture' in form_dict:
        existing['capture'] = form_dict['capture']

    save_user_config(existing)
    return redirect(url_for("index"))


if __name__ == "__main__":
    port = int(os.environ.get('PORT', 80))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host="::", port=port, debug=debug)
