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
    """Setup wizard — full-page multi-step first-boot flow."""
    from app import DEV_MODE, device_state, get_node_id, mender

    resume_step = device_state.get_setup_wizard_step()
    owl_os_version, retina_node_version = mender.get_versions()
    node_id = get_node_id()
    # A node can ship with retina-node pre-installed but never have had the
    # wizard run on it — that's still a first run, so re-run status is based
    # on wizard completion history, not on whether a package is present.
    is_rerun = device_state.has_completed_setup_wizard()
    # The wizard is forward-only, so a reload is the whole recovery story
    # and it has to land somewhere usable. The tower step's search
    # parameters otherwise live only in a page-scoped variable, so a
    # reload onto that step had nothing to search with and no way back to
    # the location step to get it. The cache already holds the coordinates
    # the last search used.
    towers_cache = device_state.get_towers_cache()
    # Prefills the contact step. Empty when nothing was ever given, which
    # is the ordinary case and renders as empty boxes.
    contact = device_state.get_telemetry_contact()

    demo_mode = request.args.get('demo') == '1'
    if demo_mode:
        is_rerun = True

    return render_template("setup.html",
                           resume_step=resume_step,
                           towers_cache=towers_cache,
                           contact=contact,
                           node_id=node_id,
                           owl_os_version=owl_os_version,
                           retina_node_version=retina_node_version,
                           is_rerun=is_rerun,
                           dev_mode=DEV_MODE,
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

    Deliberately posted from that step rather than at wizard completion. Every
    node already in the field has completed the wizard and will never see it
    again, so re-running /set-up is the re-consent path — and writing here
    means an owner can tick, continue, and close the tab without going through
    the location and tower steps or the docker work /set-up/complete triggers.

    retina-telemetry re-reads the file on every state derivation rather than
    caching it at startup, so this takes effect within seconds and needs no
    restart or ordering with that container.
    """
    from app import device_state

    device_state.save_telemetry_consent()
    return jsonify({"success": True})


@bp.route("/set-up/contact", methods=["POST"])
def contact():
    """Record whom to contact about this node, from the wizard or Settings.

    One route for both surfaces, so the wizard step and the How we reach you
    section on the configuration page cannot drift into storing different shapes.

    Every field is optional and skipping writes nothing at all. That is not a
    convenience: the spec says a node with nothing to report never calls the
    endpoint, so an absent file is how the node says it has nothing, and an
    empty document would be indistinguishable from an owner who cleared theirs.

    retina-telemetry re-reads the file rather than caching it, so a change here
    reaches the server without a restart or any ordering with that container.
    """
    from app import device_state

    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"success": False, "error": "Missing JSON body"}), 400

    submitted = {f: _blank_to_none(data.get(f)) for f in CONTACT_FIELDS}

    # The shape check the model cannot carry portably; see
    # CONTACT_COUNTRY_PATTERN. Attached to its own field so the page can mark
    # the box rather than showing a form-level complaint about one input.
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

    # `submitted` rather than the model's own dump: `.dict()` is pydantic v1
    # and `.model_dump()` is v2, and this runs against both. The model is here
    # to validate, and the two carry identical values by construction.
    device_state.save_telemetry_contact(submitted)
    return jsonify({"success": True, "stored": not validated.is_empty})


@bp.route("/set-up/claim", methods=["POST"])
def claim():
    """Ask for a claim link to be sent to an address, or clear the address.

    One route for both surfaces, as with the contact details, so the two cannot
    drift into storing different shapes.

    This is the only box on the page whose value reaches a stranger if it is
    wrong. The server mails the address a link, and clicking it binds this node
    to the account behind it, so the shape is checked here rather than left for
    the server to refuse thirty seconds later with nothing on screen to explain
    it. Nothing verifies that the address exists, and nothing can.

    Every address submitted here is an ask for a link, and an empty box
    removes the record. There is no way to store an address without asking,
    because a press the node cannot see is one that mails nothing. See
    device_state.save_telemetry_claim.

    retina-telemetry re-reads the file rather than caching it, so this reaches
    the server without a restart or any ordering between the two containers.
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
    """An empty box means "nothing here", which is also how it is cleared.

    Whitespace counts as empty: a space in the email field would otherwise be
    stored, sent, and shown back to the owner as if it were a detail.
    """
    if isinstance(value, str):
        value = value.strip()
    return value or None


@bp.route("/set-up/complete", methods=["POST"])
def complete():
    """Mark setup wizard as complete."""
    from app import RETINA_NODE_PATH, config_mgr
    from routes.mode import _write_mode, enforce_radar_mode

    # Write radar to mode.txt before docker ops so the home page cannot race
    # and see spectrum mode while enforce_radar_mode is still running.
    _write_mode('radar')

    if config_mgr.is_retina_node_installed():
        enforce_radar_mode(RETINA_NODE_PATH)

    return jsonify({"success": True})
