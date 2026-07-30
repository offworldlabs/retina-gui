"""AGC last-resort fallback for Auto-Calibrate.

A deliberate, narrow exception to calibrator.py's "nothing is written to
user.yml during a run" rule (see its module docstring) — the very last
resort of a run that has already exhausted every tower's manual gain/LNA
search. Lives in its own module, not calibrator.py, because turning
hardware AGC on/off is not part of blah2's live retune wire protocol
(Blah2Client.retune() only ever carries fc/gainReductionA/gainReductionB/
lnaState — see blah2_client.py) — it requires writing user.yml, running
config-merger, and recreating the blah2-family containers, a
fundamentally slower, Flask/subprocess/Docker-shaped mechanism than the
rest of the search's sub-second live HTTP calls. calibrator.py is
architecturally isolated from Flask/subprocess/Docker on purpose (zero
imports of flask/app/subprocess/routes.mode) — this class is injected as
Calibrator's third collaborator, exactly like blah2_client/
retina_tracker_client, so calibrator.py's own tests can fake it instead
of mocking Flask/subprocess/Docker.
"""

import subprocess
import threading

# Backstop for the fresh thread the config-merger/restart step runs on
# (see apply()) — the underlying function already self-bounds via its own
# internal subprocess timeouts (config-merger 60s + spectrum stop/rm ~90s
# + systemctl restart 30s + settle + up --force-recreate 120s), so this
# is just a defensive upper bound in case something hangs in a way that
# ignores those. A Python thread can't be force-killed, so past this
# point apply() just reports failure and moves on — the thread may still
# be running in the background.
RESTART_THREAD_JOIN_TIMEOUT_SECONDS = 300


class AgcFallbackClient:
    """Writes capture.fc/device.* to user.yml and restarts the capture
    stack, for Auto-Calibrate's once-per-run AGC last resort.

    config_mgr: the app's ConfigManager (already carries retina_node_path
    — see config_manager.py — so this class doesn't need it separately).
    dev_mode: mirrors DEV_MODE's established "skip subprocess/Docker
    entirely" pattern (see routes/config.py's /config/apply) — nothing to
    restart on a dev machine with no retina-node stack.
    """

    def __init__(self, config_mgr, dev_mode=False):
        self._config_mgr = config_mgr
        self._dev_mode = dev_mode

    def apply(self, fc, bandwidth_number, gain_a, gain_b, lna_state):
        """Write fc + device.bandwidthNumber/gainReduction/lnaState to
        user.yml, then run config-merger and restart+settle the radar
        stack (see routes/mode.run_config_merger_and_restart). gain_a is
        a placeholder when bandwidth_number turns AGC on (the hardware
        overrides it live) but is still written as a valid number — the
        persisted config always needs one regardless of AGC state.

        Returns (success: bool, error: str|None). Never raises — mirrors
        every existing Flask-route caller of run_config_merger_and_restart.

        The config-merger/restart step runs on a fresh, dedicated thread
        rather than directly on whatever thread called apply() — for
        Auto-Calibrate, that's the Calibrator's own long-running
        background thread, already alive and busy (retuning, polling)
        for the ~10-minute search duration by the time this runs.
        Confirmed live (jonathan-node-2): the identical subprocess call
        reliably completed in seconds when run from a fresh shell or a
        normal Flask request thread (exactly what /config/apply already
        gets), but hung past a 240s timeout when called directly from
        the calibrator's thread in that state. Root cause not fully
        confirmed, but this matches the one real structural difference
        between the two call sites, and costs nothing to apply
        defensively for every caller of this method, not just the one
        that exposed it.
        """
        if self._dev_mode:
            return True, None
        if not self._config_mgr.is_retina_node_installed():
            return False, "retina-node is not installed"

        user_config = dict(self._config_mgr.load_user_config())
        capture = dict(user_config.get('capture', {}) or {})
        capture['fc'] = int(fc)
        device = dict(capture.get('device', {}) or {})
        device['bandwidthNumber'] = int(bandwidth_number)
        device['gainReduction'] = [int(gain_a), int(gain_b)]
        device['lnaState'] = int(lna_state)
        capture['device'] = device
        user_config['capture'] = capture
        self._config_mgr.save_user_config(user_config)

        # Lazy import: the same circular-import-avoidance idiom every
        # routes/*.py handler already uses for this exact function (see
        # routes/config.py, routes/calibrate.py, routes/towers.py) —
        # app.py hasn't finished constructing config_mgr/calibrator (and
        # this collaborator) when this module is first imported, so
        # importing routes.mode at call time, not module load time,
        # sidesteps the ordering problem entirely.
        from routes.mode import run_config_merger_and_restart

        outcome = {}

        def run_restart():
            try:
                outcome['error'] = run_config_merger_and_restart(
                    self._config_mgr.retina_node_path)
            except subprocess.TimeoutExpired:
                outcome['error'] = "Command timed out"
            except FileNotFoundError:
                outcome['error'] = "docker not found. Is it installed?"
            except Exception as e:
                outcome['error'] = str(e)

        thread = threading.Thread(target=run_restart, daemon=True)
        thread.start()
        thread.join(timeout=RESTART_THREAD_JOIN_TIMEOUT_SECONDS)
        if thread.is_alive():
            return False, "Command timed out"

        error = outcome.get('error')
        return error is None, error
