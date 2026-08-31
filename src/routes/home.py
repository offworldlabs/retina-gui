from flask import Blueprint, redirect, render_template, request

from routes.mode import get_current_mode

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


@bp.route("/eula")
def eula():
    """Display EULA page."""
    return render_template("eula.html")
