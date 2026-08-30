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
from mdns_peers import peer_directory_from_env
from mender import MenderClient
from network_manager import NetworkManager
from node_name import NodeName
from remote_access import RemoteAccess
from retina_tracker_client import RetinaTrackerClient
from ssh_keys import SSHKeyManager
from telemetry_status import TelemetryStatus
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
# Written by the retina-telemetry container, read-only to us. That service
# binds no ports, so this file is the only way anything it knows reaches an
# operator — see telemetry_status.py.
TELEMETRY_STATUS_PATH = os.environ.get('TELEMETRY_STATUS_PATH',
    os.path.join(PROJECT_ROOT, 'dev_data', 'telemetry-status.json') if DEV_MODE
    else '/data/retina-telemetry/status.json'
)

# The zone the Cloudflare tunnel publishes this node under. Deliberately not
# retina.fm: a page served from a node can set a cookie scoped to its parent
# domain, which the browser would then send to every other host on that domain,
# including the ingest API. Nodes run in customers' homes on hardware they
# control, so that separation is not theoretical.
REMOTE_ACCESS_DOMAIN = os.environ.get('REMOTE_ACCESS_DOMAIN', 'retnode.com')

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

telemetry_status = TelemetryStatus(
    status_path=TELEMETRY_STATUS_PATH,
    node_ref_cache_path=os.path.join(DATA_DIR, "telemetry-node-ref"),
)

node_name = NodeName(os.path.join(DATA_DIR, "node-name"), dev_mode=DEV_MODE)

remote_access = RemoteAccess(os.path.join(DATA_DIR, "remote-access.json"))


def secret_key():
    """Flask's signing key, persisted so sessions survive a restart.

    This used to be `os.urandom(32).hex()` with no fallback to disk, which was
    harmless while nothing used the session: every restart minted a new key and
    invalidated cookies nobody held. It stops being harmless the moment remote
    access exists, because every GUI restart, and every OTA, would then sign the
    owner out mid-session with no explanation.

    Mode 0600, and never logged. Anyone able to read it can forge a session
    cookie for the owner pathway.
    """
    from_env = os.environ.get('SECRET_KEY')
    if from_env:
        return from_env

    key_file = os.path.join(DATA_DIR, "secret-key")
    try:
        with open(key_file) as f:
            key = f.read().strip()
            if key:
                return key
    except OSError:
        pass

    key = os.urandom(32).hex()
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        fd = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key)
    except OSError:
        # An unwritable /data means sessions do not survive this process, which
        # is a worse GUI rather than a broken one. Everything else still works.
        pass
    return key


def read_node_id():
    """The Mender node_id, or 'Unknown' if it cannot be read.

    Also this node's mDNS host name: owl-mdns-identity derives ret*.local from
    exactly this value, so `f"{read_node_id()}.local"` is the address other
    machines reach us on. app.get_node_id() wraps this to add logging; nothing
    that runs off the request path should use that one, since it needs the
    Flask app object.
    """
    try:
        with open(NODE_ID_FILE) as f:
            return f.read().strip() or "Unknown"
    except OSError:
        return "Unknown"


# Owns a browse thread and a probe thread, so like the clients above it must
# exist once per process. start() is called from app.py, not here, so that
# importing this module under pytest does not spawn anything.
peers = peer_directory_from_env(read_node_id, DEV_MODE)


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
# config_mgr/apply_service are only reached by the preflight's recovery
# branch — writing the safe corner to user.yml and restarting the stack when
# the radio has stopped accepting retunes (see calibrator._preflight). The
# apply_service reference resolves here because ApplyService is constructed
# above; its own guard resolves `calibrator` lazily, so the cycle is fine.
calibrator = Calibrator(blah2_client, retina_tracker_client,
                        config_mgr=config_mgr, apply_service=apply_service)
tracker_capture = TrackerCaptureService(blah2_client, retina_tracker_client)


def _on_calibration_complete(status):
    """Runs on the calibration thread when a run reaches a terminal state."""
    device_state.release_calibration_lock()


calibrator.on_complete = _on_calibration_complete
