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
    """Best-effort TCP sender + JSONL file tailer for retina-tracker."""

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
        """Begin tailing the events file on a background thread, calling
        on_event(event_dict) for each new JSONL line. Call once."""
        if self._tail_thread is not None and self._tail_thread.is_alive():
            return
        self._stop.clear()
        self._tail_thread = threading.Thread(
            target=self._tail_loop, args=(on_event,), daemon=True)
        self._tail_thread.start()

    def stop(self):
        self._stop.set()
        with self._send_lock:
            self._close_sock()

    def _tail_loop(self, on_event):
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
                    on_event(event)

            time.sleep(self._poll_interval)
