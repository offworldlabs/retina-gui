"""The Tracker page's routes, which are now a door onto retina-tracker.

retina-gui holds no tracker data. These cover the three things that door has
to get right: it carries the sidecar's stream without touching it, it says so
plainly when the sidecar is unreachable rather than serving an empty page, and
the URL nodes were bookmarked under keeps working.
"""

import json
import re
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
    assert b'data-layer="unassociated"' in response.data
    # Below SNR has no button: the class is always empty on retina-tracker
    # v0.5.0 and later. See docs/features/tracker.md#what-is-plotted.
    assert b'data-layer="below_snr"' not in response.data


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


# ── the axes ────────────────────────────────────────────────────────────────

class TestTheAxesComeFromTheNode:
    """A plot scaled to its own data cannot tell a quiet sky from a narrow one.
    On a node where nearly every detection is one interfering tone, an
    autoscaled Doppler axis collapses around that tone and the picture looks
    full; two nodes are then drawn at different scales and cannot be compared.
    The node's own ambiguity bounds are the only honest range."""

    def _bounds(self, config):
        merged = MagicMock()
        merged.load_merged_config.return_value = config
        with patch.dict('sys.modules', {'app': MagicMock(config_mgr=merged)}):
            return tracker_routes._axis_bounds()

    def test_the_doppler_span_is_the_nodes_own(self):
        config = {"process": {"ambiguity": {"dopplerMin": -300, "dopplerMax": 300}}}
        assert self._bounds(config)["doppler"] == [-300, 300]

    def test_a_wider_span_is_carried_through_unchanged(self):
        """+/-1000 Hz is deployed today. Nothing here may assume a value."""
        config = {"process": {"ambiguity": {"dopplerMin": -1000, "dopplerMax": 1000}}}
        assert self._bounds(config)["doppler"] == [-1000, 1000]

    def test_delay_bins_become_kilometres_through_the_sample_rate(self):
        config = {
            "capture": {"fs": 2000000},
            "process": {"ambiguity": {"delayMin": -10, "delayMax": 400}},
        }
        low, high = self._bounds(config)["delay"]
        assert high == pytest.approx(59.96, abs=0.01)
        assert low == pytest.approx(-1.5, abs=0.01)

    def test_delay_without_a_sample_rate_is_not_guessed(self):
        """The bins are meaningless without it, and a guessed cell width would
        draw a confident axis over the wrong range."""
        config = {"process": {"ambiguity": {"delayMin": -10, "delayMax": 400}}}
        assert self._bounds(config)["delay"] is None

    def test_a_config_without_bounds_leaves_the_axes_alone(self):
        assert self._bounds({"capture": {"fs": 2000000}}) == {"doppler": None, "delay": None}

    def test_half_a_span_is_not_half_an_axis(self):
        config = {"process": {"ambiguity": {"dopplerMin": -300}}}
        assert self._bounds(config)["doppler"] is None

    @pytest.mark.parametrize("lo,hi", [(-1000, 1000), (-300, 300), (-200, 200), (-15, 15), (-1, 1)])
    def test_every_span_across_the_range_is_carried_through(self, lo, hi):
        config = {"process": {"ambiguity": {"dopplerMin": lo, "dopplerMax": hi}}}
        assert self._bounds(config)["doppler"] == [lo, hi]

    @pytest.mark.parametrize("lo,hi", [(300, -300), (0, 0), (5, 5)])
    def test_a_pair_that_is_not_an_axis_is_not_drawn(self, lo, hi):
        """Transposed would draw the axis backwards and zero-width would
        collapse it. Either way a viewer gets a confident picture of nothing,
        where autoscaling at least shows the data."""
        config = {"process": {"ambiguity": {"dopplerMin": lo, "dopplerMax": hi}}}
        assert self._bounds(config)["doppler"] is None

    def test_an_unreadable_config_still_renders_the_page(self):
        """The axes are a nicety. Losing them must not lose the plot."""
        merged = MagicMock()
        merged.load_merged_config.side_effect = OSError("no config")
        with patch.dict('sys.modules', {'app': MagicMock(config_mgr=merged)}):
            assert tracker_routes._axis_bounds() == {"doppler": None, "delay": None}

    def test_the_page_carries_them_to_the_browser(self, app_client):
        config = {
            "capture": {"fs": 2000000},
            "process": {"ambiguity": {"dopplerMin": -300, "dopplerMax": 300,
                                      "delayMin": -10, "delayMax": 400}},
        }
        merged = MagicMock()
        merged.load_merged_config.return_value = config
        with patch.dict('sys.modules', {'app': MagicMock(config_mgr=merged)}):
            body = app_client.get('/tracker').get_data(as_text=True)

        rendered = re.search(r'var AXES = (.+?);\n', body).group(1)
        axes = json.loads(rendered)
        assert axes["doppler"] == [-300, 300]
        assert axes["delay"][1] == pytest.approx(59.96, abs=0.01)


# ── the Doppler view ────────────────────────────────────────────────────────

class TestTheDopplerViewIsPresentationOnly:
    """The node processes its whole span; the page chooses how much to draw.
    The behaviour lives in the page's script, so these pin the contract the
    rendered page carries. See docs/features/tracker.md#doppler-view."""

    @pytest.fixture
    def page(self, app_client):
        config = {"process": {"ambiguity": {"dopplerMin": -1000, "dopplerMax": 1000}}}
        merged = MagicMock()
        merged.load_merged_config.return_value = config
        with patch.dict('sys.modules', {'app': MagicMock(config_mgr=merged)}):
            return app_client.get('/tracker').get_data(as_text=True)

    def test_the_toggle_offers_narrow_and_full_with_narrow_first(self, page):
        narrow = re.search(r'<button[^>]*data-dop="narrow"[^>]*>', page).group(0)
        full = re.search(r'<button[^>]*data-dop="full"[^>]*>', page).group(0)
        assert 'aria-pressed="true"' in narrow
        assert 'aria-pressed="false"' in full

    def test_narrow_defaults_to_300_hz(self, page):
        assert re.search(r'var DEFAULT_NARROW_HZ = 300;', page)

    def test_the_full_view_is_the_nodes_span_not_a_constant(self, page):
        """Full draws AXES.doppler, so a node at another span is drawn at its own."""
        assert json.loads(re.search(r'var AXES = (.+?);\n', page).group(1))["doppler"] == [-1000, 1000]
        assert "state.dopView === 'narrow' ? narrowRange() : AXES.doppler" in page

    def test_the_gear_opens_a_settings_dialog_for_the_narrow_range(self, page):
        assert 'id="settingsBtn"' in page and 'aria-haspopup="dialog"' in page
        field = re.search(r'<input[^>]*id="narrowHzInput"[^>]*>', page).group(0)
        assert 'type="number"' in field and 'min="1"' in field

    def test_the_setting_is_kept_in_the_browser_and_never_posted(self, page):
        assert "remember('narrowHz', n)" in page
        assert "remember('dopView', v)" in page
        assert "remember('narrowKm', km)" in page
        assert "remember('delView', v)" in page
        # The only requests the page makes besides the stream are Reset wedge
        # and Clear buffer; the wedge view's reads live in static/tracker-wedge.js.
        assert re.findall(r"fetch\('([^']+)'", page) == ['/tracker/coverage/reset', '/tracker/clear']

    def test_the_out_of_view_alert_is_on_the_page_and_hidden_until_needed(self, page):
        alert = re.search(r'<span[^>]*id="oovAlert"[^>]*>', page).group(0)
        assert 'hidden' in alert
        assert 'id="oovShowBtn"' in page


class TestTheDelayViewMirrorsTheDopplerView:
    """The same narrow/full choice on the bistatic range axis, set from the
    same gear. See docs/features/tracker.md#delay-view."""

    @pytest.fixture
    def page(self, app_client):
        config = {"capture": {"fs": 2000000},
                  "process": {"ambiguity": {"delayMin": -10, "delayMax": 400}}}
        merged = MagicMock()
        merged.load_merged_config.return_value = config
        with patch.dict('sys.modules', {'app': MagicMock(config_mgr=merged)}):
            return app_client.get('/tracker').get_data(as_text=True)

    def test_the_toggle_opens_on_full_so_nothing_is_hidden_by_default(self, page):
        narrow = re.search(r'<button[^>]*data-del="narrow"[^>]*>', page).group(0)
        full = re.search(r'<button[^>]*data-del="full"[^>]*>', page).group(0)
        assert 'aria-pressed="false"' in narrow
        assert 'aria-pressed="true"' in full
        assert "recall('delView', 'full') === 'narrow' ? 'narrow' : 'full'" in page

    def test_narrow_defaults_to_30_km(self, page):
        assert re.search(r'var DEFAULT_NARROW_KM = 30;', page)

    def test_the_full_view_is_the_nodes_range_not_a_constant(self, page):
        assert "state.delView === 'narrow' ? narrowDelayRange() : AXES.delay" in page
        assert "xaxis: axis('Bistatic range (km)', delayRange())" in page

    def test_the_gear_dialog_has_a_field_for_the_narrow_delay_range(self, page):
        field = re.search(r'<input[^>]*id="narrowKmInput"[^>]*>', page).group(0)
        assert 'type="number"' in field and 'min="1"' in field
        dialog = re.search(r'<dialog[^>]*id="settingsDlg".*?</dialog>', page, re.S).group(0)
        assert 'id="narrowKmInput"' in dialog and 'id="narrowHzInput"' in dialog

    def test_show_full_span_widens_both_axes(self, page):
        handler = re.search(r"getElementById\('oovShowBtn'\).*?\}\);", page, re.S).group(0)
        assert "setDopView('full')" in handler and "setDelView('full')" in handler


class TestTheNarrowDelayDefaultFitsTheNode:
    """A node at the drone profile's delayMax (100 bins, 15 km) processes less
    than the 30 km default. Opened on 30, the gear refused every save until
    the delay field was edited by hand."""

    def test_the_default_is_clipped_to_what_the_node_processes(self, app_client):
        page = app_client.get('/tracker').get_data(as_text=True)
        assert "Math.min(DEFAULT_NARROW_KM, reach)" in page
        assert "recall('narrowKm', defaultNarrowKm())" in page
        assert "narrowKmInput.value = defaultNarrowKm();" in page


# ── the Drone view ──────────────────────────────────────────────────────────

class TestTheDroneViewFollowsTheDetector:
    """The Drone view draws at the limits the Processing section sets, so it
    follows whatever a test engineer applies. It exists only in developer
    mode. See docs/features/tracker.md#drone-view."""

    DETECTOR = {
        "capture": {"fs": 2000000},
        "process": {
            "data": {"cpi": 0.5},
            "ambiguity": {"delayMin": -10, "delayMax": 100,
                          "dopplerMin": -1000, "dopplerMax": 1000},
            "detection": {"minDelay": 2, "minDoppler": 5, "maxReportedDoppler": 150},
        },
    }

    def _settings(self, config, developer=True):
        merged = MagicMock()
        merged.load_merged_config.return_value = config
        state = MagicMock()
        state.is_developer_mode.return_value = developer
        with patch.dict('sys.modules', {'app': MagicMock(config_mgr=merged, device_state=state)}):
            return tracker_routes._drone_settings()

    def test_bins_and_rows_become_metres_and_hertz(self):
        s = self._settings(self.DETECTOR)
        assert s["cell_m"] == pytest.approx(149.9, abs=0.01)
        assert s["max_reported_hz"] == 150
        assert s["bin_hz"] == 2.0

    def test_the_limits_allow_for_interpolation(self):
        """blah2 moves a detection up to half a cell from the one tested, and
        the stream rounds delay to 10 m, so a node at minDelay 1 reports
        detections from 70 m. Shading from 150 m drew real detections inside
        "Not tested"."""
        s = self._settings(self.DETECTOR)
        assert s["near_limit_m"] == 220.0   # minDelay 2: 224.8 m, down to the stream's 10 m step
        assert s["slow_limit_hz"] == 5.0    # minDoppler 5 first tests the 6 Hz row

    def test_a_node_at_min_delay_1_is_shaded_to_70_m(self):
        """Measured on a test node at minDelay 1: the nearest detection the
        stream carried was 0.07 km."""
        config = dict(self.DETECTOR, process={"detection": {"minDelay": 1}})
        assert self._settings(config)["near_limit_m"] == 70.0

    @pytest.mark.parametrize("min_doppler,limit", [(15, 15.0), (16, 15.0), (4, 3.0), (1, 1.0)])
    def test_doppler_rows_sit_on_whole_bins(self, min_doppler, limit):
        config = {"process": {"data": {"cpi": 0.5},
                              "detection": {"minDoppler": min_doppler}}}
        assert self._settings(config)["slow_limit_hz"] == limit

    def test_nothing_outside_developer_mode(self):
        assert self._settings(self.DETECTOR, developer=False) is None

    def test_a_setting_the_config_does_not_state_is_not_guessed(self):
        """An older default.yml has no maxReportedDoppler; without fs the bins
        mean nothing. The page then falls back to its own defaults."""
        s = self._settings({"process": {"detection": {"minDelay": 5, "minDoppler": 0,
                                                      "maxReportedDoppler": "x"}}})
        assert s == {"cell_m": None, "near_limit_m": None, "slow_limit_hz": None,
                     "max_reported_hz": None, "bin_hz": None}

    def test_an_unreadable_config_still_offers_the_view(self):
        merged = MagicMock()
        merged.load_merged_config.side_effect = OSError("no config")
        state = MagicMock()
        state.is_developer_mode.return_value = True
        app_module = MagicMock(config_mgr=merged, device_state=state)
        with patch.dict('sys.modules', {'app': app_module}):
            assert tracker_routes._drone_settings()["cell_m"] is None
        assert "no config" in app_module.app.logger.warning.call_args.args[0]

    def test_a_customer_page_has_no_drone_view(self, app_client):
        page = app_client.get('/tracker').get_data(as_text=True)
        assert not re.search(r'<button[^>]*data-display="drone"', page)
        assert 'id="humInput"' not in page and 'data-fade=' not in page
        assert 'var DRONE = null;' in page
        # A remembered Drone display falls back rather than opening on nothing.
        assert "state.display === 'drone' && DRONE" in page

    def test_a_developer_page_offers_it_with_its_own_controls(self, app_client, developer_mode):
        page = app_client.get('/tracker').get_data(as_text=True)
        assert re.search(r'<button[^>]*data-display="drone"', page)
        for attr in ('data-fade="15"', 'data-fade="30"', 'data-floor="8"', 'data-dtracks="slow"'):
            assert attr in page
        dialog = re.search(r'<dialog[^>]*id="settingsDlg".*?</dialog>', page, re.S).group(0)
        for field in ('droneMInput', 'humInput', 'slowInput'):
            assert 'id="' + field + '"' in dialog
        drone = json.loads(re.search(r'var DRONE = (.+?);\n', page).group(1))
        assert drone["cell_m"] == pytest.approx(149.9, abs=0.01)

    def test_it_asks_the_node_for_the_shortest_window_it_serves(self, app_client, developer_mode):
        """The sidecar serves no less than 60 s, so the page asks for that and
        fades its own shorter window. The two minimums must match."""
        page = app_client.get('/tracker').get_data(as_text=True)
        assert f'var SERVER_MIN_WINDOW = {tracker_routes.MIN_VIEW_WINDOW_S};' in page
        assert "new EventSource('/tracker/events?window=' + requestWindow())" in page

    def test_the_settings_stay_in_the_browser(self, app_client, developer_mode):
        page = app_client.get('/tracker').get_data(as_text=True)
        for key in ('fade', 'floor', 'dtracks', 'droneM', 'hum', 'slowMs'):
            assert "remember('" + key + "'" in page
        assert re.findall(r"fetch\('([^']+)'", page) == ['/tracker/coverage/reset', '/tracker/clear']


# ── the coverage wedge ──────────────────────────────────────────────────────

def fake_reply(body, status=200):
    response = MagicMock()
    response.status_code = status
    response.content = json.dumps(body).encode()
    return response


def test_the_wedge_is_relayed_with_its_status(app_client):
    """A 404 or 503 from the sidecar means "no coverage on this tracker",
    which the page words differently from unreachable, so it is not masked."""
    body = {"enabled": True, "lobes": [], "held_km": [0.0] * 72}
    with patch.object(tracker_routes.requests, "get", return_value=fake_reply(body)) as mock_get:
        response = app_client.get('/tracker/coverage')
    assert response.status_code == 200
    assert response.get_json() == body
    assert mock_get.call_args.args[0].endswith("/coverage")

    with patch.object(tracker_routes.requests, "get",
                      return_value=fake_reply({"error": "not found"}, status=404)):
        assert app_client.get('/tracker/coverage').status_code == 404


def test_an_unreachable_sidecar_is_a_502_for_the_wedge(app_client):
    with patch.object(tracker_routes.requests, "get",
                      side_effect=requests.ConnectionError("refused")):
        response = app_client.get('/tracker/coverage')
    assert response.status_code == 502
    assert "unreachable" in response.get_json()["error"]


def test_the_arcs_window_is_clamped_and_forwarded(app_client):
    with patch.object(tracker_routes.requests, "get",
                      return_value=fake_reply({"arcs": []})) as mock_get:
        app_client.get('/tracker/coverage/arcs?window=999999')
    assert mock_get.call_args.kwargs["params"] == {"window": 4 * 3600}


def test_a_replay_is_fetched_for_the_named_track(app_client):
    with patch.object(tracker_routes.requests, "get",
                      return_value=fake_reply({"id": "261017-00012C", "arcs": []})) as mock_get:
        response = app_client.get('/tracker/coverage/track/261017-00012C')
    assert response.status_code == 200
    assert mock_get.call_args.args[0].endswith("/coverage/track/261017-00012C")


@pytest.mark.parametrize("track_id", ["a.b", "a%2F..%2Fb", "x" * 41])
def test_a_track_id_that_could_reach_another_path_is_refused(app_client, track_id):
    with patch.object(tracker_routes.requests, "get") as mock_get:
        response = app_client.get('/tracker/coverage/track/' + track_id)
    assert response.status_code in (400, 404)
    mock_get.assert_not_called()


def test_resetting_the_wedge_is_forwarded(app_client):
    with patch.object(tracker_routes.requests, "post",
                      return_value=fake_reply({"ok": True})) as mock_post:
        response = app_client.post('/tracker/coverage/reset')
    assert response.status_code == 200
    assert response.get_json() == {"ok": True}
    assert mock_post.call_args.args[0].endswith("/coverage/reset")


def test_the_page_carries_the_wedge_view(app_client):
    body = app_client.get('/tracker').get_data(as_text=True)
    assert 'leaflet@1.9.4/dist/leaflet.js' in body
    assert '/static/tracker-wedge.js' in body
    assert 'data-display="wedge"' in body
    assert 'Processed range limit' in body
    assert 'Close-in, overhead' in body
    assert re.search(r'cartoKey: "[^"]*",', body)
