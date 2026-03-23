from flask import Blueprint, render_template, request, jsonify

bp = Blueprint('setup', __name__)


@bp.route("/set-up")
def wizard():
    """Setup wizard — full-page multi-step first-boot flow."""
    from app import mender, device_state, get_node_id, DEV_MODE

    resume_step = device_state.get_setup_wizard_step()
    owl_os_version, retina_node_version = mender.get_versions()
    node_id = get_node_id()
    highest_step = device_state.get_setup_wizard_highest_step()
    is_rerun = retina_node_version is not None

    return render_template("setup.html",
                           resume_step=resume_step,
                           highest_step=highest_step,
                           node_id=node_id,
                           owl_os_version=owl_os_version,
                           retina_node_version=retina_node_version,
                           is_rerun=is_rerun,
                           dev_mode=DEV_MODE)


@bp.route("/set-up/save-step", methods=["POST"])
def save_step():
    """Save current wizard step (persists across reboots)."""
    from app import device_state

    data = request.get_json()
    if not data or "step" not in data:
        return jsonify({"success": False, "error": "Missing 'step' field"}), 400
    device_state.save_setup_wizard_step(data["step"])
    return jsonify({"success": True})


@bp.route("/set-up/complete", methods=["POST"])
def complete():
    """Mark setup wizard as complete."""
    from app import device_state

    device_state.clear_setup_wizard()
    return jsonify({"success": True})
