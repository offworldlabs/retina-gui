"""Tests for the Auto-Calibrate feature.

Calibrator logic runs against a scripted FakeBlah2Client (no real HTTP, no
SDR hardware); route guards run against the Flask test client; telemetry
payload assembly is tested directly.
"""
import json
import os
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
import yaml

import calibrator as calmod
from calibrator import (
    Calibrator,
    EVIDENCE_ACTIVE,
    EVIDENCE_DETECTIONS,
    GAIN_REDUCTION_MIN,
    GAIN_REDUCTION_MAX,
)
import calibration_telemetry
from device_state import DeviceState


class FakeBlah2Client:
    """Scripted stand-in for Blah2Client.

    Overload behaviour is a rule over the currently-applied tuning; tracker
    and detection responses are callables receiving this client so tests can
    key them off the current tuning or the fake clock.
    """

    def __init__(self, overload_rule=None, tracker=None, detection=None,
                 adsb_tracks=None):
        self.clock_ms = 1000
        self.generation = 0
        self.applied = []
        self.retune_error = None
        self.ack_enabled = True
        self.rf_enabled = True
        self.overload_rule = overload_rule or (lambda fc, ga, gb: (False, False))
        self.tracker = tracker or (lambda client: None)
        self.detection = detection or (lambda client: None)
        self.adsb_tracks = adsb_tracks or (lambda client: None)

    def _now(self):
        self.clock_ms += 10
        return self.clock_ms

    @property
    def current(self):
        return self.applied[-1] if self.applied else None

    def retune(self, fc, gain_a, gain_b):
        if self.retune_error:
            return None, self.retune_error
        self.generation += 1
        self.applied.append({
            "fc": fc, "gain_a": gain_a, "gain_b": gain_b,
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
            "appliedAt": last["applied_at"],
        }

    def get_rf_status(self):
        if not self.rf_enabled or not self.applied:
            return None
        cur = self.applied[-1]
        overload_a, overload_b = self.overload_rule(
            cur["fc"], cur["gain_a"], cur["gain_b"])
        return {"overloadA": overload_a, "overloadB": overload_b,
                "timestamp": self._now()}

    def get_tracker(self):
        return self.tracker(self)

    def get_detection(self):
        return self.detection(self)

    def get_adsb_tracks(self):
        return self.adsb_tracks(self)


ORIGINAL = {"fc": 98_000_000, "gain_a": 40, "gain_b": 41}
TOWER = {"name": "Tower One", "fc": 98_000_000}
TOWER_TWO = {"name": "Tower Two", "fc": 105_100_000}


def active_track(client):
    return {"timestamp": client._now(), "n": 1, "nTentative": 0,
            "nAssociated": 0, "nActive": 1, "nCoasting": 0,
            "data": [{"id": "0A3F", "state": "ACTIVE"}]}


def empty_track(client):
    return {"timestamp": client._now(), "n": 0, "nTentative": 0,
            "nAssociated": 0, "nActive": 0, "nCoasting": 0, "data": []}


def active_track_at(delay, doppler):
    """An ACTIVE track factory carrying delay/doppler, for ADS-B match tests."""
    def make(client):
        return {"timestamp": client._now(), "n": 1, "nTentative": 0,
                "nAssociated": 0, "nActive": 1, "nCoasting": 0,
                "data": [{"id": "0A3F", "state": "ACTIVE",
                         "delay": delay, "doppler": doppler}]}
    return make


def adsb_aircraft_at(delay, doppler, hex_id="ABC123", flight="TEST1"):
    """A single-aircraft /api/adsb2dd response factory."""
    def make(client):
        return {hex_id: {"hex": hex_id, "flight": flight,
                         "delay": delay, "doppler": doppler}}
    return make


@pytest.fixture
def fast(monkeypatch):
    """Shrink all protocol timings so runs finish in milliseconds."""
    monkeypatch.setattr(calmod, "OVERLOAD_SETTLE_SECONDS", 0.01)
    monkeypatch.setattr(calmod, "ACK_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(calmod, "ACK_POLL_SECONDS", 0.005)
    monkeypatch.setattr(calmod, "RF_STATUS_TIMEOUT_SECONDS", 0.1)
    monkeypatch.setattr(calmod, "RF_STATUS_POLL_SECONDS", 0.005)
    monkeypatch.setattr(calmod, "DWELL_POLL_SECONDS", 0.01)


def run_to_completion(cal, towers, original=ORIGINAL, budget=10, dwell=0.2):
    started, error = cal.start(towers, original, budget_seconds=budget,
                               dwell_seconds=dwell)
    assert started, error
    cal._thread.join(timeout=10)
    assert not cal._thread.is_alive(), "calibration thread did not finish"
    return cal.get_status()


class TestDescent:
    def test_clean_at_max_gain_needs_no_backoff(self, fast):
        client = FakeBlah2Client(tracker=active_track)
        status = run_to_completion(Calibrator(client), [TOWER])
        assert status["state"] == "done"
        assert status["result"]["gain_a"] == GAIN_REDUCTION_MIN
        assert status["result"]["gain_b"] == GAIN_REDUCTION_MIN

    def test_backs_off_only_overloaded_tuner(self, fast):
        # tuner A overloads below 40 dB reduction; B is always clean
        client = FakeBlah2Client(
            overload_rule=lambda fc, ga, gb: (ga < 40, False),
            tracker=active_track)
        status = run_to_completion(Calibrator(client), [TOWER])
        assert status["state"] == "done"
        # descent 20->30->40 clean; refine tries 35 (overloads) and reverts
        assert status["result"]["gain_a"] == 40
        assert status["result"]["gain_b"] == GAIN_REDUCTION_MIN

    def test_refine_keeps_lower_gain_reduction_when_clean(self, fast):
        # A overloads below 33: descent lands at 40, refine 35 stays clean
        client = FakeBlah2Client(
            overload_rule=lambda fc, ga, gb: (ga < 33, False),
            tracker=active_track)
        status = run_to_completion(Calibrator(client), [TOWER])
        assert status["state"] == "done"
        assert status["result"]["gain_a"] == 35

    def test_persistent_overload_stops_at_floor(self, fast):
        # B overloads no matter what — descent must terminate at max reduction
        client = FakeBlah2Client(
            overload_rule=lambda fc, ga, gb: (False, True),
            tracker=active_track)
        status = run_to_completion(Calibrator(client), [TOWER])
        assert status["state"] == "done"
        assert status["result"]["gain_b"] == GAIN_REDUCTION_MAX

    def test_descend_stops_at_an_already_past_deadline(self, fast):
        """A deadline in the past must still let the first settle+read
        complete (so the returned gain/applied_at stay consistent with what
        was actually applied), but must not attempt any further candidates
        or the refine step."""
        import time
        client = FakeBlah2Client(
            overload_rule=lambda fc, ga, gb: (ga < 40, False))
        cal = Calibrator(client)
        gain_a, gain_b, applied_at = cal._descend(
            TOWER["fc"], [], deadline=time.monotonic() - 1)
        assert (gain_a, gain_b) == (GAIN_REDUCTION_MIN, GAIN_REDUCTION_MIN)
        assert len(client.applied) == 1  # only the initial candidate, no backoff


class TestDwell:
    def test_success_leaves_blah2_on_winner(self, fast):
        client = FakeBlah2Client(tracker=active_track)
        status = run_to_completion(Calibrator(client), [TOWER])
        assert status["state"] == "done"
        assert status["result"]["track_id"] == "0A3F"
        assert status["result"]["tower_name"] == "Tower One"
        # no restore: last applied tuning is the winner, not the original
        assert client.current["fc"] == TOWER["fc"]
        assert client.current["gain_a"] == status["result"]["gain_a"]

    def test_no_track_restores_original(self, fast):
        client = FakeBlah2Client(tracker=empty_track)
        status = run_to_completion(Calibrator(client), [TOWER])
        assert status["state"] == "failed"
        assert "No confirmed track" in status["error"]
        assert client.current["fc"] == ORIGINAL["fc"]
        assert client.current["gain_a"] == ORIGINAL["gain_a"]
        assert client.current["gain_b"] == ORIGINAL["gain_b"]
        # status must reflect the real (restored) hardware state, not the
        # last failed candidate it was still showing mid-run
        assert status["current"]["fc"] == ORIGINAL["fc"]
        assert status["current"]["gain_a"] == ORIGINAL["gain_a"]
        assert status["current"]["gain_b"] == ORIGINAL["gain_b"]

    def test_cancel_restores_original(self, fast):
        import time
        client = FakeBlah2Client(tracker=empty_track)
        cal = Calibrator(client)
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

    def test_stale_tracker_data_is_ignored(self, fast):
        # tracker reports an ACTIVE track with a timestamp older than the
        # retune — pre-retune data must not count as success
        def stale_track(client):
            return {"timestamp": 1, "nActive": 1,
                    "data": [{"id": "0001", "state": "ACTIVE"}]}
        client = FakeBlah2Client(tracker=stale_track)
        status = run_to_completion(Calibrator(client), [TOWER])
        assert status["state"] == "failed"

    def test_best_attempt_records_detection_evidence(self, fast):
        def detections(client):
            return {"timestamp": client._now(), "delay": [1.0, 2.0],
                    "doppler": [10.0, -5.0], "snr": [12.0, 8.0]}
        client = FakeBlah2Client(tracker=empty_track, detection=detections)
        status = run_to_completion(Calibrator(client), [TOWER])
        assert status["state"] == "failed"
        best = status["best_attempt"]
        assert best["evidence"] == EVIDENCE_DETECTIONS
        assert best["max_detections"] == 2
        assert "detections seen" in best["reason"]


class TestMultiTower:
    def test_falls_through_to_second_tower(self, fast):
        # only Tower Two's frequency ever confirms a track
        def tracker(client):
            if client.current and client.current["fc"] == TOWER_TWO["fc"]:
                return active_track(client)
            return empty_track(client)
        client = FakeBlah2Client(tracker=tracker)
        status = run_to_completion(Calibrator(client), [TOWER, TOWER_TWO])
        assert status["state"] == "done"
        assert status["result"]["tower_name"] == "Tower Two"
        assert len(status["history"]) == 2
        assert status["history"][0]["outcome"] == "no_confirmed_track"
        assert status["history"][1]["outcome"] == "confirmed_track"

    def test_dynamic_dwell_splits_budget_fairly_across_towers(self, fast):
        """Without a dwell_seconds override (the production path), each
        tower's dwell comes out of the remaining budget divided by the
        remaining towers — no fixed-per-tower window that could overrun,
        and no tower silently starved to zero."""
        client = FakeBlah2Client(tracker=empty_track)
        cal = Calibrator(client)
        started, error = cal.start([TOWER, TOWER_TWO], ORIGINAL, budget_seconds=0.6)
        assert started, error
        cal._thread.join(timeout=10)
        status = cal.get_status()
        assert status["state"] == "failed"
        assert len(status["history"]) == 2
        for entry in status["history"]:
            assert entry["outcome"] == "no_confirmed_track"
            assert entry.get("dwell_seconds", 0) > 0

    def test_slow_descent_yields_honest_skipped_outcome(self, fast):
        """If a tower's own descent consumes its whole share of the budget,
        it must be marked as never actually watched — not as a false
        'checked, nothing there'."""
        # Every candidate overloads on A, forcing repeated backoff — with a
        # tiny total budget the first tower's descent alone exceeds its share.
        client = FakeBlah2Client(overload_rule=lambda fc, ga, gb: (True, False))
        cal = Calibrator(client)
        started, error = cal.start([TOWER, TOWER_TWO], ORIGINAL, budget_seconds=0.015)
        assert started, error
        cal._thread.join(timeout=10)
        status = cal.get_status()
        assert status["state"] == "failed"
        assert any(e["outcome"] == "skipped_no_time" for e in status["history"])


class TestAdsbMode:
    def test_matched_track_succeeds(self, fast):
        client = FakeBlah2Client(
            tracker=active_track_at(delay=10.0, doppler=50.0),
            adsb_tracks=adsb_aircraft_at(delay=10.5, doppler=51.0))
        cal = Calibrator(client)
        started, error = cal.start([TOWER], ORIGINAL, budget_seconds=10,
                                   dwell_seconds=0.2, mode=calmod.MODE_ADSB)
        assert started, error
        cal._thread.join(timeout=10)
        status = cal.get_status()
        assert status["state"] == "done"
        assert status["result"]["adsb_hex"] == "ABC123"
        assert status["result"]["adsb_flight"] == "TEST1"

    def test_confirmed_track_without_match_does_not_succeed(self, fast):
        # blah2 confirms a track, but it's nowhere near any known aircraft
        client = FakeBlah2Client(
            tracker=active_track_at(delay=10.0, doppler=50.0),
            adsb_tracks=adsb_aircraft_at(delay=200.0, doppler=-300.0))
        cal = Calibrator(client)
        started, error = cal.start([TOWER], ORIGINAL, budget_seconds=0.3,
                                   dwell_seconds=0.2, mode=calmod.MODE_ADSB)
        assert started, error
        cal._thread.join(timeout=10)
        status = cal.get_status()
        assert status["state"] == "failed"
        assert status["result"] is None
        assert "doesn't match a known aircraft" in status["best_attempt"]["reason"]

    def test_no_adsb_feed_does_not_crash_and_does_not_succeed(self, fast):
        # adsb_tracks returning None simulates an unreachable/disabled feed
        client = FakeBlah2Client(tracker=active_track_at(delay=10.0, doppler=50.0))
        cal = Calibrator(client)
        started, error = cal.start([TOWER], ORIGINAL, budget_seconds=0.3,
                                   dwell_seconds=0.2, mode=calmod.MODE_ADSB)
        assert started, error
        cal._thread.join(timeout=10)
        status = cal.get_status()
        assert status["state"] == "failed"

    def test_track_mode_ignores_adsb_entirely(self, fast):
        # default mode must succeed on confirmation alone, regardless of
        # whether ADS-B truth would have matched
        client = FakeBlah2Client(
            tracker=active_track_at(delay=10.0, doppler=50.0),
            adsb_tracks=adsb_aircraft_at(delay=200.0, doppler=-300.0))
        status = run_to_completion(Calibrator(client), [TOWER])
        assert status["state"] == "done"

    def test_invalid_mode_rejected(self, fast):
        cal = Calibrator(FakeBlah2Client())
        started, error = cal.start([TOWER], ORIGINAL, mode="bogus")
        assert not started
        assert "Invalid mode" in error


class TestFailureModes:
    def test_unreachable_blah2_fails_the_run(self, fast):
        client = FakeBlah2Client()
        client.retune_error = "connection refused"
        status = run_to_completion(Calibrator(client), [TOWER])
        assert status["state"] == "failed"
        assert "Retune failed" in status["error"]

    def test_missing_ack_fails_the_run(self, fast):
        client = FakeBlah2Client()
        client.ack_enabled = False
        status = run_to_completion(Calibrator(client), [TOWER])
        assert status["state"] == "failed"
        assert "acknowledge" in status["error"]

    def test_missing_rf_status_fails_the_run(self, fast):
        client = FakeBlah2Client(tracker=active_track)
        client.rf_enabled = False
        status = run_to_completion(Calibrator(client), [TOWER])
        assert status["state"] == "failed"
        assert "RF status" in status["error"]

    def test_ignore_cancel_lets_apply_proceed_despite_a_pending_cancel(self, fast):
        """The restore-on-failure path must not be abortable by a second
        cancel arriving while it's in flight — otherwise blah2 could be left
        tuned to the last failed candidate instead of the original setting."""
        client = FakeBlah2Client()
        cal = Calibrator(client)
        cal._cancel.set()  # simulates a cancel already pending/re-arriving
        applied_at = cal._apply(ORIGINAL["fc"], ORIGINAL["gain_a"],
                                ORIGINAL["gain_b"], ignore_cancel=True)
        assert applied_at is not None
        assert client.current["fc"] == ORIGINAL["fc"]
        # without the flag, the same call must still raise as before
        with pytest.raises(calmod._Cancelled):
            cal._apply(ORIGINAL["fc"], ORIGINAL["gain_a"], ORIGINAL["gain_b"])

    def test_cannot_start_twice(self, fast):
        client = FakeBlah2Client(tracker=empty_track)
        cal = Calibrator(client)
        started, _ = cal.start([TOWER], ORIGINAL, budget_seconds=30,
                               dwell_seconds=30)
        assert started
        started_again, error = cal.start([TOWER], ORIGINAL)
        assert not started_again
        assert "already running" in error
        cal.cancel()
        cal._thread.join(timeout=10)

    def test_on_complete_fires_with_terminal_status(self, fast):
        client = FakeBlah2Client(tracker=active_track)
        cal = Calibrator(client)
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

    def test_start_refuses_adsb_mode_when_truth_disabled(self, app_client, config_files):
        def disable_adsb(merged):
            merged['truth']['adsb']['enabled'] = False
        self._set_merged(config_files, disable_adsb)
        resp = app_client.post('/calibrate/start', json={"mode": "adsb", "scope": "current_tower"})
        assert resp.status_code == 409
        assert "ADS-B" in resp.get_json()["error"]

    def test_start_accepts_adsb_mode_when_truth_enabled(self, app_client):
        import app as app_module
        with patch.object(app_module.calibrator, 'start',
                          return_value=(True, None)) as mock_start:
            resp = app_client.post('/calibrate/start', json={"mode": "adsb", "scope": "current_tower"})
        assert resp.status_code == 200
        assert resp.get_json()["mode"] == "adsb"
        app_module.device_state.release_calibration_lock()

        kwargs = mock_start.call_args.kwargs
        assert kwargs["mode"] == "adsb"
        assert kwargs["adsb_delay_tolerance"] == 2.0
        assert kwargs["adsb_doppler_tolerance"] == 5.0

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

    def test_apply_writes_user_config(self, app_client, config_files):
        import app as app_module
        import routes.mode as mode_module
        done = {
            "state": "done",
            "started_at": "2026-07-08T00:00:00+00:00",
            "result": {"tower_name": "Tower One", "fc": 105_100_000,
                       "gain_a": 30, "gain_b": 45, "track_id": "0A3F"},
        }
        with patch.object(app_module.calibrator, 'get_status', return_value=done), \
             patch.object(mode_module, 'run_config_merger_and_restart',
                          return_value=None):
            resp = app_client.post('/calibrate/apply')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True and body["applied"] is True

        user_path, _ = config_files
        with open(user_path) as f:
            user = yaml.safe_load(f)
        assert user['capture']['fc'] == 105_100_000
        assert user['capture']['device']['gainReduction'] == [30, 45]


class TestTelemetry:
    def test_run_report_payload(self):
        status = {
            "state": "done", "started_at": "s", "finished_at": "f",
            "error": None, "original": ORIGINAL,
            "progress": {"retunes": 5},
            "history": [{"tower_name": "Tower One"}],
            "best_attempt": None,
            "result": {"fc": 98_000_000},
        }
        report = calibration_telemetry.build_run_report(status, "ret123",
                                                        {"latitude": 1.0})
        assert report["schema"] == 1
        assert report["event"] == "run_summary"
        assert report["node_id"] == "ret123"
        assert report["run"]["state"] == "done"
        assert report["run"]["history"] == [{"tower_name": "Tower One"}]
        assert report["run"]["original"] == ORIGINAL

    def test_applied_event_payload(self):
        status = {"started_at": "s", "result": {"fc": 98_000_000}}
        event = calibration_telemetry.build_applied_event(status, "ret123")
        assert event["event"] == "applied"
        assert event["result"]["fc"] == 98_000_000

    def test_empty_url_sends_nothing(self):
        with patch('calibration_telemetry.requests.post') as mock_post:
            sent = calibration_telemetry.send_run_report("", {}, "n", None)
        assert sent is False
        mock_post.assert_not_called()

    def test_send_failure_is_swallowed(self):
        with patch('calibration_telemetry.requests.post',
                   side_effect=Exception("boom")):
            sent = calibration_telemetry.send_run_report(
                "http://example.invalid", {}, "n", None)
        assert sent is False
