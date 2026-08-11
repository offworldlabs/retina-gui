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
sidecar's tracker in place (see RetinaTrackerClient.reset()) — mirroring
blah2's own fc-triggered tracker reset. That reset happens at the start of
every *dwell*, and again after a mid-dwell backoff, so a confirmation can
only be earned from frames observed at the tuning it is reported against
(see _reset_tracker). It used to be per-tower, on the reasoning that any
confirmed track ends the search immediately so finer scope could not matter.
That assumed this calibration is the sidecar's only source, which it is not:
tracker_capture's always-on capture shares the same client, so the tracker is
fed throughout the descent too, and a per-tower reset left a confirmed track
waiting before the dwell had observed anything at all.
Evidence grading is coarser than an in-process tracker could offer
(EVIDENCE_NONE/DETECTIONS/ACTIVE only, no tentative/associated distinction)
— the sidecar's events stream only reports confirmed (ACTIVE) tracks, the
same visibility tracker-preview itself has.

Two success modes, with genuinely different dwell strategies:
  - MODE_TRACK (default): any confirmed-track event counts as success (the
    sidecar only ever emits one once a track has been promoted to ACTIVE —
    see retina_tracker/tracker.py::process_frame). No independent way to
    tell "bad gain" from "no aircraft right now", so this mode is
    time-boxed. The two phases are budgeted *separately* (see _run):
    descent runs under its own fixed ceiling (DESCENT_BACKSTOP_SECONDS),
    and only the dwell divides what is left of the run budget across the
    towers still to go. They deliberately do not share one deadline — an
    earlier design gave each tower a single descent+dwell allowance, which
    meant a descent long enough to exhaust it left zero dwell and returned
    "skipped_no_time". On any node whose descent walks more than a few LNA
    states that happened on every tower, so a whole run could be spent
    retuning without ever once watching for an aircraft.
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

Nothing is written to user.yml during a run, and nothing here touches
config-merger or Docker: every setting this engine applies goes through
blah2's live retune protocol (see blah2_client.retune) and is in-memory
only. That keeps the whole run on sub-second HTTP calls and keeps this
module free of any Flask/subprocess/Docker import. Persisting a
successful result is a separate, explicit step (POST /calibrate/apply).

On a genuine user cancellation or an unexpected/CalibrationError
exception, the original tuning is restored, unchanged from before. On
the no-track-anywhere outcome specifically, the device is instead left
on the top-ranked tower's own resolved, already proven-not-to-overload
operating point — not the arbitrary pre-run tuning. That resolved point
already degrades to the safe (max gain reduction, max LNA state) corner
on its own whenever the top tower's own descent never found anything
better — see _descend_reference/_descend_surveillance's own terminal
branches — so there's no separate "is this still safe" check needed here.

A hardware-AGC last resort used to run at this point (one AGC-on attempt
at the top-ranked tower after every manual search had failed). It was
removed: AGC only drives the reference tuner, whose manual descent
already walks to the highest gain that doesn't clip — the same operating
point AGC converges to — while the dominant reason a run fails is simply
that no aircraft was overhead, which AGC cannot influence. Reaching it
also cost two full config-merger + container-recreate cycles inside a
live run, the only Docker coupling this engine had. Users who want
hardware AGC can still set it directly in the Capture config; the AGC
guard in routes/calibrate.py refuses to start a manual search against it.

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
# How long to let a new gain/LNA candidate settle before reading its overload
# state. This does not have to cover the overload *reporting* latency: blah2
# posts an overload-status change as soon as it sees one (its capture thread
# polls the device every 250ms and posts on change), so a candidate that
# actually clips is reported promptly regardless of this value. What this
# covers is the physical settle after the retune. Note that _read_overload
# waits for a report newer than the candidate's appliedAt, and blah2 only
# emits an unchanged state on a 2s heartbeat — so on a *clean* probe that
# heartbeat, not this constant, is usually what bounds the wait.
OVERLOAD_SETTLE_SECONDS = 1.0

# Dwell: how long to wait for a confirmed track at one tuning. No fixed
# default — each tower's share of the overall budget is computed dynamically
# in _run() as (time remaining / towers remaining), so a slow descent or an
# early tower's full-length dwell can't silently starve the towers after it.
DWELL_POLL_SECONDS = 1.0

# How often the dwell re-reads overload state. A single probe reading cannot
# tell a genuinely clean operating point from one that clips intermittently,
# and the dwell is by far the longest phase — so a marginal point accepted by
# descent gets sat on for minutes. Observed on a live node: descent settled at
# lna_state 3, the device then cycled Overload_Detected/Corrected there for the
# whole dwell, and by the end the SDRplay API had stopped answering control
# calls altogether, so every later retune in that run failed. Watching during
# the dwell catches what one probe cannot.
DWELL_OVERLOAD_CHECK_SECONDS = 5.0

# How many times a dwell retreats before giving up on the tower. Each backoff
# costs part of the dwell window, and a point needing several is not one worth
# dwelling on.
MAX_DWELL_BACKOFFS = 2

# MODE_TRACK's retina-tracker feed loop polls faster than blah2's own CPI
# cadence (measured ~0.9-1s on the desk node) so a new detection frame is
# never missed — same cadence retina-tracker's own always-on capture uses
# (tracker_capture.py's POLL_INTERVAL_S). Frames are de-duplicated by
# timestamp, so polling faster than the CPI rate is free, not wasteful.
TRACKER_FEED_POLL_SECONDS = 0.2

# Overall run budget. Kept comfortably below the 20 minutes at which both
# retina-gui's own CALIBRATE_LOCK_TIMEOUT (device_state.py) and blah2-arm's
# watchdog (blah2_rspduo_restart.bash, CALIBRATE_LOCK_TIMEOUT_SECONDS=1200)
# stop treating calibrate.lock as live — past that point the watchdog would
# restart the stack underneath a still-running calibration. Raising this
# above ~20 minutes means raising both of those, in both repos, together.
TOTAL_BUDGET_SECONDS = 900

# Hard per-tower ceiling on the descent phase, whatever the run budget is —
# a backstop against a device that has wedged and is burning the full retune
# timeout on every probe, not a normal operating limit. It sits above the
# ~4.5 minute worst case for a healthy node (one that never overloads walks
# all 9 LNA states, ~10-11 probes each).
DESCENT_BACKSTOP_SECONDS = 300

# The share of a tower's own time slice that descent may consume before it
# is cut off. This is what actually guarantees every tower gets watched:
# descent and dwell are budgeted separately (see _run), but "separately"
# alone is not enough — three towers each taking the full backstop above
# would still swallow a 900s run whole and leave nothing to dwell on,
# which is the original bug wearing a different hat. Capping descent at a
# fraction of the slice means the remainder is always there for the dwell,
# so no tower can ever be tuned and then not looked at.
MAX_DESCENT_FRACTION = 0.7

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
        # Frequency blah2 was last successfully tuned to, so _apply knows
        # when it is about to change fc and must hand over via the safe
        # corner first (see _apply). None until the first successful apply.
        self._last_applied_fc = None
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
        dwell_seconds: fixed per-tower *dwell* window override, mainly for
        tests. Leave as None (the production path) to divide whatever run
        time remains after each tower's descent evenly across the towers
        still to go. Descent is never taken out of this — it runs under its
        own DESCENT_BACKSTOP_SECONDS ceiling.
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
        # Seeded from the device itself at the top of _run, NOT from config —
        # see _seed_last_applied_fc for why the two are not interchangeable.
        self._last_applied_fc = None
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

        A frequency change is preceded by a separate retune to the safe
        corner at the *current* frequency. blah2's driver applies fc before
        gain within one retune (RspDuo::retune does Update_Tuner_Frf, then
        Update_Tuner_Gr), so a single call that moves both would park the
        front end on the new frequency while still at the old one's gain.
        Reaching a new tower from a sensitive operating point therefore
        saturates the device before the attenuation this call is asking for
        ever lands — and the driver returns early on that failure, so the
        gain update is not merely late, it never executes. Worse, once the
        SDRplay API returns ServiceNotResponding (which is what a hard
        overload looks like from out here) it stays that way, so the retry
        cannot get the rescuing attenuation through either.

        Measured directly: 213 MHz at (59, 59, lna 9) is clean when reached
        from a safe state, and saturates when reached from (49, 20, lna 1)
        at 201 MHz — same destination, same requested gain, different
        starting point. Going via the safe corner costs one extra retune
        per tower change and removes the exposure entirely.

        ignore_cancel: used only by the restore-on-failure path, which must
        run to completion even if the user cancels (again) while it's in
        flight — otherwise blah2 could be left tuned to a failed candidate.
        """
        if self._last_applied_fc is not None and int(fc) != self._last_applied_fc:
            self._safe_fc_handover(ignore_cancel=ignore_cancel)
        applied_at = self._apply_tuning(fc, gain_a, gain_b, lna_state,
                                        ignore_cancel=ignore_cancel)
        self._last_applied_fc = int(fc)
        return applied_at

    def _seed_last_applied_fc(self, original_fc):
        """Record the frequency blah2 is *actually* on, before the search
        starts, so _apply can tell a real frequency change from a no-op.

        This must come from the device, not from the merged config. The two
        diverge routinely: a run that finds a track and is never persisted
        leaves the radio on the winning frequency while config.yml still
        holds the old one. Seeding from config there makes the calibrator
        believe it is already on the first tower's frequency when it is not,
        so _apply sees no change and skips the safe-corner handover —
        switching that protection off in precisely the case where the device
        has drifted furthest from what config claims.

        Seen live, and it wedged the radio: config said 545MHz, blah2 was
        actually on 213MHz at (59,44,lna5) from an unpersisted run, and the
        first retune moved fc to a strong local tower at that sensitive gain
        with no handover. Every subsequent retune failed and the whole run
        came back tuning_not_applied in 42 seconds.

        An empty retune status is not ambiguous: it means blah2 has acked no
        retune since it booted, so it is still on the frequency from
        config.yml — which is exactly what the caller passed as `original`.
        """
        status = self._client.get_retune_status()
        if status and status.get("fc") is not None:
            self._last_applied_fc = int(status["fc"])
        else:
            self._last_applied_fc = int(original_fc)

    def _safe_fc_handover(self, ignore_cancel=False):
        """Retune to the safe corner at the frequency currently in use,
        immediately before a frequency change (see _apply).

        Best-effort: a failure here is not raised. If the device won't take
        even this, the caller's own retune is about to surface the problem
        through the normal path, and _probe already treats that as an
        overload at the candidate. Raising here instead would only turn a
        contained per-candidate failure into an aborted run.
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
        # Deliberately does not guess which of the two it was. An
        # unacknowledged retune means either the radar is down, or the
        # front end is so overloaded that the SDRplay API has stopped
        # responding to control calls — from here those look identical,
        # and they need opposite responses from the user. The previous
        # wording ("Is the radar running?") named only the first, which
        # is actively misleading on a strong-signal node where the second
        # is the common case.
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

    def _verify_applied(self, fc, gain_a, gain_b, lna_state):
        """Confirm blah2 is really tuned to this before dwelling on it.

        Returns (applied_at_ms, None) if it is, or (None, reason) if not.

        A retune that fails does not stop the run — _probe folds the failure
        into an overload reading so the descent can retreat — but until now
        nothing checked, before dwelling, whether the tuning the descent
        settled on was ever actually accepted. It could not simply trust
        _descend's applied_at either: when a candidate's own retune never
        completes, _probe returns the *previous* candidate's timestamp, so
        the dwell's `timestamp >= applied_at` guard still passes for
        detections produced by the old tuning. The result was a dwell that
        looked entirely healthy while measuring a different frequency, and
        reported its outcome against the tower it thought it was on.
        Observed live: a dwell ran for minutes labelled WWLP/201MHz while
        blah2 was still on 545MHz.

        Comparing against the last acknowledged retune closes that: it is
        the device's own account of what it is tuned to, so it catches a
        failed retune, a generation blah2 abandoned (see its
        MAX_RETUNE_ATTEMPTS), and anything else that retuned the radio
        behind this run's back.
        """
        status = self._client.get_retune_status()
        if not status:
            return None, "blah2 has not acknowledged any tuning"

        actual = (status.get("fc"), status.get("gainReductionA"),
                  status.get("gainReductionB"), status.get("lnaState"))
        if actual != (fc, gain_a, gain_b, lna_state):
            # blah2_api reports a generation it gave up on (newer blah2 only,
            # absent on older builds) — a more specific reason when present.
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
        # Reset the phase on entry: the surveillance stage below sets it to
        # "refining", and the LNA loop re-enters both stages many times per
        # tower, so without this the UI reports "Refining gain" for most of a
        # descent that is in fact walking the whole ladder again.
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

        deadline: this tower's descent-only ceiling (monotonic clock), a
        backstop against a wedged device rather than a share of the run
        budget — the dwell window is derived separately once this returns
        (see _run). Never exceeded — see
        _descend_reference/_descend_surveillance.

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
               tower_entry):
        """MODE_TRACK's dwell: push live detections to the shared
        retina-tracker sidecar (see module docstring for why — blah2's own
        tracker is not trusted here) and wait for it to emit a confirmed
        (ACTIVE) track event, or dwell_deadline passes. Returns a result
        dict on success, None if the dwell budget expires. (MODE_ADSB uses
        _dwell_adsb instead — see the module docstring.)
        """
        self._update(phase="dwelling")
        # Start from a genuinely empty tracker — see _reset_tracker.
        self._reset_tracker()
        max_evidence = EVIDENCE_NONE
        max_detections = 0
        last_timestamp = None
        backoffs = 0
        next_overload_check = time.monotonic() + DWELL_OVERLOAD_CHECK_SECONDS
        overload_baseline = self._overload_reading()

        while time.monotonic() < dwell_deadline:
            self._check_cancel()

            # A single probe reading cannot distinguish a clean operating
            # point from one that clips intermittently, so keep watching the
            # one we settled on — see DWELL_OVERLOAD_CHECK_SECONDS.
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
                        return None
                    backoffs += 1
                    gain_a, gain_b, lna_state, applied_at = self._dwell_backoff(
                        fc, gain_a, gain_b, lna_state, applied_at,
                        clipped_a, clipped_b, backoffs, tower_entry)
                    # Everything before the retreat was measured at a tuning
                    # we have now abandoned — restart the tracker and the
                    # freshness guard so nothing from it is credited to the
                    # tuning we end up reporting.
                    self._reset_tracker()
                    last_timestamp = None
                    overload_baseline = self._overload_reading()

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
                # Report the tuning actually in effect — a mid-dwell backoff
                # may have moved it since descent resolved (see
                # _dwell_backoff), and history's final_* must agree with the
                # result the user is offered to persist.
                tower_entry["final_gain_a"] = gain_a
                tower_entry["final_gain_b"] = gain_b
                tower_entry["final_lna_state"] = lna_state
                return {
                    "tower_name": tower.get("name"), "fc": fc,
                    "gain_a": gain_a, "gain_b": gain_b,
                    "lna_state": lna_state,
                    "track_id": confirmed.get("track_id"),
                }

            self._maybe_update_best_attempt(tower, fc, gain_a, gain_b, lna_state,
                                            max_evidence, max_detections)
            self._sleep(TRACKER_FEED_POLL_SECONDS)

        tower_entry["outcome"] = "no_confirmed_track"
        tower_entry["max_evidence"] = max_evidence
        tower_entry["max_detections"] = max_detections
        # As on the success path: a mid-dwell backoff may have moved these
        # since descent resolved them, and the no-track fallback reads the
        # top tower's final values to decide where to leave the device.
        tower_entry["final_gain_a"] = gain_a
        tower_entry["final_gain_b"] = gain_b
        tower_entry["final_lna_state"] = lna_state
        return None

    def _reset_tracker(self):
        """Clear the sidecar's tracker and any confirmed event already held.

        Called at the start of every dwell, and again after a mid-dwell
        backoff, so a confirmation can only ever be earned from frames
        observed at the tuning it will be reported against.

        Per-tower reset is not sufficient, and the reasoning that said it was
        assumed this calibration is the sidecar's only source. It isn't:
        tracker_capture's always-on capture shares this same client, because
        the sidecar accepts one connection at a time. So the tracker is fed
        continuously whether or not a dwell is running, and a tower-start
        reset leaves the whole descent — 150s on a live node — for a track to
        accumulate and confirm before the dwell starts. The dwell's first
        check then finds it already waiting and credits it to the resolved
        tuning, having observed none of it. Seen live as a confirmed track
        with dwell_seconds 0.0.

        The applied_at guard does not close this: it only requires the
        event's *latest* detection to post-date the retune, which a track
        built across the descent still satisfies.
        """
        self._tracker_client.reset()
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

        Both signals are needed, because they cover different shapes of the
        same problem and each is blind to the other's:

        - The *level* catches overload that is still happening now. That is
          the persistent case, where a count taken after the onset never
          rises again and so reveals nothing.
        - The *counts* catch episodes that began and ended between two polls.
          This hardware clips and recovers fast enough for that to be the
          normal case — measured on a live node, 9 detect/correct cycles
          inside 90s with every level sample reading false throughout.

        Watching only the level misses oscillation; watching only the counts
        misses a steady overload already in progress when the dwell began.
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
        """Retreat one step toward safety after overload appeared mid-dwell.

        Same direction of travel as the descent's own reverts: more
        attenuation on whichever channel is clipping, and if a channel is
        already at the gain ceiling, one step up the shared LNA axis with
        both gains reset to the ceiling too — moving LNA is not per-channel,
        so its neighbours have to be reproved from the safe end (see
        _descend). Best-effort: if the device will not take the retreat,
        report the values we asked for and let the dwell carry on; the
        caller gives up after MAX_DWELL_BACKOFFS either way.

        Returns the (gain_a, gain_b, lna_state, applied_at) now in effect.
        """
        new_a, new_b, new_lna = gain_a, gain_b, lna_state
        if overload_a:
            new_a = min(gain_a + DESCENT_STEP_DB, GAIN_REDUCTION_MAX)
        if overload_b:
            new_b = min(gain_b + DESCENT_STEP_DB, GAIN_REDUCTION_MAX)
        # Gain alone cannot help a channel already at the ceiling — the
        # clipping is upstream of it, so back the LNA off instead.
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

            # Each gain candidate is its own watch, so each starts from an
            # empty tracker — otherwise a match earned at the previous, more
            # attenuating candidate would be credited to this one. See
            # _reset_tracker.
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

    # ── Run loop ───────────────────────────────────────────────

    def _run(self, towers, original, budget_seconds, dwell_seconds, mode):
        # Must happen before the first retune: the safe-corner handover can
        # only tell a frequency change from a no-op if it knows where the
        # radio actually is. See _seed_last_applied_fc.
        self._seed_last_applied_fc(original["fc"])
        result = None
        error = None
        state = "failed"
        no_track_fallback_applied = False
        # Captured the moment towers[0]'s own _descend() returns (below)
        # — the top-ranked tower's own resolved, proven-not-to-overload
        # operating point, used by the no-track-anywhere fallback (see
        # module docstring). None until/unless tower index 0 is actually
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
                # The sidecar is reset at the start of each *dwell*, not here
                # — see _reset_tracker. Resetting per tower is not enough:
                # this client is shared with tracker_capture's always-on feed
                # (the sidecar accepts one connection), so between a
                # tower-start reset and the dwell the tracker keeps being fed
                # for the whole descent and can confirm a track before the
                # dwell begins.
                # tower_entry stays thread-local until the tower is finished —
                # it is only shared (appended to status history) once the run
                # thread stops mutating it
                tower_entry = {
                    "tower_name": tower.get("name"),
                    "fc": fc,
                    "descent": [],
                    "outcome": "not_reached",
                }

                tower_started = time.monotonic()
                if mode == MODE_ADSB:
                    # Descent is still time-bounded (it's a fast,
                    # traffic-independent overload-avoidance loop) — just not
                    # via a shrinking per-tower share of the overall budget.
                    descent_deadline = tower_started + ADSB_DESCENT_DEADLINE_SECONDS
                    tower_share = None
                else:
                    # This tower's slice of what's left. Computed fresh per
                    # tower so unused time from a quick tower rolls forward
                    # rather than being lost, and so no tower can overrun
                    # into the ones after it.
                    towers_remaining = len(towers) - index
                    time_left = max(run_deadline - tower_started, 0)
                    tower_share = time_left / towers_remaining
                    # Descent may only take part of the slice; the rest is
                    # reserved for the dwell so this tower is always
                    # actually watched. See MAX_DESCENT_FRACTION.
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

                    # Never dwell on tuning the device did not actually take:
                    # the dwell would measure whatever it is really on and
                    # report the answer against this tower. See
                    # _verify_applied.
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
                        # Use the device's own applied-at, not descent's —
                        # descent's can be a previous candidate's timestamp
                        # when a retune failed, which would let stale
                        # detections through the dwell's freshness guard.
                        applied_at = verified_at
                        # Dwell gets the rest of this tower's slice — the
                        # part descent was capped out of. A slow descent
                        # therefore shortens its own dwell but can never
                        # delete it. Fixed dwell_seconds (tests) pins the
                        # window to a known length instead.
                        now = time.monotonic()
                        if dwell_seconds is not None:
                            dwell_deadline = min(now + dwell_seconds, run_deadline)
                        else:
                            dwell_deadline = min(tower_started + tower_share, run_deadline)

                        if dwell_deadline <= now:
                            # The run budget is genuinely exhausted — this
                            # tower was tuned but never actually watched. Say
                            # so, rather than recording a misleading
                            # "checked, nothing there".
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
                    # A mid-dwell backoff moves the operating point after
                    # descent resolved it, so re-read the tower's own final
                    # values here — the no-track fallback leaves the device
                    # on these, and must not restore one already abandoned
                    # for overloading.
                    if index == 0 and tower_entry.get("final_lna_state") is not None:
                        top_tower_resolved = (tower_entry["final_gain_a"],
                                              tower_entry["final_gain_b"],
                                              tower_entry["final_lna_state"])
                    self._append_history(tower_entry)
                    self._update_progress(towers_tried=index + 1)

                if result is not None:
                    # MODE_TRACK's dwell reports its own lna_state, which may
                    # have moved since descent resolved it (see
                    # _dwell_backoff). Only fill it in for a dwell that
                    # didn't — never overwrite it with a stale value.
                    result.setdefault("lna_state", lna_state)
                    state = "done"
                    break

            if result is None and error is None:
                if mode == MODE_ADSB:
                    error = ("No ADS-B-verified track: every candidate tower "
                             "and gain setting was tried, but no confirmed "
                             "track ever matched a real aircraft while one "
                             "was actually in range.")
                else:
                    error = ("No confirmed track found within the time budget. "
                             "This may simply mean no aircraft was overhead "
                             "during this run, not that the tuning is wrong.")

                # A cancel arriving right as the last tower's dwell ended
                # must still take the plain cancellation path (restore
                # original) below, not commit to the top-tower fallback
                # that follows.
                self._check_cancel()

                if towers and top_tower_resolved is not None:
                    top_tower = towers[0]
                    top_fc = int(top_tower["fc"])
                    gain_a, gain_b, lna_state = top_tower_resolved
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
