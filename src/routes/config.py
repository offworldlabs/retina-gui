import time

from flask import Blueprint, abort, jsonify, redirect, render_template, request, url_for
from pydantic import ValidationError

from apply_service import ConfigChangeRefused
from config_schema import (
    LOCATION_COORDINATE_FIELDS,
    AdsbTruthConfig,
    CaptureFormConfig,
    LocationFormConfig,
    ProcessingFormConfig,
    Tar1090Config,
    model_values,
)
from form_utils import schema_to_form_fields
from node_name import MAX_LENGTH as NAME_MAX_LENGTH
from node_name import receiver_name

bp = Blueprint('config', __name__)


# The wizard polls this mid-wizard, and a redirect would hang it. Matched
# exactly, not by prefix: /config/apply and /config/save must stay blocked.
# Same rule as _CALIBRATION_ALLOWED_PREFIXES in app.py.
# See docs/features/config-editor.md#guards
_WIZARD_ALLOWED_PATHS = ('/config/apply/status',)


@bp.before_request
def _check_wizard_not_active():
    """Block config access while the setup wizard is in progress."""
    from app import device_state
    if request.path in _WIZARD_ALLOWED_PATHS:
        return None
    if device_state.is_setup_wizard_in_progress():
        return redirect('/set-up')


@bp.context_processor
def _remote_access_context():
    """Remote access, contact and claim state, for every template this blueprint renders.

    A context processor rather than per-call arguments, so config.html's render
    points cannot drift apart.
    See docs/features/config-editor.md#administration-sections.
    """
    from app import (
        REMOTE_ACCESS_DOMAIN,
        device_state,
        mender_connect,
        read_node_id,
        remote_access,
        telemetry_status,
    )
    from remote_access import tunnel_status

    # Claim state is retina-telemetry's; None when it is not running or predates
    # the claim. See docs/features/config-editor.md#node-claim
    telemetry = telemetry_status.read()

    return {
        'remote_access': remote_access.status(),
        # Empty when nothing was ever given, which is the ordinary case.
        'contact': device_state.get_telemetry_contact(),
        # Stored address; `claim_state` is what the server did with it. They
        # can disagree until retina-telemetry's next pass.
        'claim': device_state.get_telemetry_claim(),
        'claim_state': (telemetry or {}).get('claim'),
        'claim_reported': telemetry is not None and not (telemetry or {}).get('stale'),
        # The server's name for this node, or None before it has registered.
        # See docs/features/config-editor.md#identifiers
        'server_node_ref': (telemetry or {}).get('node_ref'),
        # The *enforced* shell state, read back from mender-connect's config,
        # not what we recorded. None when that config cannot be read.
        'shell_enforced': mender_connect.is_shell_enabled(),
        # Asked of systemd: whether the connector is up is all the node can know.
        'remote_tunnel': tunnel_status() if remote_access.is_enabled() else 'off',
        # Derived, not reported, so it can be shown before provisioning.
        'remote_host': f"{read_node_id()}.{REMOTE_ACCESS_DOMAIN}",
    }


@bp.route("/config")
def config_page():
    """Configuration page with all settings."""
    return _render_config_page()


def _processing_fields(config, processing_flat=None):
    """The Processing section's fields: from `config`, or from a rejected
    submission when one is given. None outside developer mode, which the page
    reads as "no Processing section". See docs/features/config-editor.md#developer-mode.
    """
    from app import device_state
    from config_manager import ConfigManager
    if not device_state.is_developer_mode():
        return None
    if processing_flat is None:
        processing_flat = ConfigManager.flatten_processing_for_form(config.get('process', {}))
    return schema_to_form_fields(ProcessingFormConfig, processing_flat)


def _render_config_page(config_errors=None, processing_flat=None):
    """The page from the merged config. A rejected Processing save passes its
    errors and submitted values, so only that section shows the submission."""
    from app import DEV_MODE, config_mgr, device_state, node_name, ssh_keys

    config = config_mgr.load_merged_config()
    retina_installed = config_mgr.is_retina_node_installed() or DEV_MODE or request.args.get('demo') == '1'

    capture_flat = config_mgr.flatten_capture_for_form(config.get('capture', {}))
    capture_fields = schema_to_form_fields(CaptureFormConfig, capture_flat)

    location_flat = config_mgr.flatten_location_for_form(config.get('location', {}))
    location_fields = schema_to_form_fields(LocationFormConfig, location_flat)

    truth_adsb_values = (config.get('truth', {}) or {}).get('adsb', {}) or {}
    truth_fields = schema_to_form_fields(AdsbTruthConfig, truth_adsb_values)

    tar1090_values = config_mgr.parse_tar1090_adsb_source(config)
    tar1090_fields = schema_to_form_fields(Tar1090Config, tar1090_values)

    return render_template("config.html",
                           retina_installed=retina_installed,
                           processing_fields=_processing_fields(config, processing_flat),
                           capture_fields=capture_fields,
                           location_fields=location_fields,
                           truth_fields=truth_fields,
                           tar1090_fields=tar1090_fields,
                           towers_cache=device_state.get_towers_cache(),
                           config_errors=config_errors,
                           node_name=node_name.get(),
                           node_name_max_length=NAME_MAX_LENGTH,
                           ssh_keys=ssh_keys.get_keys())


# blah2 re-posts its overload state at least every 2 s, so an older one means
# blah2 has stopped and the state is unknown, not current.
# See docs/features/config-editor.md#overload-indicator
OVERLOAD_STALE_MS = 10_000
_OVERLOAD_KEYS = ('overloadA', 'overloadB', 'overloadCountA', 'overloadCountB')


@bp.route("/config/rf-status")
def rf_status():
    """Live per-tuner peak dBFS, plus the overload flags and onset counts while
    they are fresh, from blah2's API."""
    from app import blah2_client
    status = blah2_client.get_rf_status() or {}
    if status:
        overload = blah2_client.get_overload_status() or {}
        received = overload.get('receivedAt')
        if (isinstance(received, (int, float))
                and time.time() * 1000 - received <= OVERLOAD_STALE_MS):
            status.update({k: overload[k] for k in _OVERLOAD_KEYS if k in overload})
    return jsonify(status)


@bp.route("/ssh-keys", methods=["POST"])
def add_key():
    from app import ssh_keys
    from ssh_keys import SSHKeyManager

    key = request.form.get("ssh_key", "").strip()
    if key and SSHKeyManager.is_valid_ssh_key(key):
        ssh_keys.add_key(key)
    return redirect(url_for("config.config_page"))


@bp.route("/ssh-keys/delete", methods=["POST"])
def delete_key():
    from app import ssh_keys

    key = request.form.get("ssh_key", "")
    if key:
        ssh_keys.remove_key(key)
    return redirect(url_for("config.config_page"))


@bp.route("/node-name", methods=["POST"])
def set_node_name():
    """Rename this node.

    Only a label (fleet page, DNS-SD TXT record): nothing addresses the node
    by it. See docs/features/config-editor.md#identifiers.
    """
    from app import node_name

    ok, error = node_name.set(request.form.get("name", ""))
    if not ok:
        return jsonify({"ok": False, "error": error}), 400
    return jsonify({"ok": True, "name": node_name.get()})


# One YAML value (host,port,protocol): complete, or empty with adsblol_fallback
# on. Checked here, not in the schema, so each error lands on its own box.
# See docs/features/config-editor.md#ads-b-source
_ADSB_SOURCE_FIELDS = ("adsb_source_host", "adsb_source_port", "adsb_source_protocol")


def _location_errors(location_flat):
    """A form-level error for a geometry that is neither complete nor empty.

    A missing coordinate becomes NaN in blah2 and the radar silently associates
    nothing. See docs/features/config-editor.md#location-all-or-nothing.
    """
    missing = [f for f in LOCATION_COORDINATE_FIELDS if location_flat.get(f) in (None, "")]
    if not missing or len(missing) == len(LOCATION_COORDINATE_FIELDS):
        return {}

    # Form-level by choice: the rule is about the group, so one sentence beats
    # up to five boxes repeating it.
    return {"_form": (
        "A location needs all six coordinates or none. Missing: "
        + ", ".join(f.replace("_", " ") for f in missing)
        + ". Clear all six to leave this node unsited."
    )}


def _adsb_source_errors(tar1090_data):
    """Per-field errors for a source that is neither complete nor legitimately empty."""
    missing = [f for f in _ADSB_SOURCE_FIELDS if tar1090_data.get(f) in (None, "")]
    if not missing:
        return {}

    if len(missing) == len(_ADSB_SOURCE_FIELDS):
        # Nothing to point at is fine only when adsb.lol is covering for it.
        if tar1090_data.get("adsblol_fallback"):
            return {}
        reason = "an ADS-B source is required unless adsb.lol fallback is turned on"
    else:
        # A partial set joins into a malformed "host,," that tar1090 accepts
        # and then never connects on.
        reason = ("required when an ADS-B source is set "
                  "(clear all three to feed tar1090 from adsb.lol instead)")

    return {f"tar1090.{f}": reason for f in missing}


@bp.route("/config/save", methods=["POST"])
def save_config():
    """Save config form data to user.yml."""
    from app import config_mgr, get_node_id, node_name
    from config_manager import ConfigManager

    capture_flat, location_flat, truth_data, tar1090_data = ConfigManager.parse_flat_form_data(request.form.to_dict())

    # The receiver is this node, so its name is decided here rather than posted:
    # the browser cannot send a different one, and it stays a string. Only when
    # the location is being written at all, so a save of another section is not
    # turned into a location write. See docs/features/config-editor.md#receiver-name
    if location_flat:
        location_flat['rx_name'] = receiver_name(node_name.get(), get_node_id())

    all_errors = {}

    # Refuse the save too, not just the apply: otherwise unapplied changes sit
    # in user.yml and get swept into the next merge.
    # See docs/features/config-editor.md#guards
    from app import calibrator
    from app import device_state as _device_state
    if calibrator.is_running() or _device_state.is_calibration_locked()[0]:
        all_errors['_form'] = ("Auto-calibration is running. Cancel it before "
                               "changing configuration.")

    if capture_flat:
        try:
            CaptureFormConfig(**capture_flat)
        except ValidationError as e:
            all_errors.update(ConfigManager.format_validation_errors(e, 'capture'))

    if location_flat:
        try:
            LocationFormConfig(**location_flat)
        except ValidationError as e:
            all_errors.update(ConfigManager.format_validation_errors(e, 'location'))
        for key, message in _location_errors(location_flat).items():
            all_errors.setdefault(key, message)

    if truth_data:
        try:
            AdsbTruthConfig(**truth_data)
        except ValidationError as e:
            all_errors.update(ConfigManager.format_validation_errors(e, 'truth'))

    if tar1090_data:
        try:
            Tar1090Config(**tar1090_data)
        except ValidationError as e:
            all_errors.update(ConfigManager.format_validation_errors(e, 'tar1090'))
        all_errors.update(_adsb_source_errors(tar1090_data))

    if all_errors:
        from app import DEV_MODE, device_state, ssh_keys
        return render_template("config.html",
                               retina_installed=config_mgr.is_retina_node_installed() or DEV_MODE or request.args.get('demo') == '1',
                               processing_fields=_processing_fields(config_mgr.load_merged_config()),
                               capture_fields=schema_to_form_fields(CaptureFormConfig, capture_flat),
                               location_fields=schema_to_form_fields(LocationFormConfig, location_flat),
                               truth_fields=schema_to_form_fields(AdsbTruthConfig, truth_data),
                               tar1090_fields=schema_to_form_fields(Tar1090Config, tar1090_data),
                               towers_cache=device_state.get_towers_cache(),
                               config_errors=all_errors,
                               node_name=node_name.get(),
                               node_name_max_length=NAME_MAX_LENGTH,
                               ssh_keys=ssh_keys.get_keys())

    capture_nested = ConfigManager.unflatten_capture_from_form(capture_flat)
    location_nested = ConfigManager.unflatten_location_from_form(location_flat)

    tar1090_nested = {}
    if tar1090_data:
        host = tar1090_data.pop('adsb_source_host', '')
        port = tar1090_data.pop('adsb_source_port', '')
        protocol = tar1090_data.pop('adsb_source_protocol', '')
        # Write "" rather than omitting the key, or the merge restores
        # default.yml's source and undoes the clearing.
        tar1090_nested['adsb_source'] = (f"{host},{port},{protocol}"
                                         if host or port or protocol else "")
        tar1090_nested.update(tar1090_data)

    merged_config = config_mgr.load_merged_config()
    existing_user = config_mgr.load_user_config()

    new_user_config = {}
    for key in existing_user:
        # retina_tracker is still dropped though the page no longer writes it:
        # the tracker stopped reading min_snr, so a stale key is cleared out on
        # the next save. See docs/features/config-editor.md#no-tracking-section
        if key not in ('capture', 'location', 'truth', 'tar1090', 'retina_tracker'):
            new_user_config[key] = existing_user[key]

    if capture_flat:
        capture_overrides = config_mgr.compute_user_overrides(capture_nested, merged_config, existing_user, 'capture')
        if capture_overrides:
            new_user_config['capture'] = capture_overrides

    if location_flat:
        location_overrides = config_mgr.compute_user_overrides(location_nested, merged_config, existing_user, 'location')
        if location_overrides:
            new_user_config['location'] = location_overrides

    if truth_data:
        truth_nested = {'adsb': truth_data}
        truth_overrides = config_mgr.compute_user_overrides(truth_nested, merged_config, existing_user, 'truth')
        if truth_overrides:
            new_user_config['truth'] = truth_overrides

    if tar1090_nested:
        tar1090_overrides = config_mgr.compute_user_overrides(tar1090_nested, merged_config, existing_user, 'tar1090')
        if tar1090_overrides:
            new_user_config['tar1090'] = tar1090_overrides

    config_mgr.save_user_config(new_user_config)
    return redirect(url_for("config.config_page") + "?saved=1")


def _processing_errors(values):
    """Per-field errors for Processing values that pass on their own but not
    together. blah2 refuses to start on the first, so it must not be saved."""
    errors = {}
    max_reported = values.get('detection_maxReportedDoppler')
    if max_reported is not None and max_reported < values['detection_minDoppler']:
        errors['process.detection_maxReportedDoppler'] = "must be at least Minimum Doppler"
    if values['detection_minDelay'] >= values['ambiguity_delayMax']:
        errors['process.detection_minDelay'] = "must be below Maximum Delay, or nothing is tested"
    return errors


@bp.route("/config/processing/save", methods=["POST"])
def save_processing():
    """Save the Processing section to user.yml on its own, then apply.

    Its own form, because the section sits above the Node group, outside the
    form Apply changes submits. See docs/features/config-editor.md#processing-section.
    Not found outside developer mode, so the server refuses it as well as the
    page hiding it.
    """
    from app import calibrator, config_mgr, device_state
    from config_manager import ConfigManager

    if not device_state.is_developer_mode():
        abort(404)

    processing_flat = ConfigManager.parse_processing_form(request.form.to_dict())
    errors = {}

    # Same guard as /config/save. See docs/features/config-editor.md#guards
    if calibrator.is_running() or device_state.is_calibration_locked()[0]:
        errors['_form'] = ("Auto-calibration is running. Cancel it before "
                           "changing configuration.")

    values = None
    try:
        values = model_values(ProcessingFormConfig(**processing_flat))
    except ValidationError as e:
        errors.update(ConfigManager.format_validation_errors(e, 'process'))
    if values is not None:
        errors.update(_processing_errors(values))

    if errors:
        return _render_config_page(config_errors=errors, processing_flat=processing_flat)

    merged_config = config_mgr.load_merged_config()
    existing_user = config_mgr.load_user_config()
    nested = ConfigManager.unflatten_processing_from_form(values)
    overrides = config_mgr.compute_user_overrides(nested, merged_config, existing_user, 'process')
    process = ConfigManager.merge_processing_overrides(existing_user.get('process'), overrides)

    new_user_config = dict(existing_user)
    if process:
        new_user_config['process'] = process
    else:
        new_user_config.pop('process', None)
    config_mgr.save_user_config(new_user_config)
    return redirect(url_for("config.config_page") + "?saved=1")


@bp.route("/config/apply", methods=["POST"])
def apply_config():
    """Start a config apply on apply_service's thread and return immediately.

    Poll /config/apply/status. In spectrum mode only config-merger runs; blah2
    must stay stopped. See docs/features/config-editor.md#apply.
    """
    from app import DEV_MODE, apply_service, config_mgr, device_state

    if DEV_MODE:
        return jsonify({"success": True, "status": apply_service.request()})

    if not config_mgr.is_retina_node_installed():
        return jsonify({"success": False, "error": "retina-node not installed"}), 400

    # Refuse during a Mender install rather than queue behind it: its docker
    # commands are outside the restart lock. See docs/features/config-editor.md#guards
    in_progress, reason = device_state.is_any_update_in_progress()
    if in_progress:
        return jsonify({"success": False,
                        "error": f"{reason}. Apply your changes once it finishes."}), 409

    # An in-flight calibration is refused inside request(), so no route can
    # skip it. See ApplyService.ConfigChangeRefused.
    try:
        return jsonify({"success": True, "status": apply_service.request()}), 202
    except ConfigChangeRefused as refused:
        return jsonify({"success": False, "error": refused.reason}), 409


@bp.route("/config/apply/status", methods=["GET"])
def apply_config_status():
    """Progress of the current or most recent config apply."""
    from app import apply_service
    return jsonify(apply_service.get_status())
