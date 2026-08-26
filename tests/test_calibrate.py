"""Tests for the Auto-Calibrate feature.

Calibrator logic runs against a scripted FakeBlah2Client (no real HTTP, no
SDR hardware) and a scripted FakeRetinaTrackerClient (no real socket or
background tail thread) — see calibrator.py's module docstring for why
confirmation goes through the shared retina-tracker sidecar rather than an
in-process tracker or blah2's own. Route guards run against the Flask test
client.

FakeRetinaTrackerClient doesn't reimplement retina-tracker's Kalman/GNN
association — it's a controllable stand-in: send_frame() is scripted to
auto-emit a confirmed-track event after `confirm_after` frames (None =
never confirms), and reset() clears that count, mirroring the real
sidecar's RESET wiping accumulated state between candidate towers.
"""
import copy
import json
import os
import time
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
import yaml

import calibrator as calmod
from calibrator import (
    EVIDENCE_DETECTIONS,
    GAIN_REDUCTION_MAX,
    GAIN_REDUCTION_MIN,
    LNA_STATE_MAX,
    LNA_STATE_MIN,
    Calibrator,
)
from device_state import DeviceState


class FakeBlah2Client:
    """Scripted stand-in for Blah2Client.

    Overload behaviour is a rule over the currently-applied tuning (fc,
    gain_a, gain_b, lna_state); detection and adsb_tracks responses are
    callables receiving this client so tests can key them off the current
    tuning or the fake clock.
    """

    def __init__(self, overload_rule=None, detection=None, adsb_tracks=None,
                 retune_fail_rule=None, overload_status_fail_rule=None,
                 transient_overload=False):
        self.clock_ms = 1000
        self.generation = 0
        self.applied = []
        self.retune_error = None
        self.ack_enabled = True
        self.rf_enabled = True
        # Monotonic onset counts, mirroring blah2's own. Real overload is an
        # event stream, not a stable property of the tuning: the device clips
        # and recovers faster than anything polls it.
        self.overload_counts = [0, 0]
        self._last_overload = (False, False)
        # When set, an overload episode is over before anyone can observe it:
        # the reported *level* stays False while the counts still rise. This
        # is what real hardware does (measured: 9 detect/correct cycles in 90s
        # with every level sample reading false), and modelling it is what
        # makes a level-watching consumer fail these tests as it should.
        self.transient_overload = transient_overload
        self.overload_rule = overload_rule or (lambda fc, ga, gb, lna: (False, False))
        self.detection = detection or (lambda client: None)
        self.adsb_tracks = adsb_tracks or (lambda client: None)
        # Simulates a candidate that wedges the device outright rather than
        # cleanly reporting overload — see calibrator.py's _probe/_safe_revert.
        self.retune_fail_rule = retune_fail_rule or (lambda fc, ga, gb, lna: False)
        self.overload_status_fail_rule = overload_status_fail_rule or (lambda fc, ga, gb, lna: False)

    def _now(self):
        self.clock_ms += 10
        return self.clock_ms

    @property
    def current(self):
        return self.applied[-1] if self.applied else None

    def retune(self, fc, gain_a, gain_b, lna_state):
        if self.retune_error:
            return None, self.retune_error
        if self.retune_fail_rule(fc, gain_a, gain_b, lna_state):
            return None, "simulated retune failure"
        self.generation += 1
        self.applied.append({
            "fc": fc, "gain_a": gain_a, "gain_b": gain_b, "lna_state": lna_state,
            "generation": self.generation, "applied_at": self._now(),
        })
        return self.generation, None

    def get_retune_status(self):
        if not self.ack_enabled or not self.applied:
            return {}
        last = self.applied[-1]
        return {
            "generation": last["generation"],
            "fc": last["fc"],
            "gainReductionA": last["gain_a"],
            "gainReductionB": last["gain_b"],
            "lnaState": last["lna_state"],
            "appliedAt": last["applied_at"],
        }

    def get_overload_status(self):
        if not self.rf_enabled or not self.applied:
            return None
        cur = self.applied[-1]
        if self.overload_status_fail_rule(cur["fc"], cur["gain_a"], cur["gain_b"], cur["lna_state"]):
            return None
        overload_a, overload_b = self.overload_rule(
            cur["fc"], cur["gain_a"], cur["gain_b"], cur["lna_state"])
        # Count onsets, exactly as blah2's driver callback does.
        if overload_a and not self._last_overload[0]:
            self.overload_counts[0] += 1
        if overload_b and not self._last_overload[1]:
            self.overload_counts[1] += 1
        self._last_overload = (overload_a, overload_b)
        if self.transient_overload:
            # Episode already over by the time anyone looks — counts rose,
            # level reads clean. A consumer that only samples the level sees
            # nothing at all here.
            self._last_overload = (False, False)
            overload_a = overload_b = False
        return {"overloadA": overload_a, "overloadB": overload_b,
                "timestamp": self._now(),
                "overloadCountA": self.overload_counts[0],
                "overloadCountB": self.overload_counts[1]}

    def get_detection(self):
        return self.detection(self)

    def get_adsb_tracks(self):
        return self.adsb_tracks(self)


class FakeRetinaTrackerClient:
    """Stand-in for RetinaTrackerClient — no real socket or background tail
    thread. Doesn't reimplement retina-tracker's Kalman/GNN association:
    send_frame() just records the frame and, once confirm_after frames have
    been sent since the last reset, synchronously calls every registered
    listener with a confirmed-track event (mirroring what the real sidecar
    would eventually emit via its JSONL stream). confirm_after of None
    means "never confirms" — for tests exercising the no-track path.
    """

    def __init__(self, confirm_after=None, adsb_hex=None, confirm_from_reset=0):
        self.sent_frames = []
        self.reset_calls = 0
        self._listeners = []
        self._confirm_after = confirm_after
        self._adsb_hex = adsb_hex
        self._frame_count = 0
        self._next_track_id = 0
        # Only allow confirmation once this many reset() calls have
        # already happened — each tower's own descent+dwell, and the AGC
        # fallback, each call reset() exactly once before their own
        # dwell begins. Default 0 preserves today's behaviour for every
        # existing call site.
        self._confirm_from_reset = confirm_from_reset

    def send_frame(self, frame):
        self.sent_frames.append(frame)
        self._frame_count += 1
        if (self._confirm_after is not None
                and self._frame_count == self._confirm_after
                and self.reset_calls > self._confirm_from_reset):
            self._next_track_id += 1
            self._emit({
                "track_id": f"T{self._next_track_id}",
                "timestamp": frame.get("timestamp", 0),
                "adsb_hex": self._adsb_hex,
            })

    def reset(self):
        self.reset_calls += 1
        self._frame_count = 0

    def add_listener(self, on_event):
        self._listeners.append(on_event)

    def _emit(self, event):
        for listener in list(self._listeners):
            listener(event)


ORIGINAL = {"fc": 98_000_000, "gain_a": 40, "gain_b": 41, "lna_state": 4}
# Same tuning at a frequency no tower in these tests uses — for the cases
# that script a retune failure at TOWER's own fc. The preflight probes at
# whatever frequency the device is already on (see calibrator._preflight),
# so starting there keeps such a script aimed at the tower it is about,
# rather than at the preflight.
ORIGINAL_ELSEWHERE = dict(ORIGINAL, fc=90_100_000)
TOWER = {"name": "Tower One", "fc": 98_000_000}
TOWER_TWO = {"name": "Tower Two", "fc": 105_100_000}
TOWER_THREE = {"name": "Tower Three", "fc": 213_000_000}


def moving_track_detections(delay=10.0, doppler=50.0, snr=15.0, step_ms=500):
    """A single simulated target, one detection per call, with its own
    timestamp counter advancing step_ms per call — just needs to produce
    real, distinct-timestamp frames for _dwell/_dwell_adsb to forward via
    send_frame(); confirmation itself is scripted separately via
    FakeRetinaTrackerClient's confirm_after."""
    state = {"t": 0}
    def make(client):
        state["t"] += step_ms
        return {"timestamp": state["t"], "delay": [delay],
                "doppler": [doppler], "snr": [snr]}
    return make


def scattered_detections():
    """Real detections every frame, at varying delay/doppler, timestamped
    off the client's own clock (so never stale relative to applied_at, with
    no ramp-up like moving_track_detections' independent counter) — used
    both for tests wanting DETECTIONS evidence without confirmation
    (confirm_after=None) and ADS-B tests wanting a confirmed event on the
    very first poll."""
    points = [(10.0, 50.0), (300.0, -200.0), (75.0, 400.0), (150.0, -50.0)]
    calls = {"n": 0}
    def make(client):
        delay, doppler = points[calls["n"] % len(points)]
        calls["n"] += 1
        return {"timestamp": client._now(), "delay": [delay],
                "doppler": [doppler], "snr": [15.0]}
    return make


def adsb_aircraft_at(delay, doppler, hex_id="ABC123", flight="TEST1"):
    """A single-aircraft /api/adsb2dd response factory, present on every poll
    (never leaves range) — for tests where a match should happen immediately."""
    def make(client):
        return {hex_id: {"hex": hex_id, "flight": flight,
                         "delay": delay, "doppler": doppler}}
    return make


def adsb_aircraft_appears_then_leaves(delay, doppler, present_polls,
                                       hex_id="ABC123", flight="TEST1"):
    """An /api/adsb2dd factory simulating a real aircraft: present for
    `present_polls` calls, then gone for good — for testing MODE_ADSB's
    "seen but never matched, opportunity closes" cycling."""
    calls = {"n": 0}
    def make(client):
        calls["n"] += 1
        if calls["n"] > present_polls:
            return {}
        return {hex_id: {"hex": hex_id, "flight": flight,
                         "delay": delay, "doppler": doppler}}
    return make


@pytest.fixture
def fast(monkeypatch):
    """Shrink all protocol timings so runs finish in milliseconds."""
    monkeypatch.setattr(calmod, "OVERLOAD_SETTLE_SECONDS", 0.01)
    monkeypatch.setattr(calmod, "ACK_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(calmod, "ACK_POLL_SECONDS", 0.005)
    monkeypatch.setattr(calmod, "APPLY_RETRY_DELAY_SECONDS", 0.01)
    monkeypatch.setattr(calmod, "RF_STATUS_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(calmod, "RF_STATUS_POLL_SECONDS", 0.005)
    monkeypatch.setattr(calmod, "DWELL_POLL_SECONDS", 0.01)
    monkeypatch.setattr(calmod, "TRACKER_FEED_POLL_SECONDS", 0.01)
    monkeypatch.setattr(calmod, "SOAK_SECONDS", 0.5)


def run_to_completion(cal, towers, original=ORIGINAL, budget=10, dwell=3.0):
    started, error = cal.start(towers, original, budget_seconds=budget,
                               dwell_seconds=dwell)
    assert started, error
    cal._thread.join(timeout=10)
    assert not cal._thread.is_alive(), "calibration thread did not finish"
    return cal.get_status()


class TestDescent:
    def test_clean_signal_walks_to_gain_floor(self, fast):
        # Starts at the safe corner (59dB, lna_state=9) and walks toward
        # more gain *and* more LNA sensitivity while everything stays
        # clean — lands at the sensitivity floor on both axes rather than
        # stopping early anywhere.
        client = FakeBlah2Client(detection=moving_track_detections())
        tracker_client = FakeRetinaTrackerClient(confirm_after=1)
        status = run_to_completion(Calibrator(client, tracker_client), [TOWER])
        assert status["state"] == "done"
        assert status["result"]["gain_a"] == GAIN_REDUCTION_MIN
        assert status["result"]["gain_b"] == GAIN_REDUCTION_MIN
        assert status["result"]["lna_state"] == LNA_STATE_MIN

        # Prove the walk actually happened lna_state-by-lna_state all the
        # way down from the safe corner (9), not a coincidence of both
        # ends matching.
        descent = status["history"][0]["descent"]
        lna_descent_states = [e["lna_state"] for e in descent if e["phase"] == "lna_descent"]
        assert lna_descent_states == list(range(LNA_STATE_MAX - 1, LNA_STATE_MIN - 1, -1))

    def test_reference_reverts_alone_with_no_refine(self, fast):
        # tuner A overloads below 30 dB reduction; B is always clean.
        # Reference gets no refine step (see module docstring) — it just
        # reverts to the last clean step the moment it overloads. This
        # rule is lna-independent, so the identical pattern repeats at
        # every lna_state the search tries (9 down to 1) before landing
        # on the same final values regardless — lna_state itself isn't
        # asserted here since it always ends at LNA_STATE_MIN in this
        # scenario, same as the plain clean-signal case.
        client = FakeBlah2Client(
            overload_rule=lambda fc, ga, gb, lna: (ga < 30, False),
            detection=moving_track_detections())
        tracker_client = FakeRetinaTrackerClient(confirm_after=1)
        status = run_to_completion(Calibrator(client, tracker_client), [TOWER])
        assert status["state"] == "done"
        # 59 -> 49 -> 39 clean, 29 overloads -> reverts to 39
        assert status["result"]["gain_a"] == 39
        assert status["result"]["gain_b"] == GAIN_REDUCTION_MIN

    def test_surveillance_refine_keeps_lower_gain_when_clean(self, fast):
        # B overloads below 32: descent reverts to 39, refine's 34 stays
        # clean. lna-independent rule, so this repeats identically at
        # every lna_state tried and lands on the same gain_b regardless.
        client = FakeBlah2Client(
            overload_rule=lambda fc, ga, gb, lna: (False, gb < 32),
            detection=moving_track_detections())
        tracker_client = FakeRetinaTrackerClient(confirm_after=1)
        status = run_to_completion(Calibrator(client, tracker_client), [TOWER])
        assert status["state"] == "done"
        assert status["result"]["gain_b"] == 34

    def test_surveillance_refine_reverts_when_it_reoverloads(self, fast):
        # B overloads below 37: descent reverts to 39, refine's 34
        # re-overloads (34 < 37) so it must revert back to 39.
        # lna-independent rule, so this repeats identically at every
        # lna_state tried and lands on the same gain_b regardless.
        client = FakeBlah2Client(
            overload_rule=lambda fc, ga, gb, lna: (False, gb < 37),
            detection=moving_track_detections())
        tracker_client = FakeRetinaTrackerClient(confirm_after=1)
        status = run_to_completion(Calibrator(client, tracker_client), [TOWER])
        assert status["state"] == "done"
        assert status["result"]["gain_b"] == 39

    def test_persistent_overload_stops_at_gain_and_lna_ceiling(self, fast):
        # B overloads no matter what gain or LNA state — even the safety
        # corner itself (59dB, lna_state=9) overloads, so descent must
        # stop right there rather than trying more sensitive LNA states
        # it already knows are worse. There's no ladder to climb in this
        # design: starting at the safe end means "still overloaded at
        # the safe corner" is immediately terminal, not a multi-step
        # escalation — dwell may still proceed regardless ("good, not
        # best").
        client = FakeBlah2Client(
            overload_rule=lambda fc, ga, gb, lna: (False, True),
            detection=moving_track_detections())
        tracker_client = FakeRetinaTrackerClient(confirm_after=1)
        status = run_to_completion(Calibrator(client, tracker_client), [TOWER], dwell=5)
        assert status["state"] == "done"
        assert status["result"]["gain_b"] == GAIN_REDUCTION_MAX
        assert status["result"]["lna_state"] == LNA_STATE_MAX

    def test_descend_stops_at_an_already_past_deadline(self, fast):
        """A deadline in the past must still let reference's first
        settle+read complete (so the returned state stays consistent with
        what was actually applied), but must not attempt surveillance's
        phase or any LNA sensitivity step at all."""
        import time
        client = FakeBlah2Client(
            overload_rule=lambda fc, ga, gb, lna: (ga < 40, False))
        cal = Calibrator(client, FakeRetinaTrackerClient())
        gain_a, gain_b, lna_state, applied_at = cal._descend(
            TOWER["fc"], [], deadline=time.monotonic() - 1)
        # Reference's first (safe-corner) probe at 59dB, lna_state=9
        # doesn't overload (59 >= 40), so it returns immediately without
        # ever touching surveillance — gain_b stays at its own
        # safe-ceiling default, and lna_state stays at its own
        # safe-ceiling default (9), not whatever reference happened to
        # land on.
        assert (gain_a, gain_b, lna_state) == (
            GAIN_REDUCTION_MAX, GAIN_REDUCTION_MAX, LNA_STATE_MAX)
        assert len(client.applied) == 1  # only reference's first candidate

    def test_first_candidate_is_the_fully_safe_corner(self, fast):
        """The headline safety property this fix exists to guarantee: the
        very first candidate ever applied for a fresh tower is the
        fully-safe corner of the whole three-variable search space (max
        gain reduction on both tuners, max LNA state) — never anything
        more sensitive, even for the very first retune of a run."""
        client = FakeBlah2Client(detection=moving_track_detections())
        tracker_client = FakeRetinaTrackerClient(confirm_after=1)
        status = run_to_completion(Calibrator(client, tracker_client), [TOWER])
        assert status["state"] == "done"
        first = client.applied[0]
        assert (first["fc"], first["gain_a"], first["gain_b"], first["lna_state"]) == (
            TOWER["fc"], GAIN_REDUCTION_MAX, GAIN_REDUCTION_MAX, LNA_STATE_MAX)


class TestLnaDescent:
    def test_lna_descent_reverts_when_reference_cant_clear_it(self, fast):
        """Reference only clears once lna_state >= 3, regardless of gain;
        surveillance never overloads on its own. Under the "redo both
        channels at every more-sensitive LNA step" design, surveillance's
        descent is freshly repeated at every lna_state tried (9 down to
        2, inclusive of the level that ultimately fails) even though it's
        reference, not surveillance, that trips the revert — proving
        there's no "only redo the triggering channel" shortcut in this
        direction (see module docstring: that optimisation only ever
        applied to descending toward *more* attenuation, never toward
        more sensitivity)."""
        client = FakeBlah2Client(
            overload_rule=lambda fc, ga, gb, lna: (lna < 3, False),
            detection=moving_track_detections())
        tracker_client = FakeRetinaTrackerClient(confirm_after=1)
        status = run_to_completion(Calibrator(client, tracker_client), [TOWER])
        assert status["state"] == "done"
        assert status["result"]["gain_a"] == GAIN_REDUCTION_MIN  # clean once lna=3, walks to floor
        assert status["result"]["gain_b"] == GAIN_REDUCTION_MIN
        assert status["result"]["lna_state"] == 3

        descent = status["history"][0]["descent"]
        surveillance_entries = [e for e in descent if e.get("phase") == "surveillance"]
        surveillance_lna_states = {e["lna_state"] for e in surveillance_entries}
        # Surveillance is freshly redone at every lna_state tried,
        # including lna_state=2 (where reference ultimately fails and the
        # whole triple reverts) — there's no "skip the channel that
        # didn't trigger this step" shortcut when moving toward more
        # sensitivity.
        assert surveillance_lna_states == set(range(2, LNA_STATE_MAX + 1))

        revert_entry = next(e for e in descent if e["phase"] == "lna_descent_revert")
        assert revert_entry["lna_state"] == 3
        assert revert_entry["reverted_from_lna_state"] == 2

    def test_persistent_reference_overload_stops_at_lna_ceiling_immediately(self, fast):
        """A overloads no matter what gain or LNA state — even the safety
        corner (59dB, lna_state=9) itself overloads, so the search must
        stop right there rather than trying more sensitive LNA states it
        already knows are worse. There's no ladder to climb anymore: the
        search starts at the safe end and only ever moves toward more
        sensitivity, so "no clean value even at the safe corner" is
        immediately terminal, not a multi-step escalation."""
        client = FakeBlah2Client(
            overload_rule=lambda fc, ga, gb, lna: (True, False),
            detection=moving_track_detections())
        tracker_client = FakeRetinaTrackerClient(confirm_after=1)
        status = run_to_completion(Calibrator(client, tracker_client), [TOWER], dwell=5)
        assert status["state"] == "done"
        assert status["result"]["gain_a"] == GAIN_REDUCTION_MAX
        assert status["result"]["lna_state"] == LNA_STATE_MAX

    def test_lna_descent_reverts_partway_to_last_proven_safe_triple(self, fast):
        """B overloads unconditionally once lna_state drops below 6 (any
        gain) — descent walks cleanly to the sensitivity floor at
        lna_state=9,8,7,6, then trying lna_state=5 immediately overloads
        surveillance at the safety ceiling, so the whole (gain_a,
        gain_b, lna_state) triple must revert to the last state fully
        proven clean (the floor at lna_state=6), not just surveillance's
        own gain."""
        client = FakeBlah2Client(
            overload_rule=lambda fc, ga, gb, lna: (False, lna < 6),
            detection=moving_track_detections())
        tracker_client = FakeRetinaTrackerClient(confirm_after=1)
        status = run_to_completion(Calibrator(client, tracker_client), [TOWER])
        assert status["state"] == "done"
        assert status["result"]["gain_a"] == GAIN_REDUCTION_MIN
        assert status["result"]["gain_b"] == GAIN_REDUCTION_MIN
        assert status["result"]["lna_state"] == 6

        descent = status["history"][0]["descent"]
        revert_entry = next(e for e in descent if e["phase"] == "lna_descent_revert")
        assert revert_entry["gain_a"] == GAIN_REDUCTION_MIN
        assert revert_entry["gain_b"] == GAIN_REDUCTION_MIN
        assert revert_entry["lna_state"] == 6
        assert revert_entry["reverted_from_lna_state"] == 5

    def test_lna_descent_redoes_reference_even_when_surveillance_is_the_one_that_breaks(self, fast):
        """Mirror of test_lna_descent_reverts_when_reference_cant_clear_it:
        reference never overloads on its own, but B overloads
        unconditionally once lna_state drops below 4. Reference's descent
        must still be freshly redone at every lna_state tried, including
        the one where it's surveillance (not reference) that ultimately
        overloads and triggers the whole-triple revert — proving
        reference isn't assumed-safe just because it wasn't the channel
        that broke."""
        client = FakeBlah2Client(
            overload_rule=lambda fc, ga, gb, lna: (False, lna < 4),
            detection=moving_track_detections())
        tracker_client = FakeRetinaTrackerClient(confirm_after=1)
        status = run_to_completion(Calibrator(client, tracker_client), [TOWER])
        assert status["state"] == "done"
        assert status["result"]["gain_a"] == GAIN_REDUCTION_MIN
        assert status["result"]["gain_b"] == GAIN_REDUCTION_MIN
        assert status["result"]["lna_state"] == 4

        descent = status["history"][0]["descent"]
        reference_entries = [e for e in descent if e.get("phase") == "reference"]
        reference_lna_states = {e["lna_state"] for e in reference_entries}
        # Reference is freshly redone at every lna_state tried, including
        # lna_state=3 (where surveillance ultimately fails and the whole
        # triple reverts) — proving reference isn't skipped just because
        # it wasn't the channel that triggered the revert.
        assert reference_lna_states == set(range(3, LNA_STATE_MAX + 1))

        revert_entry = next(e for e in descent if e["phase"] == "lna_descent_revert")
        assert revert_entry["lna_state"] == 4
        assert revert_entry["reverted_from_lna_state"] == 3


class TestDeviceCrashHandling:
    """On real hardware, a bad gain candidate doesn't always just report
    overload cleanly — it can wedge the SDRplay device outright, surfacing
    as a retune/overload-status failure instead. See calibrator.py's
    _probe/_safe_revert: these failures are folded into the same
    overload-handling branches as a clean overload reading, rather than
    aborting the whole multi-tower run.
    """

    def test_retune_failure_reverts_to_last_clean(self, fast):
        # 59 -> 49 clean, 39's retune itself fails outright (device wedge,
        # not a clean overload reading) -> reverts to 49.
        client = FakeBlah2Client(
            retune_fail_rule=lambda fc, ga, gb, lna: ga == 39,
            detection=moving_track_detections())
        tracker_client = FakeRetinaTrackerClient(confirm_after=1)
        status = run_to_completion(Calibrator(client, tracker_client), [TOWER])
        assert status["state"] == "done"
        assert status["result"]["gain_a"] == 49

        descent = status["history"][0]["descent"]
        failed_entry = next(e for e in descent if e.get("gain_a") == 39)
        assert failed_entry["device_error"] is True
        assert status["history"][0]["device_error"] is True

    def test_overload_status_failure_reverts_to_last_clean(self, fast):
        # Same shape, but the retune itself acks fine and it's the
        # subsequent overload-status read that goes quiet — exercises
        # _probe's other propagation point distinctly from the retune-ack one.
        client = FakeBlah2Client(
            overload_status_fail_rule=lambda fc, ga, gb, lna: ga == 39,
            detection=moving_track_detections())
        tracker_client = FakeRetinaTrackerClient(confirm_after=1)
        status = run_to_completion(Calibrator(client, tracker_client), [TOWER])
        assert status["state"] == "done"
        assert status["result"]["gain_a"] == 49

        descent = status["history"][0]["descent"]
        failed_entry = next(e for e in descent if e.get("gain_a") == 39)
        assert failed_entry["device_error"] is True

    def test_revert_failure_is_graceful_not_fatal(self, fast):
        """Mirrors the real incident: once the device wedges it stays
        wedged (needs a manual out-of-band restart, not just a different
        gain value) — so the revert-of-the-revert itself also fails. Must
        still reach an ordinary terminal state, not surface a raw
        'Retune failed' run-ending error."""
        wedged = {"on": False}
        def retune_fail_rule(fc, ga, gb, lna):
            if ga <= 39:
                wedged["on"] = True
            return wedged["on"]
        client = FakeBlah2Client(retune_fail_rule=retune_fail_rule)
        tracker_client = FakeRetinaTrackerClient()  # never confirms
        status = run_to_completion(Calibrator(client, tracker_client), [TOWER])
        assert status["state"] == "failed"
        assert "Retune failed" not in status["error"]
        assert status["history"][0]["device_error"] is True

    def test_device_crash_at_one_tower_does_not_abort_whole_run(self, fast):
        """The regression test for the actual reported bug: today, any
        retune/overload-status failure anywhere propagates uncaught all the
        way to _run()'s outer handler and ends the entire multi-tower run.
        Tower one's frequency always fails to retune; tower two is
        completely normal and confirms a track — the run must fall through
        to tower two, not die after tower one."""
        tower_two_track = moving_track_detections()
        def detection(client):
            if client.current and client.current["fc"] == TOWER_TWO["fc"]:
                return tower_two_track(client)
            return None
        client = FakeBlah2Client(
            retune_fail_rule=lambda fc, ga, gb, lna: fc == TOWER["fc"],
            detection=detection)
        tracker_client = FakeRetinaTrackerClient(confirm_after=1)
        # Started from a frequency that still works, so the preflight's own
        # safe-corner probe passes and the run actually reaches the towers.
        # A device that cannot be tuned even where it already sits is a
        # different fault, and _preflight ends the run on it deliberately —
        # see TestPreflight.
        status = run_to_completion(Calibrator(client, tracker_client),
                                   [TOWER, TOWER_TWO], original=ORIGINAL_ELSEWHERE)
        assert status["state"] == "done"
        assert status["result"]["tower_name"] == "Tower Two"
        assert len(status["history"]) == 2
        assert status["history"][0].get("device_error") is True

    def test_device_error_during_lna_descent_loop_reverts_gracefully(self, fast):
        """A device-crash-style failure (retune never acks) specifically
        at lna_state=7 — after two clean, successful LNA-descent steps (9,
        8) have already completed — must compose with the new outer loop
        exactly like an ordinary overload reading: revert the whole
        triple to the last proven-safe state (the floor at lna_state=8)
        and stop trying more sensitivity, without aborting the tower's
        run. Distinct from this class's other tests, which only exercise
        a device error during the very first (lna_state=9) pass."""
        client = FakeBlah2Client(
            retune_fail_rule=lambda fc, ga, gb, lna: lna == 7,
            detection=moving_track_detections())
        tracker_client = FakeRetinaTrackerClient(confirm_after=1)
        status = run_to_completion(Calibrator(client, tracker_client), [TOWER])
        assert status["state"] == "done"
        assert status["result"]["gain_a"] == GAIN_REDUCTION_MIN
        assert status["result"]["gain_b"] == GAIN_REDUCTION_MIN
        assert status["result"]["lna_state"] == 8

        descent = status["history"][0]["descent"]
        failed_entries = [e for e in descent if e.get("lna_state") == 7 and e.get("device_error")]
        assert failed_entries, "expected a device_error entry logged at lna_state=7"

        revert_entry = next(e for e in descent if e["phase"] == "lna_descent_revert")
        assert revert_entry["lna_state"] == 8
        assert revert_entry["reverted_from_lna_state"] == 7
        assert revert_entry.get("device_error") is not True  # the revert itself succeeded


class TestDwell:
    def test_success_leaves_blah2_on_winner(self, fast):
        client = FakeBlah2Client(detection=moving_track_detections())
        tracker_client = FakeRetinaTrackerClient(confirm_after=1)
        status = run_to_completion(Calibrator(client, tracker_client), [TOWER])
        assert status["state"] == "done"
        assert status["result"]["track_id"] is not None
        assert status["result"]["tower_name"] == "Tower One"
        # no restore: last applied tuning is the winner, not the original
        assert client.current["fc"] == TOWER["fc"]
        assert client.current["gain_a"] == status["result"]["gain_a"]

    def test_no_track_anywhere_leaves_top_tower_at_its_resolved_gain(self, fast):
        """A run that genuinely finds no track anywhere no longer restores
        the arbitrary pre-run 'original' tuning — it leaves the device on
        the top-ranked tower's own resolved (gain_a, gain_b, lna_state)
        from that tower's own manual descent. Nothing ever overloads here,
        so that resolved point is the sensitivity floor, not a fixed
        "safe corner" — proving the fallback reuses the real resolved
        value rather than a separately-hardcoded one."""
        client = FakeBlah2Client()  # no detections at all — never confirms
        status = run_to_completion(Calibrator(client, FakeRetinaTrackerClient()), [TOWER])
        assert status["state"] == "failed"
        assert "No confirmed track" in status["error"]
        assert client.current["fc"] == TOWER["fc"]
        assert client.current["gain_a"] == GAIN_REDUCTION_MIN
        assert client.current["gain_b"] == GAIN_REDUCTION_MIN
        assert client.current["lna_state"] == LNA_STATE_MIN
        # status must reflect the real hardware state, not the arbitrary
        # pre-run tuning and not a stale mid-run candidate
        assert status["current"]["fc"] == TOWER["fc"]
        assert status["current"]["gain_a"] == GAIN_REDUCTION_MIN
        assert status["current"]["gain_b"] == GAIN_REDUCTION_MIN
        assert status["current"]["lna_state"] == LNA_STATE_MIN

    def test_cancel_restores_original(self, fast):
        import time
        client = FakeBlah2Client()
        cal = Calibrator(client, FakeRetinaTrackerClient())
        started, _ = cal.start([TOWER], ORIGINAL, budget_seconds=30,
                               dwell_seconds=30)
        assert started
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if cal.get_status()["phase"] == "dwelling":
                break
            time.sleep(0.01)
        cal.cancel()
        cal._thread.join(timeout=10)
        status = cal.get_status()
        assert status["state"] == "cancelled"
        assert client.current["fc"] == ORIGINAL["fc"]
        assert client.current["gain_a"] == ORIGINAL["gain_a"]

    def test_stale_detection_data_is_ignored(self, fast):
        # a detection with a timestamp older than the retune — pre-retune
        # data must not count toward confirmation, even if a confirming
        # tracker client is wired up.
        def stale_detection(client):
            return {"timestamp": 1, "delay": [10.0], "doppler": [50.0], "snr": [15.0]}
        client = FakeBlah2Client(detection=stale_detection)
        status = run_to_completion(
            Calibrator(client, FakeRetinaTrackerClient(confirm_after=1)), [TOWER])
        assert status["state"] == "failed"

    def test_best_attempt_records_detection_evidence(self, fast):
        client = FakeBlah2Client(detection=scattered_detections())
        status = run_to_completion(
            Calibrator(client, FakeRetinaTrackerClient()), [TOWER])
        assert status["state"] == "failed"
        best = status["best_attempt"]
        assert best["evidence"] >= EVIDENCE_DETECTIONS
        assert best["max_detections"] >= 1
        assert best["lna_state"] == LNA_STATE_MIN

    def test_best_attempt_records_the_lna_state_in_effect(self, fast):
        # A only clears once lna_state >= 2, so best_attempt recorded while
        # dwelling must reflect the escalated state, not the starting one.
        client = FakeBlah2Client(
            overload_rule=lambda fc, ga, gb, lna: (lna < 2, False),
            detection=scattered_detections())
        status = run_to_completion(
            Calibrator(client, FakeRetinaTrackerClient()), [TOWER])
        assert status["state"] == "failed"
        assert status["best_attempt"]["lna_state"] == 2


class TestMultiTower:
    def test_falls_through_to_second_tower(self, fast):
        # only Tower Two's frequency ever produces a confirmable target
        tower_two_track = moving_track_detections()
        def detection(client):
            if client.current and client.current["fc"] == TOWER_TWO["fc"]:
                return tower_two_track(client)
            return None
        client = FakeBlah2Client(detection=detection)
        tracker_client = FakeRetinaTrackerClient(confirm_after=1)
        status = run_to_completion(Calibrator(client, tracker_client), [TOWER, TOWER_TWO])
        assert status["state"] == "done"
        assert status["result"]["tower_name"] == "Tower Two"
        assert len(status["history"]) == 2
        assert status["history"][0]["outcome"] == "no_confirmed_track"
        assert status["history"][1]["outcome"] == "confirmed_track"
        # a fresh geometry gets its own reset before each tower's search
        assert tracker_client.reset_calls == 2

    def test_dynamic_dwell_splits_budget_fairly_across_towers(self, fast):
        """Without a dwell_seconds override (the production path), each
        tower's dwell comes out of the remaining budget divided by the
        remaining towers — no fixed-per-tower window that could overrun,
        and no tower silently starved to zero."""
        client = FakeBlah2Client()
        cal = Calibrator(client, FakeRetinaTrackerClient())
        # A default-clean descent now walks the whole LNA ladder to the
        # floor (~90 retune+settle cycles across all 9 lna_states, not
        # ~10 at one) before any dwelling happens — comfortably more
        # budget than before keeps this clear of scheduling jitter.
        started, error = cal.start([TOWER, TOWER_TWO], ORIGINAL, budget_seconds=6.0)
        assert started, error
        cal._thread.join(timeout=10)
        status = cal.get_status()
        assert status["state"] == "failed"
        assert len(status["history"]) == 2
        for entry in status["history"]:
            assert entry["outcome"] == "no_confirmed_track"
            assert entry.get("dwell_seconds", 0) > 0

    def test_long_descent_no_longer_starves_its_own_dwell(self, fast, monkeypatch):
        """Descent and dwell are budgeted separately, so a descent long
        enough to have exhausted a shared per-tower allowance still leaves
        the tower an actual dwell.

        Regression: descent and dwell used to share one per-tower deadline
        (remaining budget / remaining towers). On any node whose descent
        walks more than a few LNA states that deadline expired mid-descent,
        so every tower came back 'skipped_no_time' and a whole run could be
        spent retuning without ever watching for an aircraft once. Measured
        on real hardware: ~148s of descent against a 120s per-tower share.
        """
        # A clean node is the expensive case: nothing ever overloads, so the
        # descent walks the entire LNA ladder (9 states, both tuners each).
        monkeypatch.setattr(calmod, "OVERLOAD_SETTLE_SECONDS", 0.02)
        client = FakeBlah2Client()
        cal = Calibrator(client, FakeRetinaTrackerClient())
        started, error = cal.start([TOWER, TOWER_TWO], ORIGINAL, budget_seconds=6.0)
        assert started, error
        cal._thread.join(timeout=20)
        status = cal.get_status()

        first = status["history"][0]
        # Descent ran to its natural end rather than being cut off...
        assert first["final_lna_state"] == LNA_STATE_MIN
        assert any(e.get("lna_state") == LNA_STATE_MIN for e in first["descent"])
        # ...and the dwell it used to starve actually happened.
        assert first["outcome"] == "no_confirmed_track"
        assert first.get("dwell_seconds", 0) > 0

    def test_every_tower_is_watched_even_when_every_descent_is_slow(self, fast, monkeypatch):
        """The guarantee MAX_DESCENT_FRACTION buys: no tower is ever tuned
        and then not looked at.

        Budgeting descent and dwell separately is not sufficient on its own
        — descents long enough to each hit their own ceiling would still
        consume the whole run between them. Capping descent at a fraction
        of each tower's slice is what makes a dwell unconditional.
        """
        monkeypatch.setattr(calmod, "OVERLOAD_SETTLE_SECONDS", 0.02)
        client = FakeBlah2Client()
        cal = Calibrator(client, FakeRetinaTrackerClient())
        # Deliberately tight: a full clean descent wants more than any one
        # tower's slice here, so every tower's descent gets cut short.
        started, error = cal.start([TOWER, TOWER_TWO, TOWER_THREE], ORIGINAL,
                                   budget_seconds=3.0)
        assert started, error
        cal._thread.join(timeout=20)
        status = cal.get_status()

        assert len(status["history"]) == 3
        for entry in status["history"]:
            assert entry["outcome"] == "no_confirmed_track", (
                f"{entry['tower_name']} was tuned but never watched")
            assert entry.get("dwell_seconds", 0) > 0

    def test_descent_backstop_bounds_descent_without_killing_dwell(self, fast, monkeypatch):
        """The descent ceiling exists to contain a wedged device, not to
        ration dwell: hitting it truncates the search but the tower is
        still watched, because the dwell window is derived afterwards from
        the run budget that remains."""
        monkeypatch.setattr(calmod, "OVERLOAD_SETTLE_SECONDS", 0.02)
        monkeypatch.setattr(calmod, "DESCENT_BACKSTOP_SECONDS", 0.05)
        client = FakeBlah2Client()
        cal = Calibrator(client, FakeRetinaTrackerClient())
        started, error = cal.start([TOWER], ORIGINAL, budget_seconds=5.0)
        assert started, error
        cal._thread.join(timeout=20)
        status = cal.get_status()

        entry = status["history"][0]
        # Truncated well before the ladder's end...
        assert entry["final_lna_state"] > LNA_STATE_MIN
        # ...yet still dwelt, on budget the descent never got to consume.
        assert entry["outcome"] == "no_confirmed_track"
        assert entry.get("dwell_seconds", 0) > 0

    def test_slow_descent_yields_honest_skipped_outcome(self, fast):
        """If a tower's own descent consumes its whole share of the budget,
        it must be marked as never actually watched — not as a false
        'checked, nothing there'."""
        # Every candidate overloads on A, forcing repeated backoff — with a
        # tiny total budget the first tower's descent alone exceeds its share.
        client = FakeBlah2Client(overload_rule=lambda fc, ga, gb, lna: (True, False))
        cal = Calibrator(client, FakeRetinaTrackerClient())
        started, error = cal.start([TOWER, TOWER_TWO], ORIGINAL, budget_seconds=0.015)
        assert started, error
        cal._thread.join(timeout=10)
        status = cal.get_status()
        assert status["state"] == "failed"
        assert any(e["outcome"] == "skipped_no_time" for e in status["history"])


class TestTuningVerifiedBeforeDwell:
    """A dwell must never run against tuning the device did not take.

    Regression: when a candidate's retune failed, _probe returned the
    *previous* candidate's applied_at, so the dwell's freshness guard still
    passed for detections produced by the old tuning. The dwell looked
    healthy while measuring a different frequency and reported the answer
    against the tower it thought it was on. Observed live: minutes of dwell
    labelled WWLP/201MHz while blah2 was still tuned to 545MHz.
    """

    def test_tower_whose_retune_never_applied_is_not_dwelt_on(self, fast):
        # blah2 refuses this tower's frequency outright, so nothing the
        # descent asks for is ever actually applied there.
        client = FakeBlah2Client(
            retune_fail_rule=lambda fc, ga, gb, lna: fc == TOWER_TWO["fc"])
        tracker_client = FakeRetinaTrackerClient(confirm_after=1)
        cal = Calibrator(client, tracker_client)
        status = run_to_completion(cal, [TOWER, TOWER_TWO], dwell=0.3)

        second = status["history"][1]
        assert second["outcome"] == "tuning_not_applied", (
            f"dwelt on tuning that was never applied: {second}")
        assert "tuning_error" in second
        assert second.get("dwell_seconds") is None, "should not have dwelt at all"

    def test_verified_tuning_still_dwells_normally(self, fast):
        client = FakeBlah2Client(detection=moving_track_detections())
        cal = Calibrator(client, FakeRetinaTrackerClient(confirm_after=1))
        status = run_to_completion(cal, [TOWER], dwell=1.0)
        assert status["state"] == "done"
        assert status["history"][0]["outcome"] == "confirmed_track"


class TestDwellOverloadBackoff:
    """Descent takes one overload sample per candidate, which cannot tell a
    clean operating point from one that clips intermittently. The dwell is
    the long phase, so an unstable point accepted by descent is sat on for
    minutes — observed live degrading the SDRplay API until every later
    retune in the run failed. The dwell therefore keeps watching."""

    def test_overload_during_dwell_retreats_toward_safety(self, fast, monkeypatch):
        monkeypatch.setattr(calmod, "DWELL_OVERLOAD_CHECK_SECONDS", 0.02)
        # Clean while descending, but the resolved point clips once dwelling.
        state = {"dwelling": False}

        def overload_rule(fc, ga, gb, lna):
            return (state["dwelling"] and gb < GAIN_REDUCTION_MAX, False)

        client = FakeBlah2Client(overload_rule=overload_rule)
        cal = Calibrator(client, FakeRetinaTrackerClient())
        original_dwell = cal._dwell

        def dwell(*a, **kw):
            state["dwelling"] = True
            return original_dwell(*a, **kw)

        cal._dwell = dwell
        status = run_to_completion(cal, [TOWER], dwell=2.0)

        entry = status["history"][0]
        backoffs = entry.get("dwell_backoffs") or []
        assert backoffs, "overload during dwell was not acted on"
        first = backoffs[0]
        # Retreat must move toward *more* attenuation, never less.
        assert (first["to"]["gain_a"] >= first["from"]["gain_a"]
                or first["to"]["lna_state"] > first["from"]["lna_state"])

    def test_persistent_dwell_overload_gives_up_on_the_tower(self, fast, monkeypatch):
        monkeypatch.setattr(calmod, "DWELL_OVERLOAD_CHECK_SECONDS", 0.02)
        state = {"dwelling": False}
        # Overloads at every setting once dwelling — no retreat can help.
        client = FakeBlah2Client(
            overload_rule=lambda fc, ga, gb, lna: (state["dwelling"], False))
        cal = Calibrator(client, FakeRetinaTrackerClient())
        original_dwell = cal._dwell

        def dwell(*a, **kw):
            state["dwelling"] = True
            return original_dwell(*a, **kw)

        cal._dwell = dwell
        status = run_to_completion(cal, [TOWER], dwell=3.0)

        entry = status["history"][0]
        assert entry["outcome"] == "unstable_overload"
        assert len(entry.get("dwell_backoffs") or []) == calmod.MAX_DWELL_BACKOFFS

    def test_transient_overload_is_caught_even_though_the_level_reads_clean(
            self, fast, monkeypatch):
        """The case that matters, and the one a level-watching dwell misses.

        Real overload episodes end faster than anyone polls: measured on a
        live node, 9 detect/correct cycles inside 90s with every sample of
        the level reading false throughout. A dwell that samples the flags
        therefore sat on a visibly unstable operating point and never once
        noticed. Comparing blah2's monotonic onset counts cannot miss an
        episode regardless of poll rate.
        """
        monkeypatch.setattr(calmod, "DWELL_OVERLOAD_CHECK_SECONDS", 0.02)
        state = {"dwelling": False}
        client = FakeBlah2Client(
            overload_rule=lambda fc, ga, gb, lna: (state["dwelling"], False),
            transient_overload=True)
        cal = Calibrator(client, FakeRetinaTrackerClient())
        original_dwell = cal._dwell

        def dwell(*a, **kw):
            state["dwelling"] = True
            return original_dwell(*a, **kw)

        cal._dwell = dwell
        status = run_to_completion(cal, [TOWER], dwell=2.0)

        entry = status["history"][0]
        # The level never reads True, so this only passes by counting.
        assert entry.get("dwell_backoffs"), (
            "transient overload went unnoticed - the dwell is watching the "
            "level instead of the onset counts")

    def test_stable_dwell_never_backs_off(self, fast, monkeypatch):
        monkeypatch.setattr(calmod, "DWELL_OVERLOAD_CHECK_SECONDS", 0.02)
        client = FakeBlah2Client()  # never overloads
        cal = Calibrator(client, FakeRetinaTrackerClient())
        status = run_to_completion(cal, [TOWER], dwell=1.0)
        entry = status["history"][0]
        assert entry["outcome"] == "no_confirmed_track"
        assert not entry.get("dwell_backoffs")


class TestSafeFrequencyHandover:
    """blah2's driver applies fc before gain within one retune, so moving to
    a new tower directly from a sensitive operating point parks the front end
    on the new frequency at the old tower's gain. Verified on real hardware:
    213 MHz at (59,59,lna 9) is clean from a safe state and saturates from
    (49,20,lna 1) at 201 MHz — same destination, different origin. Every fc
    change must therefore hand over via the safe corner first.
    """

    def test_frequency_change_is_preceded_by_the_safe_corner(self, fast):
        client = FakeBlah2Client()
        cal = Calibrator(client, FakeRetinaTrackerClient())
        run_to_completion(cal, [TOWER, TOWER_TWO], dwell=0.05)

        # Every retune that changed fc must be immediately preceded by one
        # at the previous fc sitting at the safe corner.
        applied = client.applied
        changes = [i for i in range(1, len(applied))
                   if applied[i]["fc"] != applied[i - 1]["fc"]]
        assert changes, "expected at least one frequency change"
        for i in changes:
            previous = applied[i - 1]
            assert (previous["gain_a"], previous["gain_b"], previous["lna_state"]) == (
                GAIN_REDUCTION_MAX, GAIN_REDUCTION_MAX, LNA_STATE_MAX), (
                f"fc changed to {applied[i]['fc']} from an unsafe tuning: {previous}")

    def test_handover_happens_when_the_device_has_drifted_from_config(self, fast):
        """The handover must key off where the radio actually is, not where
        config says it is.

        Regression, seen live and it wedged the radio: a previous run had
        found a track at 213MHz and was never persisted, so config.yml still
        said 545MHz while blah2 sat on 213MHz at a sensitive gain. Seeding
        from config made the calibrator believe it was already on the first
        tower's frequency, so it saw no change, skipped the handover, and
        moved fc into a strong local tower at that gain. Every retune after
        that failed and the run died in 42s.
        """
        client = FakeBlah2Client()
        # The radio is genuinely somewhere else — as after an unpersisted run.
        client.retune(TOWER_TWO["fc"], 59, 44, 5)
        cal = Calibrator(client, FakeRetinaTrackerClient())
        drifted = len(client.applied)

        # `original` reports the *config* frequency, which equals the first
        # tower's — so a config-seeded handover would think nothing changed.
        run_to_completion(cal, [TOWER], original=ORIGINAL, dwell=0.1)

        first = client.applied[drifted]
        assert first["fc"] == TOWER_TWO["fc"], (
            "first retune should be the safe-corner handover at the "
            f"device's real fc, got {first}")
        assert (first["gain_a"], first["gain_b"], first["lna_state"]) == (
            GAIN_REDUCTION_MAX, GAIN_REDUCTION_MAX, LNA_STATE_MAX), (
            f"handover must be at the safe corner, got {first}")

    def test_gain_only_steps_do_not_add_a_handover(self, fast):
        """The handover is for frequency changes only — a descent's own
        gain steps share one fc and must not pay for an extra retune each."""
        client = FakeBlah2Client()
        cal = Calibrator(client, FakeRetinaTrackerClient())
        run_to_completion(cal, [TOWER], dwell=0.05)

        # applied[0] is the preflight's own safe-corner probe, which lands on
        # the same tuning the first descent candidate then asks for. That
        # duplicate is the liveness check, not a handover, so the search's
        # own retunes are what this assertion is about.
        applied = client.applied[1:]
        safe_corner = (GAIN_REDUCTION_MAX, GAIN_REDUCTION_MAX, LNA_STATE_MAX)
        # Only the descent's own legitimate visits to the safe corner should
        # appear — no consecutive duplicate pair from a spurious handover.
        for i in range(1, len(applied)):
            a, b = applied[i - 1], applied[i]
            if a["fc"] == b["fc"]:
                assert not (
                    (a["gain_a"], a["gain_b"], a["lna_state"]) == safe_corner
                    and (b["gain_a"], b["gain_b"], b["lna_state"]) == safe_corner), (
                    f"redundant safe-corner retune at unchanged fc: {a} -> {b}")

    def test_handover_failure_does_not_abort_the_run(self, fast):
        """A device that won't even take the handover is not a fatal
        condition — the caller's own retune surfaces it through the normal
        per-candidate path instead."""
        # Fail only the safe-corner retune at the first tower's frequency.
        def fail_handover(fc, ga, gb, lna):
            return (fc == TOWER["fc"] and (ga, gb, lna)
                    == (GAIN_REDUCTION_MAX, GAIN_REDUCTION_MAX, LNA_STATE_MAX))

        client = FakeBlah2Client(retune_fail_rule=fail_handover)
        cal = Calibrator(client, FakeRetinaTrackerClient())
        # Elsewhere to start with, so the failure scripted above is the
        # handover into TOWER and not the preflight probe — see
        # ORIGINAL_ELSEWHERE.
        status = run_to_completion(cal, [TOWER, TOWER_TWO], dwell=0.05,
                                   original=ORIGINAL_ELSEWHERE)
        # The run still reached a terminal state rather than erroring out.
        assert status["state"] in ("failed", "done")
        assert len(status["history"]) == 2


class TestNoTrackFallback:
    """When the whole search finds no confirmed track anywhere, the device
    is left on the top-ranked tower's own resolved (gain_a, gain_b,
    lna_state) — not the arbitrary pre-run 'original' tuning (see
    calibrator.py's _apply_top_tower_fallback)."""

    def test_no_track_fallback_lands_on_top_tower_not_last_tried(self, fast):
        client = FakeBlah2Client()  # never overloads, never confirms
        status = run_to_completion(Calibrator(client, FakeRetinaTrackerClient()),
                                   [TOWER, TOWER_TWO])
        assert status["state"] == "failed"
        assert client.current["fc"] == TOWER["fc"]
        assert client.current["fc"] != TOWER_TWO["fc"]

    def test_no_track_fallback_uses_resolved_gain_not_a_fixed_corner(self, fast):
        """B overloads below 37dB: descent reverts to 39 (not the 59dB
        safe ceiling) at every lna_state tried, landing at lna_state=1.
        The fallback must reuse that specific resolved value, not a
        separately-hardcoded safe corner."""
        client = FakeBlah2Client(
            overload_rule=lambda fc, ga, gb, lna: (False, gb < 37))
        status = run_to_completion(Calibrator(client, FakeRetinaTrackerClient()), [TOWER])
        assert status["state"] == "failed"
        assert client.current["fc"] == TOWER["fc"]
        assert client.current["gain_a"] == GAIN_REDUCTION_MIN
        assert client.current["gain_b"] == 39
        assert client.current["lna_state"] == LNA_STATE_MIN

    def test_fallback_tuning_is_recorded_for_persistence(self, fast):
        """The fallback is applied live, but live-only tuning dies at the
        next stack restart — which in the setup wizard is seconds later
        (/set-up/complete force-recreates the stack). Recording it in status
        is what lets /calibrate/apply write it to config, so the run's only
        durable output survives."""
        client = FakeBlah2Client(
            overload_rule=lambda fc, ga, gb, lna: (False, gb < 37))
        status = run_to_completion(Calibrator(client, FakeRetinaTrackerClient()), [TOWER])
        assert status["state"] == "failed"
        assert status["result"] is None
        fallback = status["fallback"]
        assert fallback is not None
        # Exactly what the device was left running, so persisting it is a
        # no-op against live state rather than a fresh, unproven tuning.
        assert fallback["fc"] == TOWER["fc"] == client.current["fc"]
        assert fallback["gain_a"] == client.current["gain_a"]
        assert fallback["gain_b"] == client.current["gain_b"] == 39
        assert fallback["lna_state"] == client.current["lna_state"]
        assert fallback["tower_name"] == TOWER["name"]

    def test_fallback_carries_the_top_tower_position(self, fast):
        """fc and tx have to reach /calibrate/apply together: persisting one
        tower's frequency against another's coordinates would leave blah2
        computing bistatic geometry from the wrong transmitter."""
        client = FakeBlah2Client()
        tower = dict(TOWER, tx={"latitude": 37.75, "longitude": -122.45,
                                "altitude": 310.0})
        status = run_to_completion(Calibrator(client, FakeRetinaTrackerClient()),
                                   [tower])
        assert status["fallback"]["tx"] == tower["tx"]

    def test_fallback_carries_no_position_for_the_configured_tower(self, fast):
        """The currently-configured tower is passed without one, and that
        absence is what stops a run that stays put rewriting location.tx."""
        client = FakeBlah2Client()
        status = run_to_completion(Calibrator(client, FakeRetinaTrackerClient()),
                                   [TOWER])
        assert status["fallback"]["tx"] is None

    def test_fallback_names_the_top_tower_not_the_last_tried(self, fast):
        """fc must stay on the top-ranked tower — in the setup wizard that
        is the tower the user just chose, and persisting a different one
        would silently overrule that choice."""
        client = FakeBlah2Client()
        status = run_to_completion(Calibrator(client, FakeRetinaTrackerClient()),
                                   [TOWER, TOWER_TWO])
        assert status["fallback"]["fc"] == TOWER["fc"]
        assert status["fallback"]["fc"] != TOWER_TWO["fc"]

    def test_cancelled_run_records_no_fallback(self, fast):
        """Cancel restores the original tuning, so there must be nothing for
        /calibrate/apply to persist — cancelling has to keep meaning "put it
        back", not "keep whatever it had reached"."""
        import time
        client = FakeBlah2Client()
        cal = Calibrator(client, FakeRetinaTrackerClient())
        started, _ = cal.start([TOWER], ORIGINAL, budget_seconds=30,
                               dwell_seconds=30)
        assert started
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if cal.get_status()["phase"] == "dwelling":
                break
            time.sleep(0.01)
        cal.cancel()
        cal._thread.join(timeout=10)
        status = cal.get_status()
        assert status["state"] == "cancelled"
        assert status["fallback"] is None

    def test_a_new_run_clears_the_previous_run_fallback(self, fast):
        """Otherwise a stale fallback from an earlier run stays persistable
        while a fresh run is still going."""
        client = FakeBlah2Client()
        cal = Calibrator(client, FakeRetinaTrackerClient())
        assert run_to_completion(cal, [TOWER])["fallback"] is not None
        started, _ = cal.start([TOWER], ORIGINAL, budget_seconds=30,
                               dwell_seconds=30)
        assert started
        try:
            assert cal.get_status()["fallback"] is None
        finally:
            cal.cancel()
            cal._thread.join(timeout=10)


class TestSkipConfirmation:
    """The setup wizard's run shape: resolve the operating point, soak it to
    prove it holds, and stop without waiting for a track (see
    calibrator.start's skip_confirmation)."""

    def test_skip_confirmation_never_confirms_but_still_persists_tuning(self, fast):
        client = FakeBlah2Client()
        tracker_client = FakeRetinaTrackerClient(confirm_after=1)
        cal = Calibrator(client, tracker_client)
        started, error = cal.start([TOWER], ORIGINAL, budget_seconds=10,
                                   skip_confirmation=True)
        assert started, error
        cal._thread.join(timeout=20)
        status = cal.get_status()
        # Even with a tracker that would confirm instantly, the soak feeds it
        # nothing and never reads it — so there is no result, and therefore
        # nothing that can be spurious.
        assert status["state"] == "failed"
        assert status["result"] is None
        assert status["skip_confirmation"] is True
        assert status["history"][0]["outcome"] == "tuned"
        # The tuning still lands, via the same fallback path a no-track run
        # uses — that is what makes this shape usable in the wizard.
        assert status["fallback"] is not None
        assert status["fallback"]["fc"] == TOWER["fc"]

    def test_soak_catches_overload_descent_could_not_see(self, fast, monkeypatch):
        """The whole reason the soak exists. Descent proves a candidate over
        OVERLOAD_SETTLE_SECONDS — one second in production. A point that only
        starts clipping once it is sat on is invisible to that, and skipping
        the dwell entirely would persist the one-second verdict."""
        monkeypatch.setattr(calmod, "DWELL_OVERLOAD_CHECK_SECONDS", 0.02)
        monkeypatch.setattr(calmod, "SOAK_SECONDS", 3.0)
        clipping = {"on": False}
        client = FakeBlah2Client(
            overload_rule=lambda fc, ga, gb, lna: (False, clipping["on"]))
        cal = Calibrator(client, FakeRetinaTrackerClient())
        started, _ = cal.start([TOWER], ORIGINAL, budget_seconds=30,
                               skip_confirmation=True)
        assert started
        # Clip only once the descent has settled and the soak has begun, so
        # this can only be caught by watching the resolved point.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if cal.get_status().get("phase") == "soaking":
                break
            time.sleep(0.01)
        else:
            cal.cancel()
            cal._thread.join(timeout=10)
            pytest.fail("soak never started")
        clipping["on"] = True
        cal._thread.join(timeout=30)

        status = cal.get_status()
        entry = status["history"][0]
        assert entry.get("dwell_backoffs"), "the soak never retreated from overload"
        # And the retreat must reach what gets persisted — otherwise the run
        # moves the hardware to safety and then writes the unsafe values back.
        assert status["fallback"]["gain_a"] == entry["final_gain_a"]
        assert status["fallback"]["gain_b"] == entry["final_gain_b"]
        assert status["fallback"]["lna_state"] == entry["final_lna_state"]
        assert status["fallback"]["gain_b"] == client.current["gain_b"]

    def test_soak_does_not_claim_to_be_watching_for_aircraft(self, fast):
        """Caught in live testing on owl: the soak reused the dwell's phase,
        so the wizard step — whose copy deliberately promises no aircraft —
        displayed "Watching for aircraft…" for its whole 45s."""
        seen = []
        client = FakeBlah2Client()
        cal = Calibrator(client, FakeRetinaTrackerClient())
        real_update = cal._update

        def spy(**kwargs):
            if "phase" in kwargs:
                seen.append(kwargs["phase"])
            return real_update(**kwargs)

        cal._update = spy
        started, _ = cal.start([TOWER], ORIGINAL, budget_seconds=30,
                               skip_confirmation=True)
        assert started
        cal._thread.join(timeout=20)
        assert "soaking" in seen
        assert "dwelling" not in seen

    def test_a_normal_run_still_reports_dwelling(self, fast):
        """The Configuration page really is watching for aircraft, and must
        keep saying so."""
        seen = []
        client = FakeBlah2Client()
        cal = Calibrator(client, FakeRetinaTrackerClient())
        real_update = cal._update

        def spy(**kwargs):
            if "phase" in kwargs:
                seen.append(kwargs["phase"])
            return real_update(**kwargs)

        cal._update = spy
        run_to_completion(cal, [TOWER], dwell=0.3)
        assert "dwelling" in seen
        assert "soaking" not in seen

    def test_a_clean_soak_reports_tuned(self, fast):
        """A point that holds for the whole soak is the success case, and
        must not be reported as any kind of shortfall."""
        client = FakeBlah2Client()
        cal = Calibrator(client, FakeRetinaTrackerClient())
        started, _ = cal.start([TOWER], ORIGINAL, budget_seconds=30,
                               skip_confirmation=True)
        assert started
        cal._thread.join(timeout=20)
        entry = cal.get_status()["history"][0]
        assert entry["outcome"] == "tuned"
        assert not entry.get("dwell_backoffs")

    def test_skip_confirmation_never_reports_skipped_no_time(self, fast):
        """The regression this flag exists to avoid. dwell_seconds=0 would
        land in the "budget genuinely exhausted" branch and label the tower
        skipped_no_time, telling the user time ran out when nothing of the
        sort happened."""
        client = FakeBlah2Client()
        cal = Calibrator(client, FakeRetinaTrackerClient())
        started, _ = cal.start([TOWER], ORIGINAL, budget_seconds=10,
                               skip_confirmation=True)
        assert started
        cal._thread.join(timeout=10)
        status = cal.get_status()
        assert status["history"][0]["outcome"] != "skipped_no_time"
        assert "no aircraft was overhead" not in (status["error"] or "")
        assert "Tuning resolved" in (status["error"] or "")

    def test_skip_confirmation_still_resolves_a_real_operating_point(self, fast):
        """Descent must do its actual job — the shorter run is only worth
        anything if the tuning it lands on is the one the descent proved."""
        client = FakeBlah2Client(
            overload_rule=lambda fc, ga, gb, lna: (False, gb < 37))
        cal = Calibrator(client, FakeRetinaTrackerClient())
        started, _ = cal.start([TOWER], ORIGINAL, budget_seconds=10,
                               skip_confirmation=True)
        assert started
        cal._thread.join(timeout=10)
        status = cal.get_status()
        assert status["fallback"]["gain_b"] == 39
        assert status["fallback"]["gain_b"] == client.current["gain_b"]
        assert status["fallback"]["lna_state"] == client.current["lna_state"]

    def test_a_normal_run_is_unaffected(self, fast):
        """The Configuration-page entry point must keep dwelling and keep
        confirming — skip_confirmation defaults off."""
        client = FakeBlah2Client(detection=moving_track_detections())
        tracker_client = FakeRetinaTrackerClient(confirm_after=1)
        status = run_to_completion(Calibrator(client, tracker_client), [TOWER])
        assert status["state"] == "done"
        assert status["result"] is not None
        assert status["skip_confirmation"] is False


class TestTrackerSidecarIntegration:
    """Directly exercises the shared-sidecar-specific mechanics that don't
    have another natural home: per-dwell reset and the staleness guard on
    confirmed events (see calibrator.py's _take_confirmed_event)."""

    def test_resets_tracker_at_the_start_of_every_dwell(self, fast):
        client = FakeBlah2Client()
        tracker_client = FakeRetinaTrackerClient()
        cal = Calibrator(client, tracker_client)
        run_to_completion(cal, [TOWER, TOWER_TWO], dwell=0.2)
        # One per dwell — the scope that matters, since the sidecar is fed by
        # tracker_capture throughout the descent too (see _reset_tracker).
        assert tracker_client.reset_calls == 2

    def test_confirmation_waiting_before_the_dwell_is_not_credited(self, fast):
        """The regression: a track confirmed from tracker_capture's always-on
        feed *during the descent* must not be handed to the dwell as though
        the dwell had observed it.

        Reset used to be per-tower, so anything the sidecar accumulated over
        the whole descent — 150s on a live node — was still there when the
        dwell started. Seen live as a confirmed track with dwell_seconds 0.0,
        credited to a tuning it had observed nothing at. The applied_at guard
        does not catch it: the event's timestamp legitimately post-dates the
        retune.
        """
        client = FakeBlah2Client()
        # Never confirms from this dwell's own frames, so a success here
        # could only have come from the pre-planted event below.
        tracker_client = FakeRetinaTrackerClient()
        cal = Calibrator(client, tracker_client)
        original_dwell = cal._dwell

        def dwell(*args, **kwargs):
            # Stand in for the descent-time confirmation: recent enough to
            # clear applied_at, but earned before this dwell watched anything.
            cal._on_track_event({"track_id": "earned-during-descent",
                                 "timestamp": 10 ** 13})
            return original_dwell(*args, **kwargs)

        cal._dwell = dwell
        status = run_to_completion(cal, [TOWER], dwell=0.3)

        assert status["state"] != "done", (
            "credited a track the dwell never observed - the tracker is not "
            "being reset at dwell start")
        assert status["history"][0]["outcome"] == "no_confirmed_track"

    def test_stale_confirmed_event_before_applied_at_is_ignored(self):
        # A confirmed event generated before this candidate's applied_at —
        # e.g. still in flight from a previous, now-irrelevant tower — must
        # not be mistaken for confirmation at the new geometry.
        cal = Calibrator(FakeBlah2Client(), FakeRetinaTrackerClient())
        cal._on_track_event({"track_id": "stale", "timestamp": 100})
        assert cal._take_confirmed_event(min_timestamp=200) is None
        assert cal._take_confirmed_event(min_timestamp=100) is not None


class TestAdsbMode:
    """MODE_ADSB has no time division (see calibrator.py's module docstring)
    — an aircraft that's simply never present makes the dwell wait forever
    by design, so tests for that case must cancel explicitly rather than
    rely on a timeout. adsb_aircraft_appears_then_leaves simulates a real
    aircraft's limited window instead of a permanently-present one, since a
    permanently-present aircraft never gives _dwell_adsb a reason to
    conclude a candidate failed.

    The engine fully supports MODE_ADSB now — it's still blocked at the
    route level (see TestRoutes). The match itself is scripted via
    FakeRetinaTrackerClient's adsb_hex, mirroring the sidecar's own native
    ADS-B matching (see calibrator.py's module docstring)."""

    def test_matched_track_succeeds_immediately(self, fast):
        client = FakeBlah2Client(
            detection=scattered_detections(),
            adsb_tracks=adsb_aircraft_at(delay=10.5, doppler=51.0))
        tracker_client = FakeRetinaTrackerClient(confirm_after=1, adsb_hex="ABC123")
        cal = Calibrator(client, tracker_client)
        started, error = cal.start([TOWER], ORIGINAL, mode=calmod.MODE_ADSB)
        assert started, error
        cal._thread.join(timeout=10)
        status = cal.get_status()
        assert status["state"] == "done"
        assert status["result"]["adsb_hex"] == "ABC123"

    def test_no_time_division_reported(self, fast):
        client = FakeBlah2Client(
            detection=scattered_detections(),
            adsb_tracks=adsb_aircraft_at(delay=10.5, doppler=51.0))
        tracker_client = FakeRetinaTrackerClient(confirm_after=1, adsb_hex="ABC123")
        cal = Calibrator(client, tracker_client)
        started, error = cal.start([TOWER], ORIGINAL, mode=calmod.MODE_ADSB)
        assert started, error
        cal._thread.join(timeout=10)
        status = cal.get_status()
        assert status["state"] == "done"
        assert status["progress"]["budget_seconds"] is None

    def test_unmatched_aircraft_departing_exhausts_the_only_candidate(self, fast):
        """Clean overload_rule -> descent lands at the sensitivity floor
        (20 dB) immediately, so there's no lower gain to cycle to: one
        aircraft shows up, a track confirms but without a matching
        adsb_hex, then the aircraft leaves — and that's the whole run for
        this (single-tower) case. Must terminate on its own, no cancel
        needed, since the gain floor is a real stopping point."""
        client = FakeBlah2Client(
            detection=scattered_detections(),
            adsb_tracks=adsb_aircraft_appears_then_leaves(
                delay=200.0, doppler=-300.0, present_polls=3))
        tracker_client = FakeRetinaTrackerClient(confirm_after=1, adsb_hex=None)
        cal = Calibrator(client, tracker_client)
        started, error = cal.start([TOWER], ORIGINAL, mode=calmod.MODE_ADSB)
        assert started, error
        cal._thread.join(timeout=10)
        status = cal.get_status()
        assert status["state"] == "failed"
        assert status["result"] is None
        assert "every candidate tower and gain setting" in status["error"]
        assert status["history"][0]["gains_tried"] == [
            {"gain_b": GAIN_REDUCTION_MIN, "overload_b": False}]
        assert "doesn't match a known aircraft" in status["best_attempt"]["reason"]
        # No track anywhere -> left on the top tower's own resolved gain
        # (the sensitivity floor here, since nothing ever overloads),
        # not the arbitrary pre-run 'original' tuning.
        assert client.current["fc"] == TOWER["fc"]
        assert client.current["gain_a"] == GAIN_REDUCTION_MIN

    def test_cycles_toward_more_sensitivity_then_stops_on_reoverload(self, fast):
        """Descent reverts B to 39, refine lands at 34 (59->49->39 clean,
        29 overloads -> revert to 39, refine's 34 stays clean). The first
        unmatched-then-departed aircraft should step B down to 29 — which
        immediately re-overloads per this rule — so the run must stop there
        rather than trying the (unreachable) sensitivity floor."""
        client = FakeBlah2Client(
            overload_rule=lambda fc, ga, gb, lna: (False, gb < 30),
            detection=scattered_detections(),
            adsb_tracks=adsb_aircraft_appears_then_leaves(
                delay=200.0, doppler=-300.0, present_polls=3))
        tracker_client = FakeRetinaTrackerClient(confirm_after=1, adsb_hex=None)
        cal = Calibrator(client, tracker_client)
        started, error = cal.start([TOWER], ORIGINAL, mode=calmod.MODE_ADSB)
        assert started, error
        cal._thread.join(timeout=10)
        status = cal.get_status()
        assert status["state"] == "failed"
        gains_tried = status["history"][0]["gains_tried"]
        assert [g["gain_b"] for g in gains_tried] == [34, 29]
        assert gains_tried[0]["overload_b"] is False
        assert gains_tried[1]["overload_b"] is True

    def test_no_traffic_waits_until_cancelled(self, fast):
        """No aircraft ever appears — by design this must wait forever, not
        time out. Confirms it's genuinely waiting (not stuck/crashed) and
        that cancelling it still restores the original tuning."""
        import time as time_module
        client = FakeBlah2Client()
        cal = Calibrator(client, FakeRetinaTrackerClient())
        started, error = cal.start([TOWER], ORIGINAL, mode=calmod.MODE_ADSB)
        assert started, error

        deadline = time_module.monotonic() + 5
        while time_module.monotonic() < deadline:
            if cal.get_status()["phase"] == "dwelling":
                break
            time_module.sleep(0.01)
        assert cal.is_running(), "run ended on its own despite no traffic ever appearing"

        cal.cancel()
        cal._thread.join(timeout=10)
        status = cal.get_status()
        assert status["state"] == "cancelled"
        assert client.current["fc"] == ORIGINAL["fc"]
        assert client.current["gain_a"] == ORIGINAL["gain_a"]
        assert client.current["gain_b"] == ORIGINAL["gain_b"]

    def test_track_mode_ignores_adsb_entirely(self, fast):
        # default mode must succeed on confirmation alone, regardless of
        # whether ADS-B truth would have matched, and keeps its time budget
        client = FakeBlah2Client(detection=moving_track_detections())
        tracker_client = FakeRetinaTrackerClient(confirm_after=1)
        status = run_to_completion(Calibrator(client, tracker_client), [TOWER])
        assert status["state"] == "done"
        assert status["progress"]["budget_seconds"] is not None

    def test_invalid_mode_rejected(self, fast):
        cal = Calibrator(FakeBlah2Client(), FakeRetinaTrackerClient())
        started, error = cal.start([TOWER], ORIGINAL, mode="bogus")
        assert not started
        assert "Invalid mode" in error


class FakeConfigManager:
    """Stand-in for ConfigManager — only the two user.yml accessors the
    preflight's recovery branch touches."""

    def __init__(self, user_config=None):
        self.user_config = copy.deepcopy(user_config) if user_config else {}
        self.saves = 0

    def load_user_config(self):
        return copy.deepcopy(self.user_config)

    def save_user_config(self, config):
        self.user_config = copy.deepcopy(config)
        self.saves += 1


class FakeApplyService:
    """Stand-in for ApplyService. request() returns immediately and
    get_status() reports the scripted outcome, as the real one does.
    on_request is how a test makes the radio start answering again — it
    stands in for the restart actually reinitialising the device."""

    def __init__(self, outcome="done", error=None, on_request=None):
        self.requests = []
        self._outcome = outcome
        self._error = error
        self._on_request = on_request
        self._status = {"state": "idle", "error": None}

    def request(self, bypass_guard=False):
        self.requests.append({"bypass_guard": bypass_guard})
        if self._on_request is not None:
            self._on_request()
        self._status = {"state": self._outcome, "error": self._error}
        return dict(self._status)

    def get_status(self):
        return dict(self._status)


def wedged_until_restart():
    """A device that refuses every retune until an apply happens — the
    live failure this preflight exists for. Returns (rule, clear) for
    FakeBlah2Client's retune_fail_rule and FakeApplyService's on_request."""
    wedged = {"still": True}
    def rule(fc, gain_a, gain_b, lna_state):
        return wedged["still"]
    def clear():
        wedged["still"] = False
    return rule, clear


@pytest.fixture
def fast_preflight(fast, monkeypatch):
    """`fast`, plus the preflight's own recovery timings."""
    monkeypatch.setattr(calmod, "PREFLIGHT_RECOVERY_PROBE_SECONDS", 0.2)
    monkeypatch.setattr(calmod, "PREFLIGHT_RECOVERY_APPLY_TIMEOUT_SECONDS", 1.0)
    monkeypatch.setattr(calmod, "PREFLIGHT_APPLY_POLL_SECONDS", 0.01)


class TestPreflight:
    """Before any tower is tried, the radio is parked at the safe corner and
    proved to still accept tuning commands — and recovered by restart if it
    doesn't. See calibrator._preflight."""

    def test_healthy_device_is_parked_at_the_safe_corner_first(self, fast_preflight):
        client = FakeBlah2Client()
        config_mgr = FakeConfigManager()
        apply_service = FakeApplyService()
        cal = Calibrator(client, FakeRetinaTrackerClient(),
                         config_mgr=config_mgr, apply_service=apply_service)
        status = run_to_completion(cal, [TOWER], dwell=0.05)

        first = client.applied[0]
        assert (first["fc"], first["gain_a"], first["gain_b"], first["lna_state"]) == (
            ORIGINAL["fc"], GAIN_REDUCTION_MAX, GAIN_REDUCTION_MAX, LNA_STATE_MAX)
        # A healthy node pays for the probe and nothing else: no restart, no
        # user.yml write, and nothing to report about it.
        assert apply_service.requests == []
        assert config_mgr.saves == 0
        assert status["preflight"] is None

    def test_probe_stays_on_the_frequency_the_device_is_actually_on(self, fast_preflight):
        # blah2 left on another tower by an unpersisted earlier run while
        # config still says ORIGINAL. Moving fc from an unknown gain is the
        # hazard the safe corner exists to avoid, and the probe has nothing
        # proven-safe to hand over from — so it must not move fc at all.
        client = FakeBlah2Client()
        client.generation = 7
        client.applied.append({
            "fc": TOWER_THREE["fc"], "gain_a": 30, "gain_b": 30, "lna_state": 2,
            "generation": 7, "applied_at": 500})
        cal = Calibrator(client, FakeRetinaTrackerClient(),
                         config_mgr=FakeConfigManager(),
                         apply_service=FakeApplyService())
        run_to_completion(cal, [TOWER], dwell=0.05)

        probe = client.applied[1]
        assert probe["fc"] == TOWER_THREE["fc"]
        assert (probe["gain_a"], probe["gain_b"], probe["lna_state"]) == (
            GAIN_REDUCTION_MAX, GAIN_REDUCTION_MAX, LNA_STATE_MAX)

    def test_wedged_device_is_recovered_and_the_run_proceeds(self, fast_preflight):
        rule, clear = wedged_until_restart()
        client = FakeBlah2Client(retune_fail_rule=rule,
                                 detection=moving_track_detections())
        config_mgr = FakeConfigManager(
            {"capture": {"device": {"gainReduction": [30, 30], "lnaState": 2},
                         "fc": ORIGINAL["fc"]}})
        apply_service = FakeApplyService(on_request=clear)
        cal = Calibrator(client, FakeRetinaTrackerClient(confirm_after=1),
                         config_mgr=config_mgr, apply_service=apply_service)
        status = run_to_completion(cal, [TOWER], dwell=3.0)

        # The one apply this engine ever makes, and the one caller allowed
        # past the config-change guard.
        assert apply_service.requests == [{"bypass_guard": True}]
        # The safe corner is written into user.yml so the restart brings
        # blah2 up on it, without disturbing the rest of the section.
        assert config_mgr.user_config["capture"]["device"] == {
            "gainReduction": [GAIN_REDUCTION_MAX, GAIN_REDUCTION_MAX],
            "lnaState": LNA_STATE_MAX}
        assert config_mgr.user_config["capture"]["fc"] == ORIGINAL["fc"]
        assert status["preflight"]["recovered"] is True
        assert status["preflight"]["previous"] == ORIGINAL
        # And having recovered it, the run did its actual job.
        assert status["state"] == "done"

    def test_unrecoverable_device_ends_the_run_before_any_tower(self, fast_preflight):
        client = FakeBlah2Client(retune_fail_rule=lambda fc, ga, gb, lna: True)
        config_mgr = FakeConfigManager()
        apply_service = FakeApplyService()
        cal = Calibrator(client, FakeRetinaTrackerClient(),
                         config_mgr=config_mgr, apply_service=apply_service)
        status = run_to_completion(cal, [TOWER, TOWER_TWO], dwell=0.05)

        assert status["state"] == "failed"
        # No tower was tried, so there is nothing to say about aircraft —
        # which is the whole point. Before the preflight this ran all the
        # way through, reported tuning_not_applied per tower, and led with
        # "no aircraft was overhead".
        assert status["history"] == []
        assert "still not accepting tuning commands after a restart" in status["error"]
        assert "maximum attenuation" in status["error"]
        # It still tried, and it still left the device safe.
        assert apply_service.requests == [{"bypass_guard": True}]
        assert config_mgr.saves == 1
        assert status["preflight"]["recovered"] is False

    def test_failed_restart_is_reported_rather_than_swallowed(self, fast_preflight):
        client = FakeBlah2Client(retune_fail_rule=lambda fc, ga, gb, lna: True)
        apply_service = FakeApplyService(outcome="failed",
                                         error="Command timed out")
        cal = Calibrator(client, FakeRetinaTrackerClient(),
                         config_mgr=FakeConfigManager(),
                         apply_service=apply_service)
        status = run_to_completion(cal, [TOWER], dwell=0.05)

        assert status["state"] == "failed"
        assert "restarting it failed" in status["error"]
        assert "Command timed out" in status["error"]

    def test_restart_that_never_finishes_gives_up(self, fast_preflight):
        client = FakeBlah2Client(retune_fail_rule=lambda fc, ga, gb, lna: True)
        # Never leaves 'running' — a restart that hung rather than failed.
        apply_service = FakeApplyService(outcome="running")
        cal = Calibrator(client, FakeRetinaTrackerClient(),
                         config_mgr=FakeConfigManager(),
                         apply_service=apply_service)
        status = run_to_completion(cal, [TOWER], dwell=0.05)

        assert status["state"] == "failed"
        assert "did not finish in time" in status["error"]

    def test_without_recovery_machinery_it_fails_fast(self, fast_preflight):
        # No config_mgr/apply_service (dev mode, and every engine-level test
        # above): the probe still runs and still ends the run honestly,
        # there is just no restart to attempt.
        client = FakeBlah2Client()
        client.ack_enabled = False
        status = run_to_completion(Calibrator(client, FakeRetinaTrackerClient()),
                                   [TOWER], dwell=0.05)

        assert status["state"] == "failed"
        assert status["history"] == []
        assert "Restart the radar services and try again" in status["error"]

    def test_cancel_after_a_recovery_does_not_restore_the_wedging_tuning(
            self, fast_preflight):
        # The original tuning is what the radio was stuck on. Putting a
        # just-recovered device back on it is the one move guaranteed to
        # undo the recovery, so a recovered run skips the restore entirely.
        import time as _time
        rule, clear = wedged_until_restart()
        client = FakeBlah2Client(retune_fail_rule=rule)
        cal = Calibrator(client, FakeRetinaTrackerClient(),
                         config_mgr=FakeConfigManager(),
                         apply_service=FakeApplyService(on_request=clear))
        started, error = cal.start([TOWER], ORIGINAL, budget_seconds=30,
                                   dwell_seconds=30)
        assert started, error
        deadline = _time.monotonic() + 5
        while _time.monotonic() < deadline:
            if cal.get_status()["phase"] == "dwelling":
                break
            _time.sleep(0.01)
        cal.cancel()
        cal._thread.join(timeout=10)
        status = cal.get_status()

        assert status["state"] == "cancelled"
        assert status["preflight"]["recovered"] is True
        assert (client.current["gain_a"], client.current["gain_b"],
                client.current["lna_state"]) != (
            ORIGINAL["gain_a"], ORIGINAL["gain_b"], ORIGINAL["lna_state"])

    def test_recovery_time_is_not_charged_to_the_search_budget(
            self, fast, monkeypatch):
        # A recovery restart can take ~90s. Starting the budget clock before
        # it would silently shorten every tower's dwell on a node that was
        # already having a bad day.
        monkeypatch.setattr(calmod, "PREFLIGHT_RECOVERY_PROBE_SECONDS", 0.2)
        monkeypatch.setattr(calmod, "PREFLIGHT_RECOVERY_APPLY_TIMEOUT_SECONDS", 5.0)
        monkeypatch.setattr(calmod, "PREFLIGHT_APPLY_POLL_SECONDS", 0.05)
        rule, clear = wedged_until_restart()
        client = FakeBlah2Client(retune_fail_rule=rule)

        polls = {"n": 0}
        apply_service = FakeApplyService(on_request=clear)
        real_get_status = apply_service.get_status
        def slow_get_status():
            polls["n"] += 1
            # 'running' for the first few polls, i.e. a restart that took
            # a meaningful slice of the budget below.
            if polls["n"] <= 10:
                return {"state": "running", "error": None}
            return real_get_status()
        apply_service.get_status = slow_get_status

        cal = Calibrator(client, FakeRetinaTrackerClient(),
                         config_mgr=FakeConfigManager(),
                         apply_service=apply_service)
        status = run_to_completion(cal, [TOWER, TOWER_TWO], budget=1.5,
                                   dwell=None)

        assert status["preflight"]["recovered"] is True
        # Both towers still got looked at, rather than the second being
        # eaten by time spent restarting before the search began.
        assert status["progress"]["towers_tried"] == 2
        assert [h["outcome"] for h in status["history"]] == [
            "no_confirmed_track", "no_confirmed_track"]


class TestFailureModes:
    def test_unreachable_blah2_fails_the_run(self, fast):
        # A radio that takes no retune at all is caught by the preflight
        # before any tower is tried (see calibrator._preflight). It used to
        # be folded into the per-candidate overload path, so the run walked
        # all three towers, recorded tuning_not_applied on each, and
        # finished behind a summary message about no aircraft being
        # overhead — the exact confusion the preflight exists to remove.
        # No history at all is the point: nothing was watched, so there is
        # nothing to report about it.
        client = FakeBlah2Client()
        client.retune_error = "connection refused"
        status = run_to_completion(Calibrator(client, FakeRetinaTrackerClient()), [TOWER])
        assert status["state"] == "failed"
        assert status["history"] == []
        assert "not accepting tuning commands" in status["error"]

    def test_missing_ack_fails_the_run(self, fast):
        client = FakeBlah2Client()
        client.ack_enabled = False
        status = run_to_completion(Calibrator(client, FakeRetinaTrackerClient()), [TOWER])
        assert status["state"] == "failed"
        assert status["history"] == []
        assert "not accepting tuning commands" in status["error"]

    def test_missing_rf_status_fails_the_run(self, fast):
        client = FakeBlah2Client(detection=moving_track_detections())
        client.rf_enabled = False
        status = run_to_completion(Calibrator(client, FakeRetinaTrackerClient()), [TOWER])
        assert status["state"] == "failed"
        assert status["history"][0]["device_error"] is True

    def test_ignore_cancel_lets_apply_proceed_despite_a_pending_cancel(self, fast):
        """The restore-on-failure path must not be abortable by a second
        cancel arriving while it's in flight — otherwise blah2 could be left
        tuned to the last failed candidate instead of the original setting."""
        client = FakeBlah2Client()
        cal = Calibrator(client, FakeRetinaTrackerClient())
        cal._cancel.set()  # simulates a cancel already pending/re-arriving
        applied_at = cal._apply(ORIGINAL["fc"], ORIGINAL["gain_a"],
                                ORIGINAL["gain_b"], ORIGINAL["lna_state"],
                                ignore_cancel=True)
        assert applied_at is not None
        assert client.current["fc"] == ORIGINAL["fc"]
        # without the flag, the same call must still raise as before
        with pytest.raises(calmod._Cancelled):
            cal._apply(ORIGINAL["fc"], ORIGINAL["gain_a"], ORIGINAL["gain_b"],
                      ORIGINAL["lna_state"])

    def test_cannot_start_twice(self, fast):
        client = FakeBlah2Client()
        cal = Calibrator(client, FakeRetinaTrackerClient())
        started, _ = cal.start([TOWER], ORIGINAL, budget_seconds=30,
                               dwell_seconds=30)
        assert started
        started_again, error = cal.start([TOWER], ORIGINAL)
        assert not started_again
        assert "already running" in error
        cal.cancel()
        cal._thread.join(timeout=10)

    def test_on_complete_fires_with_terminal_status(self, fast):
        client = FakeBlah2Client(detection=moving_track_detections())
        cal = Calibrator(client, FakeRetinaTrackerClient(confirm_after=1))
        seen = []
        cal.on_complete = seen.append
        run_to_completion(cal, [TOWER])
        assert len(seen) == 1
        assert seen[0]["state"] == "done"


@pytest.fixture
def ds(tmp_path):
    backup_dir = os.path.join(tmp_path, "mender-cloud-disabled")
    return DeviceState(
        data_dir=str(tmp_path),
        mender_services=[],
        mender_conf_path=os.path.join(tmp_path, "mender.conf"),
        mender_conf_backup_dir=backup_dir,
        mender_conf_backup_path=os.path.join(backup_dir, "mender.conf"),
    )


class TestCalibrationLock:
    def test_acquire_and_release(self, ds):
        assert ds.acquire_calibration_lock()
        assert ds.is_calibration_locked()[0]
        assert not ds.acquire_calibration_lock()
        ds.release_calibration_lock()
        assert not ds.is_calibration_locked()[0]

    def test_stale_lock_self_heals(self, ds):
        stale = {"started_at": (datetime.now() - timedelta(minutes=30)).isoformat()}
        with open(ds.calibrate_lock_file, "w") as f:
            json.dump(stale, f)
        assert not ds.is_calibration_locked()[0]

    def test_blocks_install_and_vice_versa(self, ds):
        assert ds.acquire_calibration_lock()
        ok, reason = ds.can_start_install()
        assert not ok and "calibration" in reason.lower()
        ds.release_calibration_lock()

        assert ds.acquire_install_lock("v1.0.0")
        ok, reason = ds.can_start_calibration()
        assert not ok
        ds.release_install_lock()
        ok, _ = ds.can_start_calibration()
        assert ok


class TestRoutes:
    def _set_merged(self, config_files, mutate):
        _, merged_path = config_files
        with open(merged_path) as f:
            merged = yaml.safe_load(f)
        mutate(merged)
        with open(merged_path, 'w') as f:
            yaml.safe_dump(merged, f)

    def test_start_refuses_with_agc_enabled(self, app_client, config_files):
        def enable_agc(merged):
            merged['capture']['device']['bandwidthNumber'] = 50
        self._set_merged(config_files, enable_agc)
        resp = app_client.post('/calibrate/start', json={})
        assert resp.status_code == 409
        assert "AGC" in resp.get_json()["error"]

    def test_start_rejects_invalid_mode(self, app_client):
        resp = app_client.post('/calibrate/start', json={"mode": "bogus", "scope": "current_tower"})
        assert resp.status_code == 400
        assert "Invalid mode" in resp.get_json()["error"]

    def test_start_rejects_adsb_mode_unconditionally(self, app_client, config_files):
        """ADS-B mode's engine support is complete (see calibrator.py's
        module docstring) but it's still rejected at the route regardless
        of truth.adsb.enabled — exposing it to users is a separate decision
        not yet made."""
        def enable_adsb(merged):
            merged['truth']['adsb']['enabled'] = True
        self._set_merged(config_files, enable_adsb)
        resp = app_client.post('/calibrate/start',
                               json={"mode": "adsb", "scope": "current_tower"})
        assert resp.status_code == 409
        assert "not currently available" in resp.get_json()["error"]

    def test_start_refuses_during_install(self, app_client):
        import app as app_module
        assert app_module.device_state.acquire_install_lock("v1.0.0")
        try:
            resp = app_client.post('/calibrate/start', json={})
            assert resp.status_code == 409
        finally:
            app_module.device_state.release_install_lock()

    def test_start_launches_and_locks(self, app_client):
        import app as app_module
        with patch.object(app_module.calibrator, 'start',
                          return_value=(True, None)) as mock_start:
            resp = app_client.post('/calibrate/start',
                                   json={"scope": "current_tower"})
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert app_module.device_state.is_calibration_locked()[0]
        app_module.device_state.release_calibration_lock()

        towers, original = mock_start.call_args[0]
        assert towers[0]["fc"] == original["fc"]
        assert GAIN_REDUCTION_MIN <= original["gain_a"] <= GAIN_REDUCTION_MAX
        assert LNA_STATE_MIN <= original["lna_state"] <= LNA_STATE_MAX

    def test_start_prefers_cached_towers_over_live_lookup(self, app_client):
        """When the wizard's tower search was cached, /calibrate/start uses
        it directly and never calls the live geography lookup."""
        import app as app_module
        app_module.device_state.save_towers_cache(37.7644, -122.3954, [
            {"callsign": "Cached Tower", "frequency_mhz": 91.1},
        ])
        with patch.object(app_module.calibrator, 'start',
                          return_value=(True, None)) as mock_start, \
             patch('routes.calibrate.http_requests.get') as mock_get:
            resp = app_client.post('/calibrate/start', json={})
        assert resp.status_code == 200
        mock_get.assert_not_called()

        towers, _ = mock_start.call_args[0]
        names = [t["name"] for t in towers]
        assert "Cached Tower" in names

    def test_start_refuses_while_a_config_apply_is_still_running(self, app_client):
        """The refusal has to run both ways.

        ApplyService blocks an apply starting during a run. Nothing blocked a
        run starting during an apply — can_start_calibration() knows about
        Mender installs but not about the ~45s stack restart an apply
        performs. Hit by accident on a live node: Apply Changes, then
        Auto-Calibrate a few seconds later, and every retune failed against
        restarting containers. All three towers came back
        tuning_not_applied — honest, but a whole run wasted on something
        that could just have been refused.
        """
        import app as app_module
        with patch.object(app_module.apply_service, 'is_running', return_value=True):
            resp = app_client.post('/calibrate/start', json={"mode": "track"})

        assert resp.status_code == 409
        assert 'configuration change' in resp.get_json()['error']

    def test_status_reports_a_system_update_in_progress(self, app_client):
        """A server-pushed Mender deployment installs autonomously — nothing
        here can refuse it, unlike /mender/install. It restarts the radar
        underneath a run, after which every retune fails and the run reports
        tuning_not_applied for no reason the user can see. The modal already
        polls this endpoint, so it carries the explanation."""
        import app as app_module
        with patch.object(app_module.device_state, 'is_any_update_in_progress',
                          return_value=(True, 'Installing retina-node-v0.5.0')):
            body = app_client.get('/calibrate/status').get_json()
        assert body['system_update'] == 'Installing retina-node-v0.5.0'

    def test_status_reports_no_update_when_none_is_running(self, app_client):
        import app as app_module
        with patch.object(app_module.device_state, 'is_any_update_in_progress',
                          return_value=(False, None)):
            body = app_client.get('/calibrate/status').get_json()
        assert body['system_update'] is None

    def test_status_returns_idle_initially(self, app_client):
        resp = app_client.get('/calibrate/status')
        assert resp.status_code == 200
        assert resp.get_json()["state"] in ("idle", "done", "failed", "cancelled")

    def test_apply_without_result_is_rejected(self, app_client):
        import app as app_module
        with patch.object(app_module.calibrator, 'get_status',
                          return_value={"state": "idle", "result": None}):
            resp = app_client.post('/calibrate/apply')
        assert resp.status_code == 409

    def test_start_passes_skip_confirmation_through(self, app_client):
        """The wizard's run shape has to survive the route, and the
        Configuration page must not pick it up by accident."""
        import app as app_module
        with patch.object(app_module.calibrator, 'start',
                          return_value=(True, None)) as started:
            resp = app_client.post('/calibrate/start',
                                   json={"scope": "current_tower",
                                         "skip_confirmation": True})
        assert resp.status_code == 200
        assert resp.get_json()["skip_confirmation"] is True
        assert started.call_args.kwargs["skip_confirmation"] is True
        # scope: current_tower means exactly one tower, not three.
        assert len(started.call_args.args[0]) == 1

    def test_start_defaults_to_a_full_run(self, app_client):
        """No flag from the Configuration page — three towers, full dwell."""
        import app as app_module
        with patch.object(app_module.calibrator, 'start',
                          return_value=(True, None)) as started:
            resp = app_client.post('/calibrate/start', json={})
        assert resp.status_code == 200
        assert resp.get_json()["skip_confirmation"] is False
        assert started.call_args.kwargs["skip_confirmation"] is False

    def test_apply_of_a_cancelled_run_is_rejected(self, app_client):
        """Cancel restores the original tuning and never records a fallback,
        so there is nothing to keep — the route must not invent one."""
        import app as app_module
        cancelled = {"state": "cancelled", "result": None, "fallback": None}
        with patch.object(app_module.calibrator, 'get_status', return_value=cancelled):
            resp = app_client.post('/calibrate/apply')
        assert resp.status_code == 409

    def test_apply_persists_a_no_track_fallback(self, app_client, config_files):
        """The blocker this route change exists for: a run that confirmed no
        track still resolved an operating point the descent proved the device
        tolerates, and it is applied live only. The next stack restart
        re-reads config.yml and discards it — seconds later, in the setup
        wizard's case — so it has to be writable to config just like a
        success."""
        import app as app_module
        no_track = {
            "state": "failed",
            "started_at": "2026-08-24T00:00:00+00:00",
            "result": None,
            "error": "No confirmed track found within the time budget.",
            "fallback": {"tower_name": "Tower One", "fc": 213_000_000,
                         "gain_a": 49, "gain_b": 39, "lna_state": 7},
        }
        with patch.object(app_module.calibrator, 'get_status', return_value=no_track), \
             patch.object(app_module.apply_service, 'request',
                          return_value={"state": "running"}) as queued:
            resp = app_client.post('/calibrate/apply')
        assert resp.status_code == 202
        assert resp.get_json()["success"] is True
        queued.assert_called_once_with()

        user_path, _ = config_files
        with open(user_path) as f:
            user = yaml.safe_load(f)
        assert user['capture']['fc'] == 213_000_000
        assert user['capture']['device']['gainReduction'] == [49, 39]
        assert user['capture']['device']['lnaState'] == 7
        # AGC stays off on this path too: the fallback is a manual operating
        # point exactly like a confirmed result.
        assert user['capture']['device']['bandwidthNumber'] == 0

    def test_apply_prefers_a_confirmed_result_over_a_fallback(self, app_client, config_files):
        """Defensive: the two are mutually exclusive in the engine today
        (the fallback only runs when result is None). If that ever changes,
        a confirmed track must still win over a consolation tuning."""
        import app as app_module
        both = {
            "state": "done",
            "result": {"tower_name": "Real", "fc": 105_100_000,
                       "gain_a": 30, "gain_b": 45, "lna_state": 6,
                       "track_id": "0A3F"},
            "fallback": {"tower_name": "Consolation", "fc": 213_000_000,
                         "gain_a": 59, "gain_b": 59, "lna_state": 9},
        }
        with patch.object(app_module.calibrator, 'get_status', return_value=both), \
             patch.object(app_module.apply_service, 'request',
                          return_value={"state": "running"}):
            resp = app_client.post('/calibrate/apply')
        assert resp.status_code == 202

        user_path, _ = config_files
        with open(user_path) as f:
            user = yaml.safe_load(f)
        assert user['capture']['fc'] == 105_100_000
        assert user['capture']['device']['gainReduction'] == [30, 45]

    def test_apply_is_not_blocked_by_the_config_change_guard(self, app_client):
        """The guard that refuses config changes mid-run must not refuse the
        calibration's *own* apply.

        Both go through ApplyService.request(), and persisting a result is
        the entire payoff of a successful run — so a guard that cannot tell
        the two apart would block the feature's happy path while looking
        like it was protecting it. It can: by the time /calibrate/apply is
        reachable the run has reached a terminal state, so is_running() is
        false and the lock has been released.

        Exercises the real guard, unlike the route test below which mocks
        request() and would pass either way.
        """
        import app as app_module
        import services

        with patch.object(app_module.calibrator, 'is_running', return_value=False), \
             patch.object(app_module.device_state, 'is_calibration_locked',
                          return_value=(False, None)):
            ok, reason = services.config_change_guard()
        assert ok, f"the calibration's own apply would be refused: {reason}"

        # ...and it does refuse while the run is still going.
        with patch.object(app_module.calibrator, 'is_running', return_value=True):
            ok, _ = services.config_change_guard()
        assert not ok

    def test_apply_writes_user_config(self, app_client, config_files):
        import app as app_module
        done = {
            "state": "done",
            "started_at": "2026-07-08T00:00:00+00:00",
            "result": {"tower_name": "Tower One", "fc": 105_100_000,
                       "gain_a": 30, "gain_b": 45, "lna_state": 6,
                       "track_id": "0A3F"},
        }
        with patch.object(app_module.calibrator, 'get_status', return_value=done), \
             patch.object(app_module.apply_service, 'request',
                          return_value={"state": "running"}) as queued:
            resp = app_client.post('/calibrate/apply')
        # The tuning is written synchronously; the merge+restart is queued, so
        # this returns immediately rather than blocking the browser for ~45s.
        assert resp.status_code == 202
        body = resp.get_json()
        assert body["success"] is True
        assert body["status"]["state"] == "running"
        queued.assert_called_once_with()

        user_path, _ = config_files
        with open(user_path) as f:
            user = yaml.safe_load(f)
        assert user['capture']['fc'] == 105_100_000
        assert user['capture']['device']['gainReduction'] == [30, 45]
        assert user['capture']['device']['lnaState'] == 6

    def test_apply_moves_the_transmitter_with_the_frequency(self, app_client, config_files):
        """A run that settles on an alternate tower must persist that tower's
        position too. blah2 computes bistatic geometry from location.tx, so
        writing the new fc against the old tower's coordinates leaves the node
        processing one tower's signal as if it came from another - and the
        Configuration page still naming the tower it is no longer using."""
        import app as app_module
        moved = {
            "state": "done",
            "result": {"tower_name": "KQED-TV", "fc": 105_100_000,
                       "tx": {"latitude": 37.75, "longitude": -122.45,
                              "altitude": 310.0},
                       "gain_a": 30, "gain_b": 45, "lna_state": 6,
                       "track_id": "0A3F"},
        }
        with patch.object(app_module.calibrator, 'get_status', return_value=moved), \
             patch.object(app_module.apply_service, 'request',
                          return_value={"state": "running"}):
            resp = app_client.post('/calibrate/apply')
        assert resp.status_code == 202

        user_path, _ = config_files
        with open(user_path) as f:
            user = yaml.safe_load(f)
        assert user['capture']['fc'] == 105_100_000
        assert user['location']['tx']['latitude'] == 37.75
        assert user['location']['tx']['longitude'] == -122.45
        assert user['location']['tx']['altitude'] == 310.0
        assert user['location']['tx']['name'] == 'KQED-TV'
        # The receiver is nothing to do with the tower search and must survive
        # untouched - it is the one half of location the owner measured.
        assert user['location']['rx']['name'] == '150 Mississippi'
        assert user['location']['rx']['latitude'] == 37.7644

    def test_apply_leaves_the_transmitter_alone_when_the_tower_did_not_change(
            self, app_client, config_files):
        """The currently-configured tower is offered without a tx block (see
        _towers_to_alternates), so a run that stays put must not rewrite the
        location the owner chose - not even with the same values."""
        import app as app_module
        stayed = {
            "state": "done",
            "result": {"tower_name": "Current tower", "fc": 105_100_000,
                       "gain_a": 30, "gain_b": 45, "lna_state": 6},
        }
        with patch.object(app_module.calibrator, 'get_status', return_value=stayed), \
             patch.object(app_module.apply_service, 'request',
                          return_value={"state": "running"}):
            resp = app_client.post('/calibrate/apply')
        assert resp.status_code == 202
        assert resp.get_json()["persisted"]["tx"] is None

        user_path, _ = config_files
        with open(user_path) as f:
            user = yaml.safe_load(f)
        assert user['location']['tx']['name'] == 'KSCZ-LD'
        assert user['location']['tx']['latitude'] == 37.49917

    def test_apply_truncates_an_over_long_tower_name(self, app_client, config_files):
        """TX_NAME_MAX_LENGTH is retina-telemetry's tx_callsign limit: a longer
        name means the node cannot build a NodeConfig at all, so it would stop
        registering. Live tower-finder results are not length-checked anywhere
        on this path."""
        import app as app_module
        from config_schema import TX_NAME_MAX_LENGTH
        moved = {
            "state": "done",
            "result": {"tower_name": "K" * (TX_NAME_MAX_LENGTH + 20),
                       "fc": 105_100_000,
                       "tx": {"latitude": 37.75, "longitude": -122.45,
                              "altitude": 310.0},
                       "gain_a": 30, "gain_b": 45, "lna_state": 6},
        }
        with patch.object(app_module.calibrator, 'get_status', return_value=moved), \
             patch.object(app_module.apply_service, 'request',
                          return_value={"state": "running"}):
            resp = app_client.post('/calibrate/apply')
        assert resp.status_code == 202

        user_path, _ = config_files
        with open(user_path) as f:
            user = yaml.safe_load(f)
        assert len(user['location']['tx']['name']) == TX_NAME_MAX_LENGTH

    def test_apply_reports_exactly_what_it_persisted(self, app_client, config_files):
        """The Configuration page is not reloaded by persisting, so it fills
        its own fields from this payload. Without it the page keeps showing the
        pre-calibration tuning, and the next Save posts those stale values back
        over the calibration (compute_user_overrides drops an override that
        matches the merged config)."""
        import app as app_module
        moved = {
            "state": "done",
            "result": {"tower_name": "KQED-TV", "fc": 105_100_000,
                       "tx": {"latitude": 37.75, "longitude": -122.45,
                              "altitude": 310.0},
                       "gain_a": 30, "gain_b": 45, "lna_state": 6},
        }
        with patch.object(app_module.calibrator, 'get_status', return_value=moved), \
             patch.object(app_module.apply_service, 'request',
                          return_value={"state": "running"}):
            resp = app_client.post('/calibrate/apply')
        persisted = resp.get_json()["persisted"]
        assert persisted["fc"] == 105_100_000
        assert persisted["gain_a"] == 30
        assert persisted["gain_b"] == 45
        assert persisted["lna_state"] == 6
        # Reported as well as written: the AGC Bandwidth field on the page has
        # to move too, or a later Save puts hardware AGC back on.
        assert persisted["bandwidth_number"] == 0
        assert persisted["tx"]["name"] == "KQED-TV"
        assert persisted["tx"]["latitude"] == 37.75

        # What was reported is what is on disk, field for field.
        user_path, _ = config_files
        with open(user_path) as f:
            user = yaml.safe_load(f)
        assert persisted["fc"] == user['capture']['fc']
        assert [persisted["gain_a"], persisted["gain_b"]] == \
            user['capture']['device']['gainReduction']
        assert persisted["lna_state"] == user['capture']['device']['lnaState']
        assert persisted["bandwidth_number"] == user['capture']['device']['bandwidthNumber']
        assert persisted["tx"] == user['location']['tx']


class TestAlternateTowers:
    """routes/calibrate._towers_to_alternates - where a candidate tower picks
    up the transmitter position that has to be persisted with its fc."""

    @staticmethod
    def _tower(**overrides):
        tower = {"callsign": "KQED-TV", "frequency_mhz": 105.1,
                 "latitude": 37.75, "longitude": -122.45, "altitude_m": 310}
        tower.update(overrides)
        return tower

    def test_alternates_carry_their_transmitter_position(self):
        from routes.calibrate import _towers_to_alternates
        alternates = _towers_to_alternates([self._tower()], 98_000_000, 2)
        assert alternates[0]["fc"] == 105_100_000
        assert alternates[0]["tx"] == {"latitude": 37.75, "longitude": -122.45,
                                       "altitude": 310.0}

    def test_a_tower_without_coordinates_is_searchable_but_not_persistable(self):
        """fc alone is all a run needs to tune, so the tower is still worth
        trying - but persisting its frequency against the previous tower's
        position would be worse than either tower on its own."""
        from routes.calibrate import _towers_to_alternates
        alternates = _towers_to_alternates([self._tower(latitude=None)],
                                           98_000_000, 2)
        assert alternates[0]["fc"] == 105_100_000
        assert alternates[0]["tx"] is None

    def test_a_missing_altitude_defaults_to_sea_level(self):
        """Matches what the wizard's tower select does with the same records
        (tx_altitude: selectedTower.altitude_m || 0)."""
        from routes.calibrate import _towers_to_alternates
        alternates = _towers_to_alternates([self._tower(altitude_m=None)],
                                           98_000_000, 2)
        assert alternates[0]["tx"]["altitude"] == 0.0


REPO_ROOT = os.path.join(os.path.dirname(__file__), '..')


class TestSharedCalibrateDriver:
    """static/calibrate.js is consumed by two callers that cannot be
    unit-tested here (browser JS). The one failure this guards is the one the
    extraction introduced: a consumer referencing something the module does
    not export, which silently breaks the Auto-Calibrate modal at runtime."""

    @staticmethod
    def _module_surface():
        import re
        with open(os.path.join(REPO_ROOT, 'static', 'calibrate.js')) as f:
            src = f.read()
        # The `return { ... }` block at the end is the public surface.
        tail = src[src.rindex('return {'):]
        return set(re.findall(r'^\s{8}(\w+):', tail, re.M))

    @staticmethod
    def _referenced_names():
        import re
        names = set()
        for rel in (('templates', 'config.html'), ('static', 'setup.js')):
            with open(os.path.join(REPO_ROOT, *rel)) as f:
                src = f.read()
            names |= set(re.findall(r'\bCAL\.(\w+)', src))
            names |= set(re.findall(r'\bwindow\.RetinaCalibrate\.(\w+)', src))
        return names

    def test_every_referenced_helper_is_exported(self):
        missing = self._referenced_names() - self._module_surface()
        assert not missing, (
            f"config.html/setup.js reference {sorted(missing)}, which "
            f"static/calibrate.js does not export - the Auto-Calibrate modal "
            f"and the wizard step would break at runtime")

    def test_both_consumers_actually_load_the_module(self):
        with open(os.path.join(REPO_ROOT, 'templates', 'config.html')) as f:
            cfg = f.read()
        with open(os.path.join(REPO_ROOT, 'templates', 'setup.html')) as f:
            setup = f.read()
        assert '/static/calibrate.js' in cfg
        assert '/static/calibrate.js' in setup
        # Order matters: the module must be parsed before the page script
        # that dereferences window.RetinaCalibrate at IIFE-evaluation time.
        assert cfg.index('/static/calibrate.js') < cfg.index('RetinaCalibrate;')
        assert setup.index('/static/calibrate.js') < setup.index('/static/setup.js')

    def test_config_page_no_longer_defines_its_own_copies(self):
        """The point of the extraction. If these come back, the two entry
        points can disagree about what a status means."""
        with open(os.path.join(REPO_ROOT, 'templates', 'config.html')) as f:
            cfg = f.read()
        for dupe in ('var OUTCOME_TEXT', 'function diagnose(', 'function mhz(',
                     'function preflightNotice(', 'function updateWarning('):
            assert dupe not in cfg, f"{dupe} is defined again in config.html"


class TestPersistedTuningReachesTheForm:
    """The Configuration page fills its own fields from /calibrate/apply's
    `persisted` payload, because persisting does not reload the page.

    Two silent failure modes to guard, both invisible in a browser until a
    user loses a calibration: a field name that no longer matches the form
    (adoptSaved simply finds nothing and leaves the stale value in place),
    and a persisted key the page never reads."""

    @staticmethod
    def _adopt_block():
        with open(os.path.join(REPO_ROOT, 'templates', 'config.html')) as f:
            src = f.read()
        start = src.index('var TX_FIELDS = {')
        return src[start:src.index('applyBtn.addEventListener', start)]

    def test_every_field_name_it_writes_exists_on_the_form(self):
        import re

        from config_schema import CaptureFormConfig, LocationFormConfig

        def field_names(model, section):
            fields = getattr(model, 'model_fields', None) or model.__fields__
            return {f'{section}.{name}' for name in fields}

        form_fields = (field_names(CaptureFormConfig, 'capture')
                       | field_names(LocationFormConfig, 'location'))
        written = set(re.findall(r"'((?:capture|location)\.\w+)'",
                                 self._adopt_block()))
        assert written, "the page no longer writes any field - fix this test"
        missing = written - form_fields
        assert not missing, (
            f"config.html writes {sorted(missing)} after persisting a "
            f"calibration, but no form field is named that - the stale value "
            f"stays on the page and the next Save undoes the calibration")

    def test_it_reads_every_value_the_route_reports(self, app_client, config_files):
        """A key added to `persisted` that the page ignores is a setting that
        silently stays stale on screen."""
        import app as app_module
        moved = {
            "state": "done",
            "result": {"tower_name": "KQED-TV", "fc": 105_100_000,
                       "tx": {"latitude": 37.75, "longitude": -122.45,
                              "altitude": 310.0},
                       "gain_a": 30, "gain_b": 45, "lna_state": 6},
        }
        with patch.object(app_module.calibrator, 'get_status', return_value=moved), \
             patch.object(app_module.apply_service, 'request',
                          return_value={"state": "running"}):
            persisted = app_client.post('/calibrate/apply').get_json()["persisted"]

        block = self._adopt_block()
        for key in persisted:
            if key == 'tx':
                continue  # read through TX_FIELDS, keyed by the tx sub-keys
            assert f'persisted.{key}' in block, (
                f"/calibrate/apply reports '{key}' but config.html never "
                f"puts it into a form field")
        for key in persisted['tx']:
            assert f'{key}:' in block, (
                f"the persisted transmitter carries '{key}' but config.html "
                f"has no field mapping for it")
