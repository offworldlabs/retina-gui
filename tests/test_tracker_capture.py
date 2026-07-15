"""Tests for TrackerCaptureService's always-on capture / lazy-refresh lifecycle.

Stubs retina-tracker's Tracker/get_config so these tests exercise only our
own threading/history/refresh-gating logic, not retina-tracker's Kalman
filtering — matching tests/test_calibrate.py's approach of not re-testing a
dependency's own internals.
"""

import time
from unittest.mock import patch

import pytest

import tracker_capture


class FakeBlah2Client:
    """Scripted stand-in for Blah2Client.get_detection()."""

    def __init__(self, responses=None):
        self._responses = list(responses or [])

    def get_detection(self):
        if not self._responses:
            return None
        return self._responses.pop(0)


class FakeTracker:
    """Records process_frame calls instead of doing real Kalman tracking."""

    def __init__(self, config=None, event_writer=None):
        self.calls = []
        self.event_writer = event_writer

    def process_frame(self, detections, timestamp):
        self.calls.append((detections, timestamp))


def _wait_until(predicate, timeout=2.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


@pytest.fixture(autouse=True)
def fast_intervals(monkeypatch):
    """Tiny intervals so tests don't take real-world seconds/minutes."""
    monkeypatch.setattr(tracker_capture, "POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(tracker_capture, "RENDER_INTERVAL_S", 0.03)
    monkeypatch.setattr(tracker_capture, "PRUNE_INTERVAL_S", 0.05)


@pytest.fixture(autouse=True)
def stub_tracker(monkeypatch):
    monkeypatch.setattr(tracker_capture, "Tracker", FakeTracker)
    monkeypatch.setattr(tracker_capture, "get_config", lambda: {})


def make_frame(ts, delay=1.0, doppler=2.0, snr=3.0):
    return {"timestamp": ts, "delay": [delay], "doppler": [doppler], "snr": [snr]}


def stub_refresh(monkeypatch, service, side_effect=None):
    """Replace TrackerCaptureService._refresh_data so tests don't need to
    build a real snapshot — just record that it ran."""
    calls = []

    def fake_refresh(self, history):
        calls.append(history)
        if side_effect:
            side_effect()
        with self._lock:
            self._latest_data = {"stub": True}

    monkeypatch.setattr(tracker_capture.TrackerCaptureService, "_refresh_data", fake_refresh)
    return calls


# ── HistoryBuffer ──────────────────────────────────────────────────────────

def test_frame_to_detections_basic():
    frame = {"timestamp": 1000, "delay": [1.0, 2.0], "doppler": [10.0, -20.0], "snr": [5.0, 6.0]}
    detections = tracker_capture.frame_to_detections(frame)
    assert detections == [
        {"delay": 1.0, "doppler": 10.0, "snr": 5.0},
        {"delay": 2.0, "doppler": -20.0, "snr": 6.0},
    ]


def test_history_buffer_write_event_accumulates_without_duplicates():
    hist = tracker_capture.HistoryBuffer()
    # Overlapping windows, as retina-tracker's own rolling get_recent_detections
    # would supply on successive calls for the same track.
    hist.write_event("T1", 2000, 2, [
        {"timestamp": 1000, "delay": 1.0, "doppler": 2.0, "snr": 3.0},
        {"timestamp": 2000, "delay": 1.1, "doppler": 2.1, "snr": 3.1},
    ])
    hist.write_event("T1", 3000, 2, [
        {"timestamp": 2000, "delay": 1.1, "doppler": 2.1, "snr": 3.1},
        {"timestamp": 3000, "delay": 1.2, "doppler": 2.2, "snr": 3.2},
    ])
    assert hist.tracks["T1"] == [
        (1000, 1.0, 2.0, 3.0),
        (2000, 1.1, 2.1, 3.1),
        (3000, 1.2, 2.2, 3.2),
    ]


def test_history_buffer_prune_drops_old_points_and_empty_tracks():
    hist = tracker_capture.HistoryBuffer(window_s=10)
    hist.add_raw(1000, 1.0, 2.0, 3.0)
    hist.add_raw(50000, 1.0, 2.0, 3.0)
    hist.write_event("OLD", 1000, 1, [{"timestamp": 1000, "delay": 1.0, "doppler": 2.0, "snr": 3.0}])
    hist.write_event("NEW", 50000, 1, [{"timestamp": 50000, "delay": 1.0, "doppler": 2.0, "snr": 3.0}])

    hist.prune(now_ms=51000)  # window_s=10 -> cutoff = 41000

    assert [p[0] for p in hist.raw_points] == [50000]
    assert "OLD" not in hist.tracks
    assert "NEW" in hist.tracks


def test_history_buffer_clear_resets_all_collections():
    hist = tracker_capture.HistoryBuffer()
    hist.add_raw(1000, 1.0, 2.0, 3.0)
    hist.write_event("T1", 1000, 1, [{"timestamp": 1000, "delay": 1.0, "doppler": 2.0, "snr": 3.0}])

    hist.clear()

    assert hist.raw_points == []
    assert hist.tracks == {}
    assert hist._last_track_timestamp == {}


def test_history_buffer_clear_lets_write_event_repopulate_fresh():
    # If _last_track_timestamp weren't cleared too, this would be treated
    # as an already-seen timestamp and silently dropped.
    hist = tracker_capture.HistoryBuffer()
    hist.write_event("T1", 1000, 1, [{"timestamp": 1000, "delay": 1.0, "doppler": 2.0, "snr": 3.0}])
    hist.clear()

    hist.write_event("T1", 1000, 1, [{"timestamp": 1000, "delay": 1.0, "doppler": 2.0, "snr": 3.0}])

    assert hist.tracks["T1"] == [(1000, 1.0, 2.0, 3.0)]


def test_history_buffer_to_dict_empty():
    hist = tracker_capture.HistoryBuffer()
    assert hist.to_dict() == {"raw": [], "tracks": {}}


def test_history_buffer_to_dict_shape():
    hist = tracker_capture.HistoryBuffer()
    hist.add_raw(1000, 1.0, 2.0, 3.0)
    hist.write_event("T1", 1000, 1, [{"timestamp": 1000, "delay": 1.5, "doppler": 2.5, "snr": 4.0}])

    assert hist.to_dict() == {
        "raw": [{"t": 1000, "delay": 1.0, "doppler": 2.0, "snr": 3.0}],
        "tracks": {"T1": [{"t": 1000, "delay": 1.5, "doppler": 2.5, "snr": 4.0}]},
    }


# ── Always-on capture lifecycle ─────────────────────────────────────────────

def test_start_runs_immediately_with_zero_viewers():
    client = FakeBlah2Client([])
    service = tracker_capture.TrackerCaptureService(client)
    service.start()
    assert _wait_until(service.is_running)


def test_detach_does_not_stop_capture(monkeypatch):
    stub_refresh(monkeypatch, None)
    client = FakeBlah2Client([])
    service = tracker_capture.TrackerCaptureService(client)
    service.start()
    assert _wait_until(service.is_running)

    q = service.attach()
    service.detach(q)
    time.sleep(0.1)
    # Unlike Phase 2, capture never stops on detach — only the data refresh does.
    assert service.is_running()


def test_no_refresh_without_viewers(monkeypatch):
    calls = stub_refresh(monkeypatch, None)
    client = FakeBlah2Client([make_frame(1000), make_frame(2000), make_frame(3000)])
    service = tracker_capture.TrackerCaptureService(client)
    service.start()

    time.sleep(0.3)  # plenty of refresh-interval ticks with data and zero viewers
    assert calls == []


def test_refresh_only_after_attach(monkeypatch):
    calls = stub_refresh(monkeypatch, None)
    client = FakeBlah2Client([make_frame(1000)])
    service = tracker_capture.TrackerCaptureService(client)
    service.start()
    time.sleep(0.1)
    assert calls == []  # no viewer yet

    q = service.attach()
    assert _wait_until(lambda: len(calls) >= 1)
    assert service.latest_data() == {"stub": True}
    service.detach(q)


def test_attach_requests_immediate_refresh_without_waiting_full_interval(monkeypatch):
    # RENDER_INTERVAL_S is patched small already, so use an artificially large
    # one here to prove attach() bypasses the wait rather than just being fast.
    monkeypatch.setattr(tracker_capture, "RENDER_INTERVAL_S", 10.0)
    calls = stub_refresh(monkeypatch, None)
    client = FakeBlah2Client([make_frame(1000)])
    service = tracker_capture.TrackerCaptureService(client)
    service.start()
    time.sleep(0.05)  # let the frame land in history before attaching

    q = service.attach()
    assert _wait_until(lambda: len(calls) >= 1, timeout=1.0)
    service.detach(q)


def test_prune_runs_independent_of_viewers(monkeypatch):
    stub_refresh(monkeypatch, None)
    client = FakeBlah2Client([make_frame(1000)])
    service = tracker_capture.TrackerCaptureService(client)
    service.history.window_s = 0  # anything with a timestamp is immediately "old"
    service.start()

    assert _wait_until(lambda: len(service.history.raw_points) == 1, timeout=1.0)
    # No viewer ever attached, but pruning must still run on its own cadence.
    assert _wait_until(lambda: len(service.history.raw_points) == 0, timeout=1.0)


def test_refresh_failure_does_not_kill_capture_loop(monkeypatch):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("boom")

    stub_refresh(monkeypatch, None, side_effect=flaky)
    client = FakeBlah2Client([make_frame(1000), make_frame(2000)])
    service = tracker_capture.TrackerCaptureService(client)
    service.start()

    q = service.attach()
    assert _wait_until(lambda: calls["n"] >= 2, timeout=2.0)
    assert service.is_running()
    service.detach(q)


# ── request_clear() ─────────────────────────────────────────────────────────

# HistoryBuffer.prune() compares point timestamps against real wall-clock
# time (see _run()'s `self.history.prune(int(time.time() * 1000))`), so
# fixed tiny epoch values like make_frame's default `ts=1000` look 50+
# years stale the instant PRUNE_INTERVAL_S (patched to 0.05s) fires — these
# clear() tests run long enough to cross that boundary, so they need
# realistic timestamps or prune() would silently empty raw_points on its
# own, masking whether request_clear() actually did anything.
def _now_ms():
    return int(time.time() * 1000)


def test_request_clear_wipes_history_from_capture_thread():
    # Deliberately not stubbing _refresh_data here — the clear branch's own
    # snapshot reset lives in _run() itself, not in _refresh_data().
    now = _now_ms()
    client = FakeBlah2Client([make_frame(now), make_frame(now + 10)])
    service = tracker_capture.TrackerCaptureService(client)
    service.start()
    assert _wait_until(lambda: len(service.history.raw_points) >= 2, timeout=1.0)

    service.request_clear()

    assert _wait_until(lambda: service.history.raw_points == [], timeout=1.0)
    assert service.history.tracks == {}
    assert service.latest_data() == {"raw": [], "tracks": {}}


def test_request_clear_runs_without_viewers():
    # No viewer ever attached — proves the clear branch is unconditional,
    # same as prune() already is, not gated on has_viewers.
    client = FakeBlah2Client([make_frame(_now_ms())])
    service = tracker_capture.TrackerCaptureService(client)
    service.start()
    assert _wait_until(lambda: len(service.history.raw_points) >= 1, timeout=1.0)

    service.request_clear()

    assert _wait_until(lambda: service.history.raw_points == [], timeout=1.0)


def test_frame_after_clear_populates_fresh_buffer():
    now = _now_ms()
    client = FakeBlah2Client([make_frame(now), make_frame(now + 10)])
    service = tracker_capture.TrackerCaptureService(client)
    service.start()
    assert _wait_until(lambda: len(service.history.raw_points) >= 2, timeout=1.0)

    service.request_clear()
    assert _wait_until(lambda: service.history.raw_points == [], timeout=1.0)

    new_ts = _now_ms()
    client._responses.append(make_frame(new_ts, delay=9.0))
    assert _wait_until(lambda: len(service.history.raw_points) == 1, timeout=1.0)
    assert service.history.raw_points == [(new_ts, 9.0, 2.0, 3.0)]


def test_clear_requested_during_active_capture_does_not_corrupt_state():
    now = _now_ms()
    frames = [make_frame(now + i * 10) for i in range(20)]
    client = FakeBlah2Client(frames)
    service = tracker_capture.TrackerCaptureService(client)
    service.start()

    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        service.request_clear()
        time.sleep(0.005)

    assert _wait_until(lambda: not client._responses, timeout=2.0)
    time.sleep(0.05)  # let the loop settle after the last frame

    assert service.is_running()
    assert all(isinstance(p, tuple) and len(p) == 4 for p in service.history.raw_points)


# ── Routes ───────────────────────────────────────────────────────────────

def test_data_json_returns_empty_snapshot_before_any_capture(app_client):
    resp = app_client.get('/tracker-preview/data.json')
    assert resp.status_code == 200
    assert resp.get_json() == {"raw": [], "tracks": {}}


def test_data_json_returns_current_snapshot(app_client):
    import app as app_module

    app_module.tracker_capture.history.add_raw(1000, 1.0, 2.0, 3.0)
    app_module.tracker_capture._latest_data = app_module.tracker_capture.history.to_dict()

    resp = app_client.get('/tracker-preview/data.json')
    assert resp.get_json()["raw"] == [{"t": 1000, "delay": 1.0, "doppler": 2.0, "snr": 3.0}]


def test_clear_route_calls_request_clear(app_client):
    import app as app_module

    with patch.object(app_module.tracker_capture, 'request_clear') as mock_clear:
        resp = app_client.post('/tracker-preview/clear')

    assert resp.status_code == 200
    assert resp.get_json() == {"success": True}
    mock_clear.assert_called_once()
