import os
import subprocess
import time

from flask import Blueprint, jsonify, request

bp = Blueprint('mode', __name__)

_mode_cache = 'radar'  # default mode if file read fails (e.g. dev environment without /data OR on startup before mode is set at least once)

# Gap between the sdrplay_apiService restart and the container recreate.
# Running them back to back has repeatedly wedged the device on real hardware.
# See docs/features/sdr-mode.md#settle-window.
SDRPLAY_RESTART_SETTLE_SECONDS = 30

# Budget for the stack recreate: ~20x the measured 13.5s on a loaded Pi, since
# killing the CLI mid-recreate does real damage (see stack_reconcile).
# See docs/features/sdr-mode.md#recreate-timeout-and-repair.
RECREATE_TIMEOUT_SECONDS = 300


def get_current_mode():
    """Read persisted mode. Returns 'radar', 'spectrum', or 'sdrconnect'."""
    from app import DATA_DIR
    try:
        with open(os.path.join(DATA_DIR, 'mode.txt')) as f:
            mode = f.read().strip()
            return mode if mode in ('radar', 'spectrum', 'sdrconnect') else 'radar'
    except (FileNotFoundError, OSError):
        return _mode_cache


def _write_mode(mode):
    global _mode_cache
    _mode_cache = mode
    from app import DATA_DIR
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, 'mode.txt'), 'w') as f:
            f.write(mode)
    except OSError:
        pass  # dev: no /data — in-memory cache is the fallback


def restart_sdrplay_service(on_phase=None):
    """Restart sdrplay_apiService, forcing it if the unit will not stop.

    A plain restart hangs forever when the device has wedged, because
    sdrplay_apiService ignores SIGTERM. The fallback SIGKILLs it, then
    reset-fails and starts the unit.
    See docs/features/sdr-mode.md#why-a-plain-restart-is-not-enough.

    Never raises: callers treat the restart as best-effort, and a node
    without sdrplay.service (dev machines) must stay a no-op.
    Returns None normally, or a short description of what it had to do.
    """
    if on_phase is not None:
        on_phase('restarting_sdr', None)
    try:
        result = subprocess.run(['systemctl', 'restart', 'sdrplay.service'],
                                capture_output=True, timeout=30)
        if result.returncode == 0:
            return None
        detail = 'systemctl restart returned non-zero'
    except subprocess.TimeoutExpired:
        detail = 'systemctl restart hung (service stuck stopping)'
    except (FileNotFoundError, OSError):
        return None  # no systemd/sdrplay here — dev machine

    if on_phase is not None:
        on_phase('resetting_sdr', None)
    try:
        # -f, not -x: comm is truncated to 15 chars, so -x never matches.
        # Bracketed so it cannot match (and kill) a shell carrying the pattern.
        # See docs/features/sdr-mode.md#the-forced-reset.
        subprocess.run(['pkill', '-9', '-f', '[s]drplay_apiService'],
                       capture_output=True, timeout=15)
        # Killing the process is what releases the stuck job, but systemd
        # needs a moment to reap it before the unit is actionable again.
        time.sleep(3)
        subprocess.run(['systemctl', 'reset-failed', 'sdrplay.service'],
                       capture_output=True, timeout=15)
        # Often a no-op: the restart queued above usually completes on its own
        # once the process dies. Harmless when it already has.
        subprocess.run(['systemctl', 'start', 'sdrplay.service'],
                       capture_output=True, timeout=30)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        return f'{detail}; forced reset also failed ({type(e).__name__})'
    return f'{detail}; forced a reset of sdrplay_apiService'


def _settle(seconds, on_phase):
    """Sleep out the SDRplay settle window, reporting the remaining time.

    Chunked only so callers can show a countdown: a silent 30s looks like a
    hang and drove users to click Apply twice. See docs/features/sdr-mode.md#settle-window.
    """
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        on_phase('settling', int(remaining) + 1)
        time.sleep(min(1.0, remaining))


def run_config_merger_and_restart(retina_node_path: str, on_phase=None,
                                  lock_timeout=None) -> str | None:
    """Run config-merger then, in radar mode, restart services.

    Holds the stack-restart lock (see restart_lock.py) for the whole
    operation, settle window included, so the cron watchdog and every other
    caller queue behind it instead of interleaving `docker compose` runs
    against the same project.

    lock_timeout: how long to wait for that lock. Defaults to the short,
    request-shaped wait. ApplyService, the only caller, runs off-request and
    passes the long one.

    on_phase: optional callback(phase, detail) for progress reporting.
    Returns an error string on failure, None on success.
    Lets TimeoutExpired and FileNotFoundError propagate — callers handle them.
    """
    from app import DATA_DIR
    from restart_lock import DEFAULT_TIMEOUT_SECONDS, RestartBusy, restart_lock

    on_phase = on_phase or (lambda phase, detail=None: None)
    if lock_timeout is None:
        lock_timeout = DEFAULT_TIMEOUT_SECONDS

    on_phase('waiting_for_lock', None)
    try:
        with restart_lock(DATA_DIR, timeout=lock_timeout):
            return _run_config_merger_and_restart_locked(
                retina_node_path, on_phase)
    except RestartBusy as e:
        return str(e)


def _run_config_merger_and_restart_locked(retina_node_path, on_phase):
    """The body of run_config_merger_and_restart, with the lock held."""
    on_phase('merging', None)
    result = subprocess.run(
        ['docker', 'compose', '-p', 'retina-node', 'run', '--rm', 'config-merger'],
        cwd=retina_node_path,
        capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        return f'config-merger failed: {result.stderr or result.stdout}'

    if get_current_mode() in ('spectrum', 'sdrconnect'):
        return None

    # Defensive: ensure retina-spectrum is stopped before bringing the radar stack up.
    # Non-fatal — retina-spectrum may already be stopped.
    on_phase('stopping_spectrum', None)
    try:
        subprocess.run(['docker', 'compose', '-p', 'retina-node', 'stop', 'retina-spectrum'],
                       cwd=retina_node_path, capture_output=True, timeout=60)
        subprocess.run(['docker', 'compose', '-p', 'retina-node', 'rm', '-sf', 'retina-spectrum'],
                       cwd=retina_node_path, capture_output=True, timeout=30)
    except Exception:
        pass

    # Re-initialise the USB device before blah2 claims it, as the watchdog
    # does. Non-fatal; reports its own phases.
    restart_sdrplay_service(on_phase)

    # See SDRPLAY_RESTART_SETTLE_SECONDS: skipping this wedges the device.
    _settle(SDRPLAY_RESTART_SETTLE_SECONDS, on_phase)

    on_phase('recreating', None)
    try:
        result = subprocess.run(
            ['docker', 'compose', '-p', 'retina-node', 'up', '-d', '--force-recreate'],
            cwd=retina_node_path,
            capture_output=True, text=True, timeout=RECREATE_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        # The CLI has just been SIGKILLed, quite possibly between renaming a
        # container and removing the old one. Repair before returning, or
        # every later apply fails on the name conflict it leaves behind.
        return _repair(retina_node_path, on_phase,
                       'Restarting the radar services timed out.')

    if result.returncode != 0:
        output = result.stderr or result.stdout
        if _is_name_conflict(output):
            return _repair(retina_node_path, on_phase,
                           'Restarting the radar services hit a container name conflict.')
        return f'restart failed: {output}'

    return None


def _is_name_conflict(output):
    """Compose failing because a half-recreated container still holds a name."""
    lowered = (output or '').lower()
    return 'already in use' in lowered or 'error when allocating new name' in lowered


def _repair(retina_node_path, on_phase, reason):
    """Clear half-recreated containers and restore the stack.

    Runs with the restart lock still held (every caller of this is inside
    _run_config_merger_and_restart_locked), which is what makes it safe to
    remove containers here.
    """
    from stack_reconcile import reconcile

    on_phase('repairing', None)
    removed, error = reconcile(retina_node_path)
    if error:
        return f'{reason} Automatic repair also failed: {error}'
    if removed:
        return (f'{reason} Cleaned up {len(removed)} half-created '
                f'container(s) and restarted. Check the radar is running.')
    return f'{reason} No half-created containers were left behind.'


@bp.route('/api/mode', methods=['GET'])
def get_mode():
    return jsonify({'mode': get_current_mode()})


@bp.route('/api/mode', methods=['POST'])
def set_mode():
    from app import RETINA_NODE_PATH, calibrator, config_mgr, device_state

    data = request.get_json(silent=True) or {}
    mode = data.get('mode')
    if mode not in ('radar', 'spectrum', 'sdrconnect'):
        return jsonify({'success': False, 'error': 'Invalid mode'}), 400

    # Every branch stops or restarts blah2, which would pull the SDR from a
    # calibration run. is_running() is checked too because an ADS-B run can
    # outlive the lock file's staleness window. See docs/features/sdr-mode.md#switching-modes.
    if calibrator.is_running() or device_state.is_calibration_locked()[0]:
        return jsonify({'success': False,
                        'error': 'Auto-calibration is running. Cancel it before switching modes'}), 409

    # Same reasoning as /config/apply: a Mender install is replacing the very
    # containers and manifests every branch below manipulates, and
    # mender-update's own docker commands are outside the restart lock.
    in_progress, reason = device_state.is_any_update_in_progress()
    if in_progress:
        return jsonify({'success': False,
                        'error': f'{reason}. Switch modes once it finishes.'}), 409

    node_installed = config_mgr.is_retina_node_installed()
    current_mode = get_current_mode()

    try:
        if not node_installed:
            # Dev / pre-deployment: persist mode but skip docker/systemctl commands
            _write_mode(mode)
            return jsonify({'success': True, 'mode': mode})

        from app import DATA_DIR
        from restart_lock import RestartBusy, restart_lock
        try:
            with restart_lock(DATA_DIR):
                return _set_mode_locked(mode, current_mode, RETINA_NODE_PATH)
        except RestartBusy as e:
            return jsonify({'success': False, 'error': str(e)}), 409

    except subprocess.TimeoutExpired:
        return jsonify({'success': False, 'error': 'Command timed out'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


def _set_mode_locked(mode, current_mode, retina_node_path):
    """The Docker/systemd half of set_mode, with the restart lock held.

    The lock must cover the whole transition, not just one command, because a
    config apply or the watchdog may be recreating the same containers.
    """
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
                cwd=retina_node_path,
                capture_output=True, text=True, timeout=60
            )
            if result.returncode != 0:
                return jsonify({'success': False,
                                'error': f'Failed to stop blah2: {result.stderr or result.stdout}'}), 500

        result = subprocess.run(
            ['docker', 'compose', '-p', 'retina-node', '--profile', 'spectrum', 'up', '-d', 'retina-spectrum'],
            cwd=retina_node_path,
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
                           cwd=retina_node_path, capture_output=True, timeout=60)
            subprocess.run(['docker', 'compose', '-p', 'retina-node', 'rm', '-sf', 'retina-spectrum'],
                           cwd=retina_node_path, capture_output=True, timeout=30)
        else:
            result = subprocess.run(
                ['docker', 'compose', '-p', 'retina-node', 'stop',
                 'blah2', 'blah2_api', 'blah2_web', 'blah2_host'],
                cwd=retina_node_path,
                capture_output=True, text=True, timeout=60
            )
            if result.returncode != 0:
                return jsonify({'success': False,
                                'error': f'Failed to stop blah2: {result.stderr or result.stdout}'}), 500

        # Force a clean sdrplay_apiService restart so the USB device is
        # properly re-initialised before SDRconnect claims it.  Non-fatal.
        restart_sdrplay_service()

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
                cwd=retina_node_path,
                capture_output=True, text=True, timeout=60
            )
            if result.returncode != 0:
                return jsonify({'success': False,
                                'error': f'Failed to stop retina-spectrum: {result.stderr or result.stdout}'}), 500

            # Remove the stopped container so it cannot be auto-restarted and
            # so the SDR device is cleanly released before blah2 starts.
            subprocess.run(
                ['docker', 'compose', '-p', 'retina-node', 'rm', '-sf', 'retina-spectrum'],
                cwd=retina_node_path,
                capture_output=True, text=True, timeout=30
            )

        # Force a clean sdrplay_apiService restart so the USB device is
        # properly re-initialised before blah2 claims it.  Non-fatal.
        restart_sdrplay_service()

        result = subprocess.run(
            ['docker', 'compose', '-p', 'retina-node', 'up', '-d', '--force-recreate',
             'blah2', 'blah2_api', 'blah2_web', 'blah2_host', 'retina-tracker'],
            cwd=retina_node_path,
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            return jsonify({'success': False,
                            'error': f'Failed to start blah2: {result.stderr or result.stdout}'}), 500

        _write_mode(mode)

    return jsonify({'success': True, 'mode': mode})


@bp.route('/api/spectrum/ready', methods=['GET'])
def spectrum_ready():
    """Probe retina-spectrum to see if it is serving yet.

    Returns {ready: true} as soon as the container responds on its port,
    regardless of HTTP status code.
    """
    import urllib.error
    import urllib.request

    from app import RETINA_SPECTRUM_URL
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

    Called on wizard completion and, from a background thread, to recover a
    failed Mender install (see mender_routes), so it must take the restart lock.
    Never raises, including when the lock is busy: the holder is already
    restarting the stack. See docs/features/sdr-mode.md#returning-to-radar.
    """
    from app import DATA_DIR
    from restart_lock import restart_lock

    try:
        with restart_lock(DATA_DIR):
            _enforce_radar_mode_locked(retina_node_path)
    except Exception:
        pass


def _enforce_radar_mode_locked(retina_node_path: str) -> None:
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
        restart_sdrplay_service()
        subprocess.run(
            ['docker', 'compose', '-p', 'retina-node', 'up', '-d', '--force-recreate',
             'blah2', 'blah2_api', 'blah2_web', 'blah2_host', 'retina-tracker'],
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
    from app import DATA_DIR, RETINA_NODE_PATH, config_mgr
    from restart_lock import OPPORTUNISTIC_TIMEOUT_SECONDS, restart_lock

    if not config_mgr.is_retina_node_installed():
        return '', 204
    try:
        # Opportunistic: a beacon nobody waits on, and every restart path
        # already stops retina-spectrum defensively, so giving up when the
        # lock is busy loses nothing.
        with restart_lock(DATA_DIR, timeout=OPPORTUNISTIC_TIMEOUT_SECONDS):
            subprocess.run(['docker', 'compose', '-p', 'retina-node', 'stop', 'retina-spectrum'],
                           cwd=RETINA_NODE_PATH, capture_output=True, timeout=60)
            subprocess.run(['docker', 'compose', '-p', 'retina-node', 'rm', '-sf', 'retina-spectrum'],
                           cwd=RETINA_NODE_PATH, capture_output=True, timeout=30)
        _write_mode('radar')
    except Exception:
        pass
    return '', 204
