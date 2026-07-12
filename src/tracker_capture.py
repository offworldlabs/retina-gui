"""Live tracker-preview capture service: always-on capture, lazy rendering.

Capture + tracking runs permanently once the app boots, feeding a bounded
4-hour rolling buffer so loading /tracker-preview shows recent history
immediately. Rendering (the expensive matplotlib step) stays lazy — it
only happens while at least one browser has the page open.

retina-tracker's own Tracker object doesn't retain completed-track history
beyond a ~5 second merge window (see its tracker.py: `all_tracks` is pruned
to `_MERGE_WINDOW_MS = 5000`, purely so a briefly-dropped track can
reconnect — not for history), so it can't answer "what was tracked 3 hours
ago" no matter how long it's been running. HistoryBuffer below builds that
archive ourselves via the `event_writer` hook (the same duck-typed
interface retina-tracker's own JSONL streaming output uses).
"""

import io
import queue
import threading
import time

from retina_tracker.config import get_config
from retina_tracker.tracker import Tracker

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

    Only ever touched from the single capture thread (written during
    capture, read during render — both synchronous on that thread), so no
    locking is needed here; only TrackerCaptureService's `_latest_image`
    crosses threads.

    `write_event` matches retina-tracker's event_writer duck type, called
    once per confirmed-track update with a rolling window of that track's
    most recent points (each carrying its own timestamp — see
    Track.get_recent_detections). Accumulated incrementally by only
    appending genuinely-new timestamps, so full continuous per-track
    history builds up over time even though any single call only supplies
    a short window.
    """

    def __init__(self, window_s=WINDOW_S):
        self.window_s = window_s
        self.raw_points = []  # (timestamp_ms, delay, doppler, snr)
        self.tracks = {}  # track_id -> [(timestamp_ms, delay, doppler, snr), ...]
        self._last_track_timestamp = {}  # track_id -> last recorded timestamp_ms

    def add_raw(self, timestamp_ms, delay, doppler, snr):
        self.raw_points.append((timestamp_ms, delay, doppler, snr))

    def write_event(self, track_id, timestamp, length, detections, **kwargs):
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


class TrackerCaptureService:
    """Runs capture+tracking permanently from start(); rendering stays lazy,
    gated to only run while at least one viewer is attached.
    """

    def __init__(self, blah2_client):
        self._client = blah2_client
        self._lock = threading.Lock()
        self._thread = None
        self._viewers = []  # list of queue.Queue, one per attached viewer
        self._render_requested = False
        self._latest_image = None  # bytes, or None before the first render
        self._seq = 0
        self.history = HistoryBuffer()

    def start(self):
        """Begin permanent capture — call once at app boot, independent of
        any viewer ever connecting."""
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def attach(self):
        """Register a new viewer. Does not start capture (already running
        from start()) — only makes rendering eligible and requests an
        immediate render so this viewer doesn't wait for the next cadence
        tick if history already exists."""
        q = queue.Queue()
        with self._lock:
            self._viewers.append(q)
            self._render_requested = True
        return q

    def detach(self, q):
        """Unregister a viewer. Capture keeps running regardless — only
        rendering stops once no viewers remain."""
        with self._lock:
            if q in self._viewers:
                self._viewers.remove(q)

    def latest_image(self):
        with self._lock:
            return self._latest_image

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
        tracker = Tracker(config=get_config(), event_writer=self.history)
        last_timestamp = None
        last_render = 0.0
        last_prune = time.monotonic()
        new_frames_since_render = 0

        while True:
            frame = self._client.get_detection()
            if frame is not None and frame.get("timestamp") != last_timestamp:
                last_timestamp = frame.get("timestamp")
                ts = frame["timestamp"]
                detections = frame_to_detections(frame)
                for det in detections:
                    self.history.add_raw(ts, det["delay"], det["doppler"], det.get("snr", 0.0))
                tracker.process_frame(detections, ts)
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
                    self._render_requested
                    or (new_frames_since_render > 0 and now - last_render >= RENDER_INTERVAL_S)
                )
                if do_render:
                    self._render_requested = False

            if do_render:
                last_render = now
                new_frames_since_render = 0
                try:
                    self._render(self.history)
                    self._broadcast()
                except Exception:
                    pass  # a failed render shouldn't kill the capture loop

            time.sleep(POLL_INTERVAL_S)

    def _render(self, history, tracks_only=False):
        import matplotlib.pyplot as plt
        import numpy as np

        fig, ax = plt.subplots(figsize=(14, 10))
        try:
            colors = plt.cm.tab20(np.linspace(0, 1, 20))
            track_items = list(history.tracks.items())

            for i, (track_id, points) in enumerate(track_items):
                if not points:
                    continue
                color = colors[i % 20]
                delays = [p[1] for p in points]
                dopplers = [p[2] for p in points]
                ax.plot(delays, dopplers, "-", color="black", linewidth=0.5, alpha=0.3, zorder=1)
                ax.scatter(delays, dopplers, c=[color] * len(delays), s=35, alpha=0.8,
                           edgecolors="none", zorder=2, label=f"Track {track_id}")

            if not tracks_only and history.raw_points:
                all_delays = [p[1] for p in history.raw_points]
                all_dopplers = [p[2] for p in history.raw_points]
                all_snrs = [p[3] for p in history.raw_points]
                scatter = ax.scatter(all_delays, all_dopplers, c=all_snrs, cmap="coolwarm",
                                     s=5, alpha=0.4, zorder=3, label="Detections")
                plt.colorbar(scatter, ax=ax, label="SNR (dB)")

            ax.set_xlabel("Delay", fontsize=12)
            ax.set_ylabel("Doppler (Hz)", fontsize=12)
            ax.set_title("Radar Tracks Only" if tracks_only else "Radar Tracks",
                        fontsize=14, fontweight="bold")
            ax.grid(True, alpha=0.3)

            if track_items:
                if len(track_items) <= 15:
                    ax.legend(loc="upper right", fontsize=8, ncol=2)
                else:
                    handles, labels = ax.get_legend_handles_labels()
                    ax.legend(handles[:15], labels[:15], loc="upper right", fontsize=8, ncol=2)

            plt.tight_layout()
            buf = io.BytesIO()
            fig.savefig(buf, dpi=150, bbox_inches="tight")
            buf.seek(0)
            with self._lock:
                self._latest_image = buf.getvalue()
        finally:
            # Called every few seconds for the life of the process — an
            # unclosed Figure per call would leak memory permanently now
            # that this isn't bounded to a 30-minute session anymore.
            plt.close("all")
