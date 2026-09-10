"""Client for the retina-tracker sidecar container.

Two one-way channels, neither of which carries detections any more.

*Track events in*, by tailing the JSONL file the sidecar streams to. Its
`--tcp` mode is input-only over the socket (see
retina_tracker/server.py::run_tcp_server — it never writes back on the
accepted connection), so results come back through the filesystem rather
than the connection that fed it.

*Control out*, over HTTP to the sidecar's loopback control surface. That is
new, and it is what let retina-gui stop being the transport between blah2
and the tracker. Detections now travel blah2 -> blah2_api -> retina-tracker
directly (blah2_api's `network.tracker_forward`), so this process no longer
opens the sidecar's ingest socket at all. It could not have kept doing so:
that socket accepts one connection at a time, and blah2_api now owns it.

Sending frames used to live here too. It does not, because retina-gui is
not on that path any more.
"""

import json
import os
import threading
import time

import requests


class RetinaTrackerClient:
    """JSONL file tailer plus an HTTP control client for retina-tracker.

    Several features consume the sidecar's track events (the Tracker page
    and Auto-Calibrate), so they share one instance and one tail thread via
    add_listener(). That sharing is no longer forced by the sidecar's
    single-connection ingest, since nothing here connects to it; it is just
    that one thread tailing one file is enough.
    """

    def __init__(self, events_path, control_url, poll_interval=0.2, timeout=3):
        self._events_path = events_path
        self._control_url = control_url.rstrip('/')
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._tail_thread = None
        self._stop = threading.Event()
        self._listeners = []
        self._listeners_lock = threading.Lock()

    # ── Control ────────────────────────────────────────────────

    def reset(self):
        """Clear the sidecar's Tracker state in place (see
        retina_tracker/tracker.py::Tracker.reset()).

        Used by Auto-Calibrate between candidate towers and at the start of
        every dwell, since a confirmed track only means something at the
        geometry (fc/tx position) it was seen at.

        The sidecar holds its lock for the whole reset, so a 200 means the
        tracker is already clear rather than scheduled to be — which is what
        the caller needs, because the next frame it waits on must not be
        able to associate into pre-reset state.

        Returns True if the sidecar confirmed it. Unlike the old
        fire-and-forget message down the detection socket, a failure here is
        visible; callers that care whether the tracker really is clear can
        now tell.
        """
        try:
            response = requests.post(f"{self._control_url}/reset", timeout=self._timeout)
            response.raise_for_status()
            return bool(response.json().get("ok"))
        except (requests.RequestException, ValueError):
            return False

    # ── Tailing track events ───────────────────────────────────

    def start(self, on_event):
        """Back-compat alias for add_listener()."""
        self.add_listener(on_event)

    def add_listener(self, on_event):
        """Register on_event(event_dict) to be called for every new JSONL
        line tailed from the events file. Multiple listeners are supported
        (the Tracker page and Auto-Calibrate both consume the same sidecar's
        output) — the tail thread itself is started once, on the first
        call."""
        with self._listeners_lock:
            self._listeners.append(on_event)
        if self._tail_thread is not None and self._tail_thread.is_alive():
            return
        self._stop.clear()
        self._tail_thread = threading.Thread(
            target=self._tail_loop, daemon=True)
        self._tail_thread.start()

    def stop(self):
        self._stop.set()

    def _tail_loop(self):
        # Start from current EOF, not 0 — a fresh attach shouldn't replay
        # a run's entire history, only events from here on.
        try:
            offset = os.path.getsize(self._events_path)
        except OSError:
            offset = 0

        while not self._stop.is_set():
            try:
                size = os.path.getsize(self._events_path)
            except OSError:
                time.sleep(self._poll_interval)
                continue

            if size < offset:
                # Sidecar restarted — TrackEventWriter opens its output
                # file in "w" mode on process start, truncating it.
                offset = 0

            if size > offset:
                try:
                    with open(self._events_path, "rb") as f:
                        f.seek(offset)
                        chunk = f.read()
                except OSError:
                    time.sleep(self._poll_interval)
                    continue

                offset += len(chunk)
                lines = chunk.split(b"\n")
                if not chunk.endswith(b"\n"):
                    # Writer mid-line — hold this partial line back so
                    # it's re-read whole (combined with its rest) next tick.
                    partial = lines.pop()
                    offset -= len(partial)

                for raw_line in lines:
                    raw_line = raw_line.strip()
                    if not raw_line:
                        continue
                    try:
                        event = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    with self._listeners_lock:
                        listeners = list(self._listeners)
                    for listener in listeners:
                        listener(event)

            time.sleep(self._poll_interval)
