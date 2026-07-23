"""Auto-Calibrate: tune tower/fc, per-tuner gain and LNA state until a track
confirms.

Strategy ("good, not best"): for each candidate tower, start at the *safe*
end of the gain range (maximum gain reduction, minimum LNA attenuation) —
never at maximum gain — and step toward more sensitivity in big increments,
reverting to the last clean step the instant the RF front end overloads,
then dwell at that setting waiting for a confirmed track. A confirmed track
needs a real aircraft overhead, so dwell time dominates the run — the search
minimises the number of dwells, not the granularity of the gain grid.

Starting at the safe end is not a style choice: this search always runs
with the SDR's hardware AGC disabled (see routes/calibrate.py's AGC guard —
AGC would otherwise fight the manual gain search), which means there is no
hardware-level protection against overload at all while a run is in
progress. Hardware AGC protects the ADC continuously, at hardware speed;
this software-driven search only checks in every OVERLOAD_SETTLE_SECONDS.
An earlier version of this search started cold at maximum gain (minimum
reduction) on the assumption that a couple of seconds at an overloaded
setting was merely a stability inconvenience to correct after the fact —
confirmed wrong on a real deployment near a strong broadcast tower, where
that combination (AGC off, gain pinned at maximum sensitivity) left the
front end unprotected for long enough to destabilise the SDRplay device
itself, not just log an overload. Approaching risk from the safe side and
reverting on the very first sign of trouble bounds the worst-case exposure
to one step beyond an already-proven-clean value, every time.

This same hardware doesn't always fail safely even with that discipline:
a bad candidate can wedge the device outright rather than just report
overload (see _probe/_safe_revert) — the retune never acks, or rf-status
goes quiet, surfacing as a CalibrationError instead of a clean reading.
Every descent/dwell step treats that failure exactly like an overload
reading at that candidate (revert to the last proven-clean value, or
escalate LNA state if there's no clean value yet) rather than letting it
abort the whole multi-tower run — a wedge is, if anything, a stronger
signal that this candidate is unusable, not a different kind of problem.

Three search variables, adjusted in a fixed priority order per tower (see
_descend_reference/_descend_surveillance/_descend):
  1. Reference gain reduction (tuner A) — walks toward more gain only, no
     refine. The reference channel just needs to capture the illuminator
     cleanly; the goal is simply the highest gain that doesn't clip.
  2. Surveillance gain reduction (tuner B) — walks toward more gain, then
     one refine step once a revert has happened (claw back 5dB, revert
     again if that re-overloads). This is where MODE_ADSB's
     sensitivity-cycling picks up from (see _dwell_adsb).
  3. LNA state — shared across both tuners (the SDRplay device has no
     per-tuner LNA control). Only touched if gain reduction alone can't
     clear an overload (i.e. still clipping even at gRdB's safest, 59dB
     ceiling) — gRdB is a downstream/IF-stage control, so it cannot fix a
     front end that's genuinely saturating on a very strong signal; LNA
     state (an upstream, RF-stage control) is the escalation path for
     that. Higher LNA state number means more attenuation, less gain
     (state 1 = max gain, state 9 = min gain — see RspDuo/README.md in
     blah2-arm). Escalating LNA state resets *only* whichever channel(s)
     triggered the escalation back to the safe ceiling (59dB) and
     redescends fresh — a channel that's already clean is left untouched,
     since more attenuation upstream can never newly overload a channel
     that wasn't already clipping.

Track confirmation goes through the same retina-tracker sidecar container
tracker-preview uses (github.com/offworldlabs/retina-tracker, run as its own
process — see retina_tracker_client.py), not a tracker built in-process here
or blah2's own built-in tracker, which the client has found unreliable on
real data. That sidecar's TCP server accepts one connection at a time, so
every detection frame is pushed to it via the shared RetinaTrackerClient's
send_frame() and confirmed-track events are received through a listener
callback (_on_track_event) registered once with that same client — the one
tracker-preview already tails. Because a confirmed track from one candidate
tower is physically meaningless at another (different fc/tx position means
different delay/Doppler geometry), a {"type": "RESET"} message clears the
sidecar's tracker in place before each tower's descent+dwell (see
RetinaTrackerClient.reset()) — mirroring blah2's own fc-triggered tracker
reset. Reset scope is per-tower only: since any confirmed track ends the
search immediately, finer-grained reset scope has no effect on correctness.
Evidence grading is coarser than an in-process tracker could offer
(EVIDENCE_NONE/DETECTIONS/ACTIVE only, no tentative/associated distinction)
— the sidecar's events stream only reports confirmed (ACTIVE) tracks, the
same visibility tracker-preview itself has.

Two success modes, with genuinely different dwell strategies:
  - MODE_TRACK (default): any confirmed-track event counts as success (the
    sidecar only ever emits one once a track has been promoted to ACTIVE —
    see retina_tracker/tracker.py::process_frame). No independent way to
    tell "bad gain" from "no aircraft right now", so this mode is
    time-boxed — each tower gets a fair share of the overall budget (see
    _run) and gives up when that runs out.
  - MODE_ADSB: a confirmed-track event only counts if the sidecar's own
    tracker matched it to a real aircraft (retina-tracker's Track class does
    this matching natively, from the same per-detection "adsb" field
    blah2_api already attaches to /api/detection when truth.adsb.enabled —
    see _dwell_adsb) — an event carrying a non-null adsb_hex is the success
    signal. Because that gives an independent, ground-truth answer to "is
    there even anything to detect right now", this mode has **no time
    division** (see _dwell_adsb): it waits for an ADS-B-confirmed aircraft
    with no timeout — absence of traffic is never the search's fault — and
    only treats a candidate as failed once a real aircraft was actually in
    range and still went unmatched. Gain then steps toward more sensitivity
    and tries again; once gain candidates for a tower are exhausted (floor
    or re-overload), the run moves to the next tower.

    The engine supports MODE_ADSB fully, but routes/calibrate.py still
    rejects mode=adsb at the /start endpoint — exposing it to users is a
    separate decision not yet made.

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
APPLY_RETRY_DELAY_SECONDS = 0.5
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

# Track-evidence levels, worst to best, for ranking best attempts. Mode-
# agnostic — always reflects how far the sidecar's tracker got; MODE_ADSB
# layers an additional match requirement on top for success specifically
# (see _dwell_adsb), not a different evidence scale. Coarser than an
# in-process tracker could offer: the sidecar's events stream only reports
# confirmed (ACTIVE) tracks, so there's no tentative/associated distinction
# visible from out here.
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

    def __init__(self, blah2_client, retina_tracker_client):
        self._client = blah2_client
        self._tracker_client = retina_tracker_client
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread = None
        self._status = self._idle_status()
        # Latest confirmed-track event the sidecar has emitted (see
        # _on_track_event) — read via _take_confirmed_event().
        self._last_confirmed_event = None
        # Deferred to start() rather than done here: __init__ runs at app
        # boot regardless of whether a run ever happens, and registering
        # eagerly would start retina_tracker_client's tail thread that early
        # too (see app.py's own tracker_capture.start() pytest-leak note).
        self._listener_registered = False
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
              dwell_seconds=None, mode=MODE_TRACK):
        """Start a run. Returns (started, error).

        towers: list of {"name": str, "fc": int Hz} — first entry is dwelt on
        first (normally the currently-configured tower).
        original: {"fc": int, "gain_a": int, "gain_b": int} — restored on any
        non-success terminal state.
        dwell_seconds: fixed per-tower budget override, mainly for tests.
        Leave as None to divide the remaining time evenly across the
        remaining towers each time a new tower starts (the production path).
        mode: MODE_TRACK (any confirmed track) or MODE_ADSB (confirmed track
        that also matches a real aircraft's expected position, per the
        node's own truth.adsb.delay_tolerance/doppler_tolerance config — the
        sidecar's tracker applies these, not this class). Callers are
        responsible for checking truth.adsb.enabled before using MODE_ADSB —
        this class doesn't have access to the node's config.
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
                                    budget_seconds, dwell_seconds, mode),
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
        """Registered once (see start()) with the shared RetinaTrackerClient.
        Runs on its tail thread, not the calibration thread. The sidecar
        only ever emits an event for a track that already has an id, which
        it only assigns on ACTIVE promotion (see
        retina_tracker/tracker.py::process_frame) — so receiving an event at
        all already means "confirmed", nothing further to check here."""
        with self._lock:
            self._last_confirmed_event = event

    def _take_confirmed_event(self, min_timestamp):
        """The latest confirmed event, if it's no older than min_timestamp
        (normally the current candidate's applied_at). Guards against a
        confirmed event generated by a previous, now-irrelevant tower or
        gain candidate still being in flight — the tail thread polls the
        sidecar's output file on its own schedule, independent of when we
        move on to the next candidate."""
        with self._lock:
            event = self._last_confirmed_event
        if event is not None and event.get("timestamp", 0) >= min_timestamp:
            return event
        return None

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

    def _probe(self, fc, gain_a, gain_b, lna_state, fallback_applied_at):
        """Apply one gain/LNA candidate and read back whether it overloaded,
        settling in between — one full "try a candidate" step of the
        descent loops (and of _dwell_adsb's gain-cycling loop).

        On this hardware, a bad candidate doesn't always just report
        overload cleanly — it can wedge the SDRplay device outright,
        surfacing as a CalibrationError from _apply (no retune ack) or
        _read_overload (no fresh rf-status) instead of a clean overloadA/B
        reading (see module docstring). Folding either failure into
        overload_a=overload_b=True lets callers reuse their existing
        overload-handling branches (revert to the last-clean value, or
        escalate LNA state if there's no clean value yet) unchanged —
        forcing *both* flags true even for a single-tuner caller is
        deliberate: a device error means neither channel's state is
        actually known, and erring toward "assume the worst, back off"
        matches this module's safe-descent philosophy.

        fallback_applied_at: used as applied_at when the candidate's own
        retune never completed (nothing new is actually known to be
        applied) — normally the previous candidate's own applied_at, or 0
        for the very first candidate of a fresh call.

        Returns (applied_at_ms, overload_a, overload_b, device_error_detail).
        device_error_detail is None on a normal probe, or the
        CalibrationError's message when the candidate didn't survive.
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

    def _safe_revert(self, fc, gain_a, gain_b, lna_state, fallback_applied_at):
        """Best-effort re-apply of a previously-proven-safe candidate, after
        a later candidate overloaded (or didn't survive being tried).
        Never raises: if the device won't even take the revert — e.g. it's
        still wedged — that's not a new fatal condition to propagate. The
        gain value the caller reports already reflects our best guess at a
        safe setting; the caller's dwell will simply fail to confirm a
        track (same as any other no-signal outcome) if the hardware is
        genuinely gone, and the run moves on to the next tower exactly
        like any other no-track outcome (see _run()).

        Returns (applied_at_ms, device_error_detail). device_error_detail
        is None on success, or the failure's message if the revert itself
        didn't survive — fallback_applied_at is returned unchanged in that
        case, since nothing new is actually known to have been applied.
        """
        try:
            return self._apply(fc, gain_a, gain_b, lna_state), None
        except CalibrationError as e:
            return fallback_applied_at, str(e)

    # ── Search stages ──────────────────────────────────────────

    def _descend_reference(self, fc, gain_b, lna_state, descent_log, deadline):
        """Find the highest clean gain for the reference tuner (A) only, at
        a fixed lna_state. gain_b rides along in each retune call (both
        tuners' gain are always set together) but is otherwise irrelevant
        here — only overload_a is inspected, and there's no refine step
        (see module docstring: reference just wants "as hot as possible
        without clipping", not surveillance's finer optimisation).

        Starts at the safe ceiling (GAIN_REDUCTION_MAX) and steps toward
        more gain while clean, reverting to the last settled-clean value
        the instant overload appears — see module docstring for why this
        can never start cold at a risky (low-reduction) value. A retune or
        rf-status failure for a candidate (see _probe) is treated exactly
        like an overload reading at that candidate — this hardware doesn't
        always fail safely.

        Returns (gain_a, applied_at_ms, still_overloaded).
        """
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
                    # Overloaded even at the safety ceiling (or the device
                    # never survived the safety ceiling) — gain reduction
                    # alone can't clear this; caller escalates LNA state.
                    return gain_a, applied_at, True
                # Never leave the hardware sitting at the overloaded
                # candidate — revert to the last proven-clean value.
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
        """Find the highest clean gain for the surveillance tuner (B) only,
        at a fixed lna_state and fixed (already-resolved) gain_a. Same
        safe-ceiling-first pattern as reference, plus one refine step once
        a revert has happened (claw back REFINE_STEP_DB, revert if it
        re-overloads).

        A retune or rf-status failure for a candidate (see _probe) is
        treated exactly like an overload reading at that candidate — this
        hardware doesn't always fail safely.

        Returns (gain_b, applied_at_ms, still_overloaded).
        """
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
            fc, GAIN_REDUCTION_MAX, lna_state, descent_log, deadline)

        gain_b, overload_b = GAIN_REDUCTION_MAX, False
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

    def _dwell(self, tower, fc, gain_a, gain_b, lna_state, applied_at, dwell_deadline,
               tower_entry):
        """MODE_TRACK's dwell: push live detections to the shared
        retina-tracker sidecar (see module docstring for why — blah2's own
        tracker is not trusted here) and wait for it to emit a confirmed
        (ACTIVE) track event, or dwell_deadline passes. Returns a result
        dict on success, None if the dwell budget expires. (MODE_ADSB uses
        _dwell_adsb instead — see the module docstring.)
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
                self._tracker_client.send_frame(detection)

                delays = detection.get("delay") or []
                if delays:
                    max_evidence = max(max_evidence, EVIDENCE_DETECTIONS)
                    max_detections = max(max_detections, len(delays))

            confirmed = self._take_confirmed_event(applied_at)
            if confirmed is not None:
                tower_entry["outcome"] = "confirmed_track"
                tower_entry["max_evidence"] = EVIDENCE_ACTIVE
                return {
                    "tower_name": tower.get("name"), "fc": fc,
                    "gain_a": gain_a, "gain_b": gain_b,
                    "track_id": confirmed.get("track_id"),
                }

            self._maybe_update_best_attempt(tower, fc, gain_a, gain_b, lna_state,
                                            max_evidence, max_detections)
            self._sleep(TRACKER_FEED_POLL_SECONDS)

        tower_entry["outcome"] = "no_confirmed_track"
        tower_entry["max_evidence"] = max_evidence
        tower_entry["max_detections"] = max_detections
        return None

    def _dwell_adsb(self, tower, fc, gain_a, initial_gain_b, lna_state, tower_entry):
        """MODE_ADSB's dwell: no time budget. Starting from descent's clean
        (no-overload) gainReductionB, wait for ADS-B truth to confirm a real
        aircraft is actually observable — unbounded, since no traffic isn't
        a tuning problem — then keep checking every poll for a confirmed
        track that also matches a real aircraft, for as long as some
        aircraft stays in range. The match itself is done by the sidecar's
        own tracker natively (retina-tracker's Track class initialises from
        a detection's "adsb" field — populated per-detection by blah2_api's
        /api/detection when truth.adsb.enabled, using the node's own
        truth.adsb.delay_tolerance/doppler_tolerance — so a confirmed event
        carrying a non-null adsb_hex already is the match). If every
        aircraft that showed up leaves again unmatched, that gain candidate
        has had its genuine chance: step gainReductionB toward max
        sensitivity (re-checking overload first) and try again. Returns a
        result dict on success, None once candidates are exhausted for this
        tower (sensitivity floor or re-overload).

        gainReductionA stays fixed at descent's value throughout — it's the
        surveillance channel (B) whose sensitivity determines whether a
        weak real target actually gets detected, not the reference channel.
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
                # More sensitivity than this isn't usable here — never leave
                # the hardware sitting at the overloaded candidate. A prior
                # entry normally exists (the first candidate is descent's
                # already-validated-clean initial_gain_b), but guard anyway
                # in case RF conditions shifted since descent resolved it.
                if len(gains_tried) > 1:
                    previous_gain_b = gains_tried[-2]["gain_b"]
                    applied_at, _ = self._safe_revert(
                        fc, gain_a, previous_gain_b, lna_state, applied_at)
                    self._set_current(gain_b=previous_gain_b)
                break

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
                    self._tracker_client.send_frame(detection)
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
                            "gain_a": gain_a, "gain_b": gain_b,
                            "track_id": confirmed.get("track_id"),
                            "adsb_hex": confirmed.get("adsb_hex"),
                        }
                    if confirmed is not None:
                        reason_override = "confirmed track, but doesn't match a known aircraft"
                elif aircraft_seen:
                    # every aircraft we had a real shot at is gone,
                    # unmatched — this candidate's opportunity is over
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

    # ── Run loop ───────────────────────────────────────────────

    def _run(self, towers, original, budget_seconds, dwell_seconds, mode):
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
                                  fc=fc, gain_a=GAIN_REDUCTION_MAX,
                                  gain_b=GAIN_REDUCTION_MAX, lna_state=LNA_STATE_MIN)
                # New geometry — a track confirmed at the previous tower (or
                # an earlier gain candidate at this one) means nothing here.
                self._tracker_client.reset()
                with self._lock:
                    self._last_confirmed_event = None
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
                                                  tower_entry)
                    elif time.monotonic() >= descent_deadline:
                        # Descent alone used this tower's whole budget —
                        # be honest that it was never actually watched,
                        # rather than recording a misleading "checked,
                        # nothing there".
                        tower_entry["outcome"] = "skipped_no_time"
                        result = None
                    else:
                        dwell_started = time.monotonic()
                        result = self._dwell(tower, fc, gain_a, gain_b, lna_state, applied_at,
                                             descent_deadline, tower_entry)
                        tower_entry["dwell_seconds"] = round(time.monotonic() - dwell_started, 1)
                finally:
                    if (any(e.get("device_error") for e in tower_entry.get("descent", ())) or
                            any(e.get("device_error") for e in tower_entry.get("gains_tried", ()))):
                        tower_entry["device_error"] = True
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
