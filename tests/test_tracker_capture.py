"""Tests for TrackerCaptureService's always-on capture / lazy-render lifecycle.

Stubs matplotlib's savefig/close and retina-tracker's Tracker/get_config so
these tests exercise only our own threading/history/render-gating logic,
not retina-tracker's Kalman filtering or matplotlib's actual rendering —
matching tests/test_calibrate.py's approach of not re-testing a
dependency's own internals.
"""

import time

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


def stub_render(monkeypatch, service, side_effect=None):
    """Replace TrackerCaptureService._render so tests don't need real
    matplotlib rendering — just record that it ran."""
    calls = []

    def fake_render(self, history, tracks_only=False):
        calls.append(history)
        if side_effect:
            side_effect()
        with self._lock:
            self._latest_image = b"fake-png"

    monkeypatch.setattr(tracker_capture.TrackerCaptureService, "_render", fake_render)
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


# ── Always-on capture lifecycle ─────────────────────────────────────────────

def test_start_runs_immediately_with_zero_viewers():
    client = FakeBlah2Client([])
    service = tracker_capture.TrackerCaptureService(client)
    service.start()
    assert _wait_until(service.is_running)


def test_detach_does_not_stop_capture(monkeypatch):
    stub_render(monkeypatch, None)
    client = FakeBlah2Client([])
    service = tracker_capture.TrackerCaptureService(client)
    service.start()
    assert _wait_until(service.is_running)

    q = service.attach()
    service.detach(q)
    time.sleep(0.1)
    # Unlike Phase 2, capture never stops on detach — only rendering does.
    assert service.is_running()


def test_no_render_without_viewers(monkeypatch):
    calls = stub_render(monkeypatch, None)
    client = FakeBlah2Client([make_frame(1000), make_frame(2000), make_frame(3000)])
    service = tracker_capture.TrackerCaptureService(client)
    service.start()

    time.sleep(0.3)  # plenty of render-interval ticks with data and zero viewers
    assert calls == []


def test_render_only_after_attach(monkeypatch):
    calls = stub_render(monkeypatch, None)
    client = FakeBlah2Client([make_frame(1000)])
    service = tracker_capture.TrackerCaptureService(client)
    service.start()
    time.sleep(0.1)
    assert calls == []  # no viewer yet

    q = service.attach()
    assert _wait_until(lambda: len(calls) >= 1)
    assert service.latest_image() == b"fake-png"
    service.detach(q)


def test_attach_requests_immediate_render_without_waiting_full_interval(monkeypatch):
    # RENDER_INTERVAL_S is patched small already, so use an artificially large
    # one here to prove attach() bypasses the wait rather than just being fast.
    monkeypatch.setattr(tracker_capture, "RENDER_INTERVAL_S", 10.0)
    calls = stub_render(monkeypatch, None)
    client = FakeBlah2Client([make_frame(1000)])
    service = tracker_capture.TrackerCaptureService(client)
    service.start()
    time.sleep(0.05)  # let the frame land in history before attaching

    q = service.attach()
    assert _wait_until(lambda: len(calls) >= 1, timeout=1.0)
    service.detach(q)


def test_prune_runs_independent_of_viewers(monkeypatch):
    stub_render(monkeypatch, None)
    client = FakeBlah2Client([make_frame(1000)])
    service = tracker_capture.TrackerCaptureService(client)
    service.history.window_s = 0  # anything with a timestamp is immediately "old"
    service.start()

    assert _wait_until(lambda: len(service.history.raw_points) == 1, timeout=1.0)
    # No viewer ever attached, but pruning must still run on its own cadence.
    assert _wait_until(lambda: len(service.history.raw_points) == 0, timeout=1.0)


def test_render_failure_does_not_kill_capture_loop(monkeypatch):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("boom")

    stub_render(monkeypatch, None, side_effect=flaky)
    client = FakeBlah2Client([make_frame(1000), make_frame(2000)])
    service = tracker_capture.TrackerCaptureService(client)
    service.start()

    q = service.attach()
    assert _wait_until(lambda: calls["n"] >= 2, timeout=2.0)
    assert service.is_running()
    service.detach(q)
