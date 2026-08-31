"""Login for the owner pathway, and the controls that configure it.

The login half is reachable from the owner hostname, as it has to be: it is the
thing standing in front of everything else. The configuration half lives under
/remote-access, which app.py's gate refuses on that pathway entirely: changing
the password or the toggle needs someone on the node's own network, which
includes an engineer on a Mender port-forward.
See remote_access.PRESENCE_REQUIRED_PREFIXES.
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

    request.full_path is put in the query string by the gate, so this value is
    attacker-supplied in the ordinary case of somebody being sent a link. A bare
    path is the only shape accepted: anything scheme-relative ("//evil.test") or
    absolute would turn the login page into an open redirect.
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
        # Not permanent: the cookie dies with the browser session. A node's
        # password is shared more freely than an account password, so a
        # remembered login on a borrowed laptop is a likelier way to lose
        # control of a node than the inconvenience of signing in again.
        session.permanent = False
        return redirect(target)

    # Deliberately says nothing about whether a password is even set. The only
    # real defence against guessing is the rate limit at Cloudflare's edge, and
    # there is none in this process, so this page gives an attacker no signal
    # to work with beyond pass or fail.
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
    """Mint a strong password and return it once.

    Once is all there is: only the hash is stored, so this response is the only
    time the plaintext exists anywhere. The caller has to show it to the owner
    then and there.
    """
    from app import remote_access

    password = generate_password()
    ok, error = remote_access.set_password(password)
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, "password": password, "has_password": True})


@bp.route("/remote-access/shell", methods=["POST"])
def set_shell():
    """Record and apply the remote shell agreement.

    Enforced before it is recorded, and the record is skipped if enforcement
    failed. The reverse order would leave the marker, and therefore the
    inventory attribute we report upstream, claiming the owner had declined
    interactive access while mender-connect happily kept serving it.

    Refused on the support pathway by the gate, like everything under
    /remote-access: the control an owner uses to withdraw our access is not
    reachable through the access being withdrawn.
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

    Nothing here talks to a server, and nothing needs to. set_enabled() writes
    the marker file that owl-os's mender-inventory-retina-remote-access script
    reports as the `remote_access` inventory attribute; node-infra reads that on
    its next pass and creates or tears down the tunnel accordingly.

    So this route is the whole of the node's side of provisioning. The owner
    should expect a delay rather than an immediate result: the marker reaches
    Mender on the next inventory poll, which owl-os sets to 600 seconds.
    """
    from app import remote_access

    enabled = str(request.form.get('enabled', '')).lower() in ('1', 'true', 'on', 'yes')
    ok, error = remote_access.set_enabled(enabled)
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, **remote_access.status()})

