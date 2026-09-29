"""Login for the tunnel pathway, and the /remote-access controls.

/login is reachable on the tunnel hostname without a session. Everything under
/remote-access is refused there by app.py's gate (PRESENCE_REQUIRED_PREFIXES).
See docs/features/remote-access.md.
"""

from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from remote_access import generate_password

bp = Blueprint('remote_access', __name__)


def _safe_next(target):
    """Only ever redirect back into this site.

    The value is attacker-supplied. Security invariant: accept a bare path only;
    "//host" or an absolute URL would make /login an open redirect.
    """
    if not target or not target.startswith('/'):
        return '/'
    if target.startswith('//') or target.startswith('/\\'):
        return '/'
    return target


@bp.route("/login", methods=["GET"])
def login():
    """The password prompt shown on the owner hostname."""
    if session.get('remote_authed'):
        return redirect(_safe_next(request.args.get('next')))
    return render_template("login.html",
                           next=request.args.get('next', ''),
                           error=None)


@bp.route("/login", methods=["POST"])
def do_login():
    from app import remote_access

    target = _safe_next(request.form.get('next'))
    if remote_access.verify(request.form.get('password', '')):
        session.clear()
        session['remote_authed'] = True
        # Not permanent: a shared node password should not stay remembered on
        # a borrowed laptop. See docs/features/remote-access.md#login-flow.
        session.permanent = False
        return redirect(target)

    # Deliberately says nothing about whether a password is set: no signal
    # beyond pass or fail. The only rate limit is at Cloudflare's edge.
    return render_template("login.html", next=request.form.get('next', ''),
                           error="Incorrect password"), 401


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for('remote_access.login'))


@bp.route("/remote-access/password", methods=["POST"])
def set_password():
    """Set or replace the password. Refused on the owner pathway by the gate."""
    from app import remote_access

    ok, error = remote_access.set_password(request.form.get('password', ''))
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, "has_password": True})


@bp.route("/remote-access/generate", methods=["POST"])
def generate():
    """Mint a strong password, store it, and return it."""
    from app import remote_access

    password = generate_password()
    ok, error = remote_access.set_password(password)
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, "password": password, "has_password": True})


@bp.route("/remote-access/shell", methods=["POST"])
def set_shell():
    """Record and apply the remote shell agreement.

    Order is load-bearing: enforce first, record only if that succeeded.
    See docs/features/remote-access.md#order-enforce-then-record.
    """
    from app import mender_connect, remote_access

    allowed = str(request.form.get('allowed', '')).lower() in ('1', 'true', 'on', 'yes')

    ok, error = mender_connect.set_shell_enabled(allowed)
    if not ok:
        return jsonify({"ok": False, "error": error}), 400

    ok, error = remote_access.record_shell_allowed(allowed)
    if not ok:
        return jsonify({"ok": False, "error": error}), 400

    return jsonify({"ok": True, "shell_allowed": allowed})


@bp.route("/remote-access/toggle", methods=["POST"])
def toggle():
    """Turn remote access on or off.

    Only writes the inventory marker; the tunnel follows after the next Mender
    inventory poll. See docs/features/remote-access.md#the-node-to-server-channel.
    """
    from app import remote_access

    enabled = str(request.form.get('enabled', '')).lower() in ('1', 'true', 'on', 'yes')
    ok, error = remote_access.set_enabled(enabled)
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, **remote_access.status()})

