"""Client for the retina-tracker sidecar container.

Track events come in by tailing the sidecar's JSONL file, because its --tcp
mode is input-only; control goes out over its loopback HTTP surface. Never
open the ingest socket: it takes one connection and blah2_api owns it.
See docs/features/tracker.md#the-retina-tracker-client.
"""

import json
import os
import threading
import time

import requests


class RetinaTrackerClient:
    """JSONL file tailer plus an HTTP control client for retina-tracker.

    One shared instance; consumers register via add_listener() and share one
    tail thread.
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
        """Clear the sidecar's Tracker state in place.

        Returns True only if the sidecar confirmed it. A confirmed reset has
        already happened (the sidecar holds its lock throughout), so the next
        frame cannot associate into pre-reset state.
        See docs/features/tracker.md#resetting-the-tracker.
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
        line tailed from the events file. Multiple listeners are supported;
        the tail thread is started once, on the first call."""
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
        # Start from current EOF, not 0: a fresh attach must not replay
        # a run's entire history.
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
                # Sidecar restarted: TrackEventWriter opens its output
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
                    # Writer mid-line: hold this partial line back so
                    # it is re-read whole next tick.
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
