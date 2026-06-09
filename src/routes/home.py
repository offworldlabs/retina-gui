from flask import Blueprint, render_template, request, redirect
from routes.mode import get_current_mode

bp = Blueprint('home', __name__)


@bp.route("/")
def index():
    """Home page with node ID, services, and SSH keys."""
    from app import ssh_keys, config_mgr, mender, device_state, get_node_id

    if device_state.is_setup_wizard_in_progress():
        return redirect('/set-up')

    keys = ssh_keys.get_keys()
    node_id = get_node_id()
    owl_os_version, retina_node_version = mender.get_versions()

    setup_needed = retina_node_version is None
    setup_in_progress = setup_needed and device_state.is_setup_wizard_in_progress()

    config = config_mgr.load_merged_config()
    location = config.get('location', {}) or {}
    tx = location.get('tx', {}) or {}
    tx_name = tx.get('name', '')
    rx = location.get('rx', {}) or {}
    rx_name = rx.get('name', '')

    if request.args.get('demo') == '1':
        retina_node_version = retina_node_version or '0.9.0-demo'
        setup_needed = False
        setup_in_progress = False
        tx_name = tx_name or 'KPIX — 706 MHz UHF'
        rx_name = rx_name or 'San Francisco, CA'

    return render_template("index.html",
                           ssh_keys=keys,
                           node_id=node_id,
                           owl_os_version=owl_os_version,
                           retina_node_version=retina_node_version,
                           setup_needed=setup_needed,
                           setup_in_progress=setup_in_progress,
                           tx_name=tx_name,
                           rx_name=rx_name,
                           mode=get_current_mode())


@bp.route("/eula")
def eula():
    """Display EULA page."""
    return render_template("eula.html")
