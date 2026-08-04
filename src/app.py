from flask import Flask
from flask_wtf.csrf import CSRFProtect
import os
import subprocess
import sys

# Configuration and shared services live in their own module because this one
# is executed twice — once as __main__ (systemd runs `python3 src/app.py`) and
# again as `app` the first time a route does `from app import ...`. Anything
# constructed here would therefore exist twice in one process. See services.py
# for what that broke. Re-exported below so `from app import ...` is unchanged.
from services import (  # noqa: F401  (re-exported for routes)
    PROJECT_ROOT, DEV_MODE, DATA_DIR, USER_CONFIG_PATH, MERGED_CONFIG_PATH,
    RETINA_NODE_PATH, RETINA_SPECTRUM_URL, NODE_ID_FILE, TOWER_FINDER_URL,
    BLAH2_API_URL, RETINA_TRACKER_HOST, RETINA_TRACKER_PORT,
    RETINA_TRACKER_EVENTS_PATH, MENDER_SERVICES,
    ssh_keys, network_mgr, config_mgr, mender, device_state,
    apply_service, blah2_client, retina_tracker_client, calibrator,
    tracker_capture,
)
app = Flask(__name__,
            template_folder=os.path.join(PROJECT_ROOT, 'templates'),
            static_folder=os.path.join(PROJECT_ROOT, 'static'))
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(32).hex())
csrf = CSRFProtect(app)

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
    from restart_lock import restart_lock, OPPORTUNISTIC_TIMEOUT_SECONDS
    from stack_reconcile import find_stale_containers, reconcile
    try:
        # Opportunistic, and deliberately so: this runs before Flask serves
        # anything, so a long wait here delays the whole GUI coming up. If
        # something else holds the lock it is mid-restart and will stop
        # retina-spectrum itself, so skipping costs nothing.
        with restart_lock(DATA_DIR, timeout=OPPORTUNISTIC_TIMEOUT_SECONDS):
            subprocess.run(['docker', 'compose', '-p', 'retina-node', 'stop', 'retina-spectrum'],
                           cwd=RETINA_NODE_PATH, capture_output=True, timeout=60)
            subprocess.run(['docker', 'compose', '-p', 'retina-node', 'rm', '-sf', 'retina-spectrum'],
                           cwd=RETINA_NODE_PATH, capture_output=True, timeout=30)

            # Repair a recreate this process was killed in the middle of.
            # systemd kills retina-gui's whole control group, so a crash, a
            # restart or a redeploy during an apply takes the `docker
            # compose` child with it — potentially after it renamed a
            # container but before it removed the old one. Nothing else
            # clears that, and it survives reboots: every apply from here on
            # would fail on the name conflict. Cheap when there is nothing
            # to do (one `docker ps`), so it runs unconditionally.
            stale = find_stale_containers()
            if stale:
                app.logger.warning(
                    f"Found {len(stale)} container(s) left half-created by an "
                    f"interrupted restart: {', '.join(stale)}. Repairing.")
                removed, error = reconcile(RETINA_NODE_PATH)
                if error:
                    app.logger.error(f"Startup repair failed: {error}")
                else:
                    app.logger.warning(f"Startup repair removed: {', '.join(removed)}")
    except Exception:
        pass

# Same for sdrconnect.service — never leave a node stuck serving SDRconnect
# after a GUI restart.
try:
    subprocess.run(['systemctl', 'stop', 'sdrconnect.service'], capture_output=True, timeout=30)
except Exception:
    pass


# apply_service, blah2_client, retina_tracker_client, calibrator and
# tracker_capture are constructed in services.py and imported at the top of
# this file — they must exist once per process, and this module body runs
# twice. See services.py.

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
