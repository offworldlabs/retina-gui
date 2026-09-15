"""The Tracker page and its feed.

retina-gui holds no tracker data. The record of what a node has seen lives in
retina-tracker, which serves it over a loopback SSE endpoint, and this module
is the door onto it: the page's HTML, and a proxy for the stream.

Proxying rather than linking to the sidecar directly is what keeps the page
behind the same session and Cloudflare Access checks as everything else, and
what makes it work over the support tunnel, which routes paths on this
hostname and not arbitrary ports. It is also nearly free: bytes are passed
through without being parsed, buffered or re-serialised.
"""

import json

import requests
from flask import Blueprint, Response, jsonify, redirect, render_template, request, stream_with_context

bp = Blueprint('tracker', __name__, url_prefix='/tracker')

# Nodes have been in the field under the old name long enough for the URL to
# be bookmarked, so /tracker-preview keeps working permanently. 308 rather
# than 301 because /clear is a POST and 301 would let a browser turn it into
# a GET.
legacy_bp = Blueprint('tracker_preview', __name__, url_prefix='/tracker-preview')

# The sidecar's own bounds, mirrored rather than imported because they belong
# to another repo's release: a mismatch clamps to something sane here and the
# sidecar clamps again on its side.
MIN_VIEW_WINDOW_S = 60
MAX_VIEW_WINDOW_S = 4 * 3600

CONNECT_TIMEOUT_S = 5
CONTROL_TIMEOUT_S = 5

# For turning blah2's delay bins into kilometres. Mirrored rather than imported
# for the same reason as the window bounds above: it belongs to another repo.
SPEED_OF_LIGHT = 299792458.0


def _axis_bounds():
    """What the node can see, from its own blah2 config.

    A plot scaled to its own data cannot tell a quiet sky from a narrow one.
    On a node where nearly every detection is one interfering tone, an
    autoscaled Doppler axis collapses to a sliver around that tone and the
    picture looks full; two nodes, or the same node an hour apart, are drawn
    at different scales and cannot be compared. The ambiguity bounds are the
    only honest range to draw over, and they are the node's own numbers rather
    than anything chosen here.

    blah2 states delay in bins, which are kilometres only once the sample rate
    says how wide a bin is.

    None for anything the config does not state, which leaves that axis to
    autoscale exactly as it does today. A guessed range misrepresents the node
    just as autoscaling does, only less visibly.
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
    """Whether a pair is an axis rather than a typo.

    Both halves have to be there, and the high one has to be above the low
    one. A transposed pair would draw the axis backwards and a zero-width one
    would collapse it, and in both cases a viewer would be looking at a
    confident picture of nothing. Falling back to autoscale shows the data,
    which is the honest answer when the node has not described itself.
    """
    return lo is not None and hi is not None and hi > lo


def _tracker_url(path):
    from app import RETINA_TRACKER_CONTROL_URL
    return f"{RETINA_TRACKER_CONTROL_URL.rstrip('/')}{path}"


def _view_window():
    """The ?window= a viewer is asking for, in seconds, or None for whatever
    the sidecar holds. Clamped rather than rejected: it is a display
    preference, not an assertion."""
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
    """The Tracker page. Everything it draws arrives on /tracker/events.

    The axis bounds are the exception, and they cannot: they say what the node
    can see, which is not knowable from what it happened to see.
    """
    return render_template("tracker.html", axes=_axis_bounds())


@bp.route("/events")
def events():
    """Proxy the sidecar's stream, byte for byte.

    Nothing is parsed on the way through. The sidecar frames the messages,
    owns the cursor for this connection and decides what a snapshot contains;
    this only carries them, so there is no second copy of the record here and
    no format knowledge to drift out of step.

    A viewer's connection maps to its own upstream connection. On loopback
    with one or two viewers that is cheaper than holding a shared mirror and
    fanning it out, and it means each viewer's window is honoured by the
    sidecar rather than filtered a second time here.
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
    """Wipe the record the page is drawn from, without touching the tracker.

    The same distinction the button always carried: tracking keeps running,
    so an aircraft still overhead reappears on its own within a few events.
    It lives in the sidecar now because the record does.
    """
    try:
        response = requests.post(_tracker_url("/history/clear"),
                                 timeout=CONTROL_TIMEOUT_S)
        response.raise_for_status()
        return jsonify({"success": bool(response.json().get("ok"))})
    except (requests.RequestException, ValueError):
        return jsonify({"success": False, "error": "tracker unreachable"}), 502
