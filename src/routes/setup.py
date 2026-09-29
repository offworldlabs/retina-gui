import re

from flask import Blueprint, jsonify, render_template, request
from pydantic import ValidationError

from config_manager import ConfigManager
from config_schema import (
    CLAIM_EMAIL_PATTERN,
    CONTACT_COUNTRY_PATTERN,
    CONTACT_FIELDS,
    ClaimFormConfig,
    ContactFormConfig,
)

bp = Blueprint('setup', __name__)


@bp.route("/set-up")
def wizard():
    """Setup wizard: full-page multi-step first-boot flow.

    See docs/features/setup-wizard.md.
    """
    from app import CARTO_API_KEY, DEV_MODE, device_state, get_node_id, mender, telemetry_status

    resume_step = device_state.get_setup_wizard_step()
    owl_os_version, retina_node_version = mender.get_versions()
    node_id = get_node_id()
    # From completion history, not from whether retina-node is installed.
    # See docs/features/setup-wizard.md#first-run-or-re-run
    is_rerun = device_state.has_completed_setup_wizard()
    # Lets a reload onto the tower step search again.
    # See docs/features/setup-wizard.md#rehydrating-the-tower-search
    towers_cache = device_state.get_towers_cache()
    contact = device_state.get_telemetry_contact()
    # An owned node shows its owner and can only be skipped past.
    # See docs/features/setup-wizard.md#node-claim
    claim = device_state.get_telemetry_claim()
    claim_state = (telemetry_status.read() or {}).get('claim') or {}
    claim_owned = claim_state.get('state') == 'owned'
    if claim_owned and claim_state.get('email'):
        claim = {'email': claim_state['email']}

    demo_mode = request.args.get('demo') == '1'
    if demo_mode:
        is_rerun = True

    return render_template("setup.html",
                           resume_step=resume_step,
                           towers_cache=towers_cache,
                           contact=contact,
                           claim=claim,
                           claim_owned=claim_owned,
                           node_id=node_id,
                           owl_os_version=owl_os_version,
                           retina_node_version=retina_node_version,
                           is_rerun=is_rerun,
                           dev_mode=DEV_MODE,
                           carto_api_key=CARTO_API_KEY,
                           demo_mode=demo_mode)


@bp.route("/set-up/save-step", methods=["POST"])
def save_step():
    """Save current wizard step (persists across reboots)."""
    from app import device_state

    data = request.get_json()
    if not data or "step" not in data:
        return jsonify({"success": False, "error": "Missing 'step' field"}), 400
    if data["step"] == "complete":
        device_state.clear_setup_wizard()
        device_state.mark_setup_wizard_completed()
    else:
        device_state.save_setup_wizard_step(data["step"])
    return jsonify({"success": True})


@bp.route("/set-up/consent", methods=["POST"])
def consent():
    """Record acceptance of the terms shown on the agreements step.

    Posted from that step, not at completion, so a re-run is a re-consent
    path. See docs/features/setup-wizard.md#agreements.
    """
    from app import device_state

    device_state.save_telemetry_consent()
    return jsonify({"success": True})


@bp.route("/set-up/contact", methods=["POST"])
def contact():
    """Record whom to contact about this node, from the wizard or Settings.

    One route for both surfaces so they store the same shape. Every field is
    optional, and all-empty removes the record.
    See docs/features/setup-wizard.md#contact-details.
    """
    from app import device_state

    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"success": False, "error": "Missing JSON body"}), 400

    submitted = {f: _blank_to_none(data.get(f)) for f in CONTACT_FIELDS}

    # The shape check the model cannot carry portably; see
    # CONTACT_COUNTRY_PATTERN. Keyed by field so the page can mark that box.
    country = submitted["country"]
    if country is not None and not re.match(CONTACT_COUNTRY_PATTERN, country):
        return jsonify({"success": False, "errors": {
            "country": "Use the two-letter country code for the phone number, such as US or GB.",
        }}), 400
    if country is not None:
        submitted["country"] = country.upper()

    try:
        validated = ContactFormConfig(**submitted)
    except ValidationError as e:
        return jsonify({"success": False,
                        "errors": ConfigManager.format_validation_errors(e, "contact")}), 400

    # `submitted`, not the model's dump: this runs on pydantic v1 and v2.
    device_state.save_telemetry_contact(submitted)
    return jsonify({"success": True, "stored": not validated.is_empty})


@bp.route("/set-up/claim", methods=["POST"])
def claim():
    """Ask for a claim link to be sent to an address, or clear the address.

    Shared by the wizard and the configuration page. Every address is an ask
    for a link; an empty box removes the record. The shape is checked here
    because a wrong address hands the node to a stranger.
    See docs/features/setup-wizard.md#node-claim.
    """
    from app import device_state

    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"success": False, "error": "Missing JSON body"}), 400

    email = _blank_to_none(data.get("email"))

    if email is not None and not re.match(CLAIM_EMAIL_PATTERN, email):
        return jsonify({"success": False, "errors": {
            "email": "That does not look like an email address.",
        }}), 400

    try:
        ClaimFormConfig(email=email)
    except ValidationError as e:
        return jsonify({"success": False,
                        "errors": ConfigManager.format_validation_errors(e, "claim")}), 400

    stored = device_state.save_telemetry_claim(email)
    return jsonify({"success": True, "stored": bool(stored)})


def _blank_to_none(value):
    """Map an empty or whitespace-only box to None ("nothing here")."""
    if isinstance(value, str):
        value = value.strip()
    return value or None


@bp.route("/set-up/complete", methods=["POST"])
def complete():
    """Force radar mode at the end of the wizard.

    See docs/features/setup-wizard.md#completion.
    """
    from app import RETINA_NODE_PATH, config_mgr
    from routes.mode import _write_mode, enforce_radar_mode

    # Write radar to mode.txt before docker ops so the home page cannot race
    # and see spectrum mode while enforce_radar_mode is still running.
    _write_mode('radar')

    if config_mgr.is_retina_node_installed():
        enforce_radar_mode(RETINA_NODE_PATH)

    return jsonify({"success": True})
