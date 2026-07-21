from flask import Blueprint, jsonify, request, Response, stream_with_context
import subprocess

import requests as http_requests
from pydantic import ValidationError
from config_schema import LocationFormConfig
from config_manager import ConfigManager

bp = Blueprint('towers', __name__, url_prefix='/towers')

# The tower-finder API already ranks results best-first (signal match, or
# geography if no measurements); the wizard's own map/table can show all of
# them, but the cache backing /config's Tower preset picker only needs the
# best few — capping here keeps that dropdown/manage-list usable.
MAX_CACHED_TOWERS = 5


@bp.route("/search", methods=["POST"])
def search():
    """Proxy RF-profile tower search to Tower-Finder API."""
    from app import app, TOWER_FINDER_URL, device_state

    body = request.get_json()
    if not body:
        return jsonify({"error": "Missing JSON body"}), 400

    if body.get("lat") is None or body.get("lon") is None:
        return jsonify({"error": "lat and lon are required"}), 400

    measurements = body.get("measurements") or []

    try:
        if measurements:
            # Measurement-enriched POST: gives the tower-finder actual RF data
            # so it can rank towers by signal match rather than geography alone.
            post_body = {
                "lat": body["lat"],
                "lon": body["lon"],
                "measurements": measurements,
            }
            if body.get("radius_km") is not None:
                post_body["radius_km"] = body["radius_km"]
            if body.get("limit") is not None:
                post_body["limit"] = body["limit"]
            if body.get("source") is not None:
                post_body["source"] = body["source"]
            resp = http_requests.post(
                f"{TOWER_FINDER_URL}/api/towers",
                json=post_body,
                timeout=90,
            )
        else:
            params = {"lat": body["lat"], "lon": body["lon"]}
            if body.get("altitude") is not None:
                params["altitude"] = body["altitude"]
            if body.get("radius_km") is not None:
                params["radius_km"] = body["radius_km"]
            if body.get("limit") is not None:
                params["limit"] = body["limit"]
            if body.get("source") is not None:
                params["source"] = body["source"]
            if body.get("frequencies") is not None:
                params["frequencies"] = body["frequencies"]
            resp = http_requests.get(
                f"{TOWER_FINDER_URL}/api/towers",
                params=params,
                timeout=90,
            )
        resp.raise_for_status()
        result = resp.json()
        towers = result.get("towers") or []
        if towers:
            try:
                device_state.save_towers_cache(body["lat"], body["lon"], towers[:MAX_CACHED_TOWERS])
            except Exception as e:
                app.logger.warning(f"Failed to cache tower search results: {e}")
        return jsonify(result)
    except http_requests.Timeout:
        return jsonify({"error": "Tower search timed out — try again"}), 504
    except http_requests.RequestException as e:
        app.logger.warning(f"Tower search failed: {e}")
        return jsonify({"error": "Unable to reach tower finder service"}), 502
    except Exception as e:
        app.logger.error(f"Tower search unexpected error: {e}")
        return jsonify({"error": "Tower search failed — check server logs"}), 500


@bp.route("/cache/add", methods=["POST"])
def cache_add():
    """Manually add a tower to the cached tower-preset list."""
    from app import device_state

    body = request.get_json()
    if not body:
        return jsonify({"success": False, "error": "Missing JSON body"}), 400

    callsign = (body.get("callsign") or "").strip()
    try:
        frequency_mhz = float(body.get("frequency_mhz"))
        latitude = float(body.get("latitude"))
        longitude = float(body.get("longitude"))
        altitude_m = float(body.get("altitude_m") or 0)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Frequency, latitude, longitude, and altitude must be numbers"}), 400

    if not callsign:
        return jsonify({"success": False, "error": "Name/callsign is required"}), 400
    if not (-90 <= latitude <= 90):
        return jsonify({"success": False, "error": "Latitude must be between -90 and 90"}), 400
    if not (-180 <= longitude <= 180):
        return jsonify({"success": False, "error": "Longitude must be between -180 and 180"}), 400

    device_state.add_tower_to_cache({
        "callsign": callsign,
        "name": callsign,
        "frequency_mhz": frequency_mhz,
        "latitude": latitude,
        "longitude": longitude,
        "altitude_m": altitude_m,
        "source": "manual",
    })
    cache = device_state.get_towers_cache()
    return jsonify({"success": True, "towers": cache["towers"], "cached_at": cache["cached_at"]})


@bp.route("/cache/remove", methods=["POST"])
def cache_remove():
    """Remove a tower from the cached tower-preset list by its position."""
    from app import device_state

    body = request.get_json()
    if not body or body.get("index") is None:
        return jsonify({"success": False, "error": "Missing index"}), 400

    try:
        index = int(body["index"])
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "index must be an integer"}), 400

    if not device_state.remove_tower_from_cache(index):
        return jsonify({"success": False, "error": "Tower not found"}), 404

    cache = device_state.get_towers_cache() or {}
    return jsonify({"success": True, "towers": cache.get("towers", []), "cached_at": cache.get("cached_at")})


@bp.route("/spectrum/events")
def spectrum_events():
    """Proxy SSE stream from retina-spectrum."""
    from app import RETINA_SPECTRUM_URL

    def generate():
        try:
            with http_requests.get(
                f"{RETINA_SPECTRUM_URL}/api/events",
                stream=True,
                timeout=(5, None),
            ) as r:
                for chunk in r.iter_content(chunk_size=None):
                    if chunk:
                        yield chunk
        except Exception:
            yield b'data: {"type":"error"}\n\n'

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )



@bp.route("/select", methods=["POST"])
def select():
    """Save RX + TX location to user.yml, run config-merger, and restart services.

    In spectrum mode only config-merger runs — blah2 is intentionally stopped
    and must not be restarted until the user switches back to radar mode.
    """
    from app import config_mgr, get_node_id, RETINA_NODE_PATH
    from routes.mode import run_config_merger_and_restart

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
            error = run_config_merger_and_restart(RETINA_NODE_PATH, trigger='tower_select')
            if error:
                return jsonify({"success": True, "applied": False, "error": error})
            return jsonify({"success": True, "applied": True})

        except subprocess.TimeoutExpired:
            return jsonify({"success": True, "applied": False, "error": "Command timed out"})
        except FileNotFoundError:
            return jsonify({"success": False, "applied": False, "error": "docker not found — is it installed?"})
        except Exception as e:
            return jsonify({"success": True, "applied": False, "error": str(e)})

    return jsonify({"success": True, "applied": False})
