"""Tests for TrackerCaptureService's always-on capture / delta-push lifecycle.

Uses a FakeRetinaTrackerClient stand-in for the sidecar so these tests
exercise only our own threading/history/broadcast-gating logic, not
retina-tracker's Kalman filtering (which runs out-of-process now) or the
JS/Plotly frontend — a dependency's own internals aren't re-tested here.
"""

import queue
import time
from unittest.mock import patch

import pytest

import tracker_capture

EMPTY_COLS = {"t": [], "delay": [], "doppler": [], "snr": []}


class FakeBlah2Client:
    """Scripted stand-in for Blah2Client.get_detection()."""

    def __init__(self, responses=None):
        self._responses = list(responses or [])

    def get_detection(self):
        if not self._responses:
            return None
        return self._responses.pop(0)


class FakeRetinaTrackerClient:
    """Stand-in for RetinaTrackerClient: records send_frame calls, and
    lets tests simulate the tailer thread's on_event callback synchronously
    via simulate_event() instead of touching a real socket/file."""

    def __init__(self):
        self.sent_frames = []
        self._on_event = None

    def send_frame(self, frame):
        self.sent_frames.append(frame)

    def start(self, on_event):
        self._on_event = on_event

    def simulate_event(self, event):
        assert self._on_event is not None, "start() not called yet"
        self._on_event(event)


def make_service(client=None, tracker_client=None):
    return tracker_capture.TrackerCaptureService(
        client or FakeBlah2Client([]), tracker_client or FakeRetinaTrackerClient())


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


def make_frame(ts, delay=1.0, doppler=2.0, snr=3.0):
    return {"timestamp": ts, "delay": [delay], "doppler": [doppler], "snr": [snr]}


def spy_broadcast(monkeypatch, side_effect=None):
    """Record every _broadcast(), the capture thread's only display-side
    work now that viewers build their own deltas on their own threads."""
    calls = []
    real = tracker_capture.TrackerCaptureService._broadcast

    def fake(self):
        calls.append(1)
        if side_effect:
            side_effect()
        real(self)

    monkeypatch.setattr(tracker_capture.TrackerCaptureService, "_broadcast", fake)
    return calls


def event(track_id, ts, delay=1.0, doppler=2.0, snr=3.0, **meta):
    return dict({
        "track_id": track_id,
        "timestamp": ts,
        "length": 1,
        "detections": [{"timestamp": ts, "delay": delay, "doppler": doppler, "snr": snr}],
    }, **meta)


# ── HistoryBuffer ──────────────────────────────────────────────────────────

def test_frame_to_detections_basic():
    frame = {"timestamp": 1000, "delay": [1.0, 2.0], "doppler": [10.0, -20.0], "snr": [5.0, 6.0]}
    detections = tracker_capture.frame_to_detections(frame)
    assert detections == [
        {"delay": 1.0, "doppler": 10.0, "snr": 5.0},
        {"delay": 2.0, "doppler": -20.0, "snr": 6.0},
    ]


def test_add_raw_rounds_to_the_measurement_precision():
    hist = tracker_capture.HistoryBuffer()
    hist.add_raw(1000, 123.456789, -45.678912, 12.3456)
    assert hist.raw_points == [(1000, 123.46, -45.68, 12.3)]


def test_write_event_rounds_the_same_way_as_add_raw():
    """Raw and track points must round identically: the browser tells an
    associated detection from an unassociated one by comparing them."""
    hist = tracker_capture.HistoryBuffer()
    hist.add_raw(1000, 123.456789, -45.678912, 12.3456)
    hist.write_event("T1", 1000, 1, [
        {"timestamp": 1000, "delay": 123.456789, "doppler": -45.678912, "snr": 12.3456},
    ])
    assert hist.tracks["T1"] == hist.raw_points


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


def test_write_event_keeps_the_five_metadata_fields():
    hist = tracker_capture.HistoryBuffer()
    hist.write_event(**event(
        "T1", 1000, adsb_hex="4CA2D1", length=42, is_anomalous=True,
        anomaly_types=["supersonic_doppler", "altitude_jump"],
        max_velocity_ms=704.44, shadow_fraction=0.6432,
    ))
    assert hist.track_meta["T1"] == {
        "adsb_hex": "4CA2D1",
        "length": 42,
        "max_velocity_ms": 704.4,
        "is_anomalous": True,
        "anomaly_types": ["altitude_jump", "supersonic_doppler"],
        "shadow_fraction": 0.643,
    }


def test_write_event_ignores_fields_we_deliberately_dropped():
    """adsb_initialized is the one event field discarded on purpose, and an
    unknown field from a future sidecar must not raise."""
    hist = tracker_capture.HistoryBuffer()
    hist.write_event(**event("T1", 1000, adsb_initialized=True, something_new=7))
    assert "adsb_initialized" not in hist.track_meta["T1"]
    assert "something_new" not in hist.track_meta["T1"]


def test_write_event_refreshes_metadata_on_every_update():
    hist = tracker_capture.HistoryBuffer()
    hist.write_event(**event("T1", 1000, length=1, is_anomalous=False))
    hist.write_event(**event("T1", 2000, length=2, is_anomalous=True,
                             anomaly_types=["sustained_orbit"]))
    assert hist.track_meta["T1"]["length"] == 2
    assert hist.track_meta["T1"]["is_anomalous"] is True


def test_history_buffer_prune_drops_old_points_and_empty_tracks():
    hist = tracker_capture.HistoryBuffer(window_s=10)
    hist.add_raw(1000, 1.0, 2.0, 3.0)
    hist.add_raw(50000, 1.0, 2.0, 3.0)
    hist.write_event(**event("OLD", 1000))
    hist.write_event(**event("NEW", 50000))

    hist.prune(now_ms=51000)  # window_s=10 -> cutoff = 41000

    assert [p[0] for p in hist.raw_points] == [50000]
    assert "OLD" not in hist.tracks
    assert "OLD" not in hist.track_meta
    assert "NEW" in hist.tracks


def test_history_buffer_clear_resets_all_collections():
    hist = tracker_capture.HistoryBuffer()
    hist.add_raw(1000, 1.0, 2.0, 3.0)
    hist.write_event(**event("T1", 1000))

    hist.clear()

    assert hist.raw_points == []
    assert hist.tracks == {}
    assert hist.track_meta == {}
    assert hist._last_track_timestamp == {}


def test_history_buffer_clear_lets_write_event_repopulate_fresh():
    # If _last_track_timestamp weren't cleared too, this would be treated
    # as an already-seen timestamp and silently dropped.
    hist = tracker_capture.HistoryBuffer()
    hist.write_event(**event("T1", 1000))
    hist.clear()

    hist.write_event(**event("T1", 1000))

    assert hist.tracks["T1"] == [(1000, 1.0, 2.0, 3.0)]


# ── Snapshots ──────────────────────────────────────────────────────────────

def test_snapshot_empty():
    hist = tracker_capture.HistoryBuffer()
    payload, cursor = hist.snapshot()
    assert payload == {"raw": EMPTY_COLS, "tracks": {}}
    assert cursor == {"gen": 0, "raw": 0, "tracks": {}}


def test_snapshot_is_columnar_and_carries_track_metadata():
    hist = tracker_capture.HistoryBuffer()
    hist.add_raw(1000, 1.0, 2.0, 3.0)
    hist.add_raw(2000, 1.5, 2.5, 3.5)
    hist.write_event(**event("T1", 1000, delay=9.0, adsb_hex="4CA2D1"))

    payload, _ = hist.snapshot()

    assert payload["raw"] == {
        "t": [1000, 2000], "delay": [1.0, 1.5],
        "doppler": [2.0, 2.5], "snr": [3.0, 3.5],
    }
    assert payload["tracks"]["T1"]["t"] == [1000]
    assert payload["tracks"]["T1"]["delay"] == [9.0]
    assert payload["tracks"]["T1"]["meta"]["adsb_hex"] == "4CA2D1"


def test_snapshot_window_limits_what_is_served_without_touching_the_buffer():
    hist = tracker_capture.HistoryBuffer()
    hist.add_raw(1000, 1.0, 2.0, 3.0)
    hist.add_raw(100000, 1.5, 2.5, 3.5)
    hist.write_event(**event("OLD", 1000))
    hist.write_event(**event("NEW", 100000))

    payload, _ = hist.snapshot(window_s=60, now_ms=100000)

    assert payload["raw"]["t"] == [100000]
    assert list(payload["tracks"]) == ["NEW"]
    # Retention is untouched: the window is a display concern only.
    assert len(hist.raw_points) == 2
    assert set(hist.tracks) == {"OLD", "NEW"}


def test_snapshot_cursor_covers_everything_not_just_the_window():
    """Otherwise a windowed viewer's first delta would re-send all the
    history the window had just excluded."""
    hist = tracker_capture.HistoryBuffer()
    hist.add_raw(1000, 1.0, 2.0, 3.0)
    hist.add_raw(100000, 1.5, 2.5, 3.5)

    payload, cursor = hist.snapshot(window_s=60, now_ms=100000)

    assert payload["raw"]["t"] == [100000]
    assert cursor["raw"] == 2

    delta, _ = hist.since(cursor)
    assert delta["raw"]["t"] == []


# ── Deltas ─────────────────────────────────────────────────────────────────

def test_since_returns_only_what_was_appended():
    hist = tracker_capture.HistoryBuffer()
    hist.add_raw(1000, 1.0, 2.0, 3.0)
    _, cursor = hist.snapshot()

    hist.add_raw(2000, 1.5, 2.5, 3.5)
    delta, cursor2 = hist.since(cursor)

    assert delta["raw"] == {"t": [2000], "delay": [1.5], "doppler": [2.5], "snr": [3.5]}
    assert cursor2["raw"] == 2


def test_since_is_empty_when_nothing_changed():
    hist = tracker_capture.HistoryBuffer()
    hist.add_raw(1000, 1.0, 2.0, 3.0)
    _, cursor = hist.snapshot()

    delta, _ = hist.since(cursor)

    assert delta["raw"]["t"] == []
    assert delta["tracks"] == {}


def test_since_sends_a_newly_promoted_track_whole():
    """retina-tracker backfills a track's recent history on promotion, so
    those points are older than the viewer's cursor. A track the cursor has
    never seen must arrive complete rather than truncated at the moment the
    viewer happened to connect."""
    hist = tracker_capture.HistoryBuffer()
    _, cursor = hist.snapshot()

    hist.write_event("T1", 3000, 3, [
        {"timestamp": 1000, "delay": 1.0, "doppler": 2.0, "snr": 3.0},
        {"timestamp": 2000, "delay": 1.1, "doppler": 2.1, "snr": 3.1},
        {"timestamp": 3000, "delay": 1.2, "doppler": 2.2, "snr": 3.2},
    ])
    delta, _ = hist.since(cursor)

    assert delta["tracks"]["T1"]["t"] == [1000, 2000, 3000]


def test_since_omits_tracks_with_no_new_points():
    hist = tracker_capture.HistoryBuffer()
    hist.write_event(**event("T1", 1000))
    _, cursor = hist.snapshot()

    hist.write_event(**event("T2", 2000))
    delta, _ = hist.since(cursor)

    assert list(delta["tracks"]) == ["T2"]


def test_since_survives_a_prune_that_dropped_unseen_points():
    """A viewer's position is a monotonic count, not a list index, so
    pruning the front of the buffer must not shift what it points at."""
    hist = tracker_capture.HistoryBuffer(window_s=10)
    hist.add_raw(1000, 1.0, 2.0, 3.0)
    _, cursor = hist.snapshot()

    hist.add_raw(50000, 1.5, 2.5, 3.5)
    hist.prune(now_ms=51000)  # drops the 1000 point the cursor is past

    delta, _ = hist.since(cursor)
    assert delta["raw"]["t"] == [50000]


def test_since_refuses_a_cursor_from_before_a_clear():
    hist = tracker_capture.HistoryBuffer()
    hist.add_raw(1000, 1.0, 2.0, 3.0)
    _, cursor = hist.snapshot()

    hist.clear()

    delta, new_cursor = hist.since(cursor)
    assert delta is None and new_cursor is None


def test_since_works_again_once_the_viewer_takes_a_fresh_snapshot():
    hist = tracker_capture.HistoryBuffer()
    hist.add_raw(1000, 1.0, 2.0, 3.0)
    hist.clear()

    _, cursor = hist.snapshot()
    hist.add_raw(2000, 1.5, 2.5, 3.5)

    delta, _ = hist.since(cursor)
    assert delta["raw"]["t"] == [2000]


# ── Sidecar integration ─────────────────────────────────────────────────────

def test_run_pushes_raw_frame_to_tracker_client(monkeypatch):
    spy_broadcast(monkeypatch)
    tracker_client = FakeRetinaTrackerClient()
    client = FakeBlah2Client([make_frame(1000, delay=1.5, doppler=2.5, snr=4.0)])
    service = make_service(client, tracker_client)
    service.start()

    assert _wait_until(lambda: len(tracker_client.sent_frames) == 1)
    # The raw frame is pushed, not the per-detection dicts frame_to_detections()
    # builds for add_raw() — retina-tracker's wire format wants the parallel
    # arrays, not per-detection dicts.
    assert tracker_client.sent_frames[0] == {
        "timestamp": 1000, "delay": [1.5], "doppler": [2.5], "snr": [4.0],
    }


def test_on_track_event_writes_into_history_buffer():
    tracker_client = FakeRetinaTrackerClient()
    service = make_service(tracker_client=tracker_client)
    service.start()

    tracker_client.simulate_event({
        "track_id": "T1",
        "timestamp": 1000,
        "length": 1,
        "detections": [{"timestamp": 1000, "delay": 1.5, "doppler": 2.5, "snr": 4.0}],
    })

    assert service.history.tracks["T1"] == [(1000, 1.5, 2.5, 4.0)]


def test_on_track_event_accepts_the_sidecars_full_event_schema():
    """The real writer sends ten fields; write_event must take all of them
    even though only six are kept."""
    tracker_client = FakeRetinaTrackerClient()
    service = make_service(tracker_client=tracker_client)
    service.start()

    tracker_client.simulate_event({
        "track_id": "T1", "adsb_hex": "4CA2D1", "adsb_initialized": True,
        "timestamp": 1000, "length": 1,
        "detections": [{"timestamp": 1000, "delay": 1.5, "doppler": 2.5, "snr": 4.0}],
        "is_anomalous": False, "max_velocity_ms": 231.5,
        "anomaly_types": [], "shadow_fraction": 0.0,
    })

    assert service.history.track_meta["T1"]["adsb_hex"] == "4CA2D1"


def test_start_wires_tracker_client_tailer_to_on_track_event():
    tracker_client = FakeRetinaTrackerClient()
    service = make_service(tracker_client=tracker_client)
    service.start()

    assert tracker_client._on_event == service.on_track_event


# ── Always-on capture lifecycle ─────────────────────────────────────────────

def test_start_runs_immediately_with_zero_viewers():
    client = FakeBlah2Client([])
    service = make_service(client)
    service.start()
    assert _wait_until(service.is_running)


def test_detach_does_not_stop_capture(monkeypatch):
    spy_broadcast(monkeypatch)
    client = FakeBlah2Client([])
    service = make_service(client)
    service.start()
    assert _wait_until(service.is_running)

    q = service.attach()
    service.detach(q)
    time.sleep(0.1)
    # Capture never stops on detach — only the broadcasts do.
    assert service.is_running()


def test_no_broadcast_without_viewers(monkeypatch):
    calls = spy_broadcast(monkeypatch)
    client = FakeBlah2Client([make_frame(1000), make_frame(2000), make_frame(3000)])
    service = make_service(client)
    service.start()

    time.sleep(0.3)  # plenty of cadence ticks with data and zero viewers
    assert calls == []


def test_broadcast_only_after_attach(monkeypatch):
    calls = spy_broadcast(monkeypatch)
    client = FakeBlah2Client([make_frame(1000)])
    service = make_service(client)
    service.start()
    time.sleep(0.1)
    assert calls == []  # no viewer yet

    q = service.attach()
    assert _wait_until(lambda: len(calls) >= 1)
    service.detach(q)


def test_attach_requests_immediate_broadcast_without_waiting_full_interval(monkeypatch):
    # RENDER_INTERVAL_S is patched small already, so use an artificially large
    # one here to prove attach() bypasses the wait rather than just being fast.
    monkeypatch.setattr(tracker_capture, "RENDER_INTERVAL_S", 10.0)
    calls = spy_broadcast(monkeypatch)
    client = FakeBlah2Client([make_frame(1000)])
    service = make_service(client)
    service.start()
    time.sleep(0.05)  # let the frame land in history before attaching

    q = service.attach()
    assert _wait_until(lambda: len(calls) >= 1, timeout=1.0)
    service.detach(q)


def test_attached_viewer_receives_a_tick_on_its_queue(monkeypatch):
    client = FakeBlah2Client([make_frame(1000)])
    service = make_service(client)
    service.start()

    q = service.attach()
    try:
        assert q.get(timeout=1.0) >= 1
    finally:
        service.detach(q)


def test_prune_runs_independent_of_viewers(monkeypatch):
    spy_broadcast(monkeypatch)
    client = FakeBlah2Client([make_frame(1000)])
    service = make_service(client)
    service.history.window_s = 0  # anything with a timestamp is immediately "old"
    service.start()

    assert _wait_until(lambda: len(service.history.raw_points) == 1, timeout=1.0)
    # No viewer ever attached, but pruning must still run on its own cadence.
    assert _wait_until(lambda: len(service.history.raw_points) == 0, timeout=1.0)


class EndlessBlah2Client:
    """Never runs out of frames, each with a fresh timestamp.

    The draining FakeBlah2Client cannot be used for a test that waits on a
    *second* broadcast. `_run()` only re-broadcasts when
    `new_frames_since_broadcast > 0`, and the first broadcast resets that
    counter. The capture thread starts on service.start() and consumes frames
    at POLL_INTERVAL_S while the main thread is still on its way to attach(),
    so with a fixed two-frame list there is a race: if both frames are gone
    before a viewer is registered, the first broadcast fires on
    _refresh_requested, the counter resets, no frame ever arrives again and
    the second broadcast can never happen. The wait then expires no matter
    how long it is.

    That raced roughly 1 run in 20, on any machine, and a longer timeout would
    not have helped.
    """

    def __init__(self):
        self._ts = 1000

    def get_detection(self):
        self._ts += 1000
        return make_frame(self._ts)


def test_broadcast_failure_does_not_kill_capture_loop(monkeypatch):
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("boom")

    spy_broadcast(monkeypatch, side_effect=flaky)
    service = make_service(EndlessBlah2Client())
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
    now = _now_ms()
    client = FakeBlah2Client([make_frame(now), make_frame(now + 10)])
    service = make_service(client)
    service.start()
    assert _wait_until(lambda: len(service.history.raw_points) >= 2, timeout=1.0)

    service.request_clear()

    assert _wait_until(lambda: service.history.raw_points == [], timeout=1.0)
    assert service.history.tracks == {}
    assert service.snapshot()[0] == {"raw": EMPTY_COLS, "tracks": {}}


def test_request_clear_runs_without_viewers():
    # No viewer ever attached — proves the clear branch is unconditional,
    # same as prune() already is, not gated on has_viewers.
    client = FakeBlah2Client([make_frame(_now_ms())])
    service = make_service(client)
    service.start()
    assert _wait_until(lambda: len(service.history.raw_points) >= 1, timeout=1.0)

    service.request_clear()

    assert _wait_until(lambda: service.history.raw_points == [], timeout=1.0)


def test_frame_after_clear_populates_fresh_buffer():
    now = _now_ms()
    client = FakeBlah2Client([make_frame(now), make_frame(now + 10)])
    service = make_service(client)
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
    service = make_service(client)
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
    resp = app_client.get('/tracker/data.json')
    assert resp.status_code == 200
    assert resp.get_json() == {"raw": EMPTY_COLS, "tracks": {}}


def test_data_json_returns_columnar_snapshot(app_client):
    import app as app_module

    app_module.tracker_capture.history.add_raw(1000, 1.0, 2.0, 3.0)

    resp = app_client.get('/tracker/data.json')
    assert resp.get_json()["raw"] == {
        "t": [1000], "delay": [1.0], "doppler": [2.0], "snr": [3.0],
    }


def test_data_json_window_is_clamped_to_what_the_node_retains(app_client):
    """A query string is a display preference; it must never be able to ask
    the node to build something larger than the buffer it holds."""
    import app as app_module

    with patch.object(app_module.tracker_capture, 'snapshot',
                      return_value=({"raw": EMPTY_COLS, "tracks": {}}, {})) as mock_snap:
        app_client.get('/tracker/data.json?window=999999')
    assert mock_snap.call_args.kwargs["window_s"] == tracker_capture.WINDOW_S

    with patch.object(app_module.tracker_capture, 'snapshot',
                      return_value=({"raw": EMPTY_COLS, "tracks": {}}, {})) as mock_snap:
        app_client.get('/tracker/data.json?window=1')
    assert mock_snap.call_args.kwargs["window_s"] == tracker_capture.MIN_VIEW_WINDOW_S

    with patch.object(app_module.tracker_capture, 'snapshot',
                      return_value=({"raw": EMPTY_COLS, "tracks": {}}, {})) as mock_snap:
        app_client.get('/tracker/data.json?window=nonsense')
    assert mock_snap.call_args.kwargs["window_s"] is None


def test_clear_route_calls_request_clear(app_client):
    import app as app_module

    with patch.object(app_module.tracker_capture, 'request_clear') as mock_clear:
        resp = app_client.post('/tracker/clear')

    assert resp.status_code == 200
    assert resp.get_json() == {"success": True}
    mock_clear.assert_called_once()


def test_index_page_renders(app_client):
    resp = app_client.get('/tracker')
    assert resp.status_code == 200
    assert b'>Tracker<' in resp.data
    assert b'clearBufferBtn' in resp.data
    assert b'railList' in resp.data


def test_old_preview_urls_still_work(app_client):
    """Nodes have been in the field under the old name long enough for the
    URL to be bookmarked. 308 rather than 301 so /clear stays a POST."""
    resp = app_client.get('/tracker-preview')
    assert resp.status_code == 308
    assert resp.headers['Location'].endswith('/tracker')

    resp = app_client.get('/tracker-preview/data.json?window=900')
    assert resp.status_code == 308
    assert resp.headers['Location'].endswith('/tracker/data.json?window=900')

    resp = app_client.post('/tracker-preview/clear')
    assert resp.status_code == 308
    assert resp.headers['Location'].endswith('/tracker/clear')


def test_events_route_opens_with_a_snapshot_and_detaches_on_close(app_client):
    """The SSE connection's lifetime IS the attach()/detach() lifecycle —
    this is the contract the whole viewer-gated design depends on, so it's
    worth testing at the actual route level. The first message being a
    snapshot is the other half: it is what removes any race between what the
    snapshot contained and where the delta stream started."""
    import app as app_module

    q = queue.Queue()

    with patch.object(app_module.tracker_capture, 'attach', return_value=q) as mock_attach, \
         patch.object(app_module.tracker_capture, 'detach') as mock_detach:
        resp = app_client.get('/tracker/events')
        assert resp.status_code == 200
        assert resp.content_type.startswith('text/event-stream')

        # Pull exactly one chunk — the generator pauses at its `yield` and
        # won't attempt a (blocking) q.get() until asked for more, so this
        # can't hang even though the loop itself is infinite.
        first_chunk = next(resp.response)
        assert b'"type":"snapshot"' in first_chunk
        assert b'"tracks"' in first_chunk

        resp.close()  # WSGI close() contract -> generator's finally -> detach()

    mock_attach.assert_called_once()
    mock_detach.assert_called_once_with(q)


def test_events_stream_sends_a_delta_after_the_snapshot(app_client):
    """The whole point of the rewrite, end to end: a tick on the viewer's
    queue produces just the points appended since that viewer's last
    message, not the buffer again."""
    import app as app_module

    svc = app_module.tracker_capture
    q = queue.Queue()

    with patch.object(svc, 'attach', return_value=q), patch.object(svc, 'detach'):
        resp = app_client.get('/tracker/events')
        assert b'"type":"snapshot"' in next(resp.response)

        svc.history.add_raw(1000, 1.0, 2.0, 3.0)
        q.put(1)

        second = next(resp.response)
        assert b'"type":"delta"' in second
        assert b'"t":[1000]' in second

        # ...and only once. A second tick with nothing behind it must not
        # re-send the point the viewer already has.
        svc.history.add_raw(2000, 1.5, 2.5, 3.5)
        q.put(2)
        third = next(resp.response)
        assert b'"t":[2000]' in third
        assert b'1000' not in third

        resp.close()


def test_events_stream_skips_a_broadcast_with_nothing_behind_it(app_client, monkeypatch):
    """A tick can fire with no new points (a clear, or a viewer attaching
    while another is mid-cadence). That must cost a heartbeat, not a message
    the page would treat as an update."""
    import app as app_module
    import routes.tracker as tracker_routes

    monkeypatch.setattr(tracker_routes, "HEARTBEAT_SECONDS", 0.05)
    svc = app_module.tracker_capture
    q = queue.Queue()

    with patch.object(svc, 'attach', return_value=q), patch.object(svc, 'detach'):
        resp = app_client.get('/tracker/events')
        assert b'"type":"snapshot"' in next(resp.response)

        q.put(1)  # broadcast, but nothing was appended
        assert next(resp.response).startswith(b': keepalive')

        resp.close()
