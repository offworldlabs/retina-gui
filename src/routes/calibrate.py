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

bp = Blueprint('calibrate', __name__, url_prefix='/calibrate')

# Total towers tried per run, including the currently-configured one — the
# tower-finder already ranks by expected signal, so this is the best N.
# Deliberately small: every tower costs a full descent (up to ~4.5 minutes on
# a node that never overloads) plus its own dwell, and dwell is where success
# actually comes from. Towers past the top-ranked one are speculative, so
# trading tower count for dwell time is the right way round — a run that
# searches five towers but dwells on none of them searches nothing at all.
MAX_TOWERS = 3

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
    from routes.mode import get_current_mode

    if not config_mgr.is_retina_node_installed():
        return jsonify({"success": False, "error": "retina-node is not installed"}), 409
    if get_current_mode() != 'radar':
        return jsonify({"success": False,
                        "error": "Radar is not running. Switch back to radar mode first"}), 409

    ok, reason = device_state.can_start_calibration()
    if not ok:
        return jsonify({"success": False, "error": reason}), 409

    # The refusal has to run both ways. ApplyService stops a config apply
    # starting during a run; this stops a run starting during an apply, which
    # nothing covered — can_start_calibration() knows about Mender installs
    # but not about the ~45s stack restart an apply performs. Hit by accident
    # while testing: Apply Changes, then Auto-Calibrate a few seconds later,
    # and every retune failed against restarting containers. All three towers
    # came back tuning_not_applied, which is honest but is a whole run wasted
    # on something that could simply have been refused.
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
        # Engine support is complete (see calibrator.py's module docstring),
        # but exposing it to users is a separate decision not yet made.
        return jsonify({"success": False,
                        "error": "ADS-B verified mode is not currently available"}), 409

    if not device_state.acquire_calibration_lock():
        return jsonify({"success": False,
                        "error": "Auto-calibration already in progress"}), 409

    started, error = calibrator.start(towers, original, mode=mode)
    if not started:
        device_state.release_calibration_lock()
        return jsonify({"success": False, "error": error}), 409

    return jsonify({"success": True, "mode": mode,
                    "towers": [tower["name"] for tower in towers]})


@bp.route("/status", methods=["GET"])
def status():
    from app import calibrator, device_state

    payload = calibrator.get_status()

    # A Mender deployment pushed from the server installs autonomously —
    # mender-updated polls on its own and retina-gui is never consulted, so
    # unlike /mender/install there is no guard that can refuse it. It
    # replaces the containers underneath a run, after which every retune
    # fails; the run then reports tuning_not_applied and gets abandoned for
    # no reason the user can see. Annotating the status the modal already
    # polls turns that from a mystery into an explanation. Cheap enough to
    # do on every poll: it is a file-exists check plus a small JSON read.
    #
    # Deliberately reported rather than prevented. Blocking deployments
    # would mean publishing Mender Update Control maps, and a map left
    # behind by a crashed GUI would stall the fleet's updates - a worse
    # failure than the one being avoided. Judged an accepted risk: the
    # overlap window is narrow and the run already fails safely.
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
    """Persist a successful calibration result: write user.yml, then queue the
    config-merger + service restart (mirrors /towers/select).

    can_start_calibration() below already covers a Mender install in progress,
    which is why this route needs no separate update guard.
    """
    from app import apply_service, calibrator, config_mgr, device_state

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
    device['lnaState'] = int(result['lna_state'])
    # Always assert AGC off: a calibration result is by definition a manual
    # gain/LNA operating point (the AGC guard above refuses to run against
    # hardware AGC), so persisting one must never inherit a stale AGC-on
    # bandwidth from whatever was in user.yml before.
    device['bandwidthNumber'] = 0
    capture['device'] = device
    user_config['capture'] = capture
    config_mgr.save_user_config(user_config)

    # The user.yml write above stays synchronous — it must be on disk before
    # this returns. Only the slow merge+restart goes to the shared queue,
    # which always merges whatever is in user.yml when it runs, so it picks up
    # the write above. Poll /config/apply/status for progress.
    return jsonify({"success": True, "status": apply_service.request()}), 202
