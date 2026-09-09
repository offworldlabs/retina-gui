"""Live Tracker capture service: always-on capture, delta-pushed display.

Capture + tracking runs permanently once the app boots, feeding a bounded
4-hour rolling buffer so loading /tracker shows recent history immediately.
That 4 hours is what the node *collects and retains*, and it never changes:
the page's view window only narrows what a viewer is shown, and is applied
when a snapshot is served rather than by touching the buffer.

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

Two decisions here exist purely to keep a node cheap, and both are measured:

*Columnar, rounded, and only ever sent once.* A snapshot is four parallel
arrays per point-set rather than one dict per point, so the four key names
are not repeated once per detection; and values are rounded on the way in
(see add_raw) to the precision the measurement actually has. Together that
took a full 4-hour snapshot from 44.0 MB to 14.1 MB in benchmarking.

*Deltas, not snapshots.* The capture thread used to rebuild the entire
snapshot every 3 seconds and every viewer re-fetched all of it — 878 ms and
44 MB per tick on a busy node. Now the capture thread only broadcasts a
tick, and each SSE connection asks HistoryBuffer for whatever has been
appended since its own last message (see since()). Three seconds of a busy
node is about 90 points, 2.9 KB, and 0.05 ms. The per-viewer queue that
attach() already handed out is what makes this cheap: the server always
knew what it had sent each viewer, so there is no cursor to negotiate and
no timestamps to reconcile.
"""

import bisect
import queue
import threading
import time

POLL_INTERVAL_S = 0.2
RENDER_INTERVAL_S = 3.0
PRUNE_INTERVAL_S = 60
WINDOW_S = 4 * 3600  # 4 hours of retained history — collection, not display

# Rounding applied once, when a point is stored, rather than once per
# viewer per refresh. Two decimals of bistatic range is 10 m and two of
# Doppler is 0.01 Hz, both far finer than the measurement behind them; SNR
# is reported to 0.1 dB. Rounding here also makes raw and track points
# compare exactly, which is what lets the browser tell an associated
# detection from an unassociated one.
DELAY_DP = 2
DOPPLER_DP = 2
SNR_DP = 1

# View windows the page may ask for. Bounded so a query string cannot ask
# the node to build something larger than it retains.
MIN_VIEW_WINDOW_S = 60


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


def _columns(points):
    """Four parallel arrays from a list of (t, delay, doppler, snr) tuples.

    This is also the shape Plotly wants for x/y, so the browser hands them
    straight over instead of rebuilding them with .map() on every update."""
    return {
        "t": [p[0] for p in points],
        "delay": [p[1] for p in points],
        "doppler": [p[2] for p in points],
        "snr": [p[3] for p in points],
    }


def _first_index_at_or_after(points, cutoff_ms):
    """Index of the first point at or after cutoff_ms.

    Both raw_points and each track's points are appended in timestamp
    order, so this is a bisect rather than a scan — which is what makes a
    view window a saving rather than a cost. A 1-tuple sorts before any
    longer tuple sharing its first element, so this lands on the first
    point whose timestamp is >= cutoff."""
    return bisect.bisect_left(points, (cutoff_ms,))


class HistoryBuffer:
    """Bounded rolling history: raw detections, per-track points, per-track
    metadata, and enough bookkeeping to answer "what is new since X".

    Touched from two threads now that retina-tracker runs as a sidecar:
    the capture thread (add_raw, prune, clear) and the RetinaTrackerClient
    tailer thread (write_event, via TrackerCaptureService.on_track_event),
    plus request threads reading snapshots — so every method below is
    lock-guarded.

    `write_event` matches retina-tracker's event_writer duck type (and the
    sidecar's own JSONL track-event schema), called once per confirmed-track
    update with a rolling window of that track's most recent points (each
    carrying its own timestamp — see Track.get_recent_detections).
    Accumulated incrementally by only appending genuinely-new timestamps,
    so full continuous per-track history builds up over time even though
    any single call only supplies a short window.

    Delta bookkeeping: raw_points and each track's list are append-only
    between prunes, so a viewer's position is just a count. Because prune()
    drops from the *front*, that count has to be monotonic rather than a
    list index — hence _raw_base and _track_base, which record how many
    points have been dropped ahead of the ones still held. clear() bumps
    _gen instead, which invalidates every outstanding cursor at once and
    makes viewers take a fresh snapshot.
    """

    def __init__(self, window_s=WINDOW_S):
        self._lock = threading.Lock()
        self.window_s = window_s
        self.raw_points = []  # (timestamp_ms, delay, doppler, snr)
        self.tracks = {}  # track_id -> [(timestamp_ms, delay, doppler, snr), ...]
        self.track_meta = {}  # track_id -> what the tracker thinks of it
        self._last_track_timestamp = {}  # track_id -> last recorded timestamp_ms
        self._gen = 0
        self._raw_base = 0  # raw points dropped by prune() so far
        self._track_base = {}  # track_id -> points dropped from the front

    def add_raw(self, timestamp_ms, delay, doppler, snr):
        with self._lock:
            self.raw_points.append((
                timestamp_ms,
                round(delay, DELAY_DP),
                round(doppler, DOPPLER_DP),
                round(snr, SNR_DP),
            ))

    def write_event(self, track_id, timestamp, length, detections,
                    adsb_hex=None, is_anomalous=False, anomaly_types=None,
                    max_velocity_ms=0.0, shadow_fraction=0.0, **_unused):
        """Record one confirmed-track update.

        The keyword arguments are named rather than swallowed so it is
        visible which of retina-tracker's event fields we keep and which we
        deliberately drop: `adsb_initialized` is the only one discarded on
        purpose, being a nuance the page has no use for. Anything the
        sidecar adds later lands in **_unused rather than breaking."""
        with self._lock:
            last_seen = self._last_track_timestamp.get(track_id)
            points = self.tracks.setdefault(track_id, [])
            newest = last_seen
            for det in detections:
                ts = det["timestamp"]
                if last_seen is not None and ts <= last_seen:
                    continue
                points.append((
                    ts,
                    round(det["delay"], DELAY_DP),
                    round(det["doppler"], DOPPLER_DP),
                    round(det.get("snr", 0.0), SNR_DP),
                ))
                if newest is None or ts > newest:
                    newest = ts
            if newest is not None:
                self._last_track_timestamp[track_id] = newest

            # Refreshed on every event, so the rail always shows the
            # tracker's current opinion rather than its first one.
            self.track_meta[track_id] = {
                "adsb_hex": adsb_hex,
                "length": length,
                "max_velocity_ms": round(max_velocity_ms or 0.0, 1),
                "is_anomalous": bool(is_anomalous),
                "anomaly_types": sorted(anomaly_types or []),
                "shadow_fraction": round(shadow_fraction or 0.0, 3),
            }

    def prune(self, now_ms):
        with self._lock:
            cutoff = now_ms - self.window_s * 1000
            dropped = _first_index_at_or_after(self.raw_points, cutoff)
            if dropped:
                self._raw_base += dropped
                self.raw_points = self.raw_points[dropped:]
            empty = []
            for track_id, points in self.tracks.items():
                cut = _first_index_at_or_after(points, cutoff)
                if not cut:
                    continue
                if cut >= len(points):
                    empty.append(track_id)
                else:
                    self._track_base[track_id] = self._track_base.get(track_id, 0) + cut
                    self.tracks[track_id] = points[cut:]
            for track_id in empty:
                del self.tracks[track_id]
                self._last_track_timestamp.pop(track_id, None)
                self.track_meta.pop(track_id, None)
                self._track_base.pop(track_id, None)

    def clear(self):
        """Wipe all buffered history and invalidate every viewer's cursor."""
        with self._lock:
            self.raw_points = []
            self.tracks = {}
            self.track_meta = {}
            self._last_track_timestamp = {}
            self._raw_base = 0
            self._track_base = {}
            self._gen += 1

    # ── Reading ────────────────────────────────────────────────

    def _cursor(self):
        """Caller must hold the lock."""
        return {
            "gen": self._gen,
            "raw": self._raw_base + len(self.raw_points),
            "tracks": {
                tid: self._track_base.get(tid, 0) + len(pts)
                for tid, pts in self.tracks.items()
            },
        }

    def cursor(self):
        with self._lock:
            return self._cursor()

    def snapshot(self, window_s=None, now_ms=None):
        """Everything the browser needs to draw from cold, plus the cursor
        to continue from. `window_s` limits it to the most recent slice;
        None means the whole retained buffer. The cursor is always the end
        of *everything*, not the end of the window, so deltas continue from
        now regardless of how much history was served."""
        if window_s is not None and now_ms is None:
            now_ms = int(time.time() * 1000)
        with self._lock:
            if window_s is None:
                raw = self.raw_points
            else:
                raw = self.raw_points[
                    _first_index_at_or_after(self.raw_points, now_ms - window_s * 1000):]
            tracks = {}
            for tid, points in self.tracks.items():
                if window_s is None:
                    selected = points
                else:
                    selected = points[
                        _first_index_at_or_after(points, now_ms - window_s * 1000):]
                if not selected:
                    continue
                tracks[tid] = dict(_columns(selected), meta=self.track_meta.get(tid, {}))
            return {"raw": _columns(raw), "tracks": tracks}, self._cursor()

    def since(self, cursor):
        """Whatever has been appended since `cursor`.

        Returns (payload, new_cursor). payload is None when the cursor
        cannot be honoured — a different generation, meaning clear() ran —
        and the caller should send a fresh snapshot instead. An empty
        payload ({"raw": ..., "tracks": {}} with no points) is returned
        when nothing has changed, which the caller may skip sending.

        A track the cursor has never seen comes back whole, which is what
        makes a newly promoted track arrive with the backfilled history
        retina-tracker hands over on promotion rather than truncated at
        the moment the viewer happened to connect."""
        if not cursor or "gen" not in cursor:
            return None, None
        with self._lock:
            if cursor["gen"] != self._gen:
                return None, None
            seen_tracks = cursor.get("tracks") or {}
            start = max(0, cursor.get("raw", 0) - self._raw_base)
            new_raw = self.raw_points[start:]
            tracks = {}
            for tid, points in self.tracks.items():
                base = self._track_base.get(tid, 0)
                offset = max(0, seen_tracks.get(tid, 0) - base)
                selected = points[offset:]
                if not selected:
                    continue
                tracks[tid] = dict(_columns(selected), meta=self.track_meta.get(tid, {}))
            return {"raw": _columns(new_raw), "tracks": tracks}, self._cursor()


class TrackerCaptureService:
    """Runs capture+tracking permanently from start(); display work is
    gated to only happen while at least one viewer is attached.

    The capture thread does no serialisation of its own any more. It
    appends to the buffer and, on the render cadence, broadcasts a bare
    tick; each attached SSE connection then builds its own delta on its own
    request thread. So the cost of an extra viewer is that viewer's delta,
    and nothing is built at all when nobody is watching.
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
        from start()) — only makes broadcasts eligible, and requests an
        immediate one so this viewer doesn't wait for the next cadence
        tick."""
        q = queue.Queue()
        with self._lock:
            self._viewers.append(q)
            self._refresh_requested = True
        return q

    def detach(self, q):
        """Unregister a viewer. Capture keeps running regardless — only
        the broadcasts stop once no viewers remain."""
        with self._lock:
            if q in self._viewers:
                self._viewers.remove(q)

    def snapshot(self, window_s=None):
        """Full state plus a cursor, for a viewer starting from cold."""
        return self.history.snapshot(window_s=window_s)

    def since(self, cursor):
        """Whatever is new for a viewer holding `cursor`."""
        return self.history.since(cursor)

    def request_clear(self):
        """Request that HistoryBuffer be wiped on the capture thread's next
        loop tick (cross-thread signal, mirroring attach()'s
        _refresh_requested flag). Deliberately does NOT touch the sidecar's
        own Tracker — it keeps running and tracking unmodified; only the
        display buffer is reset. Any still-active track will repopulate on
        its own within a few ticks, since retina-tracker resends its own
        recent-history window on the next confirmed update."""
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
        last_broadcast = 0.0
        last_prune = time.monotonic()
        new_frames_since_broadcast = 0

        while True:
            # Clear check runs first, before this tick's frame is even
            # polled, so a frame that arrives later in this same tick lands
            # in the now-empty buffer rather than being wiped right after
            # being added. Runs unconditionally (independent of
            # has_viewers), same reasoning as prune()'s independence below —
            # a clear should be visible immediately, not wait for the
            # viewer-gated broadcast cadence.
            with self._lock:
                do_clear = self._clear_requested
                if do_clear:
                    self._clear_requested = False
            if do_clear:
                try:
                    self.history.clear()
                    new_frames_since_broadcast = 0
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
                new_frames_since_broadcast += 1

            now = time.monotonic()

            # Pruning runs on its own fixed cadence, independent of viewer
            # presence — capture is unconditional, so this is the only
            # thing keeping memory bounded. Tying it to the (viewer-gated)
            # broadcast cycle instead would mean the buffer grows unbounded
            # for as long as nobody's watching, exactly backwards from the
            # point of a fixed-size window.
            if now - last_prune >= PRUNE_INTERVAL_S:
                last_prune = now
                self.history.prune(int(time.time() * 1000))

            with self._lock:
                has_viewers = bool(self._viewers)
                do_broadcast = has_viewers and (
                    self._refresh_requested
                    or (new_frames_since_broadcast > 0
                        and now - last_broadcast >= RENDER_INTERVAL_S)
                )
                if do_broadcast:
                    self._refresh_requested = False

            if do_broadcast:
                last_broadcast = now
                new_frames_since_broadcast = 0
                try:
                    self._broadcast()
                except Exception:
                    pass  # a failed broadcast shouldn't kill the capture loop

            time.sleep(POLL_INTERVAL_S)
