import os
import subprocess
import sys

from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, url_for
from flask.sessions import SecureCookieSessionInterface
from flask_wtf.csrf import CSRFError, CSRFProtect

# Configuration and shared services live in services.py because this module
# body runs twice per process (as __main__ and again as `app`), so anything
# constructed here would exist twice. Re-exported so `from app import ...` works.
# See docs/architecture.md#one-instance-per-process.
from services import (  # noqa: F401  (re-exported for routes)
    BLAH2_API_URL,
    CARTO_API_KEY,
    DATA_DIR,
    DEV_MODE,
    MENDER_SERVICES,
    MERGED_CONFIG_PATH,
    NODE_ID_FILE,
    PROJECT_ROOT,
    REMOTE_ACCESS_DOMAIN,
    RETINA_NODE_PATH,
    RETINA_SPECTRUM_URL,
    RETINA_TRACKER_CONTROL_URL,
    RETINA_TRACKER_EVENTS_PATH,
    TELEMETRY_STATUS_PATH,
    TOWER_FINDER_URL,
    USER_CONFIG_PATH,
    access_identity,
    apply_service,
    blah2_client,
    calibrator,
    config_mgr,
    device_state,
    mender,
    mender_connect,
    network_mgr,
    node_name,
    peers,
    read_node_id,
    remote_access,
    retina_tracker_client,
    secret_key,
    ssh_keys,
    telemetry_status,
)

app = Flask(__name__,
            template_folder=os.path.join(PROJECT_ROOT, 'templates'),
            static_folder=os.path.join(PROJECT_ROOT, 'static'))
# Persisted to /data rather than regenerated per process. See services.secret_key.
app.config['SECRET_KEY'] = secret_key()
# No wall-clock expiry: the setup wizard reuses one token for a run that often
# passes Flask-WTF's default hour. The token still expires with the session.
# See docs/architecture.md#csrf-tokens.
app.config['WTF_CSRF_TIME_LIMIT'] = None

# Whether Secure cookies are permitted at all. Off in dev so a server on
# http://localhost can still hold one; on elsewhere, where it gates the remote
# pathway below rather than applying to every response.
app.config['SESSION_COOKIE_SECURE'] = (
    os.environ.get('SESSION_COOKIE_SECURE', '' if DEV_MODE else '1').lower()
    in ('1', 'true', 'yes')
)
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'


class _PathwaySessionInterface(SecureCookieSessionInterface):
    """Cookie flags that follow the pathway instead of one global setting.

    Secure plus the `__Host-` name on the owner pathway only. The LAN is plain
    HTTP, where a browser drops a Secure cookie and with it the CSRF token.
    The prefix requires Secure, so the two must move together.
    See docs/architecture.md#session-cookies.
    """

    def _is_remote(self):
        try:
            from remote_access import OWNER, classify_host
            return (app.config['SESSION_COOKIE_SECURE']
                    and classify_host(request.host, read_node_id(),
                                      REMOTE_ACCESS_DOMAIN) == OWNER)
        except Exception:
            # Fail towards the LAN. A node that cannot tell which pathway it is
            # on must not end up unable to hold a session on the one owners
            # actually use, which is the failure this class exists to fix.
            return False

    def get_cookie_secure(self, app):
        return self._is_remote()

    def get_cookie_name(self, app):
        return '__Host-session' if self._is_remote() else 'session'


app.session_interface = _PathwaySessionInterface()

csrf = CSRFProtect(app)


def _reapply_shell_agreement():
    """Make mender-connect agree with the owner's recorded choice.

    The marker in /data is authoritative; mender-connect.conf is on the rootfs
    and an OS update resets it to allow a shell. Acts only on a mismatch, so the
    module body's second run and an already-agreeing node are left alone.
    See docs/architecture.md#shell-agreement.
    """
    if DEV_MODE:
        return
    try:
        wanted = remote_access.is_shell_allowed()
        actual = mender_connect.is_shell_enabled()
        # None means the config could not be read. Never write a guessed
        # replacement: it could widen access rather than restore it.
        if actual is None or actual == wanted:
            return
        ok, error = mender_connect.set_shell_enabled(wanted)
        if ok:
            app.logger.warning(
                "Re-applied the remote shell agreement: the owner has it %s, "
                "but this node was set to %s sessions. An OS update replaces "
                "mender-connect.conf, so this is the expected repair after one.",
                "on" if wanted else "off",
                "allow" if actual else "refuse")
        else:
            app.logger.error(
                "Could not re-apply the remote shell agreement (%s). This node "
                "will %s sessions while its owner has it %s.",
                error, "allow" if actual else "refuse", "on" if wanted else "off")
    except Exception:
        # Never let this stop the GUI booting. A node that will not serve its
        # own config page is worse than one whose shell setting needs a click.
        app.logger.exception("Re-applying the remote shell agreement failed")


if not DEV_MODE:
    device_state.apply_startup_preferences()
    _reapply_shell_agreement()
    # node-infra reads only the inventory marker, and an untouched node has
    # none, so publish it at boot. Best effort: never worth failing a boot.
    # See docs/architecture.md#support-access-marker.
    try:
        remote_access.publish_marker()
    except Exception:
        app.logger.exception("Could not publish the support access marker")

# Always boot into radar mode — delete any persisted spectrum state
try:
    os.remove(os.path.join(DATA_DIR, 'mode.txt'))
except OSError:
    pass

# A calibration run cannot survive a GUI restart — any lock left behind is stale
device_state.release_calibration_lock()

# Backfill the setup-completed flag retina-telemetry needs. Guarded because an
# unparseable user.yml must not stop the GUI booting.
# See docs/architecture.md#setup-completed-backfill.
try:
    device_state.backfill_setup_wizard_completed(config_mgr.load_user_config())
except Exception:
    pass

# Enforce radar at the Docker level: stop and remove retina-spectrum if it is running.
# retina-spectrum is only allowed while the wizard location step or config toggle is active.
if config_mgr.is_retina_node_installed():
    from restart_lock import OPPORTUNISTIC_TIMEOUT_SECONDS, restart_lock
    from stack_reconcile import find_stale_containers, reconcile
    try:
        # Opportunistic timeout: a long wait would delay the GUI coming up,
        # and a lock holder is mid-restart and stops retina-spectrum itself.
        # See docs/architecture.md#spectrum-and-stale-containers.
        with restart_lock(DATA_DIR, timeout=OPPORTUNISTIC_TIMEOUT_SECONDS):
            subprocess.run(['docker', 'compose', '-p', 'retina-node', 'stop', 'retina-spectrum'],
                           cwd=RETINA_NODE_PATH, capture_output=True, timeout=60)
            subprocess.run(['docker', 'compose', '-p', 'retina-node', 'rm', '-sf', 'retina-spectrum'],
                           cwd=RETINA_NODE_PATH, capture_output=True, timeout=30)

            # Repair a recreate this process was killed in the middle of.
            # Nothing else clears it, and every later apply would fail.
            # See docs/architecture.md#stack-reconcile.
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


# Never under pytest: conftest reloads this module per test, and each start()
# would leak a permanent thread making real HTTP requests to other nodes.
if "pytest" not in sys.modules:
    peers.start()


def get_node_id():
    """Get node_id from Mender device identity file."""
    try:
        with open(NODE_ID_FILE) as f:
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
    # Imported here rather than at module scope: the route modules are
    # deliberately imported at the bottom of this file, after the app exists.
    from routes.fleet import banner_nodes

    owl_os_version, retina_node_version = mender.get_versions()

    # Which pathway this request arrived on; some links only work one way.
    # Defaults to LAN outside a request or before the gate runs.
    from remote_access import LAN, OWNER
    is_remote = getattr(g, 'pathway', LAN) == OWNER

    return {
        'is_remote': is_remote,
        'node_id': get_node_id(),
        'pathway': getattr(g, 'pathway', 'lan'),
        'owl_os_version': owl_os_version,
        'retina_node_version': retina_node_version,
        # The banner is in base.html, so the node list comes from here.
        'fleet_nodes': banner_nodes(),
    }


# Register blueprints
from routes.calibrate import bp as calibrate_bp
from routes.config import bp as config_bp
from routes.fleet import bp as fleet_bp
from routes.home import bp as home_bp
from routes.mender_routes import bp as mender_bp
from routes.mode import bp as mode_bp
from routes.network import bp as network_bp
from routes.remote_access import bp as remote_access_bp
from routes.setup import bp as setup_bp
from routes.towers import bp as towers_bp
from routes.tracker import bp as tracker_bp
from routes.tracker import legacy_bp as tracker_legacy_bp

app.register_blueprint(home_bp)
app.register_blueprint(config_bp)
app.register_blueprint(mender_bp)
app.register_blueprint(setup_bp)
app.register_blueprint(towers_bp)
app.register_blueprint(mode_bp)
app.register_blueprint(network_bp)
app.register_blueprint(calibrate_bp)
app.register_blueprint(tracker_bp)
app.register_blueprint(tracker_legacy_bp)
app.register_blueprint(fleet_bp)
app.register_blueprint(remote_access_bp)


# Reachable on the owner pathway without a session: the login page itself, the
# assets it needs to render, and the favicon. Everything else is behind the
# password.
_REMOTE_PUBLIC_PREFIXES = ('/login', '/static', '/favicon')


@app.before_request
def _gate_the_remote_pathways():
    """Decide what this request is allowed to be, based on the hostname it used.

    LAN hostnames pass unchallenged; the owner pathway (the tunnel hostname)
    needs a verified Access identity or a password session.

    Must stay registered before the calibration hook below: Flask runs these
    in registration order, and that hook would otherwise bounce an
    unauthenticated visitor to /config instead of the login form.
    See docs/architecture.md#request-hooks.
    """
    from remote_access import LAN, classify_host, requires_presence

    g.pathway = classify_host(request.host, read_node_id(), REMOTE_ACCESS_DOMAIN)
    if g.pathway == LAN:
        return None

    # 404 rather than 403 when the owner has not turned this on: a stale DNS
    # record or a guess is not owed confirmation that a node answers here.
    if not remote_access.is_enabled():
        abort(404)

    if request.path.startswith(_REMOTE_PUBLIC_PREFIXES):
        return None

    # Verified on the node rather than trusted from the header: if the Access
    # application were ever deleted or misconfigured, only this would notice.
    identity = access_identity.identity(
        request.headers.get('Cf-Access-Jwt-Assertion'))
    if identity:
        g.access_identity = identity

    if not (identity or session.get('remote_authed')):
        if request.method == 'GET':
            # Only offer the password form when a password exists to give;
            # otherwise show why access was refused.
            # See docs/architecture.md#pathway-gate.
            if remote_access.has_password():
                # full_path always appends '?', even with no query string, which
                # would send people to '/config?' after signing in.
                wanted = request.full_path.rstrip('?') or '/'
                return redirect(url_for('remote_access.login', next=wanted))
            return render_template('remote_denied.html'), 403
        # 403 rather than a redirect, so fetch() callers get a usable status.
        # CSRFProtect runs first and often refuses a stale page with 400.
        abort(403)

    # Some things need someone at the device, even for our own engineers, so
    # support access never becomes a route to shell access.
    if requires_presence(request.path):
        abort(403, "This can only be done from the local network")

    return None


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    """Answer a rejected CSRF token in the caller's own format.

    JSON callers get `session_expired: true` so they can say "reload" instead
    of failing to parse an HTML error page. See docs/architecture.md#csrf-tokens.
    """
    if request.accept_mimetypes.best == 'text/html' and not request.is_json:
        return e.description, 400
    return jsonify({
        'error': 'Your setup session expired. Reload the page to continue.',
        'session_expired': True,
    }), 400


# Paths that must keep working while a run holds the GUI.
# See docs/architecture.md#calibration-hold.
_CALIBRATION_ALLOWED_PREFIXES = (
    '/config',        # the page, plus /config/apply/status and /config/rf-status
    '/calibrate',     # status, cancel, apply
    '/static',
    '/favicon',
    # Fleet scope. Peers probe /healthz, so redirecting it would make a
    # calibrating node look unreachable.
    '/summary',
    '/api/fleet',
    '/healthz',
)


@app.before_request
def _keep_the_user_with_the_running_calibration():
    """While Auto-Calibrate is running, hold the browser on the config page.

    Keyed on is_running() alone, never the lock file: a stale lock would lock
    the user out of the whole GUI, while is_running() is freed by a restart.
    GETs only; POSTs elsewhere already refuse with 409.
    See docs/architecture.md#calibration-hold.
    """
    if request.method != 'GET':
        return None
    if request.path.startswith(_CALIBRATION_ALLOWED_PREFIXES):
        return None
    # The setup wizard redirects /config back to itself, so locking during
    # the wizard would bounce the browser between the two forever.
    if device_state.is_setup_wizard_in_progress():
        return None
    if calibrator.is_running():
        return redirect('/config')
    return None


if __name__ == "__main__":
    port = int(os.environ.get('PORT', 80))
    debug = os.environ.get('FLASK_DEBUG', 'false').lower() == 'true'
    app.run(host="::", port=port, debug=debug)
