from flask import Blueprint, render_template

bp = Blueprint('home', __name__)


@bp.route("/")
def index():
    """Home page with node ID, services, and SSH keys."""
    from app import ssh_keys, config_mgr, mender, device_state, get_node_id

    keys = ssh_keys.get_keys()
    node_id = get_node_id()
    owl_os_version, retina_node_version = mender.get_versions()

    setup_needed = retina_node_version is None
    setup_in_progress = device_state.is_setup_wizard_in_progress()

    config = config_mgr.load_merged_config()
    location = config.get('location', {}) or {}
    tx = location.get('tx', {}) or {}
    tx_name = tx.get('name', '')

    return render_template("index.html",
                           ssh_keys=keys,
                           node_id=node_id,
                           owl_os_version=owl_os_version,
                           retina_node_version=retina_node_version,
                           setup_needed=setup_needed,
                           setup_in_progress=setup_in_progress,
                           tx_name=tx_name)


@bp.route("/eula")
def eula():
    """Display EULA page."""
    return render_template("eula.html")
