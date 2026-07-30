"""Client for the retina-tracker sidecar container.

Talks directly to retina-tracker's own --tcp server mode — no intermediary
supervisor process. Its --tcp mode is input-only over the socket (see
retina_tracker/server.py::run_tcp_server — it never writes back on the
accepted connection); track events always go through -s/--stream-output,
which the sidecar is configured to point at a file instead of stdout. So
this client has two independent halves: push detection frames over TCP,
and tail that output file for track events.
"""

import json
import os
import socket
import threading
import time


class RetinaTrackerClient:
    """Best-effort TCP sender + JSONL file tailer for retina-tracker.

    The sidecar's TCP server accepts one connection at a time, so every
    feature that talks to a given sidecar (tracker-preview, Auto-Calibrate)
    must share the same client instance rather than each opening their own
    connection — see add_listener()."""

    def __init__(self, host, port, events_path, poll_interval=0.2, connect_timeout=3):
        self._host = host
        self._port = port
        self._events_path = events_path
        self._poll_interval = poll_interval
        self._connect_timeout = connect_timeout
        self._send_lock = threading.Lock()
        self._sock = None
        self._tail_thread = None
        self._stop = threading.Event()
        self._listeners = []
        self._listeners_lock = threading.Lock()

    # ── Sending frames ─────────────────────────────────────────

    def send_frame(self, frame):
        """Best-effort push of one raw blah2 detection frame ({timestamp,
        delay[], doppler[], snr[]}) to retina-tracker's TCP ingest port.
        Swallows connection errors — same posture as Blah2Client toward
        blah2 itself; a sidecar outage degrades to "no track evidence",
        not a crash. Lazily (re)connects on failure."""
        line = (json.dumps(frame) + "\n").encode()
        with self._send_lock:
            if self._sock is None and not self._connect():
                return
            try:
                self._sock.sendall(line)
                return
            except OSError:
                self._close_sock()
            if self._connect():
                try:
                    self._sock.sendall(line)
                except OSError:
                    self._close_sock()

    def reset(self):
        """Tell the sidecar to clear its Tracker's in-progress and completed
        state in place (see retina_tracker/tracker.py::Tracker.reset()) —
        used by Auto-Calibrate between candidate towers, since a confirmed
        track only means something at the geometry (fc/tx position) it was
        seen at. A real detection frame never carries a "type" key, so this
        can never be mistaken for one."""
        self.send_frame({"type": "RESET"})

    def _connect(self):
        try:
            self._sock = socket.create_connection(
                (self._host, self._port), timeout=self._connect_timeout)
            return True
        except OSError:
            self._sock = None
            return False

    def _close_sock(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # ── Tailing track events ───────────────────────────────────

    def start(self, on_event):
        """Back-compat alias for add_listener()."""
        self.add_listener(on_event)

    def add_listener(self, on_event):
        """Register on_event(event_dict) to be called for every new JSONL
        line tailed from the events file. Multiple listeners are supported
        (e.g. tracker-preview and Auto-Calibrate both tailing the same
        sidecar's output) — the tail thread itself is started once, on the
        first call."""
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
        with self._send_lock:
            self._close_sock()

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
