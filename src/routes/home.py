import os

from flask import Blueprint, redirect, render_template, request, send_from_directory, url_for

from routes.sdr import get_current_mode

bp = Blueprint('home', __name__)


@bp.route("/")
def index():
    """Home page with node ID, services, and SSH keys.

    Host-agnostic. Arriving on owl.local and arriving on this node's own
    ret<node_id>.local produce the same page: the shared alias is just a way in,
    and the banner is what moves you between nodes from there.
    """
    from app import config_mgr, device_state, get_node_id, mender, ssh_keys, telemetry_status

    if device_state.is_setup_wizard_in_progress():
        return redirect('/set-up')

    keys = ssh_keys.get_keys()
    node_id = get_node_id()
    owl_os_version, retina_node_version = mender.get_versions()

    setup_needed = retina_node_version is None
    setup_in_progress = setup_needed and device_state.is_setup_wizard_in_progress()

    config = config_mgr.load_merged_config()
    location = config.get('location', {}) or {}
    # `or ''` rather than a .get default: on an unsited node the key exists
    # with a null value, so the default never fires and the page renders "None".
    tx = location.get('tx', {}) or {}
    tx_name = tx.get('name') or ''
    rx = location.get('rx', {}) or {}
    rx_name = rx.get('name') or ''

    # None when the telemetry package isn't installed, which is not a fault —
    # the card is simply absent. See telemetry_status.py.
    telemetry = telemetry_status.read()

    if request.args.get('demo') == '1':
        retina_node_version = retina_node_version or '0.9.0-demo'
        setup_needed = False
        setup_in_progress = False
        tx_name = tx_name or 'KPIX - 706 MHz UHF'
        rx_name = rx_name or 'San Francisco, CA'
        telemetry = telemetry or {
            'state': 'streaming', 'detail': None, 'node_ref': 'nde4f2k9xq7m3b8',
            'node_id': node_id, 'stale': False, 'written_at': None,
        }

    return render_template("index.html",
                           ssh_keys=keys,
                           node_id=node_id,
                           owl_os_version=owl_os_version,
                           retina_node_version=retina_node_version,
                           setup_needed=setup_needed,
                           setup_in_progress=setup_in_progress,
                           tx_name=tx_name,
                           rx_name=rx_name,
                           telemetry=telemetry,
                           mode=get_current_mode())


# The two agreements, as PDFs. Read: a redirect to the static file, so the
# browser shows it. Download (?download=1): the same file sent as an
# attachment under a readable name, because the download attribute on a link
# is advice some browsers ignore. The files ship in their own change; until
# one is there, both links land on a page saying the document is still being
# finalised. See docs/features/setup-wizard.md#agreements.
LEGAL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "..", "static", "legal")
LEGAL_DOCUMENTS = {
    "eula": ("eula.pdf", "RETINA-EULA.pdf", "End User Licence Agreement"),
    "privacy-policy": ("privacy-policy.pdf", "Offworld-Labs-Privacy-Policy.pdf", "Privacy Policy"),
}


def _legal(name):
    filename, download_name, title = LEGAL_DOCUMENTS[name]
    if not os.path.exists(os.path.join(LEGAL_DIR, filename)):
        return render_template("legal_pending.html", title=title)
    if request.args.get("download") == "1":
        return send_from_directory(os.path.abspath(LEGAL_DIR), filename, as_attachment=True,
                                   download_name=download_name, mimetype="application/pdf")
    return redirect(url_for("static", filename="legal/" + filename))


@bp.route("/eula")
def eula():
    """The End User Licence Agreement: read it, or ?download=1 to save it."""
    return _legal("eula")


@bp.route("/privacy-policy")
def privacy_policy():
    """The Privacy Policy: read it, or ?download=1 to save it."""
    return _legal("privacy-policy")
