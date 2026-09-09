import json
import queue

from flask import Blueprint, Response, jsonify, redirect, render_template, request, stream_with_context

from tracker_capture import MIN_VIEW_WINDOW_S, WINDOW_S

bp = Blueprint('tracker', __name__, url_prefix='/tracker')

# Nodes have been in the field under the old name long enough for the URL to
# be bookmarked, so /tracker-preview keeps working permanently. 308 rather
# than 301 because /clear is a POST and 301 would let a browser turn it into
# a GET.
legacy_bp = Blueprint('tracker_preview', __name__, url_prefix='/tracker-preview')

# How often to send an SSE heartbeat when there's nothing new to report.
# Without this, q.get() would block forever whenever no capture happens (node
# unreachable, or simply no detections yet) — the generator would never get
# a chance to notice a closed connection, so a viewer could never be detached
# and the broadcast loop would keep considering it attached indefinitely.
HEARTBEAT_SECONDS = 15

# Compact separators, as Flask itself uses outside debug mode. Whitespace
# costs about 7 bytes per point, which is 3 MB across a full buffer.
_JSON = {"separators": (",", ":")}


def _message(payload, kind):
    """One SSE data frame. `kind` is what tells the page whether to replace
    what it holds or append to it."""
    payload["type"] = kind
    return "data: " + json.dumps(payload, **_JSON) + "\n\n"


def _view_window():
    """The ?window= a viewer is asking for, in seconds, or None for
    everything retained. Clamped rather than rejected: the query string is
    a display preference, and it must never be able to ask the node to
    build something larger than it holds."""
    raw = request.args.get("window")
    if raw is None:
        return None
    try:
        seconds = int(raw)
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return max(MIN_VIEW_WINDOW_S, min(seconds, WINDOW_S))


# strict_slashes=False so /tracker-preview and /tracker-preview/ both land
# here directly. Registering a separate "/" rule instead makes Werkzeug send
# its own canonical-slash redirect first, costing a round trip and turning a
# bookmarked POST into two hops.
@legacy_bp.route("", defaults={"path": ""}, strict_slashes=False,
                 methods=["GET", "POST"])
@legacy_bp.route("/<path:path>", methods=["GET", "POST"])
def moved(path):
    target = "/tracker/" + path if path else "/tracker"
    if request.query_string:
        target += "?" + request.query_string.decode("latin-1")
    return redirect(target, code=308)


@bp.route("")
def index():
    """Live Tracker page — see src/tracker_capture.py for the always-on
    capture and the delta stream this page consumes."""
    return render_template("tracker.html")


@bp.route("/events")
def events():
    """SSE stream, and the only path the live page uses.

    The first message is a full snapshot for the requested view window; every
    message after it carries just what has been appended since this
    connection's own last message. Sending the snapshot down the same stream
    rather than having the page fetch it separately is what removes the race
    between "what the snapshot contained" and "where the delta stream
    started": there is one ordering, and this generator owns it.

    The connection's lifetime IS the viewer session: attach() on connect,
    detach() in finally (tab close / network drop)."""
    from app import tracker_capture

    window_s = _view_window()

    def generate():
        q = tracker_capture.attach()
        try:
            payload, cursor = tracker_capture.snapshot(window_s=window_s)
            yield _message(payload, "snapshot")

            while True:
                try:
                    q.get(timeout=HEARTBEAT_SECONDS)
                except queue.Empty:
                    # SSE comment line — not a "message", just keeps the
                    # connection alive and gives this generator a chance to
                    # notice (via the next write failing) that the client
                    # already disconnected.
                    yield ": keepalive\n\n"
                    continue

                delta, new_cursor = tracker_capture.since(cursor)
                if delta is None:
                    # clear() ran, so every outstanding cursor is void.
                    # Re-seed this viewer rather than trying to reconcile.
                    payload, cursor = tracker_capture.snapshot(window_s=window_s)
                    yield _message(payload, "snapshot")
                    continue

                cursor = new_cursor
                if not delta["raw"]["t"] and not delta["tracks"]:
                    continue  # broadcast with nothing behind it
                yield _message(delta, "delta")
        finally:
            tracker_capture.detach(q)

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@bp.route("/data.json")
def data():
    """Full snapshot for the requested view window, as columnar arrays.

    Not on the live path any more — the page is fed by /events — but kept
    because it is the one place the wire format can be inspected without
    holding an SSE connection open. Always 200: even before the first
    capture this is a well-shaped empty snapshot."""
    from app import tracker_capture

    payload, _cursor = tracker_capture.snapshot(window_s=_view_window())
    return jsonify(payload)


@bp.route("/clear", methods=["POST"])
def clear():
    """Wipe the display buffer only — the underlying retina-tracker Tracker
    keeps running unmodified; see TrackerCaptureService.request_clear."""
    from app import tracker_capture

    tracker_capture.request_clear()
    return jsonify({"success": True})
