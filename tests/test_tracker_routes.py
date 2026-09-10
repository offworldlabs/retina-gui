"""The Tracker page's routes, which are now a door onto retina-tracker.

retina-gui holds no tracker data. These cover the three things that door has
to get right: it carries the sidecar's stream without touching it, it says so
plainly when the sidecar is unreachable rather than serving an empty page, and
the URL nodes were bookmarked under keeps working.
"""

from unittest.mock import MagicMock, patch

import pytest
import requests

import routes.tracker as tracker_routes

SNAPSHOT = (
    'event: snapshot\n'
    'data: {"gen":0,"window_s":900,"detections":{"associated":{"t":[1000],'
    '"delay":[10.0],"doppler":[50.0],"snr":[15.0]},"unassociated":{"t":[],'
    '"delay":[],"doppler":[],"snr":[]},"below_snr":{"t":[],"delay":[],'
    '"doppler":[],"snr":[]}},"tracks":{}}\n\n'
)


def fake_upstream(chunks, status=200):
    """Stands in for requests.get(..., stream=True) as a context manager."""
    response = MagicMock()
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    response.status_code = status
    response.iter_content.return_value = iter(chunks)
    response.iter_lines.return_value = iter(
        [line for chunk in chunks for line in chunk.decode().split("\n")])
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError("boom")
    return response


def drain(response, limit=4096):
    out = b""
    for chunk in response.response:
        out += chunk
        if len(out) >= limit:
            break
    return out


# ── the stream ──────────────────────────────────────────────────────────────

def test_the_stream_is_passed_through_untouched(app_client):
    """Nothing is parsed on the way past. The sidecar frames the messages and
    owns the cursor; re-encoding here would be a second copy of a format to
    drift out of step."""
    with patch.object(tracker_routes.requests, "get",
                      return_value=fake_upstream([SNAPSHOT.encode()])):
        response = app_client.get('/tracker/events')
        assert response.status_code == 200
        assert response.content_type.startswith('text/event-stream')
        assert drain(response) == SNAPSHOT.encode()


def test_the_window_is_forwarded_to_the_sidecar(app_client):
    """Filtering happens once, where the record is. Doing it again here would
    mean two things that both have to agree about a window."""
    with patch.object(tracker_routes.requests, "get",
                      return_value=fake_upstream([b""])) as mock_get:
        app_client.get('/tracker/events?window=900')
    assert mock_get.call_args.kwargs["params"] == {"window": 900}


@pytest.mark.parametrize("asked,sent", [
    ("999999", 4 * 3600),   # clamped down to what is held
    ("1", 60),              # clamped up off the floor
])
def test_the_window_is_clamped(app_client, asked, sent):
    with patch.object(tracker_routes.requests, "get",
                      return_value=fake_upstream([b""])) as mock_get:
        app_client.get('/tracker/events?window=' + asked)
    assert mock_get.call_args.kwargs["params"] == {"window": sent}


def test_a_nonsense_window_asks_for_everything(app_client):
    with patch.object(tracker_routes.requests, "get",
                      return_value=fake_upstream([b""])) as mock_get:
        app_client.get('/tracker/events?window=nonsense')
    assert mock_get.call_args.kwargs["params"] is None


def test_an_unreachable_sidecar_says_so_on_the_stream(app_client):
    """An empty plot and a dead sidecar look identical otherwise, which is
    the whole failure this page exists to make visible."""
    with patch.object(tracker_routes.requests, "get",
                      side_effect=requests.ConnectionError("refused")):
        response = app_client.get('/tracker/events')
        body = drain(response)

    assert response.status_code == 200, "the stream itself opened"
    assert b"event: error" in body
    assert b"tracker unreachable" in body


def test_the_stream_has_no_read_timeout(app_client):
    """A quiet sky is a silent stream. A read timeout would drop the
    connection every time nothing was overhead."""
    with patch.object(tracker_routes.requests, "get",
                      return_value=fake_upstream([b""])) as mock_get:
        app_client.get('/tracker/events')
    _connect, read = mock_get.call_args.kwargs["timeout"]
    assert read is None


# ── the snapshot endpoint ───────────────────────────────────────────────────

def test_data_json_returns_one_snapshot(app_client):
    with patch.object(tracker_routes.requests, "get",
                      return_value=fake_upstream([SNAPSHOT.encode()])):
        response = app_client.get('/tracker/data.json')

    assert response.status_code == 200
    body = response.get_json()
    assert body["detections"]["associated"]["t"] == [1000]
    assert set(body["detections"]) == {"associated", "unassociated", "below_snr"}


def test_data_json_reports_an_unreachable_sidecar(app_client):
    with patch.object(tracker_routes.requests, "get",
                      side_effect=requests.ConnectionError("refused")):
        response = app_client.get('/tracker/data.json')
    assert response.status_code == 502
    assert "error" in response.get_json()


# ── clear ───────────────────────────────────────────────────────────────────

def test_clear_wipes_the_record_in_the_sidecar(app_client):
    """The button's meaning is unchanged: clear what I am shown, keep
    tracking. It goes to the sidecar because the record does."""
    ok = MagicMock()
    ok.json.return_value = {"ok": True}
    with patch.object(tracker_routes.requests, "post", return_value=ok) as mock_post:
        response = app_client.post('/tracker/clear')

    assert response.status_code == 200
    assert response.get_json() == {"success": True}
    assert mock_post.call_args[0][0].endswith("/history/clear")


def test_clear_reports_an_unreachable_sidecar(app_client):
    with patch.object(tracker_routes.requests, "post",
                      side_effect=requests.ConnectionError("refused")):
        response = app_client.post('/tracker/clear')
    assert response.status_code == 502
    assert response.get_json()["success"] is False


# ── the page and the old URL ────────────────────────────────────────────────

def test_the_page_renders_without_the_sidecar(app_client):
    """The HTML is served by retina-gui and must not depend on the sidecar
    being up; the stream reports that separately."""
    response = app_client.get('/tracker')
    assert response.status_code == 200
    assert b'railList' in response.data
    assert b'data-layer="below_snr"' in response.data


def test_old_preview_urls_still_work(app_client):
    response = app_client.get('/tracker-preview')
    assert response.status_code == 308
    assert response.headers['Location'].endswith('/tracker')

    response = app_client.get('/tracker-preview/data.json?window=900')
    assert response.status_code == 308
    assert response.headers['Location'].endswith('/tracker/data.json?window=900')

    response = app_client.post('/tracker-preview/clear')
    assert response.status_code == 308
    assert response.headers['Location'].endswith('/tracker/clear')
