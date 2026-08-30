from flask import Blueprint, jsonify, redirect, render_template, request, url_for
from pydantic import ValidationError

from apply_service import ConfigChangeRefused
from config_schema import (
    LOCATION_COORDINATE_FIELDS,
    AdsbTruthConfig,
    CaptureFormConfig,
    LocationFormConfig,
    RetinaTrackerConfig,
    Tar1090Config,
)
from form_utils import schema_to_form_fields
from node_name import MAX_LENGTH as NAME_MAX_LENGTH
from remote_access import MIN_PASSWORD_LENGTH

bp = Blueprint('config', __name__)


# The wizard's tower step polls /config/apply/status to know when the restart
# it queued has finished. Redirecting that hands fetch() an HTML page rather
# than an error: the request succeeds, .json() rejects, and the step re-polls
# a redirect forever with its spinner still up and its skip button hidden.
# Same rule as _CALIBRATION_ALLOWED_PREFIXES in app.py — never redirect the
# status endpoint the watching window depends on.
#
# Matched exactly, not by prefix: this route only reads status, while
# /config/apply and /config/save mutate and must stay blocked mid-wizard.
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
    """Remote access state, for every template this blueprint renders.

    Deliberately not passed per call site. config.html has two render points:
    /config, and the validation-error branch of /config/save. Handing them
    the same three arguments by hand is how the second one shipped without them.
    A context processor is the version of this that cannot drift when a third
    render point appears.

    remote_host is derived rather than reported: the hostname is a pure function
    of node_id and the zone, so the page can name the address before anything
    has provisioned it. Whether the tunnel is actually *up* is a separate
    question, and comes from retina-telemetry's status document once that side
    exists.
    """
    from flask import g

    from app import REMOTE_ACCESS_DOMAIN, read_node_id, remote_access
    from remote_access import tunnel_status

    # The password is rendered only where the owner expects it to be visible:
    # their own network. /config is reachable on the owner pathway too, and
    # showing it there would make "nobody off your network can see this" false:
    # someone the password was shared with could read it back off the page.
    # Withheld rather than masked, so it is not in the HTML at all.
    pathway = getattr(g, 'pathway', 'lan')

    return {
        'remote_access': remote_access.status(),
        # Asked of systemd, not of a server. Nothing on the node is told whether
        # provisioning worked, so the honest answer to "is it reachable" is
        # whether the connector is up. See remote_access.tunnel_status.
        'remote_tunnel': tunnel_status() if remote_access.is_enabled() else 'off',
        'remote_password': remote_access.get_password() if pathway != 'owner' else '',
        'remote_password_visible': pathway != 'owner',
        'remote_host': f"{read_node_id()}.{REMOTE_ACCESS_DOMAIN}",
        'remote_password_min': MIN_PASSWORD_LENGTH,
    }


@bp.route("/config")
def config_page():
    """Configuration page with all settings."""
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

    retina_tracker_values = (config.get('retina_tracker', {}) or {})
    retina_tracker_fields = schema_to_form_fields(RetinaTrackerConfig, retina_tracker_values)

    return render_template("config.html",
                           retina_installed=retina_installed,
                           capture_fields=capture_fields,
                           location_fields=location_fields,
                           truth_fields=truth_fields,
                           tar1090_fields=tar1090_fields,
                           retina_tracker_fields=retina_tracker_fields,
                           towers_cache=device_state.get_towers_cache(),
                           node_name=node_name.get(),
                           node_name_max_length=NAME_MAX_LENGTH,
                           ssh_keys=ssh_keys.get_keys())


@bp.route("/config/rf-status")
def rf_status():
    """Live per-tuner RF overload + peak dBFS, proxied from blah2's API."""
    from app import blah2_client
    return jsonify(blah2_client.get_rf_status() or {})


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


# The three ADS-B source boxes are one YAML value (host,port,protocol), so they
# only mean anything as a set: complete, or empty because adsb.lol is feeding
# tar1090 instead. Neither of those is what the form produces on its own, hence
# both checks here.
#
# Checked here rather than in the schema so each complaint lands on the box it
# belongs to. Pydantic can see adsblol_fallback perfectly well - it is a field
# of the same model - but a model-level error carries no field name, so the
# page would raise its banner with nothing highlighted, which is the failure
# this whole change exists to remove.
_ADSB_SOURCE_FIELDS = ("adsb_source_host", "adsb_source_port", "adsb_source_protocol")


def _location_errors(location_flat):
    """Per-field errors for a geometry that is neither complete nor empty.

    A node legitimately has no location until its owner picks a tower, so an
    empty set is fine. A partial one is not: blah2 derives its whole bistatic
    solution from these six numbers, and a missing one becomes NaN rather than
    an error, so the radar runs and silently associates nothing. This is the
    only place an owner finds out.

    Names are excluded. They are labels, and a position without one is still a
    position.
    """
    missing = [f for f in LOCATION_COORDINATE_FIELDS if location_flat.get(f) in (None, "")]
    if not missing or len(missing) == len(LOCATION_COORDINATE_FIELDS):
        return {}

    # Form-level rather than per-field: the location block in config.html is
    # hand-rendered and does not look up config_errors, so per-field keys would
    # re-render the page with nothing highlighted and no explanation. It also
    # reads better as one sentence than as five lit-up boxes, because the rule
    # is about the group.
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
        # and then quietly never connects on.
        reason = ("required when an ADS-B source is set "
                  "(clear all three to feed tar1090 from adsb.lol instead)")

    return {f"tar1090.{f}": reason for f in missing}


@bp.route("/config/save", methods=["POST"])
def save_config():
    """Save config form data to user.yml."""
    from app import config_mgr
    from config_manager import ConfigManager

    capture_flat, location_flat, truth_data, tar1090_data, retina_tracker_data = ConfigManager.parse_flat_form_data(request.form.to_dict())

    all_errors = {}

    # Refuse the save itself, not just the apply that follows it. The form
    # posts here and the page then auto-POSTs /config/apply, so guarding only
    # the apply would leave the user with changes written to user.yml that
    # were never applied — and silently swept into the next merge, including
    # the one /calibrate/apply performs on a successful run.
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

    if retina_tracker_data:
        try:
            RetinaTrackerConfig(**retina_tracker_data)
        except ValidationError as e:
            all_errors.update(ConfigManager.format_validation_errors(e, 'retina_tracker'))

    if all_errors:
        from app import DEV_MODE, device_state, node_name, ssh_keys
        return render_template("config.html",
                               retina_installed=config_mgr.is_retina_node_installed() or DEV_MODE or request.args.get('demo') == '1',
                               capture_fields=schema_to_form_fields(CaptureFormConfig, capture_flat),
                               location_fields=schema_to_form_fields(LocationFormConfig, location_flat),
                               truth_fields=schema_to_form_fields(AdsbTruthConfig, truth_data),
                               tar1090_fields=schema_to_form_fields(Tar1090Config, tar1090_data),
                               retina_tracker_fields=schema_to_form_fields(RetinaTrackerConfig, retina_tracker_data),
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
        # An emptied source is written as "" rather than omitted. default.yml
        # ships a source of its own, so dropping the key here would leave the
        # merge free to put that default straight back and silently undo the
        # clearing. compute_user_overrides still discards the "" when it
        # matches what is already merged, so nothing is pinned needlessly.
        tar1090_nested['adsb_source'] = (f"{host},{port},{protocol}"
                                         if host or port or protocol else "")
        tar1090_nested.update(tar1090_data)

    merged_config = config_mgr.load_merged_config()
    existing_user = config_mgr.load_user_config()

    new_user_config = {}
    for key in existing_user:
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

    if retina_tracker_data:
        retina_tracker_overrides = config_mgr.compute_user_overrides(retina_tracker_data, merged_config, existing_user, 'retina_tracker')
        if retina_tracker_overrides:
            new_user_config['retina_tracker'] = retina_tracker_overrides

    config_mgr.save_user_config(new_user_config)
    return redirect(url_for("config.config_page") + "?saved=1")


@bp.route("/config/apply", methods=["POST"])
def apply_config():
    """Start a config apply and return immediately.

    The work (config-merger, then in radar mode a stack restart) runs on a
    background thread — see apply_service.py for why it is not done inline
    on this request. Poll /config/apply/status for progress.

    In spectrum mode only config-merger runs — blah2 is intentionally stopped
    and must not be restarted until the user switches back to radar mode.
    """
    from app import DEV_MODE, apply_service, config_mgr, device_state

    if DEV_MODE:
        return jsonify({"success": True, "status": apply_service.request()})

    if not config_mgr.is_retina_node_installed():
        return jsonify({"success": False, "error": "retina-node not installed"}), 400

    # Refuse during a Mender install rather than queue behind it. The install
    # replaces the compose manifests this apply's config-merger runs against,
    # and mender-update's own docker commands are outside the restart lock —
    # so an apply here can genuinely run concurrently with them. It would also
    # report success having skipped the restart entirely, since the install
    # sets mode.txt to 'spectrum'. An install can end in a rollback or reboot,
    # so holding the apply across it and then applying to a stack that may
    # have been replaced underneath is worse than asking the user to retry.
    in_progress, reason = device_state.is_any_update_in_progress()
    if in_progress:
        return jsonify({"success": False,
                        "error": f"{reason}. Apply your changes once it finishes."}), 409

    # An in-flight calibration is refused inside request() rather than here,
    # so no future route can reintroduce the gap this one had — see
    # ApplyService.ConfigChangeRefused.
    try:
        return jsonify({"success": True, "status": apply_service.request()}), 202
    except ConfigChangeRefused as refused:
        return jsonify({"success": False, "error": refused.reason}), 409


@bp.route("/config/apply/status", methods=["GET"])
def apply_config_status():
    """Progress of the current or most recent config apply."""
    from app import apply_service
    return jsonify(apply_service.get_status())
