import requests as http_requests
from flask import Blueprint, jsonify, request

from calibrator import (
    GAIN_REDUCTION_MAX,
    GAIN_REDUCTION_MIN,
    LNA_STATE_MAX,
    LNA_STATE_MIN,
    MODE_ADSB,
    MODE_TRACK,
    VALID_MODES,
)
from config_schema import TX_NAME_MAX_LENGTH

bp = Blueprint('calibrate', __name__, url_prefix='/calibrate')

# Total towers per run, including the configured one. Deliberately small:
# each costs a descent plus a dwell. See
# docs/features/auto-calibrate.md#candidate-towers.
MAX_TOWERS = 3

# AGC bandwidths that enable hardware AGC on the reference channel. AGC would
# fight the manual gain search, so calibration refuses to run with these set.
AGC_BANDWIDTHS = (5, 50, 100)


def _tower_tx(tower):
    """The transmitter position to persist along with this tower's fc, or
    None if the record has no usable one (then fc must not move location.tx).
    """
    latitude, longitude = tower.get("latitude"), tower.get("longitude")
    if latitude is None or longitude is None:
        return None
    try:
        return {"latitude": float(latitude), "longitude": float(longitude),
                "altitude": float(tower.get("altitude_m") or 0)}
    except (TypeError, ValueError):
        return None


def _towers_to_alternates(towers, current_fc, limit):
    """Convert a tower-finder `towers` list (cached or live) into the
    {name, fc, tx} shape the calibrator expects, excluding the current tower
    and capping at `limit`.

    Only alternates carry `tx`; the configured tower has none, which is how
    /calibrate/apply knows not to rewrite location.tx.
    """
    alternates = []
    for tower in towers:
        frequency_mhz = tower.get("frequency_mhz")
        if frequency_mhz is None:
            continue
        fc = int(float(frequency_mhz) * 1_000_000)
        if fc == current_fc:
            continue  # already first in the list
        alternates.append({"name": tower.get("callsign") or f"{frequency_mhz} MHz",
                           "fc": fc,
                           "tx": _tower_tx(tower)})
        if len(alternates) >= limit:
            break
    return alternates


def _fetch_alternate_towers(merged, current_fc, limit):
    """Best-ranked alternate towers to try, excluding the current one.

    Prefers the setup wizard's cached (RF-informed) search, else a live
    geography lookup. Best-effort: returns [] when neither is available.
    """
    from app import TOWER_FINDER_URL, app, device_state

    cached = device_state.get_towers_cache()
    if cached and cached.get("towers"):
        return _towers_to_alternates(cached["towers"], current_fc, limit)

    location = merged.get('location', {}) or {}
    rx = location.get('rx', {}) or {}
    lat, lon = rx.get('latitude'), rx.get('longitude')
    if lat is None or lon is None:
        return []

    try:
        resp = http_requests.get(
            f"{TOWER_FINDER_URL}/api/towers",
            params={"lat": lat, "lon": lon, "limit": limit + 1},
            timeout=15,
        )
        resp.raise_for_status()
        towers = resp.json().get("towers") or []
    except Exception as e:
        app.logger.warning(f"Auto-calibrate tower lookup failed: {e}")
        return []

    return _towers_to_alternates(towers, current_fc, limit)


@bp.route("/start", methods=["POST"])
def start():
    """Start an auto-calibration run against the live radar."""
    from app import apply_service, calibrator, config_mgr, device_state
    from routes.sdr import get_current_mode

    if not config_mgr.is_retina_node_installed():
        return jsonify({"success": False, "error": "retina-node is not installed"}), 409
    if get_current_mode() != 'radar':
        return jsonify({"success": False,
                        "error": "Radar is not running. Switch back to radar mode first"}), 409

    ok, reason = device_state.can_start_calibration()
    if not ok:
        return jsonify({"success": False, "error": reason}), 409

    # The other half of ApplyService's guard: no run during an apply's stack
    # restart. See docs/features/auto-calibrate.md#start-guards.
    if apply_service.is_running():
        return jsonify({"success": False,
                        "error": "A configuration change is still being applied. "
                                 "Wait for it to finish before calibrating"}), 409

    merged = config_mgr.load_merged_config()
    capture = merged.get('capture', {}) or {}
    device = capture.get('device', {}) or {}

    if device.get('bandwidthNumber') in AGC_BANDWIDTHS:
        return jsonify({
            "success": False,
            "error": "Hardware AGC is enabled (AGC Bandwidth setting). "
                     "Auto-calibrate tunes gain manually and cannot run with "
                     "AGC active. Set AGC Bandwidth to 0 in the Capture "
                     "config first.",
        }), 409

    fc = capture.get('fc')
    gain_reduction = device.get('gainReduction')
    if not isinstance(gain_reduction, list):
        gain_reduction = [gain_reduction, gain_reduction]
    lna_state = device.get('lnaState')
    if fc is None or gain_reduction[0] is None or lna_state is None:
        return jsonify({"success": False,
                        "error": "Capture config is incomplete. Finish setup first"}), 409

    def clamp(value, lo, hi):
        return max(lo, min(hi, int(value)))

    original = {
        "fc": int(fc),
        "gain_a": clamp(gain_reduction[0], GAIN_REDUCTION_MIN, GAIN_REDUCTION_MAX),
        "gain_b": clamp(gain_reduction[1], GAIN_REDUCTION_MIN, GAIN_REDUCTION_MAX),
        "lna_state": clamp(lna_state, LNA_STATE_MIN, LNA_STATE_MAX),
    }

    tx_name = ((merged.get('location', {}) or {}).get('tx', {}) or {}).get('name')
    towers = [{"name": tx_name or "Current tower", "fc": int(fc)}]

    body = request.get_json(silent=True) or {}
    if body.get("scope") != "current_tower":
        towers.extend(_fetch_alternate_towers(merged, int(fc), MAX_TOWERS - 1))

    mode = body.get("mode", MODE_TRACK)
    if mode not in VALID_MODES:
        return jsonify({"success": False, "error": f"Invalid mode: {mode}"}), 400
    if mode == MODE_ADSB:
        # Engine support is complete, but exposing it to users is a separate
        # decision not yet made. See docs/features/auto-calibrate.md#success-modes.
        return jsonify({"success": False,
                        "error": "ADS-B verified mode is not currently available"}), 409

    # Opt-in per request (static/calibrate.js's QUICK_RUN). Independent of
    # scope: how many towers vs whether to confirm. See
    # docs/features/auto-calibrate.md#entry-points-and-run-shapes.
    skip_confirmation = bool(body.get("skip_confirmation"))

    if not device_state.acquire_calibration_lock():
        return jsonify({"success": False,
                        "error": "Auto-calibration already in progress"}), 409

    started, error = calibrator.start(towers, original, mode=mode,
                                      skip_confirmation=skip_confirmation)
    if not started:
        device_state.release_calibration_lock()
        return jsonify({"success": False, "error": error}), 409

    return jsonify({"success": True, "mode": mode,
                    "skip_confirmation": skip_confirmation,
                    "towers": [tower["name"] for tower in towers]})


@bp.route("/status", methods=["GET"])
def status():
    from app import calibrator, device_state

    payload = calibrator.get_status()

    # A server-pushed Mender deployment cannot be refused and breaks a run,
    # so it is reported here rather than prevented. See
    # docs/features/auto-calibrate.md#server-pushed-mender-deployments.
    in_progress, reason = device_state.is_any_update_in_progress()
    payload["system_update"] = reason if in_progress else None

    return jsonify(payload)


@bp.route("/cancel", methods=["POST"])
def cancel():
    from app import calibrator
    calibrator.cancel()
    return jsonify({"success": True})


@bp.route("/apply", methods=["POST"])
def apply():
    """Persist a calibration run's tuning: write user.yml, then queue the
    config-merger + service restart (mirrors /towers/select).

    Takes the confirmed result, else the no-track "fallback". See
    docs/features/auto-calibrate.md#persisting-a-result.
    """
    from app import apply_service, calibrator, config_mgr, device_state

    run_status = calibrator.get_status()
    result = run_status.get("result")
    # The fallback is live-only until written here (the next restart
    # re-reads config.yml). Cancelled runs never have one.
    tuning = result if run_status.get("state") == "done" and result else run_status.get("fallback")
    if not tuning:
        return jsonify({"success": False,
                        "error": "No calibration tuning to apply"}), 409

    ok, reason = device_state.can_start_calibration()
    if not ok:
        return jsonify({"success": False, "error": reason}), 409

    user_config = dict(config_mgr.load_user_config())
    capture = dict(user_config.get('capture', {}) or {})
    capture['fc'] = int(tuning['fc'])
    device = dict(capture.get('device', {}) or {})
    device['gainReduction'] = [int(tuning['gain_a']), int(tuning['gain_b'])]
    device['lnaState'] = int(tuning['lna_state'])
    # Always assert AGC off: a result is a manual operating point and must
    # never inherit a stale AGC-on bandwidth.
    device['bandwidthNumber'] = 0
    capture['device'] = device
    user_config['capture'] = capture

    # A new fc must move location.tx with it (blah2's bistatic geometry).
    # Only alternates carry `tx`. Mirrors /towers/select.
    persisted_tx = None
    tx = tuning.get('tx')
    if tx:
        location = dict(user_config.get('location', {}) or {})
        persisted_tx = dict(location.get('tx', {}) or {})
        persisted_tx['latitude'] = tx['latitude']
        persisted_tx['longitude'] = tx['longitude']
        persisted_tx['altitude'] = tx['altitude']
        name = (tuning.get('tower_name') or '').strip()
        if name:
            # Truncated, not dropped: TX_NAME_MAX_LENGTH matches
            # retina-telemetry's tx_callsign limit.
            persisted_tx['name'] = name[:TX_NAME_MAX_LENGTH]
        location['tx'] = persisted_tx
        user_config['location'] = location

    config_mgr.save_user_config(user_config)

    # The user.yml write above must be on disk before this returns; only the
    # merge+restart is queued. Poll /config/apply/status for progress.
    return jsonify({
        "success": True,
        # Exactly what was written, so the Configuration page can update its
        # form; a stale form's Save would silently drop the calibration.
        "persisted": {
            "fc": capture['fc'],
            "gain_a": device['gainReduction'][0],
            "gain_b": device['gainReduction'][1],
            "lna_state": device['lnaState'],
            "bandwidth_number": device['bandwidthNumber'],
            "tx": persisted_tx,
        },
        "status": apply_service.request(),
    }), 202
