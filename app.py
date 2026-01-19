from flask import Flask, render_template, request, redirect, url_for, jsonify
import os
import re
import subprocess
import tempfile
import yaml

from pydantic import ValidationError
from config_schema import CaptureConfig, LocationConfig, TruthConfig, Tar1090Config
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
        elif value.lower() in ('true', 'false', 'on'):
            current[final_key] = value.lower() in ('true', 'on')
        else:
            # Try to parse as number (int or float)
            try:
                if '.' not in value:
                    current[final_key] = int(value)
                else:
                    current[final_key] = float(value)
            except ValueError:
                # Keep as string
                current[final_key] = value

    return result


@app.route("/")
def index():
    """Home page with node ID, services, and SSH keys."""
    keys = get_ssh_keys()
    config = load_user_config()

    # Get node_id from config
    node_id = config.get('network', {}).get('node_id')

    return render_template("index.html",
                           ssh_keys=keys,
                           node_id=node_id)


def parse_tar1090_adsb_source(config):
    """Split adsb_source string into separate fields for the form."""
    tar1090 = config.get('tar1090', {}) or {}
    adsb_source = tar1090.get('adsb_source', '')

    if adsb_source and ',' in adsb_source:
        parts = adsb_source.split(',', 2)
        return {
            'adsb_source_host': parts[0] if len(parts) > 0 else '',
            'adsb_source_port': int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None,
            'adsb_source_protocol': parts[2] if len(parts) > 2 else '',
            'adsblol_fallback': tar1090.get('adsblol_fallback'),
            'adsblol_radius': tar1090.get('adsblol_radius'),
        }
    return {
        'adsb_source_host': None,
        'adsb_source_port': None,
        'adsb_source_protocol': None,
        'adsblol_fallback': tar1090.get('adsblol_fallback'),
        'adsblol_radius': tar1090.get('adsblol_radius'),
    }


@app.route("/config")
def config_page():
    """Configuration page with all settings."""
    config = load_user_config()
    retina_installed = is_retina_node_installed()

    # Generate form fields from Pydantic schemas
    capture_values = config.get('capture', {}) or {}
    capture_fields = schema_to_form_fields(CaptureConfig, capture_values)

    location_values = config.get('location', {}) or {}
    location_fields = schema_to_form_fields(LocationConfig, location_values)

    truth_values = config.get('truth', {}) or {}
    truth_fields = schema_to_form_fields(TruthConfig, truth_values)

    # tar1090 needs special handling for adsb_source split
    tar1090_values = parse_tar1090_adsb_source(config)
    tar1090_fields = schema_to_form_fields(Tar1090Config, tar1090_values)

    return render_template("config.html",
                           retina_installed=retina_installed,
                           capture_fields=capture_fields,
                           location_fields=location_fields,
                           truth_fields=truth_fields,
                           tar1090_fields=tar1090_fields)

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


def format_validation_errors(validation_error, section_prefix):
    """Convert Pydantic ValidationError to dict of field -> error message."""
    errors = {}
    for error in validation_error.errors():
        # Build field path like "capture.device.gainReduction"
        field_path = section_prefix + '.' + '.'.join(str(loc) for loc in error['loc'])
        errors[field_path] = error['msg']
    return errors


def handle_unchecked_checkboxes(form_dict):
    """Set unchecked checkboxes to False (they don't get submitted)."""
    # Capture checkboxes
    if 'capture' in form_dict and 'device' in form_dict['capture']:
        device = form_dict['capture']['device']
        if 'dabNotch' not in device:
            device['dabNotch'] = False
        if 'rfNotch' not in device:
            device['rfNotch'] = False

    # Truth.adsb checkbox
    if 'truth' in form_dict and 'adsb' in form_dict['truth']:
        adsb = form_dict['truth']['adsb']
        if 'enabled' not in adsb:
            adsb['enabled'] = False

    # tar1090 checkbox
    if 'tar1090' in form_dict:
        tar1090 = form_dict['tar1090']
        if 'adsblol_fallback' not in tar1090:
            tar1090['adsblol_fallback'] = False


def join_tar1090_adsb_source(form_dict):
    """Join the 3 adsb_source fields into a single comma-separated string."""
    if 'tar1090' in form_dict:
        tar1090 = form_dict['tar1090']
        host = tar1090.pop('adsb_source_host', '')
        port = tar1090.pop('adsb_source_port', '')
        protocol = tar1090.pop('adsb_source_protocol', '')
        if host or port or protocol:
            tar1090['adsb_source'] = f"{host},{port},{protocol}"


@app.route("/config/save", methods=["POST"])
def save_config():
    """Save config form data to user.yml."""
    # Parse form data to nested dict
    form_dict = parse_form_to_nested_dict(request.form.to_dict())

    # Handle unchecked checkboxes
    handle_unchecked_checkboxes(form_dict)

    # Collect all validation errors
    all_errors = {}

    # Validate capture
    capture_data = form_dict.get('capture', {})
    if capture_data:
        try:
            CaptureConfig(**capture_data)
        except ValidationError as e:
            all_errors.update(format_validation_errors(e, 'capture'))

    # Validate location
    location_data = form_dict.get('location', {})
    if location_data:
        try:
            LocationConfig(**location_data)
        except ValidationError as e:
            all_errors.update(format_validation_errors(e, 'location'))

    # Validate truth
    truth_data = form_dict.get('truth', {})
    if truth_data:
        try:
            TruthConfig(**truth_data)
        except ValidationError as e:
            all_errors.update(format_validation_errors(e, 'truth'))

    # Validate tar1090 (before joining adsb_source)
    tar1090_data = form_dict.get('tar1090', {})
    if tar1090_data:
        try:
            Tar1090Config(**tar1090_data)
        except ValidationError as e:
            all_errors.update(format_validation_errors(e, 'tar1090'))

    # If validation errors, re-render form
    if all_errors:
        config = load_user_config()
        return render_template("config.html",
                               retina_installed=is_retina_node_installed(),
                               capture_fields=schema_to_form_fields(CaptureConfig, capture_data),
                               location_fields=schema_to_form_fields(LocationConfig, location_data),
                               truth_fields=schema_to_form_fields(TruthConfig, truth_data),
                               tar1090_fields=schema_to_form_fields(Tar1090Config, tar1090_data),
                               config_errors=all_errors)

    # Join tar1090 adsb_source fields before saving
    join_tar1090_adsb_source(form_dict)

    # Load existing config and merge (preserves fields not in form)
    existing = load_user_config()
    if 'capture' in form_dict:
        existing['capture'] = form_dict['capture']
    if 'location' in form_dict:
        existing['location'] = form_dict['location']
    if 'truth' in form_dict:
        existing['truth'] = form_dict['truth']
    if 'tar1090' in form_dict:
        existing['tar1090'] = form_dict['tar1090']

    save_user_config(existing)
    return redirect(url_for("config_page"))


@app.route("/config/apply", methods=["POST"])
def apply_config():
    """Run config-merger and restart services."""
    if not is_retina_node_installed():
        return jsonify({"success": False, "error": "retina-node not installed"}), 400

    try:
        # Run config-merger to merge user.yml with defaults
        result = subprocess.run(
            ["docker", "compose", "-p", "retina-node", "run", "--rm", "config-merger"],
            cwd=RETINA_NODE_PATH,
            capture_output=True,
            text=True,
            timeout=60
        )
        if result.returncode != 0:
            return jsonify({
                "success": False,
                "error": f"config-merger failed: {result.stderr or result.stdout}"
            }), 500

        # Restart services with new config
        result = subprocess.run(
            ["docker", "compose", "-p", "retina-node", "up", "-d", "--force-recreate"],
            cwd=RETINA_NODE_PATH,
            capture_output=True,
            text=True,
            timeout=120
        )
        if result.returncode != 0:
            return jsonify({
                "success": False,
                "error": f"restart failed: {result.stderr or result.stdout}"
            }), 500

        return jsonify({"success": True})

    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "error": "Command timed out"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    port = int(os.environ.get('PORT', 80))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host="::", port=port, debug=debug)
