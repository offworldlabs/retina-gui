"""Runs config apply (config-merger + stack restart) off the request thread.

The work runs on a background thread so no HTTP timeout or closed tab can
interrupt a restart half-way, and a repeat request while one is running
coalesces into a single extra pass instead of starting a second, colliding
`docker compose`. Serialising against other callers (mode switches, the
watchdog) is the restart lock's job, not this class's.
See docs/architecture.md#the-apply-service.
"""

import threading
from datetime import datetime, timezone


class ConfigChangeRefused(Exception):
    """Raised by request() when something is using the SDR that an apply
    would pull out from under it.

    Checked here rather than per route so routes added later cannot forget
    it. Raised, not returned, so an unhandled refusal is a 500 rather than a
    202 claiming work was queued. See docs/architecture.md#the-calibration-guard.
    """

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason

PHASE_LABELS = {
    'waiting_for_lock': 'Waiting for another restart to finish',
    'merging': 'Merging configuration',
    'stopping_spectrum': 'Releasing the SDR',
    'restarting_sdr': 'Restarting the SDR service',
    'resetting_sdr': 'SDR service stuck, forcing it down',
    'settling': 'Waiting for the SDR to settle',
    'recreating': 'Restarting radar services',
    'repairing': 'Cleaning up after an interrupted restart',
}


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


class ApplyService:
    """One-at-a-time config apply with progress and request coalescing."""

    def __init__(self, retina_node_path, dev_mode=False, restart_fn=None,
                 guard=None):
        self._retina_node_path = retina_node_path
        self._dev_mode = dev_mode
        # Optional callable returning (ok, reason), checked by request().
        self._guard = guard
        # Injected so tests can pass a fake. None resolves the real one lazily,
        # because routes.mode imports app, which constructs this class.
        self._restart_fn = restart_fn
        self._lock = threading.Lock()
        self._thread = None
        self._rerun_requested = False
        self._status = self._idle_status()

    @staticmethod
    def _idle_status():
        return {
            'state': 'idle',       # idle | running | done | failed
            'phase': None,
            'phase_label': None,
            'settle_remaining': None,
            'error': None,
            'queued': False,
            'started_at': None,
            'finished_at': None,
        }

    def get_status(self):
        with self._lock:
            return dict(self._status)

    def is_running(self):
        with self._lock:
            return self._status['state'] == 'running'

    def request(self, bypass_guard=False):
        """Start an apply, or coalesce into the one already running.

        Returns the current status dict. Never blocks on the actual work.
        Raises ConfigChangeRefused if the configured guard says something
        else is using the SDR right now.

        bypass_guard is for exactly one caller: Auto-Calibrate's own preflight
        recovery (calibrator._run_recovery_apply), where the calibration is
        the caller. No route should ever pass this.
        """
        if self._guard is not None and not bypass_guard:
            ok, reason = self._guard()
            if not ok:
                raise ConfigChangeRefused(reason)

        if self._dev_mode:
            with self._lock:
                self._status = self._idle_status()
                self._status.update(state='done', finished_at=_utcnow())
                return dict(self._status)

        with self._lock:
            if self._status['state'] == 'running':
                # Coalesce: one more pass after the current one, which will
                # pick up whatever is in user.yml by then.
                self._rerun_requested = True
                self._status['queued'] = True
                return dict(self._status)

            self._status = self._idle_status()
            self._status.update(state='running', started_at=_utcnow())
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return dict(self._status)

    def _set_phase(self, phase, detail=None):
        with self._lock:
            self._status['phase'] = phase
            self._status['phase_label'] = PHASE_LABELS.get(phase)
            self._status['settle_remaining'] = detail if phase == 'settling' else None

    def _resolve_restart_fn(self):
        if self._restart_fn is not None:
            return self._restart_fn
        from routes.mode import run_config_merger_and_restart
        return run_config_merger_and_restart

    def _run(self):
        import subprocess

        from restart_lock import BACKGROUND_TIMEOUT_SECONDS

        restart = self._resolve_restart_fn()

        while True:
            error = None
            try:
                # Off-request, so it can queue behind a long operation.
                error = restart(
                    self._retina_node_path, on_phase=self._set_phase,
                    lock_timeout=BACKGROUND_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                error = 'Command timed out'
            except FileNotFoundError:
                error = 'docker not found. Is it installed?'
            except Exception as e:
                error = str(e)

            with self._lock:
                # A request that arrived while this pass was running gets one
                # more pass — but only one, however many arrived, since they
                # would all merge the same user.yml.
                if self._rerun_requested and error is None:
                    self._rerun_requested = False
                    self._status['queued'] = False
                    continue
                self._rerun_requested = False
                self._status.update(
                    state='failed' if error else 'done',
                    phase=None,
                    phase_label=None,
                    settle_remaining=None,
                    error=error,
                    queued=False,
                    finished_at=_utcnow(),
                )
                return
