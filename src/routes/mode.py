import os
import subprocess
import time
from flask import Blueprint, jsonify, request

bp = Blueprint('mode', __name__)

_mode_cache = 'radar'  # default mode if file read fails (e.g. dev environment without /data OR on startup before mode is set at least once)

# The sdrplay_apiService restart and the immediately-following container
# recreate used to race with no settle time in between — diagnosed live
# on jonathan-node-2 as a repeated cause of the SDRplay device wedging
# outright (not just a bad gain candidate). This window gives the
# service time to finish reinitialising the USB device before blah2
# claims it again.
SDRPLAY_RESTART_SETTLE_SECONDS = 30

# Budget for the stack recreate. Measured at 13.5s on a node under load
# (4-core Pi at load 4.3), so this is ~20x headroom rather than a guess:
# the old 120s was generous too, and what actually blew it was two compose
# runs contending, not the work itself. Now that the restart lock makes that
# contention impossible, the remaining reasons to exceed even this are a
# genuinely stuck device or daemon — cases where killing the CLI mid-recreate
# does real damage (see stack_reconcile), so it is worth waiting longer
# before resorting to that.
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


def _settle(seconds, on_phase):
    """Sleep out the SDRplay settle window, reporting the remaining time.

    Chunked rather than one flat sleep purely so callers can show a
    countdown: this window is two thirds of a whole apply's wall time, and
    a progress display that sits silent through it is indistinguishable
    from a hang — which is what drove users to click Apply a second time
    and collide two `docker compose` runs in the first place.
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
    request-shaped wait, because /towers/select and /calibrate/apply still
    call this inline on a Flask request thread and must not hang a browser
    for minutes. ApplyService, which runs off-request, passes the long one.

    on_phase: optional callback(phase, detail) for progress reporting.
    Returns an error string on failure, None on success.
    Lets TimeoutExpired and FileNotFoundError propagate — callers handle them.
    """
    from app import DATA_DIR
    from restart_lock import restart_lock, RestartBusy, DEFAULT_TIMEOUT_SECONDS

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

    # Force a clean sdrplay_apiService restart so the USB device is properly
    # re-initialised before blah2 claims it.  Mirrors what the watchdog does.
    # Non-fatal: no-op on dev machines without sdrplay.service.
    on_phase('restarting_sdr', None)
    subprocess.run(['systemctl', 'restart', 'sdrplay.service'],
                   capture_output=True, timeout=30)

    # See SDRPLAY_RESTART_SETTLE_SECONDS — without this, recreating the
    # containers immediately after the restart request returns has
    # repeatedly wedged the SDRplay device on real hardware.
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
                f'container(s) and restarted — check the radar is running.')
    return f'{reason} No half-created containers were left behind.'


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
                        'error': 'Auto-calibration is running. Cancel it before switching modes'}), 409

    node_installed = config_mgr.is_retina_node_installed()
    current_mode = get_current_mode()

    try:
        if not node_installed:
            # Dev / pre-deployment: persist mode but skip docker/systemctl commands
            _write_mode(mode)
            return jsonify({'success': True, 'mode': mode})

        from app import DATA_DIR
        from restart_lock import restart_lock, RestartBusy
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

    Split out of set_mode purely so the lock scope is the whole transition
    and not just one command: every branch here stops or starts containers
    that another caller (a config apply, the cron watchdog) may be
    recreating at the same instant.
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
        subprocess.run(['systemctl', 'restart', 'sdrplay.service'],
                       capture_output=True, timeout=30)

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

    Takes the restart lock like every other recreate path — this one matters
    more than its "wizard completion" description suggests, because the
    Mender install path also calls it from a background thread to recover a
    failed install (see mender_routes), where nothing else would be
    coordinating it with a concurrent apply. Failing to get the lock is
    swallowed along with everything else here: whoever holds it is already
    performing a restart, which leaves the stack up regardless.
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
        subprocess.run(['systemctl', 'restart', 'sdrplay.service'],
                       capture_output=True, timeout=30)
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
    from restart_lock import restart_lock, OPPORTUNISTIC_TIMEOUT_SECONDS

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
