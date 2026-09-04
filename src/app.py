import os
import subprocess
import sys

from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, url_for
from flask.sessions import SecureCookieSessionInterface
from flask_wtf.csrf import CSRFError, CSRFProtect

# Configuration and shared services live in their own module because this one
# is executed twice — once as __main__ (systemd runs `python3 src/app.py`) and
# again as `app` the first time a route does `from app import ...`. Anything
# constructed here would therefore exist twice in one process. See services.py
# for what that broke. Re-exported below so `from app import ...` is unchanged.
from services import (  # noqa: F401  (re-exported for routes)
    BLAH2_API_URL,
    DATA_DIR,
    DEV_MODE,
    MENDER_SERVICES,
    MERGED_CONFIG_PATH,
    NODE_ID_FILE,
    PROJECT_ROOT,
    REMOTE_ACCESS_DOMAIN,
    RETINA_NODE_PATH,
    RETINA_SPECTRUM_URL,
    RETINA_TRACKER_EVENTS_PATH,
    RETINA_TRACKER_HOST,
    RETINA_TRACKER_PORT,
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
    tracker_capture,
)

app = Flask(__name__,
            template_folder=os.path.join(PROJECT_ROOT, 'templates'),
            static_folder=os.path.join(PROJECT_ROOT, 'static'))
# Persisted to /data rather than regenerated per process. See services.secret_key.
app.config['SECRET_KEY'] = secret_key()
# Flask-WTF expires a CSRF token 3600s after it is issued. The setup wizard
# reads its token once at page load and reuses it for the entire run (see
# postJSON in setup.js), and a real run routinely passes the hour: reading
# the agreements, an OS update, ~600 MB of packages, then calibration.
# Every POST past that point 400s. Consent, tower selection and
# /set-up/complete all fail, and /set-up/save-step stops recording progress
# without anything on screen saying so, which leaves even a reload resuming
# at the wrong step. Dropping the wall clock does not weaken the token: it
# stays signed with SECRET_KEY and bound to the session cookie, so it still
# expires when the session does.
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

    This used to be a single config: Secure everywhere, and the `__Host-` name
    whenever Secure was on. The reasoning was that the session only mattered on
    the remote pathway, which is always HTTPS, so the LAN could simply go
    without one.

    That was wrong, and badly so. A browser silently discards a Secure cookie
    delivered over plain HTTP, which is exactly what the LAN is. So the LAN held
    no session at all, and CSRFProtect keeps its token *in the session*, so
    every POST an owner made from their own network was rejected with 400: the
    config page, every toggle, the setup wizard. Curl hid it completely, because
    it keeps the cookie where a browser refuses to.

    So Secure is applied where it is true rather than where it is convenient.
    The `__Host-` prefix is still worth having on the remote pathway, where
    every node is a sibling under one registrable domain and without it one node
    could set a `.retnode.com` cookie that shadows another's. The prefix
    requires Secure, so the two answers have to move together.
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

    The choice lives in /data, which is its own partition and survives an OS
    update. The enforcement lives in /etc/mender/mender-connect.conf, on the
    A/B rootfs, which does not: an update replaces it with the template owl-os
    ships, where Terminal and PortForward are both enabled.

    Nothing re-applied it, so an owner who declined a shell had it restored by
    the next update while the marker, the config page and the `remote_shell`
    inventory attribute all still reported it declined. Recorded state and
    enforced state diverged silently, in the permissive direction, which is the
    only direction that matters here: everyone would have believed the refusal
    was still in force.

    That makes the marker in /data authoritative and this the thing that
    enforces it, rather than the write that happened to run when the owner last
    clicked.

    Acts only on a mismatch, which matters twice over. This module body is
    executed twice per process (see services.py), so the second pass finds
    nothing to do; and a node whose config already agrees is left alone rather
    than rewritten and its daemon restarted on every boot.
    """
    if DEV_MODE:
        return
    try:
        wanted = remote_access.is_shell_allowed()
        actual = mender_connect.is_shell_enabled()
        # None means the config could not be read. mender_connect refuses to
        # invent a replacement, and it is right to: writing a plausible one
        # would reconstruct transfer limits and a shell user we have no basis
        # for, and could widen access rather than restore it.
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
    # The inventory marker is the only thing node-infra reads, and a node that
    # has never been touched has no state file and so no marker. That was
    # consistent while support access defaulted off; it is not now that it
    # defaults on, and without this such a node would keep reporting
    # remote_access=false until somebody happened to toggle something.
    #
    # Best effort, like the marker write it wraps: never worth failing a boot.
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

# Enforce radar at the Docker level: stop and remove retina-spectrum if it is running.
# retina-spectrum is only allowed while the wizard location step or config toggle is active.
if config_mgr.is_retina_node_installed():
    from restart_lock import OPPORTUNISTIC_TIMEOUT_SECONDS, restart_lock
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
    # Same reasoning: the peer directory owns a browse thread and a probe
    # thread, and the probe thread makes real HTTP requests to other nodes.
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

    # Which pathway this request arrived on. Templates need it because some
    # links are only reachable one way: the service cards point at other ports
    # on the LAN, and at tunnel paths remotely. Defaults to LAN, so anything
    # rendered outside a request (or before the gate runs) keeps the behaviour
    # the node has always had.
    from remote_access import LAN, OWNER
    is_remote = getattr(g, 'pathway', LAN) == OWNER

    return {
        'is_remote': is_remote,
        'node_id': get_node_id(),
        # Which pathway this request arrived on, so templates can hide what the
        # owner pathway is not allowed to reach anyway.
        'pathway': getattr(g, 'pathway', 'lan'),
        'owl_os_version': owl_os_version,
        'retina_node_version': retina_node_version,
        # One banner tab per node, on every page. An in-memory list, copied
        # and sorted — cheap enough to do per render, and it has to be here
        # rather than per-route because the banner is in base.html.
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
app.register_blueprint(fleet_bp)
app.register_blueprint(remote_access_bp)


# Reachable on the owner pathway without a session: the login page itself, the
# assets it needs to render, and the favicon. Everything else is behind the
# password.
_REMOTE_PUBLIC_PREFIXES = ('/login', '/static', '/favicon')


@app.before_request
def _gate_the_remote_pathways():
    """Decide what this request is allowed to be, based on the hostname it used.

    Two pathways, and only one of them is challenged here:

      LAN     owl.local, ret4c844c20.local, a bare IP, and anything arriving
              over a Mender port-forward, which lands as `localhost`.
              Unauthenticated, exactly as it has always been: being on the
              network is the credential, and Mender has already authenticated
              and logged the engineer who opened the forward.
      OWNER   ret<id>.retnode.com, over the tunnel. Nothing upstream checked
              anybody, so the password is the only gate and it is checked here.

    Registered before the calibration hook below, and that ordering is load
    bearing: Flask runs app-level before_request handlers in registration order,
    and the calibration hook redirects GETs to /config. If it ran first, an
    unauthenticated visitor arriving during a calibration would be bounced to a
    page they are not allowed to see instead of to the login form.
    """
    from remote_access import LAN, classify_host, requires_presence

    g.pathway = classify_host(request.host, read_node_id(), REMOTE_ACCESS_DOMAIN)
    if g.pathway == LAN:
        return None

    # 404 rather than 403 when the owner has not turned this on. The tunnel
    # should not be up at all in that state, so anything arriving here is either
    # a stale DNS record or someone guessing; neither is owed confirmation that
    # a node answers to this name.
    if not remote_access.is_enabled():
        abort(404)

    if request.path.startswith(_REMOTE_PUBLIC_PREFIXES):
        return None

    # Cloudflare Access is the gate in front of this hostname, so a valid
    # assertion is who this is. Verified on the node rather than trusted from a
    # header: if the Access application were ever deleted or misconfigured the
    # hostname would be open, and this is the only thing that would notice.
    identity = access_identity.identity(
        request.headers.get('Cf-Access-Jwt-Assertion'))
    if identity:
        g.access_identity = identity

    if not (identity or session.get('remote_authed')):
        if request.method == 'GET':
            # Only offer the password form when a password exists to give.
            #
            # This used to redirect unconditionally, and that turned every
            # verification failure into a dead end: no password can be set from
            # the support pathway, and the page offers no way to set one, so the
            # form could only ever refuse. Somebody whose Access assertion did
            # not verify was shown a login box instead of the reason, and had
            # nothing to do but guess.
            #
            # The form still exists for the deferred owner-password design, and
            # still works the moment a password is set. It is simply no longer
            # where failures land.
            if remote_access.has_password():
                # full_path always appends '?', even with no query string, which
                # would send people to '/config?' after signing in.
                wanted = request.full_path.rstrip('?') or '/'
                return redirect(url_for('remote_access.login', next=wanted))
            return render_template('remote_denied.html'), 403
        # A POST from a page whose session expired. 403 rather than a redirect,
        # so fetch() callers get a status they can act on instead of an HTML
        # login page parsed as JSON.
        #
        # Note this is not the only rejection such a request can get. CSRFProtect
        # is constructed above, so its before_request is registered first and
        # runs first: a stale page's POST usually carries a stale token and is
        # refused with 400 before reaching here. Both are refusals; only the
        # status differs, and which one you see depends on whether the token
        # outlived the session.
        abort(403)

    # Authenticated, but some things still need someone at the device, and that
    # applies to our own engineers too. If an Access session could add an SSH key
    # here, support access would be a route to shell access and the two owner
    # agreements would stop being independent of each other.
    if requires_presence(request.path):
        abort(403, "This can only be done from the local network")

    return None


@app.errorhandler(CSRFError)
def handle_csrf_error(e):
    """Answer a rejected CSRF token in the caller's own format.

    Flask-WTF's default is an HTML error page, which every fetch() in the GUI
    then hands to r.json(). The owner does not see "your session expired",
    they see `Failed to save: Unexpected token '<'` on top of a wizard step
    that looks fine, because the parse error lands in the same catch block as
    a genuine save failure.

    A JSON body with session_expired lets the caller say the one useful thing
    instead: reload the page. The token is only rejected now if the GUI
    restarted without its persisted key (see load_or_create_secret_key) or
    the browser dropped the session cookie, both of which a reload fixes.
    """
    if request.accept_mimetypes.best == 'text/html' and not request.is_json:
        return e.description, 400
    return jsonify({
        'error': 'Your setup session expired. Reload the page to continue.',
        'session_expired': True,
    }), 400


# Paths that must keep working while a run holds the GUI: the config page
# itself, its assets, and the endpoints its modal polls to show progress and
# to cancel. Everything else is a navigation away from a run the user cannot
# see from anywhere else.
_CALIBRATION_ALLOWED_PREFIXES = (
    '/config',        # the page, plus /config/apply/status and /config/rf-status
    '/calibrate',     # status, cancel, apply
    '/static',
    '/favicon',
    # Fleet-scope routes belong to whoever is browsing, who may not be anywhere
    # near this node and is not the person who started a calibration on it.
    # Holding them on this node's config page would be answering a question
    # about the fleet with a page about one node — and /healthz is how every
    # other node decides whether this one still exists, so a redirect there
    # would make a calibrating node look unreachable to its peers.
    '/summary',
    '/api/fleet',
    '/healthz',
)


@app.before_request
def _keep_the_user_with_the_running_calibration():
    """While Auto-Calibrate is running, hold the browser on the config page.

    A run owns the SDR for up to 15 minutes and blocks every config change
    for its duration, but it is only visible in one place — the modal on
    /config. Navigating away used to leave it running invisibly, with no way
    to cancel it short of waiting out the budget or restarting the GUI.
    Rather than let someone strand a run they cannot see, send them back to
    the one page that can show and stop it.

    Deliberately keyed on is_running() alone, never the lock file. The lock
    survives a GUI crash by design, and a stale one here would lock the user
    out of the entire interface with no way to reach the button that clears
    it. is_running() is in-memory, so a restart always frees the GUI.

    Only GETs are redirected. POSTs to other routes already refuse with a
    409 and an explanation, which is more useful to a caller than a redirect
    to an HTML page.
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
