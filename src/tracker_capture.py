"""Live tracker-preview capture service: always-on capture, lazy data refresh.

Capture + tracking runs permanently once the app boots, feeding a bounded
4-hour rolling buffer so loading /tracker-preview shows recent history
immediately. Building the JSON snapshot the browser renders (client-side,
via Plotly) stays lazy — it only happens while at least one browser has the
page open.

retina-tracker's own Tracker object doesn't retain completed-track history
beyond a ~5 second merge window (see its tracker.py: `all_tracks` is pruned
to `_MERGE_WINDOW_MS = 5000`, purely so a briefly-dropped track can
reconnect — not for history), so it can't answer "what was tracked 3 hours
ago" no matter how long it's been running. HistoryBuffer below builds that
archive ourselves via the `write_event` hook (the same duck-typed interface
retina-tracker's own JSONL streaming output uses).

retina-tracker itself runs out-of-process, as a sidecar container (see
retina_tracker_client.py) — detections are pushed to it over TCP, and its
track events are tailed from a JSONL file it streams to, rather than
calling a local Tracker object directly.
"""

import queue
import threading
import time

POLL_INTERVAL_S = 0.2
RENDER_INTERVAL_S = 3.0
PRUNE_INTERVAL_S = 60
WINDOW_S = 4 * 3600  # 4 hours of retained history


def frame_to_detections(frame):
    """Convert a Blah2Client.get_detection() frame into retina-tracker's
    per-detection dicts. Mirrors retina_tracker's own
    server.py::process_streaming_frame conversion."""
    delays = frame.get("delay", [])
    dopplers = frame.get("doppler", [])
    snrs = frame.get("snr", [])
    adsb_list = frame.get("adsb", [])

    detections = []
    for idx, (delay, doppler, snr) in enumerate(zip(delays, dopplers, snrs)):
        detection = {"delay": delay, "doppler": doppler, "snr": snr}
        if adsb_list and idx < len(adsb_list) and adsb_list[idx] is not None:
            detection["adsb"] = adsb_list[idx]
        detections.append(detection)
    return detections


class HistoryBuffer:
    """Bounded rolling history: raw detections + per-track points.

    Touched from two threads now that retina-tracker runs as a sidecar:
    the capture thread (add_raw, prune, clear, to_dict) and the
    RetinaTrackerClient tailer thread (write_event, via
    TrackerCaptureService.on_track_event) — so every method below is
    lock-guarded.

    `write_event` matches retina-tracker's event_writer duck type (and the
    sidecar's own JSONL track-event schema), called once per confirmed-track
    update with a rolling window of that track's most recent points (each
    carrying its own timestamp — see Track.get_recent_detections).
    Accumulated incrementally by only appending genuinely-new timestamps,
    so full continuous per-track history builds up over time even though
    any single call only supplies a short window.
    """

    def __init__(self, window_s=WINDOW_S):
        self._lock = threading.Lock()
        self.window_s = window_s
        self.raw_points = []  # (timestamp_ms, delay, doppler, snr)
        self.tracks = {}  # track_id -> [(timestamp_ms, delay, doppler, snr), ...]
        self._last_track_timestamp = {}  # track_id -> last recorded timestamp_ms

    def add_raw(self, timestamp_ms, delay, doppler, snr):
        with self._lock:
            self.raw_points.append((timestamp_ms, delay, doppler, snr))

    def write_event(self, track_id, timestamp, length, detections, **kwargs):
        with self._lock:
            last_seen = self._last_track_timestamp.get(track_id)
            points = self.tracks.setdefault(track_id, [])
            newest = last_seen
            for det in detections:
                ts = det["timestamp"]
                if last_seen is not None and ts <= last_seen:
                    continue
                points.append((ts, det["delay"], det["doppler"], det.get("snr", 0.0)))
                if newest is None or ts > newest:
                    newest = ts
            if newest is not None:
                self._last_track_timestamp[track_id] = newest

    def prune(self, now_ms):
        with self._lock:
            cutoff = now_ms - self.window_s * 1000
            self.raw_points = [p for p in self.raw_points if p[0] >= cutoff]
            empty = []
            for track_id, points in self.tracks.items():
                kept = [p for p in points if p[0] >= cutoff]
                if kept:
                    self.tracks[track_id] = kept
                else:
                    empty.append(track_id)
            for track_id in empty:
                del self.tracks[track_id]
                self._last_track_timestamp.pop(track_id, None)

    def clear(self):
        """Wipe all buffered history."""
        with self._lock:
            self.raw_points = []
            self.tracks = {}
            self._last_track_timestamp = {}

    def to_dict(self):
        """JSON-serializable snapshot sent to the browser on every refresh
        tick. Values already originate as plain JSON floats (traced through
        retina-tracker's Track.update() -> frame_to_detections() above), so
        no numpy-safe encoding is needed here."""
        with self._lock:
            return {
                "raw": [
                    {"t": t, "delay": delay, "doppler": doppler, "snr": snr}
                    for (t, delay, doppler, snr) in self.raw_points
                ],
                "tracks": {
                    track_id: [
                        {"t": t, "delay": delay, "doppler": doppler, "snr": snr}
                        for (t, delay, doppler, snr) in points
                    ]
                    for track_id, points in self.tracks.items()
                },
            }


class TrackerCaptureService:
    """Runs capture+tracking permanently from start(); the JSON data refresh
    stays lazy, gated to only run while at least one viewer is attached.
    """

    def __init__(self, blah2_client, retina_tracker_client):
        self._client = blah2_client
        self._tracker_client = retina_tracker_client
        self._lock = threading.Lock()
        self._thread = None
        self._viewers = []  # list of queue.Queue, one per attached viewer
        self._refresh_requested = False
        self._clear_requested = False
        self.history = HistoryBuffer()
        self._latest_data = self.history.to_dict()  # always a valid snapshot, never None
        self._seq = 0

    def start(self):
        """Begin permanent capture — call once at app boot, independent of
        any viewer ever connecting."""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()
        self._tracker_client.start(self.on_track_event)

    def on_track_event(self, event):
        """Called from RetinaTrackerClient's tailer thread whenever the
        sidecar streams a new track event. Field names line up exactly
        with retina-tracker's TrackEventWriter schema, so no translation
        is needed."""
        self.history.write_event(**event)

    def attach(self):
        """Register a new viewer. Does not start capture (already running
        from start()) — only makes data refresh eligible and requests an
        immediate refresh so this viewer doesn't wait for the next cadence
        tick if history already exists."""
        q = queue.Queue()
        with self._lock:
            self._viewers.append(q)
            self._refresh_requested = True
        return q

    def detach(self, q):
        """Unregister a viewer. Capture keeps running regardless — only
        the data refresh stops once no viewers remain."""
        with self._lock:
            if q in self._viewers:
                self._viewers.remove(q)

    def latest_data(self):
        with self._lock:
            return self._latest_data

    def request_clear(self):
        """Request that HistoryBuffer be wiped on the capture thread's next
        loop tick (cross-thread signal, mirroring attach()'s
        _refresh_requested flag). Deliberately does NOT touch the sidecar's
        own Tracker — it keeps running and tracking unmodified; only the
        display buffer is reset. Any still-active track will repopulate on
        its own within a few refresh ticks, since retina-tracker resends
        its own recent-history window on the next confirmed update."""
        with self._lock:
            self._clear_requested = True

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def _broadcast(self):
        with self._lock:
            self._seq += 1
            seq = self._seq
            viewers = list(self._viewers)
        for q in viewers:
            q.put(seq)

    def _run(self):
        last_timestamp = None
        last_render = 0.0
        last_prune = time.monotonic()
        new_frames_since_render = 0

        while True:
            # Clear check runs first, before this tick's frame is even
            # polled, so a frame that arrives later in this same tick lands
            # in the now-empty buffer rather than being wiped right after
            # being added. Runs unconditionally (independent of
            # has_viewers), same reasoning as prune()'s independence below —
            # a clear should be visible immediately, not wait for the
            # viewer-gated refresh cadence.
            with self._lock:
                do_clear = self._clear_requested
                if do_clear:
                    self._clear_requested = False
            if do_clear:
                try:
                    self.history.clear()
                    with self._lock:
                        self._latest_data = self.history.to_dict()
                    new_frames_since_render = 0
                    self._broadcast()
                except Exception:
                    pass  # a failed clear must not kill the capture loop

            frame = self._client.get_detection()
            if frame is not None and frame.get("timestamp") != last_timestamp:
                last_timestamp = frame.get("timestamp")
                ts = frame["timestamp"]
                detections = frame_to_detections(frame)
                for det in detections:
                    self.history.add_raw(ts, det["delay"], det["doppler"], det.get("snr", 0.0))
                self._tracker_client.send_frame(frame)
                new_frames_since_render += 1

            now = time.monotonic()

            # Pruning runs on its own fixed cadence, independent of viewer
            # presence — capture is unconditional now, so this is the only
            # thing keeping memory bounded. Tying it to the (viewer-gated)
            # render cycle instead would mean the buffer grows unbounded
            # for as long as nobody's watching, exactly backwards from the
            # point of a fixed-size window.
            if now - last_prune >= PRUNE_INTERVAL_S:
                last_prune = now
                self.history.prune(int(time.time() * 1000))

            with self._lock:
                has_viewers = bool(self._viewers)
                do_render = has_viewers and (
                    self._refresh_requested
                    or (new_frames_since_render > 0 and now - last_render >= RENDER_INTERVAL_S)
                )
                if do_render:
                    self._refresh_requested = False

            if do_render:
                last_render = now
                new_frames_since_render = 0
                try:
                    self._refresh_data(self.history)
                    self._broadcast()
                except Exception:
                    pass  # a failed refresh shouldn't kill the capture loop

            time.sleep(POLL_INTERVAL_S)

    def _refresh_data(self, history):
        """Build a fresh JSON snapshot and publish it for readers on other
        threads. Called only from the capture thread."""
        snapshot = history.to_dict()
        with self._lock:
            self._latest_data = snapshot
