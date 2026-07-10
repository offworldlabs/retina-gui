"""Auto-Calibrate: tune tower/fc and per-tuner gain until a track confirms.

Strategy ("good, not best"): for each candidate tower, start at maximum gain
(minimum gain reduction), back off the overloaded tuner in big steps until the
RF front end runs clean, then dwell at that setting waiting for blah2's
tracker to confirm a track. A confirmed track needs a real aircraft overhead,
so dwell time dominates the run — the search minimises the number of dwells,
not the granularity of the gain grid.

Two success modes:
  - MODE_TRACK (default): any track reaching ASSOCIATED/ACTIVE state
    (nActive > 0) counts as success. Simple, but a persistent clutter/
    multipath return can also pass blah2's M-of-N confirmation.
  - MODE_ADSB: additionally requires that confirmed track's delay/doppler to
    fall within tolerance of a real aircraft's expected position (from
    blah2_api's /api/adsb2dd, computed for the node's actual rx/tx geometry
    and fc) — much stronger evidence, but needs truth.adsb.enabled and a
    working ADS-B feed on the node.

Nothing is written to user.yml during a run; candidates are applied via
blah2's live retune channel only. On any non-success terminal state the
original tuning is restored. Persisting a successful result is a separate,
explicit step (POST /calibrate/apply).

All blah2-side timestamps (retune appliedAt, rf-status, detection/tracker
CPI timestamps) share blah2's system clock, so freshness comparisons never
mix clock domains.
"""

import copy
import threading
import time
from datetime import datetime, timezone

# Gain reduction bounds (dB) — mirror blah2's RspDuo limits.
GAIN_REDUCTION_MIN = 20
GAIN_REDUCTION_MAX = 59

# Descent: big backoff jumps per overloaded tuner, one optional refine step.
DESCENT_STEP_DB = 10
REFINE_STEP_DB = 5

# Retune protocol timing.
ACK_TIMEOUT_SECONDS = 2.0
ACK_POLL_SECONDS = 0.2
RF_STATUS_TIMEOUT_SECONDS = 6.0
RF_STATUS_POLL_SECONDS = 0.3
OVERLOAD_SETTLE_SECONDS = 2.0

# Dwell: how long to wait for a confirmed track at one tuning. No fixed
# default — each tower's share of the overall budget is computed dynamically
# in _run() as (time remaining / towers remaining), so a slow descent or an
# early tower's full-length dwell can't silently starve the towers after it.
DWELL_POLL_SECONDS = 1.0

# Overall run budget.
TOTAL_BUDGET_SECONDS = 600

# Success modes.
MODE_TRACK = "track"
MODE_ADSB = "adsb"
VALID_MODES = (MODE_TRACK, MODE_ADSB)

# ADS-B match tolerances (dB/Hz-ish units matching blah2's own delay/doppler
# bins) — overridden from the node's truth.adsb.* config by routes/calibrate.py;
# these are just sane fallbacks for direct/test use.
DEFAULT_ADSB_DELAY_TOLERANCE = 2.0
DEFAULT_ADSB_DOPPLER_TOLERANCE = 5.0

# Track-evidence levels, worst to best, for ranking best attempts. Mode-
# agnostic — always reflects how far blah2's own tracker got; MODE_ADSB
# layers an additional match requirement on top for success specifically
# (see _dwell), not a different evidence scale.
EVIDENCE_NONE = 0
EVIDENCE_DETECTIONS = 1
EVIDENCE_TENTATIVE = 2
EVIDENCE_ASSOCIATED = 3
EVIDENCE_ACTIVE = 4

EVIDENCE_LABELS = {
    EVIDENCE_NONE: "no detections seen",
    EVIDENCE_DETECTIONS: "detections seen, no track initiated",
    EVIDENCE_TENTATIVE: "tentative tracks initiated, none associated",
    EVIDENCE_ASSOCIATED: "tracks associated, none confirmed",
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

    def __init__(self, blah2_client):
        self._client = blah2_client
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread = None
        self._status = self._idle_status()
        # Called with the final status dict when a run reaches a terminal
        # state (telemetry hook). Exceptions are swallowed.
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
            "error": None,
            "original": None,
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
              dwell_seconds=None, mode=MODE_TRACK,
              adsb_delay_tolerance=DEFAULT_ADSB_DELAY_TOLERANCE,
              adsb_doppler_tolerance=DEFAULT_ADSB_DOPPLER_TOLERANCE):
        """Start a run. Returns (started, error).

        towers: list of {"name": str, "fc": int Hz} — first entry is dwelt on
        first (normally the currently-configured tower).
        original: {"fc": int, "gain_a": int, "gain_b": int} — restored on any
        non-success terminal state.
        dwell_seconds: fixed per-tower budget override, mainly for tests.
        Leave as None to divide the remaining time evenly across the
        remaining towers each time a new tower starts (the production path).
        mode: MODE_TRACK (any confirmed track) or MODE_ADSB (confirmed track
        that also matches a real aircraft's expected position). Callers are
        responsible for checking truth.adsb.enabled before using MODE_ADSB —
        this class doesn't have access to the node's config.
        """
        if self.is_running():
            return False, "Calibration already running"
        if not towers:
            return False, "No candidate towers"
        if mode not in VALID_MODES:
            return False, f"Invalid mode: {mode}"

        with self._lock:
            self._status = self._idle_status()
            self._status.update({
                "state": "running",
                "mode": mode,
                "started_at": _utcnow(),
                "original": dict(original),
                "_started_monotonic": time.monotonic(),
            })
            self._status["progress"]["towers_total"] = len(towers)
            self._status["progress"]["budget_seconds"] = budget_seconds
        self._cancel.clear()
        self._thread = threading.Thread(
            target=self._run, args=(list(towers), dict(original),
                                    budget_seconds, dwell_seconds, mode,
                                    adsb_delay_tolerance, adsb_doppler_tolerance),
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

    # ── Retune protocol ────────────────────────────────────────

    def _apply(self, fc, gain_a, gain_b, ignore_cancel=False):
        """Request a retune and wait for blah2's ack. Returns appliedAt (ms).

        ignore_cancel: used only by the restore-on-failure path, which must
        run to completion even if the user cancels (again) while it's in
        flight — otherwise blah2 could be left tuned to a failed candidate.
        """
        last_error = None
        for attempt in range(2):
            self._check_cancel(ignore_cancel=ignore_cancel)
            generation, error = self._client.retune(fc, gain_a, gain_b)
            if generation is None:
                last_error = error
                self._sleep(0.5, ignore_cancel=ignore_cancel)
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
        raise CalibrationError(
            f"Retune failed: {last_error} — is the radar running?")

    def _read_overload(self, applied_at_ms):
        """Overload flags from an rf-status report newer than applied_at_ms."""
        deadline = time.monotonic() + RF_STATUS_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            self._check_cancel()
            rf = self._client.get_rf_status()
            if rf and rf.get("timestamp", 0) >= applied_at_ms:
                self._update_rf(rf.get("overloadA"), rf.get("overloadB"))
                return bool(rf.get("overloadA")), bool(rf.get("overloadB"))
            time.sleep(RF_STATUS_POLL_SECONDS)
        raise CalibrationError(
            "blah2 is not reporting RF status — it may be running an older "
            "version without live-tune support")

    # ── Search stages ──────────────────────────────────────────

    def _descend(self, fc, descent_log, deadline):
        """Find the highest clean gain per tuner: start at max gain, back off
        the overloaded tuner in DESCENT_STEP_DB jumps, then one refine step.
        Returns (gain_a, gain_b, applied_at_ms).

        deadline: this tower's shared descent+dwell budget (monotonic
        clock). Never exceeded — each iteration always finishes its current
        settle+read (so the returned gain/applied_at stay self-consistent
        with what's actually on the hardware), but a new candidate is only
        tried if there's still time left, so a slow descent can eat into
        this tower's dwell but can never run past its whole budget.
        """
        gain_a = gain_b = GAIN_REDUCTION_MIN
        backed_a = backed_b = False

        applied_at = self._apply(fc, gain_a, gain_b)
        while True:
            self._sleep(OVERLOAD_SETTLE_SECONDS)
            overload_a, overload_b = self._read_overload(applied_at)
            descent_log.append({"gain_a": gain_a, "gain_b": gain_b,
                                "overload_a": overload_a, "overload_b": overload_b})
            if not overload_a and not overload_b:
                break
            at_floor_a = gain_a >= GAIN_REDUCTION_MAX
            at_floor_b = gain_b >= GAIN_REDUCTION_MAX
            if (not overload_a or at_floor_a) and (not overload_b or at_floor_b):
                # Still overloaded with nowhere left to back off (at max
                # gain reduction on every overloaded channel) — carry on;
                # the dwell may still work.
                break
            if time.monotonic() >= deadline:
                break  # out of time for this tower — use what we have
            if overload_a and not at_floor_a:
                gain_a = min(gain_a + DESCENT_STEP_DB, GAIN_REDUCTION_MAX)
            if overload_b and not at_floor_b:
                gain_b = min(gain_b + DESCENT_STEP_DB, GAIN_REDUCTION_MAX)
            self._set_current(gain_a=gain_a, gain_b=gain_b)
            applied_at = self._apply(fc, gain_a, gain_b)
            backed_a = backed_a or overload_a
            backed_b = backed_b or overload_b

        # One refine step: claw back REFINE_STEP_DB on tuners that backed off,
        # revert whichever one re-overloads. Skipped if out of time — it's a
        # bonus optimization, not required for a usable result.
        refine_a = max(gain_a - REFINE_STEP_DB, GAIN_REDUCTION_MIN) if backed_a else gain_a
        refine_b = max(gain_b - REFINE_STEP_DB, GAIN_REDUCTION_MIN) if backed_b else gain_b
        if (refine_a, refine_b) != (gain_a, gain_b) and time.monotonic() < deadline:
            self._update(phase="refining")
            self._set_current(gain_a=refine_a, gain_b=refine_b)
            applied_at = self._apply(fc, refine_a, refine_b)
            self._sleep(OVERLOAD_SETTLE_SECONDS)
            overload_a, overload_b = self._read_overload(applied_at)
            descent_log.append({"gain_a": refine_a, "gain_b": refine_b,
                                "overload_a": overload_a, "overload_b": overload_b})
            final_a = gain_a if overload_a else refine_a
            final_b = gain_b if overload_b else refine_b
            if (final_a, final_b) != (refine_a, refine_b):
                self._set_current(gain_a=final_a, gain_b=final_b)
                applied_at = self._apply(fc, final_a, final_b)
            gain_a, gain_b = final_a, final_b

        return gain_a, gain_b, applied_at

    def _dwell(self, tower, fc, gain_a, gain_b, applied_at, dwell_deadline,
               tower_entry, mode, adsb_delay_tolerance, adsb_doppler_tolerance):
        """Hold the tuning, watching for a confirmed track. Returns a result
        dict on success, None if the dwell budget expires.

        MODE_TRACK: any track reaching ACTIVE is success. MODE_ADSB: an
        ACTIVE track only succeeds if its delay/doppler also matches a real
        aircraft's expected position — an unmatched confirmed track keeps
        dwelling (real evidence, recorded as such, but not proof yet).
        """
        self._update(phase="dwelling")
        max_evidence = EVIDENCE_NONE
        max_detections = 0

        while time.monotonic() < dwell_deadline:
            self._check_cancel()
            reason_override = None

            tracker = self._client.get_tracker()
            if tracker and tracker.get("timestamp", 0) >= applied_at:
                active_tracks = [t for t in (tracker.get("data") or [])
                                 if t.get("state") == "ACTIVE"]
                if active_tracks and mode == MODE_ADSB:
                    adsb_tracks = self._client.get_adsb_tracks()
                    matched_track, matched_aircraft = self._find_adsb_match(
                        active_tracks, adsb_tracks,
                        adsb_delay_tolerance, adsb_doppler_tolerance)
                    if matched_track is not None:
                        tower_entry["outcome"] = "confirmed_track"
                        tower_entry["max_evidence"] = EVIDENCE_ACTIVE
                        return {
                            "tower_name": tower.get("name"), "fc": fc,
                            "gain_a": gain_a, "gain_b": gain_b,
                            "track_id": matched_track.get("id"),
                            "adsb_hex": matched_aircraft.get("hex"),
                            "adsb_flight": matched_aircraft.get("flight"),
                        }
                    max_evidence = max(max_evidence, EVIDENCE_ACTIVE)
                    reason_override = "confirmed track, but doesn't match a known aircraft"
                elif active_tracks:
                    tower_entry["outcome"] = "confirmed_track"
                    tower_entry["max_evidence"] = EVIDENCE_ACTIVE
                    return {
                        "tower_name": tower.get("name"), "fc": fc,
                        "gain_a": gain_a, "gain_b": gain_b,
                        "track_id": active_tracks[0].get("id"),
                    }
                elif tracker.get("nAssociated", 0) > 0 or tracker.get("nCoasting", 0) > 0:
                    max_evidence = max(max_evidence, EVIDENCE_ASSOCIATED)
                elif tracker.get("nTentative", 0) > 0:
                    max_evidence = max(max_evidence, EVIDENCE_TENTATIVE)

            detection = self._client.get_detection()
            if detection and detection.get("timestamp", 0) >= applied_at:
                count = len(detection.get("delay") or [])
                if count > 0:
                    max_evidence = max(max_evidence, EVIDENCE_DETECTIONS)
                    max_detections = max(max_detections, count)

            self._maybe_update_best_attempt(tower, fc, gain_a, gain_b,
                                            max_evidence, max_detections,
                                            reason=reason_override)
            self._sleep(DWELL_POLL_SECONDS)

        tower_entry["outcome"] = "no_confirmed_track"
        tower_entry["max_evidence"] = max_evidence
        tower_entry["max_detections"] = max_detections
        return None

    @staticmethod
    def _find_adsb_match(active_tracks, adsb_tracks, delay_tol, doppler_tol):
        """First ACTIVE track whose delay/doppler falls within tolerance of
        a currently-visible ADS-B aircraft's expected position. Returns
        (track, aircraft) or (None, None) — including when adsb_tracks is
        None/empty (feed unreachable or no aircraft in range right now)."""
        if not adsb_tracks:
            return None, None
        for track in active_tracks:
            t_delay, t_doppler = track.get("delay"), track.get("doppler")
            if t_delay is None or t_doppler is None:
                continue
            for aircraft in adsb_tracks.values():
                a_delay, a_doppler = aircraft.get("delay"), aircraft.get("doppler")
                if a_delay is None or a_doppler is None:
                    continue
                if (abs(t_delay - a_delay) <= delay_tol
                        and abs(t_doppler - a_doppler) <= doppler_tol):
                    return track, aircraft
        return None, None

    def _maybe_update_best_attempt(self, tower, fc, gain_a, gain_b,
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
                "evidence": evidence,
                "reason": reason or EVIDENCE_LABELS[evidence],
                "max_detections": max_detections,
            }

    def _set_current(self, **kwargs):
        with self._lock:
            current = dict(self._status.get("current") or {})
            current.update(kwargs)
            self._status["current"] = current

    # ── Run loop ───────────────────────────────────────────────

    def _run(self, towers, original, budget_seconds, dwell_seconds, mode,
             adsb_delay_tolerance, adsb_doppler_tolerance):
        result = None
        error = None
        state = "failed"
        run_deadline = time.monotonic() + budget_seconds

        try:
            for index, tower in enumerate(towers):
                self._check_cancel()
                if time.monotonic() >= run_deadline:
                    break
                fc = int(tower["fc"])
                self._update(phase="descending")
                self._set_current(tower_index=index, tower_name=tower.get("name"),
                                  fc=fc, gain_a=GAIN_REDUCTION_MIN,
                                  gain_b=GAIN_REDUCTION_MIN)
                # tower_entry stays thread-local until the tower is finished —
                # it is only shared (appended to status history) once the run
                # thread stops mutating it
                tower_entry = {
                    "tower_name": tower.get("name"),
                    "fc": fc,
                    "descent": [],
                    "outcome": "not_reached",
                }
                # This tower's total budget (descent + dwell together), so a
                # slow descent shrinks its own dwell rather than overrunning
                # into towers after it. Fixed dwell_seconds (tests) keeps the
                # old fixed-window behaviour; None (production) divides
                # whatever's left evenly across the towers left to try.
                if dwell_seconds is not None:
                    tower_deadline = min(time.monotonic() + dwell_seconds, run_deadline)
                else:
                    towers_remaining = len(towers) - index
                    time_left = max(run_deadline - time.monotonic(), 0)
                    tower_deadline = time.monotonic() + (time_left / towers_remaining)

                try:
                    gain_a, gain_b, applied_at = self._descend(
                        fc, tower_entry["descent"], tower_deadline)
                    tower_entry["final_gain_a"] = gain_a
                    tower_entry["final_gain_b"] = gain_b
                    self._set_current(gain_a=gain_a, gain_b=gain_b)

                    if time.monotonic() >= tower_deadline:
                        # Descent alone used this tower's whole budget —
                        # be honest that it was never actually watched,
                        # rather than recording a misleading "checked,
                        # nothing there".
                        tower_entry["outcome"] = "skipped_no_time"
                        result = None
                    else:
                        dwell_started = time.monotonic()
                        result = self._dwell(tower, fc, gain_a, gain_b, applied_at,
                                             tower_deadline, tower_entry, mode,
                                             adsb_delay_tolerance, adsb_doppler_tolerance)
                        tower_entry["dwell_seconds"] = round(time.monotonic() - dwell_started, 1)
                finally:
                    self._append_history(tower_entry)
                    self._update_progress(towers_tried=index + 1)

                if result is not None:
                    state = "done"
                    break

            if result is None and error is None:
                what = ("No ADS-B-verified track" if mode == MODE_ADSB
                        else "No confirmed track")
                error = (f"{what} found within the time budget — this may "
                        "simply mean no aircraft was overhead during this "
                        "run, not that the tuning is wrong.")

        except _Cancelled:
            state = "cancelled"
            error = "Cancelled by user"
        except CalibrationError as e:
            state = "failed"
            error = str(e)
        except Exception as e:
            state = "failed"
            error = f"Unexpected error: {e}"

        # Restore the original tuning on any non-success outcome so a failed
        # run never leaves blah2 parked on a random candidate. ignore_cancel
        # is required, not just belt-and-suspenders: a second cancel click
        # while this is in flight must not be able to abort it, or blah2
        # could be left tuned to the last (failed) candidate.
        if state != "done":
            self._update(phase="restoring")
            try:
                self._apply(original["fc"], original["gain_a"], original["gain_b"],
                           ignore_cancel=True)
                self._set_current(tower_index=None, tower_name=None,
                                  fc=original["fc"], gain_a=original["gain_a"],
                                  gain_b=original["gain_b"])
            except Exception:
                pass  # blah2 unreachable — restart:always re-reads config.yml

        self._update(state=state, phase=None, result=result, error=error,
                     finished_at=_utcnow())

        if self.on_complete is not None:
            try:
                self.on_complete(self.get_status())
            except Exception:
                pass
