"""Process-wide configuration and shared service singletons.

Everything here must exist exactly once per process. It lives in its own
module for a specific reason: `app.py` is executed *twice*.

systemd starts it as `python3 src/app.py`, so its module body runs under the
name `__main__`. Route handlers then do `from app import calibrator` at
request time, and because `__main__` and `app` are distinct entries in
sys.modules, Python imports the same file a second time and runs the whole
body again. Anything constructed at app.py's module level therefore exists
twice, in one process.

For stateless helpers that is merely wasteful. For anything owning a socket,
a thread, or in-memory run state it is a bug, and one that bit hard:
retina-tracker's sidecar accepts a single TCP connection at a time, so two
RetinaTrackerClient instances meant two connections, one of which was
accepted and served while the other sat unread in the kernel's backlog
forever. Which instance won was a startup race. When the calibrator held the
losing one, its detection frames and its tracker RESET went into a socket
nobody was reading -- silently, because confirmed-track events arrive over a
*file* tail that kept working, fed by tracker_capture's always-on capture.
Auto-Calibrate appeared to work while its own feed reached nothing, and
credited tracks it had never observed.

Importing these from here fixes that: `services` is only ever reachable under
one name, so it executes once no matter how many times app.py does. app.py
re-exports the names, so `from app import ...` continues to work unchanged.
"""

import os

from apply_service import ApplyService
from blah2_client import Blah2Client
from calibrator import Calibrator
from config_manager import ConfigManager
from device_state import DeviceState
from mender import MenderClient
from network_manager import NetworkManager
from retina_tracker_client import RetinaTrackerClient
from ssh_keys import SSHKeyManager
from tracker_capture import TrackerCaptureService

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

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

MENDER_SERVICES = ["mender-authd", "mender-updated", "mender-connect"]

# ── Shared services ────────────────────────────────────────────

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

device_state = DeviceState(
    data_dir=DATA_DIR,
    mender_services=MENDER_SERVICES,
    mender_conf_path="/data/mender/mender.conf",
    mender_conf_backup_dir="/data/mender-cloud-disabled",
    mender_conf_backup_path="/data/mender-cloud-disabled/mender.conf",
    dev_mode=DEV_MODE,
)

def config_change_guard():
    """Refuse a config apply while Auto-Calibrate is using the SDR.

    Both signals are needed, as elsewhere: is_running() is authoritative for
    this process and never expires, while the lock file also covers a run
    left behind by a crashed or restarted GUI. See ApplyService's
    ConfigChangeRefused for why this is enforced there rather than per-route.

    `calibrator` is defined below this point, which is fine: the name is
    resolved when the guard runs, not when it is defined.
    """
    if calibrator.is_running() or device_state.is_calibration_locked()[0]:
        return False, ("Auto-calibration is running. Cancel it before "
                       "changing configuration")
    return True, None


apply_service = ApplyService(RETINA_NODE_PATH, dev_mode=DEV_MODE,
                             guard=config_change_guard)
blah2_client = Blah2Client(BLAH2_API_URL)
# One client per sidecar, shared by every feature that talks to it — its TCP
# server accepts a single connection at a time, so a second instance does not
# get a second conversation, it gets a socket nobody ever reads from.
retina_tracker_client = RetinaTrackerClient(
    RETINA_TRACKER_HOST, RETINA_TRACKER_PORT, RETINA_TRACKER_EVENTS_PATH)
calibrator = Calibrator(blah2_client, retina_tracker_client)
tracker_capture = TrackerCaptureService(blah2_client, retina_tracker_client)


def _on_calibration_complete(status):
    """Runs on the calibration thread when a run reaches a terminal state."""
    device_state.release_calibration_lock()


calibrator.on_complete = _on_calibration_complete
