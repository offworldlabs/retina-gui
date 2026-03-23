from flask import Blueprint, render_template, request, redirect, url_for, jsonify
import subprocess

from pydantic import ValidationError
from config_schema import (
    AdsbTruthConfig, Tar1090Config,
    CaptureFormConfig, LocationFormConfig,
)
from form_utils import schema_to_form_fields

bp = Blueprint('config', __name__)


@bp.route("/config")
def config_page():
    """Configuration page with all settings."""
    from app import config_mgr, ssh_keys

    config = config_mgr.load_merged_config()
    retina_installed = config_mgr.is_retina_node_installed()

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
                           capture_fields=capture_fields,
                           location_fields=location_fields,
                           truth_fields=truth_fields,
                           tar1090_fields=tar1090_fields,
                           ssh_keys=ssh_keys.get_keys())


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


@bp.route("/config/save", methods=["POST"])
def save_config():
    """Save config form data to user.yml."""
    from app import config_mgr
    from config_manager import ConfigManager

    capture_flat, location_flat, truth_data, tar1090_data = ConfigManager.parse_flat_form_data(request.form.to_dict())

    all_errors = {}

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

    if all_errors:
        from app import ssh_keys
        return render_template("config.html",
                               retina_installed=config_mgr.is_retina_node_installed(),
                               capture_fields=schema_to_form_fields(CaptureFormConfig, capture_flat),
                               location_fields=schema_to_form_fields(LocationFormConfig, location_flat),
                               truth_fields=schema_to_form_fields(AdsbTruthConfig, truth_data),
                               tar1090_fields=schema_to_form_fields(Tar1090Config, tar1090_data),
                               config_errors=all_errors,
                               ssh_keys=ssh_keys.get_keys())

    capture_nested = ConfigManager.unflatten_capture_from_form(capture_flat)
    location_nested = ConfigManager.unflatten_location_from_form(location_flat)

    tar1090_nested = {}
    if tar1090_data:
        host = tar1090_data.pop('adsb_source_host', '')
        port = tar1090_data.pop('adsb_source_port', '')
        protocol = tar1090_data.pop('adsb_source_protocol', '')
        if host or port or protocol:
            tar1090_nested['adsb_source'] = f"{host},{port},{protocol}"
        tar1090_nested.update(tar1090_data)

    merged_config = config_mgr.load_merged_config()
    existing_user = config_mgr.load_user_config()

    new_user_config = {}
    for key in existing_user:
        if key not in ('capture', 'location', 'truth', 'tar1090'):
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


@bp.route("/config/apply", methods=["POST"])
def apply_config():
    """Run config-merger and restart services."""
    from app import config_mgr, RETINA_NODE_PATH

    if not config_mgr.is_retina_node_installed():
        return jsonify({"success": False, "error": "retina-node not installed"}), 400

    try:
        result = subprocess.run(
            ["docker", "compose", "-p", "retina-node", "run", "--rm", "config-merger"],
            cwd=RETINA_NODE_PATH,
            capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            return jsonify({"success": False, "error": f"config-merger failed: {result.stderr or result.stdout}"}), 500

        result = subprocess.run(
            ["docker", "compose", "-p", "retina-node", "up", "-d", "--force-recreate"],
            cwd=RETINA_NODE_PATH,
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            return jsonify({"success": False, "error": f"restart failed: {result.stderr or result.stdout}"}), 500

        return jsonify({"success": True})

    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "error": "Command timed out"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
