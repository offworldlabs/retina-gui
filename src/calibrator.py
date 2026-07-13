"""Auto-Calibrate: tune tower/fc, per-tuner gain and LNA state until a track
confirms.

Strategy ("good, not best"): for each candidate tower, start at maximum gain
(minimum gain reduction, minimum LNA attenuation), back off in big steps
until the RF front end runs clean, then dwell at that setting waiting for a
confirmed track. A confirmed track needs a real aircraft overhead, so dwell
time dominates the run — the search minimises the number of dwells, not the
granularity of the gain grid.

Three search variables, adjusted in a fixed priority order per tower (see
_descend_reference/_descend_surveillance/_descend):
  1. Reference gain reduction (tuner A) — descend only, no refine. The
     reference channel just needs to capture the illuminator cleanly; the
     goal is simply the highest gain that doesn't clip.
  2. Surveillance gain reduction (tuner B) — descend, then one refine step
     (claw back 5dB, revert if it re-overloads). This is where MODE_ADSB's
     sensitivity-cycling picks up from (see _dwell_adsb).
  3. LNA state — shared across both tuners (the SDRplay device has no
     per-tuner LNA control). Only touched if gain reduction alone can't
     clear an overload (i.e. still clipping at gRdB's 59dB ceiling) — gRdB
     is a downstream/IF-stage control, so it cannot fix a front end that's
     genuinely saturating on a very strong signal; LNA state (an upstream,
     RF-stage control) is the escalation path for that. Higher LNA state
     number means more attenuation, less gain (state 1 = max gain, state 9
     = min gain — see RspDuo/README.md in blah2-arm). Escalating LNA state
     resets *only* whichever channel(s) triggered the escalation back to
     20dB and redescends fresh — a channel that's already clean is left
     untouched, since more attenuation upstream can never newly overload a
     channel that wasn't already clipping.

Track confirmation runs entirely in this process: a fresh retina-tracker
(github.com/offworldlabs/retina-tracker) Tracker instance per tower is fed
the same detection frames already being polled, and its own ACTIVE state is
the success signal — not blah2's own built-in tracker, which the client has
found to be unreliable on real data. Reset scope is per-tower only (matching
blah2's own fc-triggered reset): since any confirmed track ends the search
immediately, finer-grained reset scope has no effect on correctness.

Two success modes, with genuinely different dwell strategies:
  - MODE_TRACK (default): any track reaching ASSOCIATED/ACTIVE state
    (nActive > 0) counts as success. No independent way to tell "bad gain"
    from "no aircraft right now", so this mode is time-boxed — each tower
    gets a fair share of the overall budget (see _run) and gives up when
    that runs out.
  - MODE_ADSB: a confirmed track only counts if its delay/doppler also
    matches a real aircraft's expected position (from blah2_api's
    /api/adsb2dd, computed for the node's actual rx/tx geometry and fc).
    Because that gives an independent, ground-truth answer to "is there
    even anything to detect right now", this mode has **no time division**
    (see _dwell_adsb): it waits for an ADS-B-confirmed aircraft with no
    timeout — absence of traffic is never the search's fault — and only
    treats a candidate as failed once a real aircraft was actually in range
    and still went unmatched. Gain then steps toward more sensitivity and
    tries again; once gain candidates for a tower are exhausted (floor or
    re-overload), the run moves to the next tower.

    MODE_ADSB is currently benched — deliberately left stale (still using
    blah2's own tracker, not migrated to retina-tracker) and blocked at the
    route level (routes/calibrate.py) so it isn't reachable by users. Kept
    in place rather than removed since it's meant to be revisited later.

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

from retina_tracker.config import get_config as get_retina_tracker_config
from retina_tracker.tracker import Tracker as RetinaTracker

# Gain reduction bounds (dB) — mirror blah2's RspDuo limits.
GAIN_REDUCTION_MIN = 20
GAIN_REDUCTION_MAX = 59

# LNA state bounds — mirror blah2's RspDuo limits. Shared across both
# tuners (no per-tuner LNA control on this device). Higher number = more
# attenuation = less gain (state 1 = max gain, state 9 = min gain).
LNA_STATE_MIN = 1
LNA_STATE_MAX = 9

# Descent: big backoff jumps per overloaded tuner, one optional refine step
# (surveillance only — see module docstring for why reference doesn't get one).
DESCENT_STEP_DB = 10
REFINE_STEP_DB = 5

# MODE_ADSB gain cycling: step gainReductionB this much toward max sensitivity
# each time a real (ADS-B-confirmed) aircraft was seen but never matched.
ADSB_GAIN_STEP_DB = 5

# MODE_ADSB's descent phase still needs *some* ceiling (it's a fast,
# aircraft-independent overload-avoidance loop, not the part waiting on
# traffic), just not one derived from a shrinking per-tower time division.
ADSB_DESCENT_DEADLINE_SECONDS = 120

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

# MODE_TRACK's retina-tracker feed loop polls faster than blah2's own CPI
# cadence (measured ~0.9-1s on the desk node) so a new detection frame is
# never missed — same cadence retina-tracker's own always-on capture uses
# (tracker_capture.py's POLL_INTERVAL_S). Frames are de-duplicated by
# timestamp, so polling faster than the CPI rate is free, not wasteful.
TRACKER_FEED_POLL_SECONDS = 0.2

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


def _frame_to_detections(frame):
    """Convert a Blah2Client.get_detection() frame into retina-tracker's
    per-detection dicts. Mirrors retina_tracker's own
    server.py::process_streaming_frame conversion."""
    delays = frame.get("delay", [])
    dopplers = frame.get("doppler", [])
    snrs = frame.get("snr", [])
    return [{"delay": delay, "doppler": doppler, "snr": snr}
            for delay, doppler, snr in zip(delays, dopplers, snrs)]


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
            # MODE_ADSB has no time division — don't report a budget that
            # isn't actually enforced (see module docstring).
            self._status["progress"]["budget_seconds"] = (
                None if mode == MODE_ADSB else budget_seconds)
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

    def _apply(self, fc, gain_a, gain_b, lna_state, ignore_cancel=False):
        """Request a retune and wait for blah2's ack. Returns appliedAt (ms).

        ignore_cancel: used only by the restore-on-failure path, which must
        run to completion even if the user cancels (again) while it's in
        flight — otherwise blah2 could be left tuned to a failed candidate.
        """
        last_error = None
        for attempt in range(2):
            self._check_cancel(ignore_cancel=ignore_cancel)
            generation, error = self._client.retune(fc, gain_a, gain_b, lna_state)
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

    def _descend_reference(self, fc, gain_b, lna_state, descent_log, deadline):
        """Find the highest clean gain for the reference tuner (A) only, at
        a fixed lna_state. gain_b rides along in each retune call (both
        tuners' gain are always set together) but is otherwise irrelevant
        here — only overload_a is inspected, and there's no refine step
        (see module docstring: reference just wants "as hot as possible
        without clipping", not surveillance's finer optimisation).

        Returns (gain_a, applied_at_ms, still_overloaded).
        """
        gain_a = GAIN_REDUCTION_MIN
        applied_at = self._apply(fc, gain_a, gain_b, lna_state)
        while True:
            self._sleep(OVERLOAD_SETTLE_SECONDS)
            overload_a, _ = self._read_overload(applied_at)
            descent_log.append({"phase": "reference", "gain_a": gain_a,
                                "lna_state": lna_state, "overload_a": overload_a})
            if not overload_a:
                return gain_a, applied_at, False
            if gain_a >= GAIN_REDUCTION_MAX or time.monotonic() >= deadline:
                return gain_a, applied_at, True
            gain_a = min(gain_a + DESCENT_STEP_DB, GAIN_REDUCTION_MAX)
            self._set_current(gain_a=gain_a)
            applied_at = self._apply(fc, gain_a, gain_b, lna_state)

    def _descend_surveillance(self, fc, gain_a, lna_state, descent_log, deadline):
        """Find the highest clean gain for the surveillance tuner (B) only,
        at a fixed lna_state and fixed (already-resolved) gain_a. Same
        backoff pattern as reference, plus one refine step once clean
        (claw back REFINE_STEP_DB, revert if it re-overloads).

        Returns (gain_b, applied_at_ms, still_overloaded).
        """
        gain_b = GAIN_REDUCTION_MIN
        backed_b = False
        applied_at = self._apply(fc, gain_a, gain_b, lna_state)
        while True:
            self._sleep(OVERLOAD_SETTLE_SECONDS)
            _, overload_b = self._read_overload(applied_at)
            descent_log.append({"phase": "surveillance", "gain_b": gain_b,
                                "lna_state": lna_state, "overload_b": overload_b})
            if not overload_b:
                break
            if gain_b >= GAIN_REDUCTION_MAX or time.monotonic() >= deadline:
                return gain_b, applied_at, True
            gain_b = min(gain_b + DESCENT_STEP_DB, GAIN_REDUCTION_MAX)
            self._set_current(gain_b=gain_b)
            applied_at = self._apply(fc, gain_a, gain_b, lna_state)
            backed_b = True

        if backed_b and time.monotonic() < deadline:
            refine_b = max(gain_b - REFINE_STEP_DB, GAIN_REDUCTION_MIN)
            self._update(phase="refining")
            self._set_current(gain_b=refine_b)
            applied_at = self._apply(fc, gain_a, refine_b, lna_state)
            self._sleep(OVERLOAD_SETTLE_SECONDS)
            _, overload_b = self._read_overload(applied_at)
            descent_log.append({"phase": "surveillance_refine", "gain_b": refine_b,
                                "lna_state": lna_state, "overload_b": overload_b})
            if overload_b:
                self._set_current(gain_b=gain_b)
                applied_at = self._apply(fc, gain_a, gain_b, lna_state)
            else:
                gain_b = refine_b

        return gain_b, applied_at, False

    def _descend(self, fc, descent_log, deadline):
        """Run the three-variable search in priority order: reference gain,
        then surveillance gain, then (only if either is still overloaded at
        its 59dB ceiling) escalate LNA state — resetting and redescending
        only whichever channel(s) actually triggered the escalation, since
        an already-clean channel can never be newly overloaded by more
        upstream attenuation. See module docstring for the full rationale.

        deadline: this tower's shared descent+dwell budget (monotonic
        clock). Never exceeded — see _descend_reference/_descend_surveillance.

        Returns (gain_a, gain_b, lna_state, applied_at_ms).
        """
        lna_state = LNA_STATE_MIN
        gain_a, applied_at, overload_a = self._descend_reference(
            fc, GAIN_REDUCTION_MIN, lna_state, descent_log, deadline)

        gain_b, overload_b = GAIN_REDUCTION_MIN, False
        if time.monotonic() < deadline:
            gain_b, applied_at, overload_b = self._descend_surveillance(
                fc, gain_a, lna_state, descent_log, deadline)

        while ((overload_a or overload_b) and lna_state < LNA_STATE_MAX
               and time.monotonic() < deadline):
            lna_state += 1
            self._set_current(lna_state=lna_state)
            descent_log.append({"phase": "lna_escalation", "lna_state": lna_state})
            if overload_a:
                gain_a, applied_at, overload_a = self._descend_reference(
                    fc, gain_b, lna_state, descent_log, deadline)
            if overload_b:
                gain_b, applied_at, overload_b = self._descend_surveillance(
                    fc, gain_a, lna_state, descent_log, deadline)

        return gain_a, gain_b, lna_state, applied_at

    def _dwell(self, tower, fc, gain_a, gain_b, applied_at, dwell_deadline,
               tower_entry, tracker):
        """MODE_TRACK's dwell: feed live detections through a local
        retina-tracker instance (see module docstring for why — blah2's own
        tracker is not trusted here) until it confirms an ACTIVE track, or
        dwell_deadline passes. Returns a result dict on success, None if the
        dwell budget expires. (MODE_ADSB uses _dwell_adsb instead, still on
        blah2's own tracker — see the module docstring.)

        tracker: a fresh retina_tracker.Tracker for this tower (reset scope
        is per-tower, not per-gain-candidate — see module docstring).
        """
        self._update(phase="dwelling")
        max_evidence = EVIDENCE_NONE
        max_detections = 0
        last_timestamp = None

        while time.monotonic() < dwell_deadline:
            self._check_cancel()

            detection = self._client.get_detection()
            timestamp = detection.get("timestamp") if detection else None
            if (detection and timestamp != last_timestamp
                    and timestamp is not None and timestamp >= applied_at):
                last_timestamp = timestamp
                frame_detections = _frame_to_detections(detection)
                tracker.process_frame(frame_detections, timestamp)

                if frame_detections:
                    max_evidence = max(max_evidence, EVIDENCE_DETECTIONS)
                    max_detections = max(max_detections, len(frame_detections))

                active_tracks = tracker.get_active_tracks()
                if active_tracks:
                    tower_entry["outcome"] = "confirmed_track"
                    tower_entry["max_evidence"] = EVIDENCE_ACTIVE
                    return {
                        "tower_name": tower.get("name"), "fc": fc,
                        "gain_a": gain_a, "gain_b": gain_b,
                        "track_id": active_tracks[0].id,
                    }
                elif any(t.state_status.name in ("ASSOCIATED", "COASTING")
                         for t in tracker.tracks):
                    max_evidence = max(max_evidence, EVIDENCE_ASSOCIATED)
                elif tracker.tracks:
                    max_evidence = max(max_evidence, EVIDENCE_TENTATIVE)

            self._maybe_update_best_attempt(tower, fc, gain_a, gain_b,
                                            max_evidence, max_detections)
            self._sleep(TRACKER_FEED_POLL_SECONDS)

        tower_entry["outcome"] = "no_confirmed_track"
        tower_entry["max_evidence"] = max_evidence
        tower_entry["max_detections"] = max_detections
        return None

    def _dwell_adsb(self, tower, fc, gain_a, initial_gain_b, lna_state, tower_entry,
                     adsb_delay_tolerance, adsb_doppler_tolerance):
        """MODE_ADSB's dwell: no time budget. Starting from descent's clean
        (no-overload) gainReductionB, wait for ADS-B truth to confirm a real
        aircraft is actually observable — unbounded, since no traffic isn't
        a tuning problem — then keep checking every poll for a matching
        confirmed track for as long as some aircraft stays in range. If
        every aircraft that showed up leaves again unmatched, that gain
        candidate has had its genuine chance: step gainReductionB toward
        max sensitivity (re-checking overload first) and try again. Returns
        a result dict on success, None once candidates are exhausted for
        this tower (sensitivity floor or re-overload).

        gainReductionA stays fixed at descent's value throughout — it's the
        surveillance channel (B) whose sensitivity determines whether a
        weak real target actually gets detected, not the reference channel.
        """
        self._update(phase="dwelling")
        gain_b = initial_gain_b
        gains_tried = []
        max_evidence = EVIDENCE_NONE
        max_detections = 0

        while True:
            self._check_cancel()
            applied_at = self._apply(fc, gain_a, gain_b, lna_state)
            self._set_current(gain_a=gain_a, gain_b=gain_b)
            self._sleep(OVERLOAD_SETTLE_SECONDS)
            overload_a, overload_b = self._read_overload(applied_at)
            gains_tried.append({"gain_b": gain_b, "overload_b": overload_b})
            if overload_b:
                break  # more sensitivity than this isn't usable here

            aircraft_seen = False
            while True:
                self._check_cancel()
                reason_override = None

                adsb_tracks = self._client.get_adsb_tracks()
                tracker = self._client.get_tracker()
                active_tracks = []
                if tracker and tracker.get("timestamp", 0) >= applied_at:
                    active_tracks = [t for t in (tracker.get("data") or [])
                                     if t.get("state") == "ACTIVE"]

                if adsb_tracks:
                    aircraft_seen = True
                    if active_tracks:
                        matched_track, matched_aircraft = self._find_adsb_match(
                            active_tracks, adsb_tracks,
                            adsb_delay_tolerance, adsb_doppler_tolerance)
                        if matched_track is not None:
                            tower_entry["outcome"] = "confirmed_track"
                            tower_entry["max_evidence"] = EVIDENCE_ACTIVE
                            tower_entry["gains_tried"] = gains_tried
                            return {
                                "tower_name": tower.get("name"), "fc": fc,
                                "gain_a": gain_a, "gain_b": gain_b,
                                "track_id": matched_track.get("id"),
                                "adsb_hex": matched_aircraft.get("hex"),
                                "adsb_flight": matched_aircraft.get("flight"),
                            }
                        reason_override = "confirmed track, but doesn't match a known aircraft"
                elif aircraft_seen:
                    # every aircraft we had a real shot at is gone,
                    # unmatched — this candidate's opportunity is over
                    break

                if active_tracks:
                    max_evidence = max(max_evidence, EVIDENCE_ACTIVE)
                elif tracker and (tracker.get("nAssociated", 0) > 0
                                   or tracker.get("nCoasting", 0) > 0):
                    max_evidence = max(max_evidence, EVIDENCE_ASSOCIATED)
                elif tracker and tracker.get("nTentative", 0) > 0:
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

            next_gain_b = gain_b - ADSB_GAIN_STEP_DB
            if next_gain_b < GAIN_REDUCTION_MIN:
                break
            gain_b = next_gain_b

        tower_entry["outcome"] = "no_confirmed_track"
        tower_entry["max_evidence"] = max_evidence
        tower_entry["max_detections"] = max_detections
        tower_entry["gains_tried"] = gains_tried
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
                # MODE_ADSB has no time division (see module docstring) — the
                # overall budget only bounds MODE_TRACK's tower rotation.
                if mode != MODE_ADSB and time.monotonic() >= run_deadline:
                    break
                fc = int(tower["fc"])
                self._update(phase="descending")
                self._set_current(tower_index=index, tower_name=tower.get("name"),
                                  fc=fc, gain_a=GAIN_REDUCTION_MIN,
                                  gain_b=GAIN_REDUCTION_MIN, lna_state=LNA_STATE_MIN)
                # tower_entry stays thread-local until the tower is finished —
                # it is only shared (appended to status history) once the run
                # thread stops mutating it
                tower_entry = {
                    "tower_name": tower.get("name"),
                    "fc": fc,
                    "descent": [],
                    "outcome": "not_reached",
                }

                if mode == MODE_ADSB:
                    # Descent is still time-bounded (it's a fast,
                    # traffic-independent overload-avoidance loop) — just not
                    # via a shrinking per-tower share of the overall budget.
                    descent_deadline = time.monotonic() + ADSB_DESCENT_DEADLINE_SECONDS
                else:
                    # This tower's total budget (descent + dwell together), so
                    # a slow descent shrinks its own dwell rather than
                    # overrunning into towers after it. Fixed dwell_seconds
                    # (tests) keeps the old fixed-window behaviour; None
                    # (production) divides whatever's left evenly across the
                    # remaining towers.
                    if dwell_seconds is not None:
                        descent_deadline = min(time.monotonic() + dwell_seconds, run_deadline)
                    else:
                        towers_remaining = len(towers) - index
                        time_left = max(run_deadline - time.monotonic(), 0)
                        descent_deadline = time.monotonic() + (time_left / towers_remaining)

                try:
                    gain_a, gain_b, lna_state, applied_at = self._descend(
                        fc, tower_entry["descent"], descent_deadline)
                    tower_entry["final_gain_a"] = gain_a
                    tower_entry["final_gain_b"] = gain_b
                    tower_entry["final_lna_state"] = lna_state
                    self._set_current(gain_a=gain_a, gain_b=gain_b, lna_state=lna_state)

                    if mode == MODE_ADSB:
                        result = self._dwell_adsb(tower, fc, gain_a, gain_b, lna_state,
                                                  tower_entry, adsb_delay_tolerance,
                                                  adsb_doppler_tolerance)
                    elif time.monotonic() >= descent_deadline:
                        # Descent alone used this tower's whole budget —
                        # be honest that it was never actually watched,
                        # rather than recording a misleading "checked,
                        # nothing there".
                        tower_entry["outcome"] = "skipped_no_time"
                        result = None
                    else:
                        dwell_started = time.monotonic()
                        tracker = RetinaTracker(config=get_retina_tracker_config())
                        result = self._dwell(tower, fc, gain_a, gain_b, applied_at,
                                             descent_deadline, tower_entry, tracker)
                        tower_entry["dwell_seconds"] = round(time.monotonic() - dwell_started, 1)
                finally:
                    self._append_history(tower_entry)
                    self._update_progress(towers_tried=index + 1)

                if result is not None:
                    result["lna_state"] = lna_state
                    state = "done"
                    break

            if result is None and error is None:
                if mode == MODE_ADSB:
                    error = ("No ADS-B-verified track — every candidate tower "
                             "and gain setting was tried, but no confirmed "
                             "track ever matched a real aircraft while one "
                             "was actually in range.")
                else:
                    error = ("No confirmed track found within the time budget "
                             "— this may simply mean no aircraft was overhead "
                             "during this run, not that the tuning is wrong.")

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
                           original["lna_state"], ignore_cancel=True)
                self._set_current(tower_index=None, tower_name=None,
                                  fc=original["fc"], gain_a=original["gain_a"],
                                  gain_b=original["gain_b"], lna_state=original["lna_state"])
            except Exception:
                pass  # blah2 unreachable — restart:always re-reads config.yml

        self._update(state=state, phase=None, result=result, error=error,
                     finished_at=_utcnow())

        if self.on_complete is not None:
            try:
                self.on_complete(self.get_status())
            except Exception:
                pass
