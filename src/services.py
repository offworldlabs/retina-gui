"""Process-wide configuration and shared service singletons.

Everything here must exist exactly once per process. app.py's module body runs
twice (as __main__ and again as `app`), so singletons built there would be
duplicated; this module is only reachable under one name, so it runs once.
app.py re-exports these names. See docs/architecture.md#one-instance-per-process.
"""

import os

from access_identity import AccessIdentity
from apply_service import ApplyService
from blah2_client import Blah2Client
from calibrator import Calibrator
from config_manager import ConfigManager
from device_state import DeviceState
from mdns_peers import peer_directory_from_env
from mender import MenderClient
from mender_connect import MenderConnect
from network_manager import NetworkManager
from node_name import NodeName
from remote_access import RemoteAccess
from retina_tracker_client import RetinaTrackerClient
from ssh_keys import SSHKeyManager
from telemetry_status import TelemetryStatus

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

# CARTO basemap key for the wizard's tower map. Empty is a working, watermarked
# map. Never commit a key here (public repo); only ever use a tile-scoped key.
# See docs/operations/node-files.md#carto-basemap-key.
CARTO_API_KEY = os.environ.get('CARTO_API_KEY', '')
# blah2_api directly (network_mode: host), NOT the :8080 blah2_host nginx
# proxy, which doesn't forward /capture/* at all.
BLAH2_API_URL = os.environ.get('BLAH2_API_URL', 'http://localhost:3000')
# retina-tracker sidecar's control surface only. Its ingest socket accepts one
# connection and belongs to blah2_api, which forwards detections to it.
RETINA_TRACKER_CONTROL_URL = os.environ.get(
    'RETINA_TRACKER_CONTROL_URL', 'http://localhost:30101')
# JSONL track events the sidecar writes (its -s flag). Tailed, because
# retina-tracker's --tcp mode is input-only.
RETINA_TRACKER_EVENTS_PATH = os.environ.get('RETINA_TRACKER_EVENTS_PATH',
    os.path.join(PROJECT_ROOT, 'dev_data', 'retina-tracker-events.jsonl') if DEV_MODE
    else '/data/retina-node/retina-tracker/output/events.jsonl'
)
# Written by retina-telemetry, read-only to us. That service binds no ports, so
# this file is the only way its state reaches an operator.
TELEMETRY_STATUS_PATH = os.environ.get('TELEMETRY_STATUS_PATH',
    os.path.join(PROJECT_ROOT, 'dev_data', 'telemetry-status.json') if DEV_MODE
    else '/data/retina-telemetry/status.json'
)

# The zone the Cloudflare tunnel publishes this node under. Deliberately not
# the main product domain, so a node-served page cannot set cookies the
# ingest API would receive. See docs/architecture.md#session-cookies.
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

# Verifies Cloudflare Access assertions. Refuses everything until node-infra
# delivers its config beside the tunnel token.
access_identity = AccessIdentity(
    config_path=os.environ.get("ACCESS_CONFIG_PATH", "/data/cloudflared/access.json"),
)

# Enforces the remote shell agreement that RemoteAccess records. Kept separate
# so the recording is testable without systemd.
mender_connect = MenderConnect(
    conf_path=os.environ.get("MENDER_CONNECT_CONF", "/etc/mender/mender-connect.conf"),
    dev_mode=DEV_MODE,
)


def secret_key():
    """Flask's signing key, persisted so sessions survive a restart and an OTA.

    Mode 0600, and never logged. Anyone able to read it can forge a session
    cookie for the owner pathway. See docs/architecture.md#session-cookies.
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

    Also this node's mDNS host name (owl-mdns-identity derives ret*.local from
    it). Use this, not app.get_node_id(), off the request path: that one needs
    the Flask app.
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

    Both signals are needed: is_running() is authoritative for this process,
    and the lock file also covers a run left by a crashed GUI. `calibrator` is
    defined below; the name resolves when the guard runs.
    See docs/architecture.md#the-calibration-guard.
    """
    if calibrator.is_running() or device_state.is_calibration_locked()[0]:
        return False, ("Auto-calibration is running. Cancel it before "
                       "changing configuration")
    return True, None


apply_service = ApplyService(RETINA_NODE_PATH, dev_mode=DEV_MODE,
                             guard=config_change_guard)
blah2_client = Blah2Client(BLAH2_API_URL)
# One client per sidecar, shared by every feature that consumes its events.
retina_tracker_client = RetinaTrackerClient(
    RETINA_TRACKER_EVENTS_PATH, RETINA_TRACKER_CONTROL_URL)
# config_mgr/apply_service are only for the preflight's recovery branch (see
# calibrator._preflight). The guard resolves `calibrator` lazily, so the
# cycle is fine.
calibrator = Calibrator(blah2_client, retina_tracker_client,
                        config_mgr=config_mgr, apply_service=apply_service)


def _on_calibration_complete(status):
    """Runs on the calibration thread when a run reaches a terminal state."""
    device_state.release_calibration_lock()


calibrator.on_complete = _on_calibration_complete
