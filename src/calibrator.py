"""Auto-Calibrate: tune tower/fc, per-tuner gain and LNA state until a track
confirms.

Strategy ("good, not best"): for each candidate tower, start at the *safe*
end of every search axis (maximum gain reduction, maximum LNA state — the
least sensitive setting on both) — never at maximum gain or maximum
sensitivity on either axis — and step toward more sensitivity in big
increments,
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
overload (see _probe/_safe_revert) — the retune never acks, or
overload-status goes quiet, surfacing as a CalibrationError instead of a
clean reading.
Every descent/dwell step treats that failure exactly like an overload
reading at that candidate (revert to the last proven-safe value — a
single channel's own gain, or, for LNA state, the whole (gain_a, gain_b,
lna_state) triple together — or, if there's no clean value yet even at
the safety ceiling, stop there) rather than letting it abort the whole
multi-tower run — a wedge is, if anything, a stronger signal that this
candidate is unusable, not a different kind of problem.

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
     per-tuner LNA control), so it's resolved as a single outer loop
     around both tuners' gain descents rather than a fourth per-tuner
     step. Higher LNA state number means more attenuation, less gain
     (state 1 = max gain/least attenuation, state 9 = min gain/most
     attenuation — see RspDuo/README.md in blah2-arm) — so LNA state's
     *safe* end is its highest number, just like gain reduction's,
     though for a different physical reason (gRdB's max is max
     downstream/IF-stage attenuation; LNA's max is max upstream/RF-stage
     attenuation). Every tower's search starts at lna_state=9 with both
     tuners' gain descended per steps 1-2 above; if both come back clean
     (or as clean as gain reduction alone can make them), the search
     tries one step more sensitive (lna_state - 1) and redescends *both*
     tuners' gain fresh from the 59dB ceiling — unlike gain reduction's
     own descent, where reverting to more attenuation can never newly
     overload an already-clean channel, moving LNA state toward more
     sensitivity is a fundamentally different direction: it can newly
     overload a channel that was clean a moment ago, so both tuners must
     be freshly reproved at every step, with no "only redo the
     triggering channel" shortcut. The instant either channel overloads
     (or hits a device error) at a new, more-sensitive lna_state, the
     whole (gain_a, gain_b, lna_state) triple reverts together to the
     last state fully proven clean — not a per-channel revert, since LNA
     is one shared register and a mismatched-LNA-state combination
     across the two tuners isn't physically meaningful — and the search
     stops there. If gain reduction alone still can't clear an overload
     even at lna_state=9 (i.e. still clipping at the safest corner of
     the whole search space), that's a terminal condition for this
     tower: there's no safer LNA state to retreat to, so the search
     stops immediately rather than exploring more sensitive states it
     already knows are worse.

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

Nothing is written to user.yml during a run, with one narrow, explicit
exception: if every tower's manual gain/LNA search fails to confirm a
track anywhere, one last-resort attempt is made with hardware AGC turned
on at the top-ranked tower's frequency (see _run_agc_fallback) — this is
the only point in a run that touches user.yml/config-merger/Docker, via
an injected collaborator (see agc_fallback.py), never directly. AGC only
drives the reference tuner's gain — the surveillance tuner and the
shared LNA state are not managed by it, so they're set to the top-ranked
tower's own resolved (gain_b, lna_state) from its own manual descent
(captured in _run, reused by both this AGC attempt and the fallback
below), not left unprotected. On a genuine user cancellation or an
unexpected/CalibrationError exception, the original tuning is restored,
unchanged from before. On the no-track-anywhere outcome specifically, the
device is instead left on the top-ranked tower's own resolved, already
proven-not-to-overload operating point (optionally after the AGC attempt
above) — not the arbitrary pre-run tuning. That resolved point already
degrades to the safe (max gain reduction, max LNA state) corner on its
own whenever the top tower's own descent never found anything better —
see _descend_reference/_descend_surveillance's own terminal branches —
so there's no separate "is this still safe" check needed here.
Persisting a successful result is a separate, explicit step (POST
/calibrate/apply).

All blah2-side timestamps (retune appliedAt, overload-status, detection/tracker
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

# AGC last-resort fallback (see module docstring): once every tower's
# manual gain/LNA search has failed, exactly one additional attempt is
# made at the top-ranked tower's frequency with hardware AGC on instead
# of manual gain search on the reference tuner only. 5 matches
# routes/calibrate.py's AGC_BANDWIDTHS — the lowest of the three valid
# AGC-on settings, the most conservative choice for a one-shot attempt
# this late in an already-possibly-troubled run.
AGC_FALLBACK_BANDWIDTH_NUMBER = 5
AGC_BANDWIDTH_OFF = 0

# How long to dwell (waiting for a confirmed track) once AGC is on and
# the capture stack has restarted. Its own fixed budget, not derived from
# TOTAL_BUDGET_SECONDS/the per-tower division — this only runs once the
# whole tower rotation is already exhausted, so it isn't stealing time
# from towers that already had their turn.
AGC_FALLBACK_DWELL_SECONDS = 90

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

    def __init__(self, blah2_client, retina_tracker_client, agc_fallback_client=None):
        self._client = blah2_client
        self._tracker_client = retina_tracker_client
        # Optional third collaborator (see agc_fallback.py) — the run's
        # once-per-run AGC last resort. None (the default, used by most
        # existing tests) simply skips Feature 2; the top-tower fallback
        # still applies on its own (see _run()).
        self._agc_fallback = agc_fallback_client
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
            "blah2 is not reporting overload status — it may be running an older "
            "version without live-tune support")

    def _probe(self, fc, gain_a, gain_b, lna_state, fallback_applied_at):
        """Apply one gain/LNA candidate and read back whether it overloaded,
        settling in between — one full "try a candidate" step of the
        descent loops (and of _dwell_adsb's gain-cycling loop).

        On this hardware, a bad candidate doesn't always just report
        overload cleanly — it can wedge the SDRplay device outright,
        surfacing as a CalibrationError from _apply (no retune ack) or
        _read_overload (no fresh overload-status) instead of a clean
        overloadA/B reading (see module docstring). Folding either failure into
        overload_a=overload_b=True lets callers reuse their existing
        overload-handling branches (revert to the last proven-safe
        value — a single channel's own gain, or, for LNA state, the
        whole (gain_a, gain_b, lna_state) triple together) unchanged —
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
        overload-status failure for a candidate (see _probe) is treated
        exactly like an overload reading at that candidate — this hardware
        doesn't always fail safely.

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

        A retune or overload-status failure for a candidate (see _probe) is
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
        then surveillance gain, both starting at the *safe* end of every
        axis (max gain reduction, max LNA state — see module docstring for
        why LNA state's safe end is its *highest* number, not its lowest).
        Once a (gain_a, gain_b) pair comes back clean — or as clean as gain
        reduction alone can make it — at the current lna_state, the search
        tries one step more sensitive (lna_state - 1) and redescends *both*
        tuners' gain fresh from the 59dB ceiling: unlike gain reduction's
        own descent, where retreating to more attenuation can never newly
        overload an already-clean channel, moving lna_state toward more
        sensitivity is a fundamentally different direction that can newly
        overload a channel that was clean a moment ago — so both tuners
        are always freshly reproved, with no "only redo the triggering
        channel" shortcut. The instant either channel overloads (or hits a
        device error) at a new, more-sensitive lna_state, the whole
        (gain_a, gain_b, lna_state) triple reverts together to the last
        state fully proven clean and the search stops there — not a
        per-channel revert, since LNA state is a single register shared by
        both tuners and a mismatched-lna-state combination across them
        isn't physically meaningful. If gain reduction alone still can't
        clear an overload even at lna_state=9 (the safest corner of the
        whole search space), that's immediately terminal for this tower:
        there's no safer LNA state to retreat to, so the search stops
        right there rather than climbing a ladder toward states it
        already knows are worse.

        deadline: this tower's shared descent+dwell budget (monotonic
        clock). Never exceeded — see _descend_reference/_descend_surveillance.

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
               tower_entry, phase_label="dwelling"):
        """MODE_TRACK's dwell: push live detections to the shared
        retina-tracker sidecar (see module docstring for why — blah2's own
        tracker is not trusted here) and wait for it to emit a confirmed
        (ACTIVE) track event, or dwell_deadline passes. Returns a result
        dict on success, None if the dwell budget expires. (MODE_ADSB uses
        _dwell_adsb instead — see the module docstring.)

        phase_label: lets the AGC last-resort fallback (see
        _run_agc_fallback) report a distinct "agc_dwelling" phase instead
        of the ordinary per-tower "dwelling" — everything else about this
        method is identical either way.
        """
        self._update(phase=phase_label)
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

    def _apply_top_tower_fallback(self, top_tower, top_fc, gain_a, gain_b, lna_state):
        """Feature 1's core: leave blah2 tuned to the top-ranked tower's
        frequency at its own resolved (gain_a, gain_b, lna_state) — the
        values that tower's own manual search already proved don't
        overload — rather than the arbitrary pre-run 'original' tuning.
        Not a separately-fixed "safe corner": see module docstring for
        why the resolved triple already degrades to the safe corner on
        its own whenever the top tower's own descent never found
        anything better. Best-effort and swallows its own failure,
        exactly like the original-tuning restore this replaces for the
        no-track-anywhere case — if blah2 is genuinely unreachable,
        nothing more can be done from here (restart:always re-reads
        config.yml).
        """
        self._update(phase="restoring")
        try:
            self._apply(top_fc, gain_a, gain_b, lna_state, ignore_cancel=True)
            self._set_current(tower_index=0, tower_name=top_tower.get("name"),
                              fc=top_fc, gain_a=gain_a, gain_b=gain_b,
                              lna_state=lna_state)
        except Exception:
            pass

    def _run_agc_fallback(self, top_tower, top_fc, gain_a, gain_b, lna_state):
        """The run's very last resort (see module docstring): try
        hardware AGC once, at the top-ranked tower's frequency only,
        after every tower's manual gain/LNA search has already failed.
        Not part of blah2's live retune protocol — turning AGC on/off
        goes through the injected _agc_fallback collaborator (kept out
        of this class's own Flask/subprocess/Docker-free design).

        AGC only drives the reference tuner (A)'s gain — the
        surveillance tuner (B) and the shared LNA state are NOT managed
        by it at all, so both still need real, proven-not-to-overload
        values or surveillance has zero overload protection during this
        attempt. gain_a/gain_b/lna_state here are the top tower's own
        resolved values from its own manual descent (see _run) — gain_a
        is a don't-care placeholder once AGC takes over live, but
        gain_b/lna_state are load-bearing for real.

        Reuses _dwell (MODE_TRACK's "any confirmed track" criterion)
        regardless of the run's own mode — MODE_ADSB isn't reachable via
        routes/calibrate.py today, so this keeps the fallback simple
        rather than plumbing MODE_ADSB's stricter aircraft-matching
        through a config-driven (not gain-driven) AGC state. Revisit if
        MODE_ADSB is ever exposed.

        applied_at=0 for the dwell's staleness guard is deliberate, not
        an oversight: unlike a live retune, there's no real "applied at"
        timestamp for a config-file-driven container restart, but the
        blah2 container was just fully recreated, so there's no stale
        pre-restart detection data that could wrongly pass the guard —
        safety instead comes from the explicit reset() + confirmed-event
        clear immediately below, exactly as every tower transition
        already relies on.

        The enable step (self._agc_fallback.apply(...), a real
        subprocess/Docker call) is not itself cancellable mid-flight —
        only the dwell afterward checks for cancellation. Accepted:
        matches how every other blocking subprocess call in this
        codebase already behaves.

        Returns (result, error, cancelled):
          - result: a success dict (same shape as _dwell's, plus
            bandwidth_number for /calibrate/apply to persist), or None.
          - error: a human-readable failure reason to append to the
            run's existing "no confirmed track" message, or None on
            success.
          - cancelled: True if the user's cancel is what ended this
            attempt (caller should report state="cancelled", not
            "failed").

        Whatever happens, AGC is never left on: any non-success path
        here turns it back off and re-applies the top tower's own
        resolved values at top_fc before returning — this method fully
        owns the terminal device state for this branch; _run()'s own
        restore-original logic must not also run afterwards (see _run()).
        """
        self._update(phase="agc_fallback")
        self._tracker_client.reset()
        with self._lock:
            self._last_confirmed_event = None
        self._set_current(tower_index=0, tower_name=top_tower.get("name"),
                          fc=top_fc, gain_a=None, gain_b=gain_b, lna_state=lna_state)

        ok, apply_error = self._agc_fallback.apply(
            top_fc, AGC_FALLBACK_BANDWIDTH_NUMBER, gain_a, gain_b, lna_state)
        if not ok:
            # AGC was never actually turned on — nothing to turn back
            # off. Fall through to the plain (live-retune) top-tower
            # fallback.
            self._apply_top_tower_fallback(top_tower, top_fc, gain_a, gain_b, lna_state)
            return None, f"Tried hardware AGC as a last resort, but it could not be enabled: {apply_error}", False

        self._set_current(gain_a=gain_a, gain_b=gain_b, lna_state=lna_state)

        tower_entry = {"tower_name": top_tower.get("name"), "fc": top_fc,
                       "descent": [], "outcome": "not_reached", "agc": True}
        dwell_deadline = time.monotonic() + AGC_FALLBACK_DWELL_SECONDS
        try:
            result = self._dwell(top_tower, top_fc, gain_a, gain_b, lna_state,
                                 applied_at=0, dwell_deadline=dwell_deadline,
                                 tower_entry=tower_entry, phase_label="agc_dwelling")
        except _Cancelled:
            self._append_history(tower_entry)
            # Best-effort cleanup even on cancel — AGC must never be left
            # on silently (see module docstring).
            self._agc_fallback.apply(top_fc, AGC_BANDWIDTH_OFF, gain_a, gain_b, lna_state)
            self._set_current(tower_index=0, tower_name=top_tower.get("name"),
                              fc=top_fc, gain_a=gain_a, gain_b=gain_b,
                              lna_state=lna_state)
            return None, "Cancelled by user", True

        self._append_history(tower_entry)
        if result is not None:
            # gain_a here is the placeholder written alongside AGC, not
            # what AGC is actually running at any instant — accurate
            # enough to persist (config needs *some* valid number even
            # under AGC) without pretending to know AGC's live gain.
            # gain_b/lna_state are the real, load-bearing values.
            # bandwidth_number tells /calibrate/apply to persist AGC-on
            # (see routes/calibrate.py).
            result["lna_state"] = lna_state
            result["bandwidth_number"] = AGC_FALLBACK_BANDWIDTH_NUMBER
            return result, None, False

        off_ok, off_error = self._agc_fallback.apply(
            top_fc, AGC_BANDWIDTH_OFF, gain_a, gain_b, lna_state)
        if not off_ok:
            return None, (
                "Also tried hardware AGC as a last resort — still no confirmed "
                f"track, and AGC could not be turned back off automatically "
                f"({off_error}); check the Capture config's AGC Bandwidth setting."), False

        self._set_current(tower_index=0, tower_name=top_tower.get("name"),
                          fc=top_fc, gain_a=gain_a, gain_b=gain_b, lna_state=lna_state)
        return None, ("Also tried hardware AGC as a last resort at the "
                      "top-ranked tower — still no confirmed track."), False

    # ── Run loop ───────────────────────────────────────────────

    def _run(self, towers, original, budget_seconds, dwell_seconds, mode):
        result = None
        error = None
        state = "failed"
        no_track_fallback_applied = False
        # Captured the moment towers[0]'s own _descend() returns (below)
        # — the top-ranked tower's own resolved, proven-not-to-overload
        # operating point. Serves both the no-track-anywhere fallback and
        # the AGC last resort's surveillance/LNA values (see module
        # docstring) — None until/unless tower index 0 is actually
        # reached.
        top_tower_resolved = None
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
                                  gain_b=GAIN_REDUCTION_MAX, lna_state=LNA_STATE_MAX)
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
                    if index == 0:
                        top_tower_resolved = (gain_a, gain_b, lna_state)
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

                # A cancel arriving right as the last tower's dwell ended
                # must still take the plain cancellation path (restore
                # original) below, not commit to the slow AGC/top-tower
                # fallback that follows.
                self._check_cancel()

                if towers and top_tower_resolved is not None:
                    top_tower = towers[0]
                    top_fc = int(top_tower["fc"])
                    gain_a, gain_b, lna_state = top_tower_resolved
                    if self._agc_fallback is not None:
                        agc_result, agc_error, agc_cancelled = self._run_agc_fallback(
                            top_tower, top_fc, gain_a, gain_b, lna_state)
                        if agc_result is not None:
                            result = agc_result
                            state = "done"
                            error = None
                        elif agc_cancelled:
                            state = "cancelled"
                            error = "Cancelled by user"
                        else:
                            state = "failed"
                            error = f"{error} {agc_error}" if agc_error else error
                    else:
                        self._apply_top_tower_fallback(top_tower, top_fc, gain_a, gain_b, lna_state)
                    no_track_fallback_applied = True
                # else: towers is empty, or the run's own deadline
                # expired before towers[0]'s descent ever ran (both
                # unreachable in ordinary production use) — fall through
                # to the generic restore-original logic below, since
                # there's no fresh top-tower data to fall back to.

        except _Cancelled:
            state = "cancelled"
            error = "Cancelled by user"
        except CalibrationError as e:
            state = "failed"
            error = str(e)
        except Exception as e:
            state = "failed"
            error = f"Unexpected error: {e}"

        # Restore the original tuning on any non-success outcome, UNLESS
        # the no-track-anywhere fallback above already left blah2 on the
        # top tower's own proven-safe tuning (see module docstring) —
        # applying 'original' on top of that would silently undo it.
        # ignore_cancel is required, not just belt-and-suspenders: a
        # second cancel click while this is in flight must not be able
        # to abort it, or blah2 could be left tuned to the last (failed)
        # candidate.
        if state != "done" and not no_track_fallback_applied:
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
