from flask import Flask
from flask_wtf.csrf import CSRFProtect
import os
import subprocess
import sys

from config_manager import ConfigManager
from device_state import DeviceState
from mender import MenderClient
from network_manager import NetworkManager
from ssh_keys import SSHKeyManager

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = Flask(__name__,
            template_folder=os.path.join(PROJECT_ROOT, 'templates'),
            static_folder=os.path.join(PROJECT_ROOT, 'static'))
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(32).hex())
csrf = CSRFProtect(app)

DEV_MODE = os.environ.get('DEV_MODE', '').lower() in ('1', 'true', 'yes')

# Configurable paths - override via environment for local dev.
# In dev mode, default to a writable local directory instead of the on-device paths.
DATA_DIR = os.environ.get('DATA_DIR',
    os.path.join(PROJECT_ROOT, 'dev_data') if DEV_MODE else '/data/retina-gui'
)
USER_CONFIG_PATH = os.environ.get('USER_CONFIG_PATH',
    os.path.join(PROJECT_ROOT, 'dev_data', 'user.yml') if DEV_MODE else '/data/retina-node/config/user.yml'
)
MERGED_CONFIG_PATH = os.environ.get('MERGED_CONFIG_PATH',
    os.path.join(PROJECT_ROOT, 'dev_data', 'config.yml') if DEV_MODE else '/data/retina-node/config/config.yml'
)
RETINA_NODE_PATH = os.environ.get('RETINA_NODE_PATH', '/data/mender-docker-compose/current/manifests')
RETINA_SPECTRUM_URL = os.environ.get('RETINA_SPECTRUM_URL', 'http://localhost:3020')
NODE_ID_FILE = os.environ.get('NODE_ID_FILE', '/data/mender/node_id')
TOWER_FINDER_URL = os.environ.get('TOWER_FINDER_URL', 'https://tower-finder.retina.fm')
# blah2_api runs with network_mode: host and listens directly on this port —
# NOT the :8080 blah2_host nginx proxy, which doesn't forward /capture/* at all.
BLAH2_API_URL = os.environ.get('BLAH2_API_URL', 'http://localhost:3000')
# Empty = telemetry disabled; set to the config-snapshot ingest endpoint to
# enable. Renamed from CALIBRATION_TELEMETRY_URL — telemetry now covers every
# config-apply/mode-switch action, not just Auto-Calibrate.
CONFIG_TELEMETRY_URL = os.environ.get('CONFIG_TELEMETRY_URL', '')
# retina-tracker sidecar (network_mode: host, see retina-node's docker-compose.yml)
RETINA_TRACKER_HOST = os.environ.get('RETINA_TRACKER_HOST', 'localhost')
RETINA_TRACKER_PORT = int(os.environ.get('RETINA_TRACKER_PORT', '30100'))
# Path the sidecar streams JSONL track events to (-s flag, see its compose
# command) — tailed rather than read over the TCP socket, since retina-tracker's
# --tcp mode is input-only (see retina_tracker_client.py's module docstring).
RETINA_TRACKER_EVENTS_PATH = os.environ.get('RETINA_TRACKER_EVENTS_PATH',
    os.path.join(PROJECT_ROOT, 'dev_data', 'retina-tracker-events.jsonl') if DEV_MODE
    else '/data/retina-node/retina-tracker/output/events.jsonl'
)
DEV_MODE = os.environ.get('DEV_MODE', '').lower() in ('1', 'true', 'yes')

# Shared services
ssh_keys = SSHKeyManager(os.path.join(DATA_DIR, "authorized_keys"))
network_mgr = NetworkManager(dev_mode=DEV_MODE)

config_mgr = ConfigManager(
    user_config_path=USER_CONFIG_PATH,
    merged_config_path=MERGED_CONFIG_PATH,
    retina_node_path=RETINA_NODE_PATH,
)

mender = MenderClient(
    server_url=os.environ.get('MENDER_SERVER_URL', 'https://hosted.mender.io'),
    release_name=os.environ.get('MENDER_RELEASE_NAME', 'retina-node'),
    device_type=os.environ.get('MENDER_DEVICE_TYPE', 'pi5-v3-arm64'),
    dev_mode=DEV_MODE,
    dev_data_dir=DATA_DIR,
)

MENDER_SERVICES = ["mender-authd", "mender-updated", "mender-connect"]

device_state = DeviceState(
    data_dir=DATA_DIR,
    mender_services=MENDER_SERVICES,
    mender_conf_path="/data/mender/mender.conf",
    mender_conf_backup_dir="/data/mender-cloud-disabled",
    mender_conf_backup_path="/data/mender-cloud-disabled/mender.conf",
    dev_mode=DEV_MODE,
)

if not DEV_MODE:
    device_state.apply_startup_preferences()

# Always boot into radar mode — delete any persisted spectrum state
try:
    os.remove(os.path.join(DATA_DIR, 'mode.txt'))
except OSError:
    pass

# A calibration run cannot survive a GUI restart — any lock left behind is stale
device_state.release_calibration_lock()

# Enforce radar at the Docker level: stop and remove retina-spectrum if it is running.
# retina-spectrum is only allowed while the wizard location step or config toggle is active.
if config_mgr.is_retina_node_installed():
    try:
        subprocess.run(['docker', 'compose', '-p', 'retina-node', 'stop', 'retina-spectrum'],
                       cwd=RETINA_NODE_PATH, capture_output=True, timeout=60)
        subprocess.run(['docker', 'compose', '-p', 'retina-node', 'rm', '-sf', 'retina-spectrum'],
                       cwd=RETINA_NODE_PATH, capture_output=True, timeout=30)
    except Exception:
        pass

# Same for sdrconnect.service — never leave a node stuck serving SDRconnect
# after a GUI restart.
try:
    subprocess.run(['systemctl', 'stop', 'sdrconnect.service'], capture_output=True, timeout=30)
except Exception:
    pass


from blah2_client import Blah2Client
from calibrator import Calibrator
from retina_tracker_client import RetinaTrackerClient
from tracker_capture import TrackerCaptureService

blah2_client = Blah2Client(BLAH2_API_URL)
calibrator = Calibrator(blah2_client)
retina_tracker_client = RetinaTrackerClient(
    RETINA_TRACKER_HOST, RETINA_TRACKER_PORT, RETINA_TRACKER_EVENTS_PATH)
tracker_capture = TrackerCaptureService(blah2_client, retina_tracker_client)


def _on_calibration_complete(status):
    """Runs on the calibration thread when a run reaches a terminal state.

    No telemetry here — a config snapshot is only meaningful once a result is
    actually persisted (see routes/calibrate.py's apply(), which triggers one
    via run_config_merger_and_restart), not on every terminal state including
    failures/cancels that change nothing on disk.
    """
    device_state.release_calibration_lock()


calibrator.on_complete = _on_calibration_complete

# Never auto-start under pytest: conftest.py's app_client fixture reloads this
# module per-test, and start() spawns a permanent, never-stopped background
# thread — under pytest that would leak one such thread per test (each making
# real requests.get() calls that can race with any test mocking requests
# globally).
if "pytest" not in sys.modules:
    tracker_capture.start()


def get_node_id():
    """Get node_id from Mender device identity file."""
    try:
        with open(NODE_ID_FILE, 'r') as f:
            node_id = f.read().strip()
            if node_id:
                return node_id
    except FileNotFoundError:
        app.logger.debug(f"Node ID file not found: {NODE_ID_FILE}")
    except Exception as e:
        app.logger.warning(f"Could not read node_id from {NODE_ID_FILE}: {e}")
    return 'Unknown'


# Inject common template variables (navbar, footer)
@app.context_processor
def inject_globals():
    owl_os_version, retina_node_version = mender.get_versions()
    return {
        'node_id': get_node_id(),
        'owl_os_version': owl_os_version,
        'retina_node_version': retina_node_version,
    }


# Register blueprints
from routes.home import bp as home_bp
from routes.config import bp as config_bp
from routes.mender_routes import bp as mender_bp
from routes.setup import bp as setup_bp
from routes.towers import bp as towers_bp
from routes.mode import bp as mode_bp
from routes.network import bp as network_bp
from routes.calibrate import bp as calibrate_bp
from routes.tracker_preview import bp as tracker_preview_bp

app.register_blueprint(home_bp)
app.register_blueprint(config_bp)
app.register_blueprint(mender_bp)
app.register_blueprint(setup_bp)
app.register_blueprint(towers_bp)
app.register_blueprint(mode_bp)
app.register_blueprint(network_bp)
app.register_blueprint(calibrate_bp)
app.register_blueprint(tracker_preview_bp)


if __name__ == "__main__":
    port = int(os.environ.get('PORT', 80))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host="::", port=port, debug=debug)
