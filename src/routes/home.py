from flask import Blueprint, jsonify, redirect, render_template, request

from node_name import MAX_LENGTH as NAME_MAX_LENGTH
from routes.fleet import entry_point_response, is_entry_point
from routes.mode import get_current_mode

bp = Blueprint('home', __name__)


@bp.route("/")
def index():
    """Home page with node ID, services, and SSH keys."""
    from app import config_mgr, device_state, get_node_id, mender, node_name, ssh_keys, telemetry_status

    # Someone who typed owl.local is asking for "a node", and on a network with
    # several of them the honest answer is the list, not whichever one happened
    # to win the race to answer. Checked before the setup wizard: a visitor
    # looking for the fleet should not be dropped into one node's first-run
    # wizard just because that node is the one that replied.
    if is_entry_point(request.host):
        response = entry_point_response()
        if response is not None:
            return response

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
                           node_name=node_name.get(),
                           node_name_max_length=NAME_MAX_LENGTH,
                           mode=get_current_mode())


@bp.route("/node-name", methods=["POST"])
def set_node_name():
    """Rename this node.

    The name is only a label — it is what the fleet page shows on this node's
    card instead of ret4c844c20, and it reaches the other nodes through the
    DNS-SD TXT record. Nothing addresses the node by it, so a rename cannot
    break a bookmark or an SSH config.
    """
    from app import node_name

    ok, error = node_name.set(request.form.get("name", ""))
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, "name": node_name.get()})


@bp.route("/eula")
def eula():
    """Display EULA page."""
    return render_template("eula.html")
