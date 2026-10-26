import re

from flask import Blueprint, jsonify, render_template, request
from pydantic import ValidationError

from apply_service import ConfigChangeRefused
from config_manager import ConfigManager
from config_schema import (
    CLAIM_EMAIL_PATTERN,
    CONTACT_COUNTRY_PATTERN,
    CONTACT_FIELDS,
    ClaimFormConfig,
    ContactFormConfig,
)
from node_name import MAX_LENGTH as NAME_MAX_LENGTH

bp = Blueprint('setup', __name__)

# Step keys the wizard no longer has, and where a run that saved one resumes.
# The saved resume point is only a step name, and the build that reads it
# can be newer than the one that wrote it: the OS update lands mid-wizard.
# The old flow's claim was its third step; the claim is ninth now, keyed
# `account` so the old name cannot match it, and a node that was on the old
# claim resumes at the first step it has not seen. A step that moves in a
# later rework gets a new key and a row here the same way.
# See docs/features/setup-wizard.md#persistence-and-resume.
LEGACY_RESUME_STEPS = {'claim': 'system'}


def resume_point(step):
    """Where a saved step resumes: itself, or its mapping for a retired key."""
    return LEGACY_RESUME_STEPS.get(step, step)


@bp.route("/set-up")
def wizard():
    """Setup wizard: full-page multi-step first-boot flow.

    See docs/features/setup-wizard.md.
    """
    from app import (
        CARTO_API_KEY,
        DEV_MODE,
        config_mgr,
        device_state,
        get_node_id,
        mender,
        node_name,
        remote_access,
        telemetry_status,
    )
    from routes.fleet import DASHBOARD_URL
    from routes.towers import MAX_CACHED_TOWERS, MAX_CACHED_TOWERS_BY_HAND

    resume_step = resume_point(device_state.get_setup_wizard_step())
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
                           max_cached_towers=MAX_CACHED_TOWERS,
                           max_kept_towers=MAX_CACHED_TOWERS_BY_HAND,
                           contact=contact,
                           # The support switches, as the Configuration page
                           # shows them. See docs/features/setup-wizard.md#remote-support
                           remote_access=remote_access.status(),
                           # The filters the calibrate step offers before a
                           # run. See docs/features/setup-wizard.md#filters
                           capture_filters=current_capture_filters(config_mgr),
                           capture_fc_mhz=current_capture_fc_mhz(config_mgr),
                           node_name=node_name.get(),
                           node_name_max_length=NAME_MAX_LENGTH,
                           claim=claim,
                           claim_owned=claim_owned,
                           # Where a claimed node appears, so the claim step
                           # can say which account the link binds it to.
                           dashboard_url=DASHBOARD_URL,
                           node_id=node_id,
                           owl_os_version=owl_os_version,
                           retina_node_version=retina_node_version,
                           is_rerun=is_rerun,
                           dev_mode=DEV_MODE,
                           carto_api_key=CARTO_API_KEY,
                           demo_mode=demo_mode)


def current_capture_fc_mhz(config_mgr):
    """The configured centre frequency in MHz, or None when there is none.

    user.yml first, since the tower step writes there and the merge may not
    have run yet; then the merged config. The calibrate step's filter advice
    starts from it on a reload.
    """
    for load in (config_mgr.load_user_config, config_mgr.load_merged_config):
        try:
            fc = ((load() or {}).get("capture") or {}).get("fc")
        except Exception:
            continue
        try:
            if fc is not None and float(fc) > 0:
                return round(float(fc) / 1e6, 3)
        except (TypeError, ValueError):
            continue
    return None


def current_capture_filters(config_mgr):
    """The receiver's two notch filters, as the running config has them.

    The merged config is what blah2 reads; user.yml is consulted when there is
    no merged config yet (retina-node not installed). Absent keys read as off,
    matching what parse_flat_form_data assumes for an unticked box.
    """
    def read(nested):
        device = ((nested or {}).get("capture") or {}).get("device") or {}
        return {
            "dab_notch": bool(device.get("dabNotch", False)),
            "rf_notch": bool(device.get("rfNotch", False)),
        }

    try:
        merged = config_mgr.load_merged_config()
    except Exception:
        merged = None
    if merged and (merged.get("capture") or {}).get("device"):
        return read(merged)
    try:
        return read(config_mgr.load_user_config())
    except Exception:
        return read(None)


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


@bp.route("/set-up/filters", methods=["POST"])
def filters():
    """Set the receiver's two notch filters, then queue a config apply.

    The calibrate step offers them before a run because changing either one
    moves the signal level enough to need recalibrating. Writes only the two
    keys, so nothing else in the capture section is touched, and returns 202
    with the apply status when retina-node is installed, as /towers/select
    does. See docs/features/setup-wizard.md#filters.
    """
    from app import apply_service, config_mgr, device_state

    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"success": False, "error": "Missing JSON body"}), 400
    wanted = {}
    for field, key in (("dab_notch", "dabNotch"), ("rf_notch", "rfNotch")):
        if field in data:
            if not isinstance(data[field], bool):
                return jsonify({"success": False,
                                "error": f"{field} must be true or false"}), 400
            wanted[key] = data[field]
    if not wanted:
        return jsonify({"success": False, "error": "Nothing to set"}), 400

    user_config = dict(config_mgr.load_user_config())
    capture = dict(user_config.get("capture") or {})
    device = dict(capture.get("device") or {})
    device.update(wanted)
    capture["device"] = device
    user_config["capture"] = capture
    config_mgr.save_user_config(user_config)

    if not config_mgr.is_retina_node_installed():
        return jsonify({"success": True, "applied": False})

    in_progress, reason = device_state.is_any_update_in_progress()
    if in_progress:
        return jsonify({"success": False,
                        "error": f"{reason}. Change the filters once it finishes."}), 409

    try:
        return jsonify({"success": True, "status": apply_service.request()}), 202
    except ConfigChangeRefused as refused:
        return jsonify({"success": False, "error": refused.reason}), 409


@bp.route("/set-up/complete", methods=["POST"])
def complete():
    """Force radar mode at the end of the wizard.

    See docs/features/setup-wizard.md#completion.
    """
    from app import RETINA_NODE_PATH, config_mgr
    from routes.sdr import _write_mode, enforce_radar_mode

    # The naming step comes after the tower step, which wrote the receiver
    # name before there was a name to write. Refresh it here, before the
    # restart below: that restart's `up` runs config-merger first (retina-node's
    # compose makes the stack depend on it), so it merges the name too.
    # See docs/features/setup-wizard.md#name-your-node
    _refresh_receiver_name(config_mgr)

    # Write radar to mode.txt before docker ops so the home page cannot race
    # and see spectrum mode while enforce_radar_mode is still running.
    _write_mode('radar')

    if config_mgr.is_retina_node_installed():
        enforce_radar_mode(RETINA_NODE_PATH)

    return jsonify({"success": True})


def _refresh_receiver_name(config_mgr):
    """Rewrite location.rx.name in user.yml from the one rule for it.

    Returns True when the file changed. Only when a receiver is already
    recorded: a node without a location has nothing to name. Best effort,
    and never raises: completion must not fail over a label. Nothing on the
    page waits on this, so every failure is logged, or nobody would know.
    See docs/features/setup-wizard.md#name-your-node.
    """
    from app import app, get_node_id, node_name
    from node_name import receiver_name

    try:
        user_config = dict(config_mgr.load_user_config())
        rx = ((user_config.get("location") or {}).get("rx") or {})
        if not rx:
            return False
        wanted = receiver_name(node_name.get(), get_node_id())
        if rx.get("name") == wanted:
            return False
        location = dict(user_config["location"])
        location["rx"] = {**rx, "name": wanted}
        user_config["location"] = location
        config_mgr.save_user_config(user_config)
        return True
    except Exception as e:
        app.logger.error(f"Wizard completion: could not write the receiver name to user.yml: {e}")
        return False
