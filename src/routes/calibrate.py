from flask import Blueprint, jsonify, request
import subprocess

import requests as http_requests

from calibrator import (
    GAIN_REDUCTION_MIN, GAIN_REDUCTION_MAX, MODE_TRACK, MODE_ADSB, VALID_MODES,
    DEFAULT_ADSB_DELAY_TOLERANCE, DEFAULT_ADSB_DOPPLER_TOLERANCE,
)

bp = Blueprint('calibrate', __name__, url_prefix='/calibrate')

# Total towers tried per run, including the currently-configured one — the
# tower-finder already ranks by expected signal, so this is the best N.
MAX_TOWERS = 5

# AGC bandwidths that enable hardware AGC on the reference channel — the AGC
# would fight the gain search, so calibration refuses to run with these set.
AGC_BANDWIDTHS = (5, 50, 100)


def _towers_to_alternates(towers, current_fc, limit):
    """Convert a tower-finder `towers` list (cached or live) into the
    {name, fc} shape the calibrator expects, excluding the current tower and
    capping at `limit`."""
    alternates = []
    for tower in towers:
        frequency_mhz = tower.get("frequency_mhz")
        if frequency_mhz is None:
            continue
        fc = int(float(frequency_mhz) * 1_000_000)
        if fc == current_fc:
            continue  # already first in the list
        alternates.append({"name": tower.get("callsign") or f"{frequency_mhz} MHz",
                           "fc": fc})
        if len(alternates) >= limit:
            break
    return alternates


def _fetch_alternate_towers(merged, current_fc, limit):
    """Best-ranked alternate towers to try, excluding the current one.

    Prefers the setup wizard's cached search — it's RF-measurement-informed
    (real signal strength, not just geography) and avoids a second live
    tower-finder call at calibration time. Falls back to a plain geography
    lookup only if the wizard was never run (or was skipped) on this node.

    Best-effort throughout: returns [] if location is unset, no cache
    exists, and the service is unreachable — the run then just searches the
    current tower.
    """
    from app import app, TOWER_FINDER_URL, device_state

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
    from app import calibrator, config_mgr, device_state
    from routes.mode import get_current_mode

    if not config_mgr.is_retina_node_installed():
        return jsonify({"success": False, "error": "retina-node is not installed"}), 409
    if get_current_mode() != 'radar':
        return jsonify({"success": False,
                        "error": "Radar is not running — switch back to radar mode first"}), 409

    ok, reason = device_state.can_start_calibration()
    if not ok:
        return jsonify({"success": False, "error": reason}), 409

    merged = config_mgr.load_merged_config()
    capture = merged.get('capture', {}) or {}
    device = capture.get('device', {}) or {}

    if device.get('bandwidthNumber') in AGC_BANDWIDTHS:
        return jsonify({
            "success": False,
            "error": "Hardware AGC is enabled (AGC Bandwidth setting). "
                     "Auto-calibrate tunes gain manually and cannot run with "
                     "AGC active — set AGC Bandwidth to 0 in the Capture "
                     "config first.",
        }), 409

    fc = capture.get('fc')
    gain_reduction = device.get('gainReduction')
    if not isinstance(gain_reduction, list):
        gain_reduction = [gain_reduction, gain_reduction]
    if fc is None or gain_reduction[0] is None:
        return jsonify({"success": False,
                        "error": "Capture config is incomplete — finish setup first"}), 409

    def clamp(gain):
        return max(GAIN_REDUCTION_MIN, min(GAIN_REDUCTION_MAX, int(gain)))

    original = {
        "fc": int(fc),
        "gain_a": clamp(gain_reduction[0]),
        "gain_b": clamp(gain_reduction[1]),
    }

    tx_name = ((merged.get('location', {}) or {}).get('tx', {}) or {}).get('name')
    towers = [{"name": tx_name or "Current tower", "fc": int(fc)}]

    body = request.get_json(silent=True) or {}
    if body.get("scope") != "current_tower":
        towers.extend(_fetch_alternate_towers(merged, int(fc), MAX_TOWERS - 1))

    mode = body.get("mode", MODE_TRACK)
    if mode not in VALID_MODES:
        return jsonify({"success": False, "error": f"Invalid mode: {mode}"}), 400

    adsb_delay_tolerance = DEFAULT_ADSB_DELAY_TOLERANCE
    adsb_doppler_tolerance = DEFAULT_ADSB_DOPPLER_TOLERANCE
    if mode == MODE_ADSB:
        adsb_cfg = (merged.get('truth', {}) or {}).get('adsb', {}) or {}
        if not adsb_cfg.get('enabled'):
            return jsonify({
                "success": False,
                "error": "ADS-B mode requires ADS-B truth to be enabled on "
                         "this node (truth.adsb.enabled) — enable it in the "
                         "ADS-B config first, or use standard mode.",
            }), 409
        adsb_delay_tolerance = adsb_cfg.get('delay_tolerance', DEFAULT_ADSB_DELAY_TOLERANCE)
        adsb_doppler_tolerance = adsb_cfg.get('doppler_tolerance', DEFAULT_ADSB_DOPPLER_TOLERANCE)

    if not device_state.acquire_calibration_lock():
        return jsonify({"success": False,
                        "error": "Auto-calibration already in progress"}), 409

    started, error = calibrator.start(towers, original, mode=mode,
                                      adsb_delay_tolerance=adsb_delay_tolerance,
                                      adsb_doppler_tolerance=adsb_doppler_tolerance)
    if not started:
        device_state.release_calibration_lock()
        return jsonify({"success": False, "error": error}), 409

    return jsonify({"success": True, "mode": mode,
                    "towers": [tower["name"] for tower in towers]})


@bp.route("/status", methods=["GET"])
def status():
    from app import calibrator
    return jsonify(calibrator.get_status())


@bp.route("/cancel", methods=["POST"])
def cancel():
    from app import calibrator
    calibrator.cancel()
    return jsonify({"success": True})


@bp.route("/apply", methods=["POST"])
def apply():
    """Persist a successful calibration result: write user.yml and do the
    one-time config-merger + service restart (mirrors /towers/select)."""
    from app import calibrator, config_mgr, device_state, RETINA_NODE_PATH
    from routes.mode import run_config_merger_and_restart

    run_status = calibrator.get_status()
    result = run_status.get("result")
    if run_status.get("state") != "done" or not result:
        return jsonify({"success": False,
                        "error": "No successful calibration result to apply"}), 409

    ok, reason = device_state.can_start_calibration()
    if not ok:
        return jsonify({"success": False, "error": reason}), 409

    user_config = dict(config_mgr.load_user_config())
    capture = dict(user_config.get('capture', {}) or {})
    capture['fc'] = int(result['fc'])
    device = dict(capture.get('device', {}) or {})
    device['gainReduction'] = [int(result['gain_a']), int(result['gain_b'])]
    capture['device'] = device
    user_config['capture'] = capture
    config_mgr.save_user_config(user_config)

    try:
        error = run_config_merger_and_restart(RETINA_NODE_PATH)
        if error:
            return jsonify({"success": True, "applied": False, "error": error})
    except subprocess.TimeoutExpired:
        return jsonify({"success": True, "applied": False, "error": "Command timed out"})
    except FileNotFoundError:
        return jsonify({"success": False, "applied": False,
                        "error": "docker not found — is it installed?"})
    except Exception as e:
        return jsonify({"success": True, "applied": False, "error": str(e)})

    from app import send_calibration_applied_event
    send_calibration_applied_event(run_status)

    return jsonify({"success": True, "applied": True})
