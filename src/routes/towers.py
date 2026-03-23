from flask import Blueprint, jsonify, request
import subprocess

import requests as http_requests
from pydantic import ValidationError
from config_schema import LocationFormConfig
from config_manager import ConfigManager

bp = Blueprint('towers', __name__, url_prefix='/towers')


@bp.route("/search")
def search():
    """Proxy tower search to Tower-Finder API."""
    from app import app, TOWER_FINDER_URL

    lat = request.args.get("lat")
    lon = request.args.get("lon")
    if not lat or not lon:
        return jsonify({"error": "lat and lon are required"}), 400

    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (ValueError, TypeError):
        return jsonify({"error": "lat and lon must be numbers"}), 400

    if not (-90 <= lat_f <= 90) or not (-180 <= lon_f <= 180):
        return jsonify({"error": "lat must be -90..90 and lon must be -180..180"}), 400

    params = {
        "lat": lat,
        "lon": lon,
        "altitude": request.args.get("altitude", "0"),
        "limit": request.args.get("limit", "20"),
        "source": request.args.get("source", "auto"),
    }
    radius_km = request.args.get("radius_km")
    if radius_km:
        params["radius_km"] = radius_km
    frequencies = request.args.get("frequencies")
    if frequencies:
        params["frequencies"] = frequencies

    try:
        resp = http_requests.get(
            f"{TOWER_FINDER_URL}/api/towers",
            params=params,
            timeout=90,
        )
        resp.raise_for_status()
        return jsonify(resp.json())
    except http_requests.Timeout:
        return jsonify({"error": "Tower search timed out — try again"}), 504
    except http_requests.RequestException as e:
        app.logger.warning(f"Tower search failed: {e}")
        return jsonify({"error": "Unable to reach tower finder service"}), 502


@bp.route("/elevation")
def elevation():
    """Proxy elevation lookup to Tower-Finder API."""
    from app import app, TOWER_FINDER_URL

    lat = request.args.get("lat")
    lon = request.args.get("lon")
    if not lat or not lon:
        return jsonify({"error": "lat and lon are required"}), 400

    try:
        resp = http_requests.get(
            f"{TOWER_FINDER_URL}/api/elevation",
            params={"lat": lat, "lon": lon},
            timeout=15,
        )
        resp.raise_for_status()
        return jsonify(resp.json())
    except http_requests.RequestException as e:
        app.logger.warning(f"Elevation lookup failed: {e}")
        return jsonify({"error": "Elevation lookup failed"}), 502


@bp.route("/select", methods=["POST"])
def select():
    """Save RX + TX location to user.yml, run config-merger, and restart services."""
    from app import config_mgr, get_node_id, RETINA_NODE_PATH

    data = request.get_json()
    if not data:
        return jsonify({"success": False, "error": "Missing JSON body"}), 400

    node_id = get_node_id()
    location_flat = {
        "rx_latitude": data.get("rx_latitude"),
        "rx_longitude": data.get("rx_longitude"),
        "rx_altitude": data.get("rx_altitude"),
        "rx_name": node_id,
        "tx_latitude": data.get("tx_latitude"),
        "tx_longitude": data.get("tx_longitude"),
        "tx_altitude": data.get("tx_altitude"),
        "tx_name": data.get("tx_callsign", ""),
    }

    try:
        LocationFormConfig(**location_flat)
    except ValidationError as e:
        errors = ConfigManager.format_validation_errors(e, "location")
        return jsonify({"success": False, "errors": errors}), 400

    location_nested = ConfigManager.unflatten_location_from_form(location_flat)
    existing_user = config_mgr.load_user_config()

    new_user_config = dict(existing_user)
    new_user_config["location"] = location_nested

    # Set center frequency from tower broadcast frequency (MHz -> Hz)
    frequency_mhz = data.get("frequency_mhz")
    if frequency_mhz is not None:
        try:
            fc_hz = int(float(frequency_mhz) * 1_000_000)
            capture = new_user_config.get("capture", {}) or {}
            capture["fc"] = fc_hz
            new_user_config["capture"] = capture
        except (ValueError, TypeError):
            pass

    config_mgr.save_user_config(new_user_config)

    if config_mgr.is_retina_node_installed():
        try:
            result = subprocess.run(
                ["docker", "compose", "-p", "retina-node", "run", "--rm", "config-merger"],
                cwd=RETINA_NODE_PATH,
                capture_output=True, text=True, timeout=60
            )
            if result.returncode != 0:
                return jsonify({"success": True, "applied": False,
                                "error": f"config-merger failed: {result.stderr or result.stdout}"})

            result = subprocess.run(
                ["docker", "compose", "-p", "retina-node", "up", "-d", "--force-recreate"],
                cwd=RETINA_NODE_PATH,
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                return jsonify({"success": True, "applied": False,
                                "error": f"restart failed: {result.stderr or result.stdout}"})

            return jsonify({"success": True, "applied": True})

        except subprocess.TimeoutExpired:
            return jsonify({"success": True, "applied": False, "error": "Command timed out"})
        except FileNotFoundError:
            return jsonify({"success": False, "applied": False, "error": "docker not found — is it installed?"})
        except Exception as e:
            return jsonify({"success": True, "applied": False, "error": str(e)})

    return jsonify({"success": True, "applied": False})
