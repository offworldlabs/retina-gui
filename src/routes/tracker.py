"""The Tracker page and a byte-for-byte proxy for retina-tracker's SSE feed.

retina-gui holds no tracker data; the sidecar does.
See docs/features/tracker.md#why-the-page-is-proxied.
"""

import json

import requests
from flask import Blueprint, Response, jsonify, redirect, render_template, request, stream_with_context

bp = Blueprint('tracker', __name__, url_prefix='/tracker')

# /tracker-preview is bookmarked in the field, so it redirects permanently.
# 308, not 301: /clear is a POST and 301 lets a browser turn it into a GET.
legacy_bp = Blueprint('tracker_preview', __name__, url_prefix='/tracker-preview')

# Mirrors the sidecar's own bounds (another repo); it clamps again on its side.
MIN_VIEW_WINDOW_S = 60
MAX_VIEW_WINDOW_S = 4 * 3600

CONNECT_TIMEOUT_S = 5
CONTROL_TIMEOUT_S = 5

# For turning blah2's delay bins into kilometres.
SPEED_OF_LIGHT = 299792458.0


def _axis_bounds():
    """The plot's axis ranges, from the node's blah2 ambiguity bounds.

    Returns {"doppler": [lo, hi] Hz, "delay": [lo, hi] km}, with None for an
    axis the config does not state (it then autoscales). Never guess a range.
    See docs/features/tracker.md#axis-bounds.
    """
    from app import config_mgr

    blank = {"doppler": None, "delay": None}
    try:
        config = config_mgr.load_merged_config() or {}
    except Exception:
        return blank

    ambiguity = (config.get("process", {}) or {}).get("ambiguity", {}) or {}
    fs = (config.get("capture", {}) or {}).get("fs")

    bounds = dict(blank)
    lo, hi = ambiguity.get("dopplerMin"), ambiguity.get("dopplerMax")
    if _drawable(lo, hi):
        bounds["doppler"] = [lo, hi]

    lo, hi = ambiguity.get("delayMin"), ambiguity.get("delayMax")
    if _drawable(lo, hi) and fs:
        cell_km = SPEED_OF_LIGHT / float(fs) / 1000.0
        bounds["delay"] = [lo * cell_km, hi * cell_km]

    return bounds


def _drawable(lo, hi):
    """Whether a pair is an axis rather than a typo: both present, hi > lo."""
    return lo is not None and hi is not None and hi > lo


def _tracker_url(path):
    from app import RETINA_TRACKER_CONTROL_URL
    return f"{RETINA_TRACKER_CONTROL_URL.rstrip('/')}{path}"


def _view_window():
    """The ?window= a viewer is asking for, in seconds, or None for whatever
    the sidecar holds. Clamped rather than rejected: it is a display
    preference."""
    raw = request.args.get("window")
    if raw is None:
        return None
    try:
        seconds = int(raw)
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return max(MIN_VIEW_WINDOW_S, min(seconds, MAX_VIEW_WINDOW_S))


def _sse_error(message):
    return "event: error\ndata: " + json.dumps({"error": message}) + "\n\n"


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
    """The Tracker page. Everything it draws arrives on /tracker/events,
    except the axis bounds, which come from config."""
    return render_template("tracker.html", axes=_axis_bounds())


@bp.route("/events")
def events():
    """Proxy the sidecar's stream, byte for byte, one upstream per viewer.

    Nothing is parsed: the sidecar owns framing, cursors and snapshots.
    See docs/features/tracker.md#the-stream-proxy.
    """
    window_s = _view_window()
    url = _tracker_url("/events")
    params = {"window": window_s} if window_s else None

    def generate():
        try:
            # No read timeout: a quiet sky is a silent stream, and the
            # sidecar's own keepalive is what proves the link is alive.
            with requests.get(url, params=params, stream=True,
                              timeout=(CONNECT_TIMEOUT_S, None)) as upstream:
                upstream.raise_for_status()
                for chunk in upstream.iter_content(chunk_size=None):
                    if chunk:
                        yield chunk
        except requests.RequestException as e:
            yield _sse_error(f"tracker unreachable: {e.__class__.__name__}")

    return Response(
        stream_with_context(generate()),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@bp.route("/data.json")
def data():
    """One snapshot, for looking at the wire format without holding a stream
    open. Not on the page's path: it opens the stream, takes the first
    message and hangs up."""
    window_s = _view_window()
    params = {"window": window_s} if window_s else None
    try:
        with requests.get(_tracker_url("/events"), params=params, stream=True,
                          timeout=(CONNECT_TIMEOUT_S, CONTROL_TIMEOUT_S)) as upstream:
            upstream.raise_for_status()
            for line in upstream.iter_lines(decode_unicode=True):
                if line and line.startswith("data: "):
                    return Response(line[6:], content_type="application/json")
    except requests.RequestException as e:
        return jsonify({"error": f"tracker unreachable: {e.__class__.__name__}"}), 502
    return jsonify({"error": "no snapshot"}), 502


@bp.route("/clear", methods=["POST"])
def clear():
    """Wipe the sidecar's record the page is drawn from; tracking keeps running.

    See docs/features/tracker.md#clearing-the-buffer.
    """
    try:
        response = requests.post(_tracker_url("/history/clear"),
                                 timeout=CONTROL_TIMEOUT_S)
        response.raise_for_status()
        return jsonify({"success": bool(response.json().get("ok"))})
    except (requests.RequestException, ValueError):
        return jsonify({"success": False, "error": "tracker unreachable"}), 502
