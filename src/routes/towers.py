import requests as http_requests
from flask import Blueprint, Response, jsonify, request, stream_with_context
from pydantic import ValidationError

from apply_service import ConfigChangeRefused
from config_manager import ConfigManager
from config_schema import TX_NAME_MAX_LENGTH, LocationFormConfig

bp = Blueprint('towers', __name__, url_prefix='/towers')

# The tower-finder API already ranks results best-first (signal match, or
# geography if no measurements); the wizard's own map/table can show all of
# them, but the cache backing /config's Tower preset picker only needs the
# best few — capping here keeps that dropdown/manage-list usable.
MAX_CACHED_TOWERS = 5

# The address geocoder allows 10s per provider across two providers, plus the
# 1s throttle it holds Nominatim to. 30 covers a slow two-provider miss
# without tying up a browser for the 90s a tower search is allowed.
GEOCODE_TIMEOUT_S = 30
# The upstream's own bound (its AddressQuery), repeated so an over-long
# address is refused here with a sentence instead of upstream with a 422.
GEOCODE_QUERY_MAX_LENGTH = 200
# One cached upstream lookup, and nothing waits on the result.
ELEVATION_TIMEOUT_S = 15


def _cacheable_towers(towers):
    """Screen finder results before they back /config's preset picker.

    Picking a preset assigns straight into the location inputs with
    `el.value = ...`. maxlength does not constrain a programmatic assignment
    and the validity flag it would otherwise raise is ignored because the form
    is novalidate, so an over-long or out-of-range tower lands in the form
    intact and the save then fails on a field the owner never touched. Manual
    adds are already screened in cache_add; this is the same screen on the
    search path, which until now cached whatever the service returned.

    Auto-Calibrate reads the same cache as its alternate-tower list, and a
    tower whose coordinates cannot be a position is no use to either caller.
    """
    from app import app

    kept = []
    for tower in towers:
        if not isinstance(tower, dict):
            continue
        try:
            latitude = float(tower.get("latitude"))
            longitude = float(tower.get("longitude"))
        except (TypeError, ValueError):
            continue
        if not (-90 <= latitude <= 90) or not (-180 <= longitude <= 180):
            continue
        # Names are trimmed rather than dropped: TX_NAME_MAX_LENGTH is
        # retina-telemetry's tx_callsign limit, not a reason to lose an
        # otherwise usable tower. Both keys are trimmed because the picker
        # falls back callsign -> name, and a facility name runs long far more
        # readily than a callsign does.
        trimmed = dict(tower)
        for key in ("callsign", "name"):
            value = trimmed.get(key)
            if isinstance(value, str) and len(value) > TX_NAME_MAX_LENGTH:
                trimmed[key] = value[:TX_NAME_MAX_LENGTH]
        kept.append(trimmed)

    if len(kept) != len(towers):
        app.logger.warning(
            f"Dropped {len(towers) - len(kept)} tower search result(s) with "
            "unusable coordinates before caching"
        )
    return kept


@bp.route("/search", methods=["POST"])
def search():
    """Proxy RF-profile tower search to retina-server API."""
    from app import TOWER_FINDER_URL, app, device_state

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
        towers = _cacheable_towers(result.get("towers") or [])
        if towers:
            try:
                device_state.save_towers_cache(body["lat"], body["lon"], towers[:MAX_CACHED_TOWERS])
            except Exception as e:
                app.logger.warning(f"Failed to cache tower search results: {e}")
        return jsonify(result)
    except http_requests.Timeout:
        return jsonify({"error": "Tower search timed out, try again"}), 504
    except http_requests.RequestException as e:
        app.logger.warning(f"Tower search failed: {e}")
        return jsonify({"error": "Unable to reach tower finder service"}), 502
    except Exception as e:
        app.logger.error(f"Tower search unexpected error: {e}")
        return jsonify({"error": "Tower search failed, check server logs"}), 500


@bp.route("/geocode", methods=["POST"])
def geocode():
    """Resolve a typed address to coordinates, via the tower-finder service.

    The node GUI is served over plain HTTP on a LAN, so the browser's
    Geolocation API is unavailable to it and the "Use my location" button in
    the wizard stays commented out. This runs server-side, so that constraint
    never reaches it: typing an address is the one way an owner can fill the
    coordinates without reading them off a map.

    Unlike search() above, the upstream's two failure codes are passed through
    rather than collapsed into one. 404 means neither geocoder knew the
    address and the spelling is worth another look; 503 means one could not be
    reached and the very same query is worth retrying. A search box has to
    tell those apart, and flattening them here would throw away the only
    reason the endpoint distinguishes them.

    The query text is deliberately kept out of every log line, as it is
    upstream: an address typed into this box is the most personal thing the
    route handles, and the outcome alone is what an operator needs.
    """
    from app import TOWER_FINDER_URL, app

    body = request.get_json(silent=True) or {}
    query = (body.get("query") or "").strip()
    if not query:
        return jsonify({"error": "Enter an address to look up"}), 400
    if len(query) > GEOCODE_QUERY_MAX_LENGTH:
        return jsonify({"error": "That address is too long to look up"}), 400

    try:
        resp = http_requests.post(
            f"{TOWER_FINDER_URL}/api/geocode",
            json={"query": query},
            timeout=GEOCODE_TIMEOUT_S,
        )
        if resp.status_code == 404:
            return jsonify({"error": _upstream_detail(
                resp, "No match for that address")}), 404
        if resp.status_code == 503:
            return jsonify({"error": _upstream_detail(
                resp, "Address lookup is unavailable right now")}), 503
        resp.raise_for_status()
        return jsonify(resp.json())
    except http_requests.Timeout:
        return jsonify({"error": "Address lookup timed out, try again"}), 504
    except http_requests.RequestException as e:
        app.logger.warning(f"Address lookup failed: {e}")
        return jsonify({"error": "Unable to reach the address lookup service"}), 502
    except Exception as e:
        app.logger.error(f"Address lookup unexpected error: {e}")
        return jsonify({"error": "Address lookup failed, check server logs"}), 500


@bp.route("/elevation")
def elevation():
    """Ground elevation at a point, for prefilling the altitude box.

    Advisory in every sense. Nothing gates on altitude and the wizard leaves
    the box blank on failure rather than reporting one, so this answers with a
    plain error the caller is expected to swallow. It exists because the box
    was otherwise filled by nothing at all, which left `rx_altitude` at 0 in
    the radar config for every owner who did not happen to type a figure.

    GET, matching the upstream, which also keeps it clear of CSRF entirely.
    """
    from app import TOWER_FINDER_URL, app

    try:
        lat = float(request.args.get("lat", ""))
        lon = float(request.args.get("lon", ""))
    except (TypeError, ValueError):
        return jsonify({"error": "lat and lon are required"}), 400
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return jsonify({"error": "lat and lon are out of range"}), 400

    try:
        resp = http_requests.get(
            f"{TOWER_FINDER_URL}/api/elevation",
            params={"lat": lat, "lon": lon},
            timeout=ELEVATION_TIMEOUT_S,
        )
        resp.raise_for_status()
        return jsonify(resp.json())
    except http_requests.RequestException as e:
        # Info rather than warning: a missing altitude prefill is not a fault
        # the owner or an operator needs to act on.
        app.logger.info(f"Elevation lookup failed: {e}")
        return jsonify({"error": "Unable to reach the elevation service"}), 502


def _upstream_detail(resp, fallback):
    """The upstream's own sentence, or ours when it did not send one.

    FastAPI reports an HTTPException's message in `detail`, but puts a list of
    field errors there for a validation failure. Only a plain string is
    something to show an owner.
    """
    try:
        detail = (resp.json() or {}).get("detail")
    except ValueError:
        return fallback
    return detail if isinstance(detail, str) and detail.strip() else fallback


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
    # Caught here as well as in LocationFormConfig so the rejection lands on the
    # field the operator is typing into, rather than later when the tower is
    # selected and the whole location block fails to validate.
    if len(callsign) > TX_NAME_MAX_LENGTH:
        return jsonify({
            "success": False,
            "error": f"Name/callsign must be {TX_NAME_MAX_LENGTH} characters or fewer",
        }), 400
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
    """Save RX + TX location to user.yml, then queue a config apply.

    The apply runs on the shared background queue (see apply_service.py) —
    poll /config/apply/status for progress. In spectrum mode it only runs
    config-merger: blah2 is intentionally stopped and must not be restarted
    until the user switches back to radar mode.
    """
    from app import apply_service, config_mgr, device_state, get_node_id

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
        validated = LocationFormConfig(**location_flat)
    except ValidationError as e:
        errors = ConfigManager.format_validation_errors(e, "location")
        return jsonify({"success": False, "errors": errors}), 400

    # The model now permits a wholly empty location, because a node
    # legitimately has none. This endpoint is the one that sets one, so an
    # empty POST must not silently unsite a configured node.
    if not validated.is_located:
        return jsonify({
            "success": False,
            "errors": {"location": "a tower selection must carry a full receiver "
                                   "and transmitter position"},
        }), 400

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

    if not config_mgr.is_retina_node_installed():
        return jsonify({"success": True, "applied": False})

    in_progress, reason = device_state.is_any_update_in_progress()
    if in_progress:
        return jsonify({"success": False,
                        "error": f"{reason}. Choose a tower once it finishes."}), 409

    # The config write above is a local file write and stays synchronous — it
    # must be visible to anything that reads user.yml the moment this returns.
    # Only the slow part (config-merger plus a stack restart, ~45s) is handed
    # to the shared queue, which is what stops this request blocking a browser
    # for minutes when it lands behind another restart. Poll
    # /config/apply/status for progress; the queue always merges whatever is
    # in user.yml when it runs, so it necessarily picks up the write above.
    # A calibration in flight is refused inside request() — see
    # ApplyService.ConfigChangeRefused for why that check is not repeated
    # here.
    try:
        return jsonify({"success": True, "status": apply_service.request()}), 202
    except ConfigChangeRefused as refused:
        return jsonify({"success": False, "error": refused.reason}), 409
