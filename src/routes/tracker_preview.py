import queue

from flask import Blueprint, Response, jsonify, render_template, stream_with_context

bp = Blueprint('tracker_preview', __name__, url_prefix='/tracker-preview')

# How often to send an SSE heartbeat when there's nothing new to report.
# Without this, q.get() would block forever whenever no render happens (node
# unreachable, or simply no detections yet) — the generator would never get
# a chance to notice a closed connection, so a viewer could never be detached
# and the capture thread would keep running for the full MAX_SESSION_S
# regardless of whether anyone is still watching.
HEARTBEAT_SECONDS = 15


@bp.route("")
def index():
    """Live tracker-preview page — see src/tracker_capture.py for the
    viewer-lifecycle-gated background capture this page drives."""
    return render_template("tracker_preview.html")


@bp.route("/events")
def events():
    """SSE stream: one message per newly-refreshed data snapshot. The connection's
    lifetime IS the capture session's lifetime — attach() on connect,
    detach() in finally (tab close / network drop) so the background
    capture thread only ever runs while this is open."""
    from app import tracker_capture

    def generate():
        q = tracker_capture.attach()
        try:
            while True:
                try:
                    seq = q.get(timeout=HEARTBEAT_SECONDS)
                    yield f"data: {{\"seq\": {seq}}}\n\n"
                except queue.Empty:
                    # SSE comment line — not a "message", just keeps the
                    # connection alive and gives this generator a chance to
                    # notice (via the next write failing) that the client
                    # already disconnected.
                    yield ": keepalive\n\n"
        finally:
            tracker_capture.detach(q)

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@bp.route("/data.json")
def data():
    """Latest tracker-history snapshot as JSON, rendered client-side by
    Plotly. Always 200 — even before the first refresh this is a
    well-shaped empty snapshot ({"raw": [], "tracks": {}})."""
    from app import tracker_capture

    return jsonify(tracker_capture.latest_data())


@bp.route("/clear", methods=["POST"])
def clear():
    """Wipe the display buffer only — the underlying retina-tracker Tracker
    keeps running unmodified; see TrackerCaptureService.request_clear."""
    from app import tracker_capture

    tracker_capture.request_clear()
    return jsonify({"success": True})
