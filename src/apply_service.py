"""Runs config apply (config-merger + stack restart) off the request thread.

Why this exists, rather than /config/apply calling
run_config_merger_and_restart directly as it used to:

A whole apply measures ~45s on real hardware, two thirds of which is the
SDRplay settle window — a fixed sleep with nothing to show for it. Run
synchronously inside the POST, that is 45 seconds of a spinner that cannot
distinguish "working" from "hung", and the only feedback the user ever gets
is a single success/failure at the end. Users reasonably concluded it had
stalled and clicked Apply again, which started a *second* `docker compose`
against the same project: that is what produced the container-name
conflicts ("The container name /tar1090 is already in use"), and the
resulting contention is what pushed the compose step past its own 120s
timeout and produced "Command timed out" — which re-enabled the button and
invited yet another click.

So the fix is two-part and both halves live here:

  - The work runs on a background thread and the route returns immediately.
    No HTTP timeout, proxy timeout, or closed tab can interrupt a restart
    half-way through any more.
  - A repeat request while one is already running does NOT start a second
    run. It sets a re-run flag, and the worker starts one more pass when the
    current one finishes. Since config-merger reads user.yml at the moment
    it runs (it is never handed a snapshot), that single extra pass picks up
    every config change saved in the meantime — which is exactly the "hold
    the config and apply it at the right time" behaviour, without the user
    having to time anything.

Serialisation against *other* callers (mode switches, the cron watchdog,
tower select) is not this class's job — that is the restart lock inside
run_config_merger_and_restart. This class only ensures the config-apply
path never queues work against itself.
"""

import threading
from datetime import datetime, timezone


class ConfigChangeRefused(Exception):
    """Raised by request() when something is using the SDR that an apply
    would pull out from under it.

    The check lives here, not in the routes, because routes are exactly what
    gets forgotten. /api/mode and /mender/install each grew their own
    calibration guard, but /config/apply and /towers/select — added later —
    did not, and nothing caught it. Demonstrated on a live node: clicking
    Apply Changes during an Auto-Calibrate run recreated all seven
    containers underneath it, after which every retune failed and the run
    carried on to report an ordinary-looking "no confirmed track". Both of
    those buttons sit on the same page as the Auto-Calibrate one.

    Every path that restarts the stack for a config change funnels through
    request(), so guarding it covers the routes that exist and the ones that
    do not yet. Raising rather than returning a refusal is deliberate: a
    caller that forgets to handle this gets a 500, not a silent 202 that
    claims work was queued when it was not.
    """

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason

PHASE_LABELS = {
    'waiting_for_lock': 'Waiting for another restart to finish',
    'merging': 'Merging configuration',
    'stopping_spectrum': 'Releasing the SDR',
    'restarting_sdr': 'Restarting the SDR service',
    'resetting_sdr': 'SDR service stuck — forcing it down',
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
        # Optional callable returning (ok, reason). Checked by request()
        # before any work starts — see ConfigChangeRefused for why the check
        # belongs here rather than in each route.
        self._guard = guard
        # Injected collaborator, same idiom as Calibrator's clients: tests
        # pass a fake instead of monkeypatching routes.mode, which conftest's
        # importlib.reload(app) would swap out from under them anyway.
        # None means "resolve the real one lazily" — routes.mode imports app,
        # and app constructs this class, so it cannot be imported at module
        # load time without a cycle.
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

        bypass_guard exists for exactly one caller: Auto-Calibrate's own
        preflight recovery (see calibrator._run_recovery_apply). The guard's
        job is to stop a config apply pulling the SDR out from under a
        running calibration — but there the calibration *is* the caller, it
        holds the calibration lock itself, and restarting the stack is the
        only way to unwedge the device it is about to search with. Refusing
        it there would mean the guard blocking the one apply that exists to
        make the run possible. No route should ever pass this.
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
                # Not attached to a request, so it can afford to queue behind
                # a long operation (a mode switch, the watchdog) rather than
                # give up and make the user click again.
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
