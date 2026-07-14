import os
import subprocess
from flask import Blueprint, jsonify, request

bp = Blueprint('mode', __name__)

_mode_cache = 'radar'  # default mode if file read fails (e.g. dev environment without /data OR on startup before mode is set at least once)


def get_current_mode():
    """Read persisted mode. Returns 'radar', 'spectrum', or 'sdrconnect'."""
    from app import DATA_DIR
    try:
        with open(os.path.join(DATA_DIR, 'mode.txt')) as f:
            mode = f.read().strip()
            return mode if mode in ('radar', 'spectrum', 'sdrconnect') else 'radar'
    except (FileNotFoundError, OSError):
        return _mode_cache


def _send_config_snapshot(trigger):
    """Best-effort: never let a telemetry-assembly problem break the caller.
    The actual send is fire-and-forget on its own thread (see config_telemetry)."""
    try:
        from app import config_mgr, get_node_id, CONFIG_TELEMETRY_URL
        import config_telemetry
        config_telemetry.send_config_snapshot(
            CONFIG_TELEMETRY_URL, get_node_id(), get_current_mode(),
            config_mgr.load_merged_config(), trigger)
    except Exception:
        pass


def _write_mode(mode, trigger='mode_switch'):
    global _mode_cache
    _mode_cache = mode
    from app import DATA_DIR
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, 'mode.txt'), 'w') as f:
            f.write(mode)
    except OSError:
        pass  # dev: no /data — in-memory cache is the fallback
    _send_config_snapshot(trigger)


def run_config_merger_and_restart(retina_node_path: str, trigger: str = 'config_apply') -> str | None:
    """Run config-merger then, in radar mode, restart services.

    Returns an error string on failure, None on success — a snapshot is only
    sent on success, since that's the point the merged config actually
    reflects what's now running.
    Lets TimeoutExpired and FileNotFoundError propagate — callers handle them.
    """
    result = subprocess.run(
        ['docker', 'compose', '-p', 'retina-node', 'run', '--rm', 'config-merger'],
        cwd=retina_node_path,
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        return f'config-merger failed: {result.stderr or result.stdout}'

    if get_current_mode() in ('spectrum', 'sdrconnect'):
        _send_config_snapshot(trigger)
        return None

    # Defensive: ensure retina-spectrum is stopped before bringing the radar stack up.
    # Non-fatal — retina-spectrum may already be stopped.
    try:
        subprocess.run(['docker', 'compose', '-p', 'retina-node', 'stop', 'retina-spectrum'],
                       cwd=retina_node_path, capture_output=True, timeout=60)
        subprocess.run(['docker', 'compose', '-p', 'retina-node', 'rm', '-sf', 'retina-spectrum'],
                       cwd=retina_node_path, capture_output=True, timeout=30)
    except Exception:
        pass

    # Force a clean sdrplay_apiService restart so the USB device is properly
    # re-initialised before blah2 claims it.  Mirrors what the watchdog does.
    # Non-fatal: no-op on dev machines without sdrplay.service.
    subprocess.run(['systemctl', 'restart', 'sdrplay.service'],
                   capture_output=True, timeout=30)

    result = subprocess.run(
        ['docker', 'compose', '-p', 'retina-node', 'up', '-d', '--force-recreate'],
        cwd=retina_node_path,
        capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        return f'restart failed: {result.stderr or result.stdout}'

    _send_config_snapshot(trigger)
    return None


@bp.route('/api/mode', methods=['GET'])
def get_mode():
    return jsonify({'mode': get_current_mode()})


@bp.route('/api/mode', methods=['POST'])
def set_mode():
    from app import RETINA_NODE_PATH, config_mgr, calibrator, device_state

    data = request.get_json(silent=True) or {}
    mode = data.get('mode')
    if mode not in ('radar', 'spectrum', 'sdrconnect'):
        return jsonify({'success': False, 'error': 'Invalid mode'}), 400

    # Every branch below (including 'radar', which force-recreates the
    # containers) stops or restarts blah2 — any of that would yank the SDR
    # out from under an active calibration run. Checking calibrator.is_running()
    # directly (not just the lock file) matters because MODE_ADSB has no time
    # limit: a genuine multi-hour run would outlive the lock file's own
    # 20-minute staleness window, but is_running() is always correct.
    if calibrator.is_running() or device_state.is_calibration_locked()[0]:
        return jsonify({'success': False,
                        'error': 'Auto-calibration is running — cancel it before switching modes'}), 409

    node_installed = config_mgr.is_retina_node_installed()
    current_mode = get_current_mode()

    try:
        if not node_installed:
            # Dev / pre-deployment: persist mode but skip docker/systemctl commands
            _write_mode(mode)
            return jsonify({'success': True, 'mode': mode})

        if mode == 'spectrum':
            # Write mode first so the watchdog guard fires immediately and cannot
            # see blah2 stopped mid-transition and trigger a spurious stack restart.
            _write_mode(mode)

            if current_mode == 'sdrconnect':
                subprocess.run(['systemctl', 'stop', 'sdrconnect.service'],
                               capture_output=True, timeout=30)
            else:
                result = subprocess.run(
                    ['docker', 'compose', '-p', 'retina-node', 'stop',
                     'blah2', 'blah2_api', 'blah2_web', 'blah2_host'],
                    cwd=RETINA_NODE_PATH,
                    capture_output=True, text=True, timeout=60
                )
                if result.returncode != 0:
                    return jsonify({'success': False,
                                    'error': f'Failed to stop blah2: {result.stderr or result.stdout}'}), 500

            result = subprocess.run(
                ['docker', 'compose', '-p', 'retina-node', '--profile', 'spectrum', 'up', '-d', 'retina-spectrum'],
                cwd=RETINA_NODE_PATH,
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                return jsonify({'success': False,
                                'error': f'Failed to start retina-spectrum: {result.stderr or result.stdout}'}), 500

        elif mode == 'sdrconnect':
            # Write mode first, same reasoning as the spectrum transition above.
            _write_mode(mode)

            if current_mode == 'spectrum':
                subprocess.run(['docker', 'compose', '-p', 'retina-node', 'stop', 'retina-spectrum'],
                               cwd=RETINA_NODE_PATH, capture_output=True, timeout=60)
                subprocess.run(['docker', 'compose', '-p', 'retina-node', 'rm', '-sf', 'retina-spectrum'],
                               cwd=RETINA_NODE_PATH, capture_output=True, timeout=30)
            else:
                result = subprocess.run(
                    ['docker', 'compose', '-p', 'retina-node', 'stop',
                     'blah2', 'blah2_api', 'blah2_web', 'blah2_host'],
                    cwd=RETINA_NODE_PATH,
                    capture_output=True, text=True, timeout=60
                )
                if result.returncode != 0:
                    return jsonify({'success': False,
                                    'error': f'Failed to stop blah2: {result.stderr or result.stdout}'}), 500

            # Force a clean sdrplay_apiService restart so the USB device is
            # properly re-initialised before SDRconnect claims it.  Non-fatal.
            subprocess.run(['systemctl', 'restart', 'sdrplay.service'],
                           capture_output=True, timeout=30)

            result = subprocess.run(['systemctl', 'start', 'sdrconnect.service'],
                                    capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                return jsonify({'success': False,
                                'error': f'Failed to start sdrconnect.service: {result.stderr or result.stdout}'}), 500

        else:  # radar
            if current_mode == 'sdrconnect':
                subprocess.run(['systemctl', 'stop', 'sdrconnect.service'],
                               capture_output=True, timeout=30)
            else:
                result = subprocess.run(
                    ['docker', 'compose', '-p', 'retina-node', 'stop', 'retina-spectrum'],
                    cwd=RETINA_NODE_PATH,
                    capture_output=True, text=True, timeout=60
                )
                if result.returncode != 0:
                    return jsonify({'success': False,
                                    'error': f'Failed to stop retina-spectrum: {result.stderr or result.stdout}'}), 500

                # Remove the stopped container so it cannot be auto-restarted and
                # so the SDR device is cleanly released before blah2 starts.
                subprocess.run(
                    ['docker', 'compose', '-p', 'retina-node', 'rm', '-sf', 'retina-spectrum'],
                    cwd=RETINA_NODE_PATH,
                    capture_output=True, text=True, timeout=30
                )

            # Force a clean sdrplay_apiService restart so the USB device is
            # properly re-initialised before blah2 claims it.  Non-fatal.
            subprocess.run(['systemctl', 'restart', 'sdrplay.service'],
                           capture_output=True, timeout=30)

            result = subprocess.run(
                ['docker', 'compose', '-p', 'retina-node', 'up', '-d', '--force-recreate',
                 'blah2', 'blah2_api', 'blah2_web', 'blah2_host'],
                cwd=RETINA_NODE_PATH,
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                return jsonify({'success': False,
                                'error': f'Failed to start blah2: {result.stderr or result.stdout}'}), 500

            _write_mode(mode)

        return jsonify({'success': True, 'mode': mode})

    except subprocess.TimeoutExpired:
        return jsonify({'success': False, 'error': 'Command timed out'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/spectrum/ready', methods=['GET'])
def spectrum_ready():
    """Probe retina-spectrum to see if it is serving yet.

    Returns {ready: true} as soon as the container responds on its port,
    regardless of HTTP status code.
    """
    from app import RETINA_SPECTRUM_URL
    import urllib.request
    import urllib.error
    try:
        urllib.request.urlopen(RETINA_SPECTRUM_URL, timeout=2)
        return jsonify({'ready': True})
    except urllib.error.HTTPError:
        return jsonify({'ready': True})  # server responded — it's up
    except Exception:
        return jsonify({'ready': False})


@bp.route('/api/sdrconnect/ready', methods=['GET'])
def sdrconnect_ready():
    """Probe sdrconnect.service to see if it is up yet.

    SDRconnect has no HTTP port to probe, so this checks systemd unit state.
    """
    result = subprocess.run(['systemctl', 'is-active', 'sdrconnect.service'],
                            capture_output=True, timeout=5)
    return jsonify({'ready': result.returncode == 0})


def enforce_radar_mode(retina_node_path: str) -> None:
    """Stop retina-spectrum/sdrconnect and bring the radar stack up unconditionally.

    Called on wizard completion so the node is always left in a clean radar
    state regardless of what happened during the wizard flow. Non-fatal: errors
    are swallowed so callers don't need to handle them.
    """
    try:
        subprocess.run(
            ['docker', 'compose', '-p', 'retina-node', 'stop', 'retina-spectrum'],
            cwd=retina_node_path, capture_output=True, timeout=60
        )
        subprocess.run(
            ['docker', 'compose', '-p', 'retina-node', 'rm', '-sf', 'retina-spectrum'],
            cwd=retina_node_path, capture_output=True, timeout=30
        )
        subprocess.run(['systemctl', 'stop', 'sdrconnect.service'],
                       capture_output=True, timeout=30)
        subprocess.run(['systemctl', 'restart', 'sdrplay.service'],
                       capture_output=True, timeout=30)
        subprocess.run(
            ['docker', 'compose', '-p', 'retina-node', 'up', '-d', '--force-recreate',
             'blah2', 'blah2_api', 'blah2_web', 'blah2_host'],
            cwd=retina_node_path, capture_output=True, timeout=120
        )
    except Exception:
        pass


@bp.route('/api/mode/release-spectrum', methods=['POST'])
def release_spectrum():
    """Stop retina-spectrum and revert to radar mode.

    Called via navigator.sendBeacon when the user navigates away from the
    wizard location step mid-flow. Returns 204 — callers do not inspect the
    response body.
    """
    from app import RETINA_NODE_PATH, config_mgr
    if not config_mgr.is_retina_node_installed():
        return '', 204
    try:
        subprocess.run(['docker', 'compose', '-p', 'retina-node', 'stop', 'retina-spectrum'],
                       cwd=RETINA_NODE_PATH, capture_output=True, timeout=60)
        subprocess.run(['docker', 'compose', '-p', 'retina-node', 'rm', '-sf', 'retina-spectrum'],
                       cwd=RETINA_NODE_PATH, capture_output=True, timeout=30)
        _write_mode('radar')
    except Exception:
        pass
    return '', 204
