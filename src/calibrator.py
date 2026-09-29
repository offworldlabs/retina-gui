"""Auto-Calibrate engine: tune tower/fc, per-tuner gain reduction and the
shared LNA state until a track confirms (or, with skip_confirmation, until
the operating point is proven not to overload).

Safety invariants, each explained in docs/features/auto-calibrate.md:
- Every axis starts at its safe end (max gain reduction, max LNA state) and
  steps toward sensitivity, reverting on the first overload. Never start at
  max gain: hardware AGC is off for the whole run. See #safe-end-first.
- A device error while probing a candidate is treated as an overload at that
  candidate, not as a run-ending failure. See #device-wedges.
- A frequency change always goes via the safe corner at the old fc. See
  #safe-corner-handover-on-a-frequency-change.
- The only user.yml write is the preflight's recovery, and it is never
  reverted. Everything else is live retune only. See #preflight-and-recovery.

This module imports no Flask, subprocess or Docker; the recovery restart
goes through an injected ApplyService. All blah2-side timestamps share
blah2's clock, so freshness comparisons never mix clock domains.
"""

import copy
import threading
import time
from datetime import datetime, timezone

# Bounds mirror blah2's RspDuo limits. LNA state is one register shared by
# both tuners; higher number = more attenuation (1 = max gain, 9 = min gain).
# See docs/features/auto-calibrate.md#lna-state-vs-gain-reduction.
GAIN_REDUCTION_MIN = 20
GAIN_REDUCTION_MAX = 59

LNA_STATE_MIN = 1
LNA_STATE_MAX = 9

# Descent step, and the one refine step (surveillance only).
# See docs/features/auto-calibrate.md#search-order.
DESCENT_STEP_DB = 10
REFINE_STEP_DB = 5

# MODE_ADSB: step gainReductionB this much more sensitive after an aircraft
# was in range but never matched.
ADSB_GAIN_STEP_DB = 5

# MODE_ADSB descent ceiling per tower (MODE_ADSB has no run budget).
ADSB_DESCENT_DEADLINE_SECONDS = 120

# Preflight recovery (see _preflight). Sized in
# docs/features/auto-calibrate.md#timing-constants.
# Bounded from above: the preflight runs before TOTAL_BUDGET_SECONDS starts,
# and 180 + 60 + 900 = 1140 must stay under the 1200s calibrate.lock timeout
# shared with blah2-arm's watchdog. See #lock-timeouts in the same doc.
PREFLIGHT_RECOVERY_APPLY_TIMEOUT_SECONDS = 180
PREFLIGHT_RECOVERY_PROBE_SECONDS = 60
PREFLIGHT_APPLY_POLL_SECONDS = 1.0

# Retune protocol timing.
ACK_TIMEOUT_SECONDS = 2.0
ACK_POLL_SECONDS = 0.2
APPLY_RETRY_DELAY_SECONDS = 0.5
RF_STATUS_TIMEOUT_SECONDS = 6.0
RF_STATUS_POLL_SECONDS = 0.3
# Physical settle after a retune, before reading overload. Not the reporting
# latency. See docs/features/auto-calibrate.md#settle-timing.
OVERLOAD_SETTLE_SECONDS = 1.0

# MODE_ADSB dwell poll interval.
DWELL_POLL_SECONDS = 1.0

# How often a dwell or soak re-reads overload state: one probe cannot tell a
# clean point from one that clips intermittently.
# See docs/features/auto-calibrate.md#dwell-and-overload-watch.
DWELL_OVERLOAD_CHECK_SECONDS = 5.0

# Skip-confirmation soak length. Never skip the soak outright: descent only
# proves a point for one second. ~9 checks, room for MAX_DWELL_BACKOFFS.
# See docs/features/auto-calibrate.md#skip-confirmation-and-the-soak.
SOAK_SECONDS = 45.0

# Mid-dwell retreats allowed before the tower is abandoned (unstable_overload).
MAX_DWELL_BACKOFFS = 2

# MODE_TRACK dwell/soak poll interval. Faster than blah2's ~1s CPI so no
# frame is missed; frames are de-duplicated by timestamp.
TRACKER_FEED_POLL_SECONDS = 0.2

# Must stay below the 1200s after which retina-gui's CALIBRATE_LOCK_TIMEOUT
# (device_state.py) and blah2-arm's watchdog (blah2_rspduo_restart.bash,
# CALIBRATE_LOCK_TIMEOUT_SECONDS) stop treating calibrate.lock as live and the
# watchdog restarts the stack under the run. Change all three together.
# See docs/features/auto-calibrate.md#lock-timeouts.
TOTAL_BUDGET_SECONDS = 900

# Per-tower descent backstop against a wedged device, above the ~4.5 min
# healthy worst case. See docs/features/auto-calibrate.md#time-budget.
DESCENT_BACKSTOP_SECONDS = 300

# Share of a tower's time slice descent may use, so the rest is always left
# for the dwell. See docs/features/auto-calibrate.md#time-budget.
MAX_DESCENT_FRACTION = 0.7

# Success modes.
MODE_TRACK = "track"
MODE_ADSB = "adsb"
VALID_MODES = (MODE_TRACK, MODE_ADSB)

# Track-evidence levels, worst to best, for ranking best attempts.
# See docs/features/auto-calibrate.md#evidence-levels.
EVIDENCE_NONE = 0
EVIDENCE_DETECTIONS = 1
EVIDENCE_ACTIVE = 2

EVIDENCE_LABELS = {
    EVIDENCE_NONE: "no detections seen",
    EVIDENCE_DETECTIONS: "detections seen, no confirmed track",
    EVIDENCE_ACTIVE: "confirmed track",
}


class CalibrationError(Exception):
    """A failure that aborts the whole run (blah2 unreachable/unresponsive)."""


class _Cancelled(Exception):
    """Internal: the user cancelled the run."""


def _utcnow():
    return datetime.now(timezone.utc).isoformat()


class Calibrator:
    """Runs the calibration search in a background thread.

    Status is an in-memory dict guarded by a lock (same shape as
    NetworkManager's WiFi-connect flow); the run lock-file lives in
    DeviceState and is managed by the caller (routes/calibrate.py).
    """

    def __init__(self, blah2_client, retina_tracker_client,
                 config_mgr=None, apply_service=None):
        self._client = blah2_client
        self._tracker_client = retina_tracker_client
        # Only used by _preflight's recovery branch. None (tests, dev mode)
        # means the probe still runs but there is no restart to attempt.
        # Injected so this module stays free of Flask/subprocess/Docker.
        self._config_mgr = config_mgr
        self._apply_service = apply_service
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread = None
        self._status = self._idle_status()
        # Latest confirmed-track event (see _on_track_event).
        self._last_confirmed_event = None
        # Frequency blah2 is on, so _apply can detect an fc change and hand
        # over via the safe corner first. Seeded from the device in _run.
        self._last_applied_fc = None
        # Registered in start(), not here: registering at app boot would
        # start retina_tracker_client's tail thread that early too (see
        # app.py's peers.start() pytest-leak note).
        self._listener_registered = False
        # Called with the final status dict when a run reaches a terminal
        # state. Exceptions are swallowed.
        self.on_complete = None

    @staticmethod
    def _idle_status():
        return {
            "state": "idle",
            "mode": MODE_TRACK,
            "phase": None,
            "started_at": None,
            "finished_at": None,
            "current": None,
            "progress": {"towers_tried": 0, "towers_total": 0, "retunes": 0,
                         "elapsed_seconds": 0, "budget_seconds": TOTAL_BUDGET_SECONDS},
            "rf": {"overload_a": None, "overload_b": None},
            "best_attempt": None,
            "result": None,
            # The no-track run's operating point, persistable like "result".
            # Always None for a cancelled run. See
            # docs/features/auto-calibrate.md#end-of-run-restore-or-fallback.
            "fallback": None,
            # Lets consumers tell "never looked for a track" from "looked and
            # found nothing".
            "skip_confirmation": False,
            "error": None,
            "original": None,
            # Only set when the preflight had to recover the device.
            "preflight": None,
            "history": [],
        }

    # ── Public API ─────────────────────────────────────────────

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def get_status(self):
        with self._lock:
            status = copy.deepcopy(self._status)
        if status["state"] == "running":
            started = status.get("_started_monotonic")
            if started is not None:
                status["progress"]["elapsed_seconds"] = int(time.monotonic() - started)
        status.pop("_started_monotonic", None)
        return status

    def start(self, towers, original, budget_seconds=TOTAL_BUDGET_SECONDS,
              dwell_seconds=None, mode=MODE_TRACK, skip_confirmation=False):
        """Start a run. Returns (started, error).

        towers: list of {"name": str, "fc": int Hz, "tx": dict | None}, tried
        in order (normally the configured tower first). "tx" is carried
        through to result/fallback untouched; the configured tower has none,
        so only a run that moves tower can rewrite location.tx.
        original: {"fc", "gain_a", "gain_b", "lna_state"}, restored on a
        non-success outcome.
        dwell_seconds: fixed per-tower dwell window, for tests. None divides
        the run budget per tower (see docs/features/auto-calibrate.md#time-budget).
        skip_confirmation: resolve and soak each operating point, never wait
        for a track. Still watches for overload, and is deliberately not
        dwell_seconds=0 (that reports skipped_no_time). See
        docs/features/auto-calibrate.md#skip-confirmation-and-the-soak.
        mode: MODE_TRACK or MODE_ADSB. The caller must check
        truth.adsb.enabled before using MODE_ADSB; this class has no config.
        """
        if self.is_running():
            return False, "Calibration already running"
        if not towers:
            return False, "No candidate towers"
        if mode not in VALID_MODES:
            return False, f"Invalid mode: {mode}"

        if not self._listener_registered:
            self._tracker_client.add_listener(self._on_track_event)
            self._listener_registered = True

        with self._lock:
            self._status = self._idle_status()
            self._status.update({
                "state": "running",
                "mode": mode,
                "skip_confirmation": bool(skip_confirmation),
                "started_at": _utcnow(),
                "original": dict(original),
                "_started_monotonic": time.monotonic(),
            })
            self._status["progress"]["towers_total"] = len(towers)
            # MODE_ADSB has no time division, so report no budget.
            self._status["progress"]["budget_seconds"] = (
                None if mode == MODE_ADSB else budget_seconds)
        # Seeded from the device (not config) at the top of _run.
        self._last_applied_fc = None
        self._cancel.clear()
        self._thread = threading.Thread(
            target=self._run, args=(list(towers), dict(original),
                                    budget_seconds, dwell_seconds, mode,
                                    bool(skip_confirmation)),
            daemon=True)
        self._thread.start()
        return True, None

    def cancel(self):
        self._cancel.set()

    # ── Status helpers ─────────────────────────────────────────

    def _update(self, **kwargs):
        with self._lock:
            self._status.update(kwargs)

    def _update_progress(self, **kwargs):
        with self._lock:
            self._status["progress"].update(kwargs)

    def _update_rf(self, overload_a, overload_b):
        with self._lock:
            self._status["rf"] = {"overload_a": overload_a, "overload_b": overload_b}

    def _append_history(self, entry):
        with self._lock:
            self._status["history"].append(entry)

    def _check_cancel(self, ignore_cancel=False):
        if not ignore_cancel and self._cancel.is_set():
            raise _Cancelled()

    def _sleep(self, seconds, ignore_cancel=False):
        """Sleep in small increments so cancel stays responsive."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._check_cancel(ignore_cancel=ignore_cancel)
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    # ── retina-tracker sidecar events ───────────────────────────

    def _on_track_event(self, event):
        """Listener for the shared RetinaTrackerClient; runs on its tail thread.
        The sidecar only emits events for ACTIVE tracks, so any event means
        confirmed. See docs/features/auto-calibrate.md#track-confirmation."""
        with self._lock:
            self._last_confirmed_event = event

    def _take_confirmed_event(self, min_timestamp):
        """The latest confirmed event if no older than min_timestamp (normally
        the candidate's applied_at), so an event from a previous candidate
        still in flight is never credited to this one."""
        with self._lock:
            event = self._last_confirmed_event
        if event is not None and event.get("timestamp", 0) >= min_timestamp:
            return event
        return None

    # ── Retune protocol ────────────────────────────────────────

    def _apply(self, fc, gain_a, gain_b, lna_state, ignore_cancel=False):
        """Request a retune and wait for blah2's ack. Returns appliedAt (ms).

        A frequency change is always preceded by a retune to the safe corner
        at the current frequency: blah2 applies fc before gain, so one call
        moving both can saturate the device at the new fc before the new
        attenuation lands. See
        docs/features/auto-calibrate.md#safe-corner-handover-on-a-frequency-change.

        ignore_cancel: only for the restore/fallback paths, which must finish
        even if the user cancels again mid-flight.
        """
        if self._last_applied_fc is not None and int(fc) != self._last_applied_fc:
            self._safe_fc_handover(ignore_cancel=ignore_cancel)
        applied_at = self._apply_tuning(fc, gain_a, gain_b, lna_state,
                                        ignore_cancel=ignore_cancel)
        self._last_applied_fc = int(fc)
        return applied_at

    def _seed_last_applied_fc(self, original_fc):
        """Record the frequency blah2 is actually on, so _apply can tell a
        real frequency change from a no-op.

        Must come from the device, never from config: the two diverge after
        an unpersisted run, and seeding from config silently disables the
        safe-corner handover. An empty retune status means blah2 is still on
        config.yml's fc (original_fc). See
        docs/features/auto-calibrate.md#seeding-the-current-frequency.
        """
        status = self._client.get_retune_status()
        if status and status.get("fc") is not None:
            self._last_applied_fc = int(status["fc"])
        else:
            self._last_applied_fc = int(original_fc)

    # ── Preflight ──────────────────────────────────────────────

    def _preflight(self, original):
        """Park the device at the safe corner and prove it still responds,
        recovering it by restart if it doesn't. See
        docs/features/auto-calibrate.md#preflight-and-recovery.

        Probes at the seeded fc (what blah2 is actually on), never
        original["fc"]: moving fc from an unknown gain is the hazard the
        handover exists for. Must run after _seed_last_applied_fc.

        Returns None if the device is usable; raises CalibrationError naming
        the fault if not.
        """
        self._update(phase="preflight")
        fc = self._last_applied_fc
        try:
            self._apply_tuning(fc, GAIN_REDUCTION_MAX, GAIN_REDUCTION_MAX,
                               LNA_STATE_MAX)
            return
        except CalibrationError as e:
            # Rebound: `as e` is unbound at the end of the except block, and
            # this message is still needed several branches below.
            probe_error = e

        if self._apply_service is None or self._config_mgr is None:
            raise CalibrationError(
                f"The radio is not accepting tuning commands ({probe_error}). "
                "Restart the radar services and try again.")

        self._update(phase="recovering")
        # Recorded before the write, so a run that dies below still reports
        # the intervention and _run still knows not to restore.
        self._set_preflight(recovered=False, restarted=True, previous=dict(original))
        try:
            self._persist_safe_corner()
        except Exception as e:
            # Without it the restart brings blah2 back on the wedging tuning.
            raise CalibrationError(
                "The radio stopped accepting tuning commands, and its safe "
                f"settings could not be saved: {e}") from e

        error = self._run_recovery_apply()
        if error:
            raise CalibrationError(
                f"The radio stopped accepting tuning commands, and restarting "
                f"it failed: {error}")

        # blah2 has to come up and claim the device before it can ack
        # anything, so this retries rather than asking once.
        deadline = time.monotonic() + PREFLIGHT_RECOVERY_PROBE_SECONDS
        last_error = probe_error
        while True:
            try:
                self._apply_tuning(fc, GAIN_REDUCTION_MAX, GAIN_REDUCTION_MAX,
                                   LNA_STATE_MAX)
                self._set_preflight(recovered=True, restarted=True,
                                    previous=dict(original))
                return
            except CalibrationError as e:
                last_error = e
            if time.monotonic() >= deadline:
                raise CalibrationError(
                    f"The radio is still not accepting tuning commands after a "
                    f"restart ({last_error}). It has been set to maximum "
                    f"attenuation; the SDRplay service may need attention on "
                    f"the node itself.")
            self._sleep(PREFLIGHT_APPLY_POLL_SECONDS)

    def _persist_safe_corner(self):
        """Write the safe corner to user.yml so the recovery restart brings
        blah2 up on it. The engine's only user.yml write, and deliberately
        never reverted. See
        docs/features/auto-calibrate.md#the-one-useryml-write-and-why-it-is-not-reverted."""
        user_config = self._config_mgr.load_user_config() or {}
        device = user_config.setdefault("capture", {}).setdefault("device", {})
        device["gainReduction"] = [GAIN_REDUCTION_MAX, GAIN_REDUCTION_MAX]
        device["lnaState"] = LNA_STATE_MAX
        self._config_mgr.save_user_config(user_config)

    def _run_recovery_apply(self):
        """Run the ordinary config-apply path and poll it to completion.

        bypass_guard is required: the guard refuses applies during a
        calibration, and this is the one caller that owns the run.

        Returns None on success, or a message on failure.
        """
        try:
            self._apply_service.request(bypass_guard=True)
        except Exception as e:
            return str(e)

        deadline = time.monotonic() + PREFLIGHT_RECOVERY_APPLY_TIMEOUT_SECONDS
        while True:
            status = self._apply_service.get_status()
            state = status.get("state")
            if state == "done":
                return None
            if state == "failed":
                return status.get("error") or "the restart failed"
            if time.monotonic() >= deadline:
                return "the restart did not finish in time"
            # ignore_cancel: never leave containers half-recreated. The run
            # aborts at the next _check_cancel, once the stack is settled.
            self._sleep(PREFLIGHT_APPLY_POLL_SECONDS, ignore_cancel=True)

    def _set_preflight(self, **kwargs):
        with self._lock:
            self._status["preflight"] = dict(kwargs)

    def _safe_fc_handover(self, ignore_cancel=False):
        """Retune to the safe corner at the current frequency, just before a
        frequency change (see _apply).

        Best-effort, never raises: the caller's own retune surfaces any
        problem, and _probe treats that as an overload at the candidate.
        """
        try:
            self._apply_tuning(self._last_applied_fc, GAIN_REDUCTION_MAX,
                               GAIN_REDUCTION_MAX, LNA_STATE_MAX,
                               ignore_cancel=ignore_cancel)
        except CalibrationError:
            pass

    def _apply_tuning(self, fc, gain_a, gain_b, lna_state, ignore_cancel=False):
        """One retune request plus ack wait. Callers should normally use
        _apply, which adds the safe-corner handover on a frequency change."""
        last_error = None
        for _attempt in range(2):
            self._check_cancel(ignore_cancel=ignore_cancel)
            generation, error = self._client.retune(fc, gain_a, gain_b, lna_state)
            if generation is None:
                last_error = error
                self._sleep(APPLY_RETRY_DELAY_SECONDS, ignore_cancel=ignore_cancel)
                continue
            deadline = time.monotonic() + ACK_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                self._check_cancel(ignore_cancel=ignore_cancel)
                status = self._client.get_retune_status()
                if status and status.get("generation") == generation:
                    self._update_progress(
                        retunes=self._status["progress"]["retunes"] + 1)
                    return status.get("appliedAt", 0)
                time.sleep(ACK_POLL_SECONDS)
            last_error = "blah2 did not acknowledge the retune"
        # Deliberately names both causes: radar down and a hard overload look
        # identical from here. See
        # docs/features/auto-calibrate.md#unacknowledged-retunes.
        raise CalibrationError(
            f"Retune failed: {last_error}. Either the radar is not running, "
            "or this tower is strong enough to overload the receiver even at "
            "minimum gain")

    def _read_overload(self, applied_at_ms):
        """Overload flags from an overload-status report newer than
        applied_at_ms."""
        deadline = time.monotonic() + RF_STATUS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            self._check_cancel()
            rf = self._client.get_overload_status()
            if rf and rf.get("timestamp", 0) >= applied_at_ms:
                self._update_rf(rf.get("overloadA"), rf.get("overloadB"))
                return bool(rf.get("overloadA")), bool(rf.get("overloadB"))
            time.sleep(RF_STATUS_POLL_SECONDS)
        raise CalibrationError(
            "blah2 is not reporting overload status. It may be running an older "
            "version without live-tune support")

    def _probe(self, fc, gain_a, gain_b, lna_state, fallback_applied_at):
        """Apply one gain/LNA candidate, settle, and read whether it overloaded.

        A CalibrationError (the device wedged) is folded into
        overload_a = overload_b = True, deliberately both, so callers revert
        exactly as for an overload. See docs/features/auto-calibrate.md#device-wedges.

        fallback_applied_at: returned as applied_at when the retune itself
        never completed (the previous candidate's, or 0 for the first).

        Returns (applied_at_ms, overload_a, overload_b, device_error_detail),
        the last being None on a normal probe or the error message.
        """
        try:
            applied_at = self._apply(fc, gain_a, gain_b, lna_state)
        except CalibrationError as e:
            return fallback_applied_at, True, True, str(e)
        self._sleep(OVERLOAD_SETTLE_SECONDS)
        try:
            overload_a, overload_b = self._read_overload(applied_at)
        except CalibrationError as e:
            return applied_at, True, True, str(e)
        return applied_at, overload_a, overload_b, None

    def _verify_applied(self, fc, gain_a, gain_b, lna_state):
        """Confirm blah2's last acked retune matches this tuning before
        dwelling on it, so a dwell never measures a different frequency.
        See docs/features/auto-calibrate.md#verifying-before-dwelling.

        Returns (applied_at_ms, None) if it is, or (None, reason) if not.
        """
        status = self._client.get_retune_status()
        if not status:
            return None, "blah2 has not acknowledged any tuning"

        actual = (status.get("fc"), status.get("gainReductionA"),
                  status.get("gainReductionB"), status.get("lnaState"))
        if actual != (fc, gain_a, gain_b, lna_state):
            # Newer blah2_api reports a generation it gave up on.
            rejected = status.get("rejected") or {}
            if rejected:
                return None, (
                    f"blah2 abandoned the retune after {rejected.get('attempts')} "
                    f"attempts; it is still tuned to fc={actual[0]} "
                    f"gain=({actual[1]},{actual[2]}) lna={actual[3]}")
            return None, (
                f"blah2 is tuned to fc={actual[0]} gain=({actual[1]},{actual[2]}) "
                f"lna={actual[3]}, not the resolved fc={fc} "
                f"gain=({gain_a},{gain_b}) lna={lna_state}")

        return status.get("appliedAt", 0), None

    def _safe_revert(self, fc, gain_a, gain_b, lna_state, fallback_applied_at):
        """Best-effort re-apply of a proven-safe candidate after a later one
        overloaded or failed. Never raises: a wedged device just fails the
        dwell like any other no-track outcome.

        Returns (applied_at_ms, device_error_detail); on failure the detail
        is the message and fallback_applied_at is returned unchanged.
        """
        try:
            return self._apply(fc, gain_a, gain_b, lna_state), None
        except CalibrationError as e:
            return fallback_applied_at, str(e)

    # ── Search stages ──────────────────────────────────────────

    def _descend_reference(self, fc, gain_b, lna_state, descent_log, deadline):
        """Find the highest clean gain for the reference tuner (A) at a fixed
        lna_state. Only overload_a is inspected; no refine step.

        Starts at GAIN_REDUCTION_MAX and must never start lower. See
        docs/features/auto-calibrate.md#safe-end-first.

        Returns (gain_a, applied_at_ms, still_overloaded).
        """
        # Reset the phase: surveillance leaves it on "refining", and the LNA
        # loop re-enters both stages many times per tower.
        self._update(phase="descending")
        gain_a = GAIN_REDUCTION_MAX
        clean_gain_a = None
        applied_at, overload_a, _, device_error = self._probe(
            fc, gain_a, gain_b, lna_state, 0)
        while True:
            entry = {"phase": "reference", "gain_a": gain_a,
                    "lna_state": lna_state, "overload_a": overload_a}
            if device_error:
                entry["device_error"] = True
                entry["device_error_detail"] = device_error
            descent_log.append(entry)
            if overload_a:
                if clean_gain_a is None:
                    # Overloaded (or failed) at the safety ceiling: gain
                    # reduction alone can't clear this. Caller decides.
                    return gain_a, applied_at, True
                # Never leave the hardware sitting at the overloaded
                # candidate: revert to the last proven-clean value.
                applied_at, revert_error = self._safe_revert(
                    fc, clean_gain_a, gain_b, lna_state, applied_at)
                revert_entry = {"phase": "reference_revert", "gain_a": clean_gain_a,
                                "lna_state": lna_state, "reverted_from": gain_a}
                if revert_error:
                    revert_entry["device_error"] = True
                    revert_entry["device_error_detail"] = revert_error
                descent_log.append(revert_entry)
                self._set_current(gain_a=clean_gain_a)
                return clean_gain_a, applied_at, False
            clean_gain_a = gain_a
            if gain_a <= GAIN_REDUCTION_MIN or time.monotonic() >= deadline:
                return gain_a, applied_at, False
            gain_a = max(gain_a - DESCENT_STEP_DB, GAIN_REDUCTION_MIN)
            self._set_current(gain_a=gain_a)
            applied_at, overload_a, _, device_error = self._probe(
                fc, gain_a, gain_b, lna_state, applied_at)

    def _descend_surveillance(self, fc, gain_a, lna_state, descent_log, deadline):
        """Find the highest clean gain for the surveillance tuner (B) at a
        fixed lna_state and gain_a. Same safe-ceiling-first walk as
        reference, plus one REFINE_STEP_DB refine after a revert.

        Returns (gain_b, applied_at_ms, still_overloaded).
        """
        self._update(phase="descending")
        gain_b = GAIN_REDUCTION_MAX
        clean_gain_b = None
        reverted = False
        applied_at, _, overload_b, device_error = self._probe(
            fc, gain_a, gain_b, lna_state, 0)
        while True:
            entry = {"phase": "surveillance", "gain_b": gain_b,
                    "lna_state": lna_state, "overload_b": overload_b}
            if device_error:
                entry["device_error"] = True
                entry["device_error_detail"] = device_error
            descent_log.append(entry)
            if overload_b:
                if clean_gain_b is None:
                    return gain_b, applied_at, True
                applied_at, revert_error = self._safe_revert(
                    fc, gain_a, clean_gain_b, lna_state, applied_at)
                revert_entry = {"phase": "surveillance_revert", "gain_b": clean_gain_b,
                                "lna_state": lna_state, "reverted_from": gain_b}
                if revert_error:
                    revert_entry["device_error"] = True
                    revert_entry["device_error_detail"] = revert_error
                descent_log.append(revert_entry)
                gain_b = clean_gain_b
                reverted = True
                self._set_current(gain_b=gain_b)
                break
            clean_gain_b = gain_b
            if gain_b <= GAIN_REDUCTION_MIN or time.monotonic() >= deadline:
                break
            gain_b = max(gain_b - DESCENT_STEP_DB, GAIN_REDUCTION_MIN)
            self._set_current(gain_b=gain_b)
            applied_at, _, overload_b, device_error = self._probe(
                fc, gain_a, gain_b, lna_state, applied_at)

        if reverted and time.monotonic() < deadline:
            refine_b = max(gain_b - REFINE_STEP_DB, GAIN_REDUCTION_MIN)
            self._update(phase="refining")
            self._set_current(gain_b=refine_b)
            applied_at, _, overload_b, device_error = self._probe(
                fc, gain_a, refine_b, lna_state, applied_at)
            entry = {"phase": "surveillance_refine", "gain_b": refine_b,
                    "lna_state": lna_state, "overload_b": overload_b}
            if device_error:
                entry["device_error"] = True
                entry["device_error_detail"] = device_error
            descent_log.append(entry)
            if overload_b:
                self._set_current(gain_b=gain_b)
                applied_at, _ = self._safe_revert(fc, gain_a, gain_b, lna_state, applied_at)
            else:
                gain_b = refine_b

        return gain_b, applied_at, False

    def _descend(self, fc, descent_log, deadline):
        """Resolve (gain_a, gain_b, lna_state) for one tower: reference gain,
        then surveillance gain, inside an outer LNA loop starting at
        LNA_STATE_MAX.

        Invariants (see docs/features/auto-calibrate.md#lna-state-vs-gain-reduction):
        each more-sensitive LNA step redescends BOTH tuners from the 59dB
        ceiling, and an overload there reverts the whole triple together.
        Still overloaded at lna_state=9 is terminal for the tower.

        deadline: this tower's descent deadline (monotonic clock), checked
        between probes.

        Returns (gain_a, gain_b, lna_state, applied_at_ms).
        """
        lna_state = LNA_STATE_MAX
        gain_a, applied_at, overload_a = self._descend_reference(
            fc, GAIN_REDUCTION_MAX, lna_state, descent_log, deadline)

        gain_b, overload_b = GAIN_REDUCTION_MAX, False
        if time.monotonic() < deadline:
            gain_b, applied_at, overload_b = self._descend_surveillance(
                fc, gain_a, lna_state, descent_log, deadline)

        while (not overload_a and not overload_b and lna_state > LNA_STATE_MIN
               and time.monotonic() < deadline):
            safe_gain_a, safe_gain_b, safe_lna_state = gain_a, gain_b, lna_state
            lna_state -= 1
            self._set_current(lna_state=lna_state)
            descent_log.append({"phase": "lna_descent", "lna_state": lna_state})

            new_gain_a, applied_at, overload_a = self._descend_reference(
                fc, GAIN_REDUCTION_MAX, lna_state, descent_log, deadline)
            new_gain_b, overload_b = GAIN_REDUCTION_MAX, False
            if time.monotonic() < deadline:
                new_gain_b, applied_at, overload_b = self._descend_surveillance(
                    fc, new_gain_a, lna_state, descent_log, deadline)

            if overload_a or overload_b:
                applied_at, revert_error = self._safe_revert(
                    fc, safe_gain_a, safe_gain_b, safe_lna_state, applied_at)
                revert_entry = {
                    "phase": "lna_descent_revert",
                    "gain_a": safe_gain_a, "gain_b": safe_gain_b,
                    "lna_state": safe_lna_state,
                    "reverted_from_lna_state": lna_state,
                }
                if revert_error:
                    revert_entry["device_error"] = True
                    revert_entry["device_error_detail"] = revert_error
                descent_log.append(revert_entry)
                gain_a, gain_b, lna_state = safe_gain_a, safe_gain_b, safe_lna_state
                self._set_current(gain_a=gain_a, gain_b=gain_b, lna_state=lna_state)
                break

            gain_a, gain_b = new_gain_a, new_gain_b

        return gain_a, gain_b, lna_state, applied_at

    def _dwell(self, tower, fc, gain_a, gain_b, lna_state, applied_at, dwell_deadline,
               tower_entry, watch_only=False):
        """MODE_TRACK's dwell: watch for a confirmed track event from the
        retina-tracker sidecar (fed by blah2_api, not by this class) until
        dwell_deadline, re-checking overload and backing off as needed.
        With watch_only (the skip-confirmation soak) only the overload watch
        runs. See docs/features/auto-calibrate.md#dwell-and-overload-watch.

        Returns a result dict on success, else None.
        """
        # A soak has its own phase so the UI never says "Watching for
        # aircraft" during it.
        self._update(phase="soaking" if watch_only else "dwelling")
        # Start from an empty tracker (see _reset_tracker). A soak never
        # reads the tracker, so it skips the reset.
        if not watch_only:
            self._reset_tracker()
        max_evidence = EVIDENCE_NONE
        max_detections = 0
        last_timestamp = None
        backoffs = 0
        next_overload_check = time.monotonic() + DWELL_OVERLOAD_CHECK_SECONDS
        overload_baseline = self._overload_reading()

        while time.monotonic() < dwell_deadline:
            self._check_cancel()

            # Keep watching for intermittent clipping; see
            # DWELL_OVERLOAD_CHECK_SECONDS.
            if time.monotonic() >= next_overload_check:
                next_overload_check = time.monotonic() + DWELL_OVERLOAD_CHECK_SECONDS
                reading = self._overload_reading()
                clipped_a, clipped_b = self._overload_since(overload_baseline, reading)
                if clipped_a or clipped_b:
                    overload_baseline = reading
                    self._update_rf(clipped_a, clipped_b)
                    if backoffs >= MAX_DWELL_BACKOFFS:
                        tower_entry["outcome"] = "unstable_overload"
                        tower_entry["max_evidence"] = max_evidence
                        tower_entry["max_detections"] = max_detections
                        # Record where the backoffs left the device, or the
                        # no-track fallback persists the tuning just proved
                        # unstable.
                        tower_entry["final_gain_a"] = gain_a
                        tower_entry["final_gain_b"] = gain_b
                        tower_entry["final_lna_state"] = lna_state
                        return None
                    backoffs += 1
                    gain_a, gain_b, lna_state, applied_at = self._dwell_backoff(
                        fc, gain_a, gain_b, lna_state, applied_at,
                        clipped_a, clipped_b, backoffs, tower_entry)
                    # Nothing measured at the abandoned tuning may be
                    # credited to the new one.
                    self._reset_tracker()
                    last_timestamp = None
                    overload_baseline = self._overload_reading()

            if watch_only:
                # The soak's only job is the overload check above.
                self._sleep(TRACKER_FEED_POLL_SECONDS)
                continue

            detection = self._client.get_detection()
            timestamp = detection.get("timestamp") if detection else None
            if (detection and timestamp != last_timestamp
                    and timestamp is not None and timestamp >= applied_at):
                last_timestamp = timestamp
                delays = detection.get("delay") or []
                if delays:
                    max_evidence = max(max_evidence, EVIDENCE_DETECTIONS)
                    max_detections = max(max_detections, len(delays))

            confirmed = self._take_confirmed_event(applied_at)
            if confirmed is not None:
                tower_entry["outcome"] = "confirmed_track"
                tower_entry["max_evidence"] = EVIDENCE_ACTIVE
                # The tuning actually in effect after any backoff; must agree
                # with the result offered for persisting.
                tower_entry["final_gain_a"] = gain_a
                tower_entry["final_gain_b"] = gain_b
                tower_entry["final_lna_state"] = lna_state
                return {
                    "tower_name": tower.get("name"), "fc": fc,
                    "tx": tower.get("tx"),
                    "gain_a": gain_a, "gain_b": gain_b,
                    "lna_state": lna_state,
                    "track_id": confirmed.get("track_id"),
                }

            self._maybe_update_best_attempt(tower, fc, gain_a, gain_b, lna_state,
                                            max_evidence, max_detections)
            self._sleep(TRACKER_FEED_POLL_SECONDS)

        # A soak that reached its deadline held; it never searched for a
        # track, so it is "tuned", not "no_confirmed_track".
        tower_entry["outcome"] = "tuned" if watch_only else "no_confirmed_track"
        tower_entry["max_evidence"] = max_evidence
        tower_entry["max_detections"] = max_detections
        # After any backoff; the no-track fallback reads these.
        tower_entry["final_gain_a"] = gain_a
        tower_entry["final_gain_b"] = gain_b
        tower_entry["final_lna_state"] = lna_state
        return None

    def _reset_tracker(self):
        """Clear the sidecar's tracker and any confirmed event already held.

        Must run at the start of every dwell and after every mid-dwell
        backoff, not per tower: blah2_api feeds the sidecar continuously, so
        a per-tower reset lets a track confirm during the descent. See
        docs/features/auto-calibrate.md#tracker-reset-per-dwell.
        """
        # Recorded rather than raised: the dwell is still worth running.
        if not self._tracker_client.reset():
            self._update(tracker_reset_failed=True)
        with self._lock:
            self._last_confirmed_event = None

    def _overload_reading(self):
        """Current overload level plus blah2's monotonic onset counts (the
        counts are None on an older blah2 that doesn't report them)."""
        rf = self._client.get_overload_status()
        if not rf:
            return None
        return {
            "level_a": bool(rf.get("overloadA")),
            "level_b": bool(rf.get("overloadB")),
            "count_a": rf.get("overloadCountA"),
            "count_b": rf.get("overloadCountB"),
        }

    @staticmethod
    def _overload_since(baseline, current):
        """Did either tuner clip at or since the baseline reading?

        Uses both the level (a steady overload) and the onset counts
        (clip-and-recover between polls); each is blind to the other's case.
        See docs/features/auto-calibrate.md#dwell-and-overload-watch.
        """
        if not current:
            return False, False
        clipped_a, clipped_b = current["level_a"], current["level_b"]
        if baseline and current["count_a"] is not None and baseline["count_a"] is not None:
            clipped_a = clipped_a or current["count_a"] > baseline["count_a"]
            clipped_b = clipped_b or current["count_b"] > baseline["count_b"]
        return clipped_a, clipped_b

    def _dwell_backoff(self, fc, gain_a, gain_b, lna_state, applied_at,
                       overload_a, overload_b, attempt, tower_entry):
        """Retreat one step toward safety after overload appeared mid-dwell:
        more attenuation on the clipping channel, or, if it is already at the
        ceiling, LNA state + 1 with both gains reset to the ceiling.
        Best-effort; reports the values asked for even if the device refused.

        Returns the (gain_a, gain_b, lna_state, applied_at) now in effect.
        """
        new_a, new_b, new_lna = gain_a, gain_b, lna_state
        if overload_a:
            new_a = min(gain_a + DESCENT_STEP_DB, GAIN_REDUCTION_MAX)
        if overload_b:
            new_b = min(gain_b + DESCENT_STEP_DB, GAIN_REDUCTION_MAX)
        # A channel at the gain ceiling is clipping upstream: back off LNA.
        if ((overload_a and new_a == gain_a) or (overload_b and new_b == gain_b)) \
                and lna_state < LNA_STATE_MAX:
            new_lna = lna_state + 1
            new_a = new_b = GAIN_REDUCTION_MAX

        entry = {"phase": "dwell_overload_backoff", "attempt": attempt,
                 "overload_a": overload_a, "overload_b": overload_b,
                 "from": {"gain_a": gain_a, "gain_b": gain_b, "lna_state": lna_state},
                 "to": {"gain_a": new_a, "gain_b": new_b, "lna_state": new_lna}}
        tower_entry.setdefault("dwell_backoffs", []).append(entry)

        new_applied_at, revert_error = self._safe_revert(
            fc, new_a, new_b, new_lna, applied_at)
        if revert_error:
            entry["device_error"] = True
            entry["device_error_detail"] = revert_error
        self._set_current(gain_a=new_a, gain_b=new_b, lna_state=new_lna)
        return new_a, new_b, new_lna, new_applied_at

    def _dwell_adsb(self, tower, fc, gain_a, initial_gain_b, lna_state, tower_entry):
        """MODE_ADSB's dwell, with no time budget: wait (unbounded) for an
        aircraft in range, succeed on a confirmed event carrying adsb_hex,
        and step gainReductionB more sensitive each time every aircraft
        leaves unmatched. gain_a stays fixed. See
        docs/features/auto-calibrate.md#success-modes.

        Returns a result dict on success, None once this tower's candidates
        are exhausted (sensitivity floor or re-overload).
        """
        self._update(phase="dwelling")
        gain_b = initial_gain_b
        applied_at = 0
        gains_tried = []
        max_evidence = EVIDENCE_NONE
        max_detections = 0

        while True:
            self._check_cancel()
            self._set_current(gain_a=gain_a, gain_b=gain_b)
            applied_at, overload_a, overload_b, device_error = self._probe(
                fc, gain_a, gain_b, lna_state, applied_at)
            entry = {"gain_b": gain_b, "overload_b": overload_b}
            if device_error:
                entry["device_error"] = True
                entry["device_error_detail"] = device_error
            gains_tried.append(entry)
            if overload_b:
                # Never leave the hardware on the overloaded candidate. The
                # first candidate has no prior entry (it is descent's clean
                # value), hence the guard.
                if len(gains_tried) > 1:
                    previous_gain_b = gains_tried[-2]["gain_b"]
                    applied_at, _ = self._safe_revert(
                        fc, gain_a, previous_gain_b, lna_state, applied_at)
                    self._set_current(gain_b=previous_gain_b)
                break

            # Each gain candidate is its own watch (see _reset_tracker).
            self._reset_tracker()
            aircraft_seen = False
            last_timestamp = None
            while True:
                self._check_cancel()
                reason_override = None

                adsb_tracks = self._client.get_adsb_tracks()

                detection = self._client.get_detection()
                timestamp = detection.get("timestamp") if detection else None
                if (detection and timestamp != last_timestamp
                        and timestamp is not None and timestamp >= applied_at):
                    last_timestamp = timestamp
                    delays = detection.get("delay") or []
                    if delays:
                        max_evidence = max(max_evidence, EVIDENCE_DETECTIONS)
                        max_detections = max(max_detections, len(delays))

                confirmed = self._take_confirmed_event(applied_at)
                if confirmed is not None:
                    max_evidence = max(max_evidence, EVIDENCE_ACTIVE)

                if adsb_tracks:
                    aircraft_seen = True
                    if confirmed is not None and confirmed.get("adsb_hex"):
                        tower_entry["outcome"] = "confirmed_track"
                        tower_entry["max_evidence"] = EVIDENCE_ACTIVE
                        tower_entry["gains_tried"] = gains_tried
                        return {
                            "tower_name": tower.get("name"), "fc": fc,
                            "tx": tower.get("tx"),
                            "gain_a": gain_a, "gain_b": gain_b,
                            "track_id": confirmed.get("track_id"),
                            "adsb_hex": confirmed.get("adsb_hex"),
                        }
                    if confirmed is not None:
                        reason_override = "confirmed track, but doesn't match a known aircraft"
                elif aircraft_seen:
                    # Every aircraft in range left unmatched: next candidate.
                    break

                self._maybe_update_best_attempt(tower, fc, gain_a, gain_b, lna_state,
                                                max_evidence, max_detections,
                                                reason=reason_override)
                self._sleep(DWELL_POLL_SECONDS)

            next_gain_b = gain_b - ADSB_GAIN_STEP_DB
            if next_gain_b < GAIN_REDUCTION_MIN:
                break
            gain_b = next_gain_b

        tower_entry["outcome"] = "no_confirmed_track"
        tower_entry["max_evidence"] = max_evidence
        tower_entry["max_detections"] = max_detections
        tower_entry["gains_tried"] = gains_tried
        return None

    def _maybe_update_best_attempt(self, tower, fc, gain_a, gain_b, lna_state,
                                   evidence, max_detections, reason=None):
        with self._lock:
            best = self._status.get("best_attempt")
            if best and (best["evidence"], best["max_detections"]) >= (evidence, max_detections):
                return
            self._status["best_attempt"] = {
                "tower_name": tower.get("name"),
                "fc": fc,
                "gain_a": gain_a,
                "gain_b": gain_b,
                "lna_state": lna_state,
                "evidence": evidence,
                "reason": reason or EVIDENCE_LABELS[evidence],
                "max_detections": max_detections,
            }

    def _set_current(self, **kwargs):
        with self._lock:
            current = dict(self._status.get("current") or {})
            current.update(kwargs)
            self._status["current"] = current

    def _apply_top_tower_fallback(self, top_tower, top_fc, gain_a, gain_b, lna_state):
        """No-track outcome: leave blah2 on the top-ranked tower at its own
        resolved (proven not to overload) tuning instead of the original,
        and record it as status["fallback"]. Best-effort. See
        docs/features/auto-calibrate.md#end-of-run-restore-or-fallback.
        """
        self._update(phase="restoring")
        # Recorded before the retune and kept even if it fails: it is what
        # /calibrate/apply persists, and persisting is what makes it stick.
        self._update(fallback={
            "tower_name": top_tower.get("name"),
            "fc": top_fc,
            # Alternates only (routes/calibrate._towers_to_alternates). fc and
            # tx must be persisted together.
            "tx": top_tower.get("tx"),
            "gain_a": gain_a,
            "gain_b": gain_b,
            "lna_state": lna_state,
        })
        try:
            self._apply(top_fc, gain_a, gain_b, lna_state, ignore_cancel=True)
            self._set_current(tower_index=0, tower_name=top_tower.get("name"),
                              fc=top_fc, gain_a=gain_a, gain_b=gain_b,
                              lna_state=lna_state)
        except Exception:
            pass

    # ── Run loop ───────────────────────────────────────────────

    def _run(self, towers, original, budget_seconds, dwell_seconds, mode,
             skip_confirmation=False):
        # Must happen before the first retune (see _seed_last_applied_fc).
        self._seed_last_applied_fc(original["fc"])
        result = None
        error = None
        state = "failed"
        no_track_fallback_applied = False
        # towers[0]'s resolved (final) tuning, for the no-track fallback.
        # None unless tower 0 is reached.
        top_tower_resolved = None

        try:
            # Deliberately before the budget clock starts, so a ~90s recovery
            # is not charged to the dwells. Raises if the radio can't be tuned.
            self._preflight(original)
            run_deadline = time.monotonic() + budget_seconds
            # Rebase elapsed onto the budget start; started_at keeps wall time.
            self._update(_started_monotonic=time.monotonic())

            for index, tower in enumerate(towers):
                self._check_cancel()
                # The run budget only bounds MODE_TRACK.
                if mode != MODE_ADSB and time.monotonic() >= run_deadline:
                    break
                fc = int(tower["fc"])
                self._update(phase="descending")
                self._set_current(tower_index=index, tower_name=tower.get("name"),
                                  fc=fc, gain_a=GAIN_REDUCTION_MAX,
                                  gain_b=GAIN_REDUCTION_MAX, lna_state=LNA_STATE_MAX)
                # No tracker reset here: it happens per dwell (see
                # _reset_tracker).
                # tower_entry stays thread-local until appended to history.
                tower_entry = {
                    "tower_name": tower.get("name"),
                    "fc": fc,
                    "descent": [],
                    "outcome": "not_reached",
                }

                tower_started = time.monotonic()
                if mode == MODE_ADSB:
                    # Descent is still bounded, just not by a budget share.
                    descent_deadline = tower_started + ADSB_DESCENT_DEADLINE_SECONDS
                    tower_share = None
                else:
                    # This tower's slice of what's left, recomputed per tower
                    # so unused time rolls forward. Descent is capped at a
                    # fraction of it so the dwell always gets the rest. See
                    # docs/features/auto-calibrate.md#time-budget.
                    towers_remaining = len(towers) - index
                    time_left = max(run_deadline - tower_started, 0)
                    tower_share = time_left / towers_remaining
                    descent_deadline = min(
                        tower_started + tower_share * MAX_DESCENT_FRACTION,
                        tower_started + DESCENT_BACKSTOP_SECONDS,
                        run_deadline)

                try:
                    gain_a, gain_b, lna_state, applied_at = self._descend(
                        fc, tower_entry["descent"], descent_deadline)
                    tower_entry["final_gain_a"] = gain_a
                    tower_entry["final_gain_b"] = gain_b
                    tower_entry["final_lna_state"] = lna_state
                    if index == 0:
                        top_tower_resolved = (gain_a, gain_b, lna_state)
                    self._set_current(gain_a=gain_a, gain_b=gain_b, lna_state=lna_state)

                    # Never dwell on tuning the device did not take.
                    verified_at, tuning_error = self._verify_applied(
                        fc, gain_a, gain_b, lna_state)
                    if tuning_error:
                        tower_entry["outcome"] = "tuning_not_applied"
                        tower_entry["tuning_error"] = tuning_error
                        tower_entry["device_error"] = True
                        result = None
                    elif mode == MODE_ADSB:
                        applied_at = verified_at
                        result = self._dwell_adsb(tower, fc, gain_a, gain_b, lna_state,
                                                  tower_entry)
                    else:
                        # The device's applied-at, not descent's (which can
                        # be a previous candidate's after a failed retune).
                        applied_at = verified_at
                        if skip_confirmation:
                            # Soak, never skip: see SOAK_SECONDS. _dwell
                            # records final_* after any backoff.
                            soak_started = time.monotonic()
                            self._dwell(tower, fc, gain_a, gain_b, lna_state,
                                        verified_at,
                                        min(soak_started + SOAK_SECONDS,
                                            run_deadline),
                                        tower_entry, watch_only=True)
                            # Not dwell_seconds: no aircraft were looked for.
                            tower_entry["soak_seconds"] = round(
                                time.monotonic() - soak_started, 1)
                            result = None
                            continue
                        # Dwell gets the rest of this tower's slice.
                        now = time.monotonic()
                        if dwell_seconds is not None:
                            dwell_deadline = min(now + dwell_seconds, run_deadline)
                        else:
                            dwell_deadline = min(tower_started + tower_share, run_deadline)

                        if dwell_deadline <= now:
                            # Budget exhausted: tuned but never watched.
                            tower_entry["outcome"] = "skipped_no_time"
                            result = None
                        else:
                            dwell_started = now
                            result = self._dwell(tower, fc, gain_a, gain_b, lna_state,
                                                 applied_at, dwell_deadline, tower_entry)
                            tower_entry["dwell_seconds"] = round(
                                time.monotonic() - dwell_started, 1)
                finally:
                    if (any(e.get("device_error") for e in tower_entry.get("descent", ())) or
                            any(e.get("device_error") for e in tower_entry.get("gains_tried", ()))):
                        tower_entry["device_error"] = True
                    # Re-read final_* after any mid-dwell backoff, so the
                    # fallback never restores a tuning abandoned for overload.
                    if index == 0 and tower_entry.get("final_lna_state") is not None:
                        top_tower_resolved = (tower_entry["final_gain_a"],
                                              tower_entry["final_gain_b"],
                                              tower_entry["final_lna_state"])
                    self._append_history(tower_entry)
                    self._update_progress(towers_tried=index + 1)

                if result is not None:
                    # MODE_TRACK's result carries its own (possibly backed
                    # off) lna_state; only MODE_ADSB's needs filling in.
                    result.setdefault("lna_state", lna_state)
                    state = "done"
                    break

            if result is None and error is None:
                if skip_confirmation:
                    # State stays "failed" (no confirmed result), but the
                    # message must not claim no aircraft was overhead.
                    error = ("Tuning resolved. This run found the best "
                             "settings for this tower and checked they held "
                             "without overloading the receiver, but was not "
                             "asked to wait for a confirmed track.")
                elif mode == MODE_ADSB:
                    error = ("No ADS-B-verified track: every candidate tower "
                             "and gain setting was tried, but no confirmed "
                             "track ever matched a real aircraft while one "
                             "was actually in range.")
                else:
                    error = ("No confirmed track found within the time budget. "
                             "This may simply mean no aircraft was overhead "
                             "during this run, not that the tuning is wrong.")

                # A late cancel must still restore original, not fall back.
                self._check_cancel()

                if towers and top_tower_resolved is not None:
                    top_tower = towers[0]
                    top_fc = int(top_tower["fc"])
                    gain_a, gain_b, lna_state = top_tower_resolved
                    self._apply_top_tower_fallback(top_tower, top_fc, gain_a, gain_b, lna_state)
                    no_track_fallback_applied = True
                # else: tower 0 never resolved; restore original below.

        except _Cancelled:
            state = "cancelled"
            error = "Cancelled by user"
        except CalibrationError as e:
            state = "failed"
            error = str(e)
        except Exception as e:
            state = "failed"
            error = f"Unexpected error: {e}"

        # Restore original on a non-success outcome, except after the
        # no-track fallback, and NEVER after a preflight restart (keyed on
        # `restarted`, not `recovered`): those settings wedged the radio.
        # ignore_cancel is required so a second cancel cannot abort it. See
        # docs/features/auto-calibrate.md#end-of-run-restore-or-fallback.
        with self._lock:
            restarted = bool((self._status.get("preflight") or {}).get("restarted"))
        if state != "done" and not no_track_fallback_applied and not restarted:
            self._update(phase="restoring")
            try:
                self._apply(original["fc"], original["gain_a"], original["gain_b"],
                           original["lna_state"], ignore_cancel=True)
                self._set_current(tower_index=None, tower_name=None,
                                  fc=original["fc"], gain_a=original["gain_a"],
                                  gain_b=original["gain_b"], lna_state=original["lna_state"])
            except Exception:
                pass  # blah2 unreachable; restart:always re-reads config.yml

        self._update(state=state, phase=None, result=result, error=error,
                     finished_at=_utcnow())

        if self.on_complete is not None:
            try:
                self.on_complete(self.get_status())
            except Exception:
                pass
