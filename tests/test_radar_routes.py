"""The Passive Radar page, its Live and Max-Hold views, and the proxy behind it.

retina-gui holds no radar data, so these cover the door onto blah2_api: which
paths it opens, what it relays, and what it does when blah2_api is not there.
"""

import logging
import os
from unittest.mock import MagicMock, patch

import pytest
import requests

import app as app_module
import routes.radar as radar_routes


def upstream(body=b'{}', status=200, content_type='application/json'):
    response = MagicMock()
    response.content = body
    response.status_code = status
    response.headers = {'Content-Type': content_type}
    return response


@pytest.fixture(autouse=True)
def blah2_reachable():
    """Each test starts with no outage on record."""
    radar_routes._down.clear()
    yield
    radar_routes._down.clear()


def relaying(response):
    return patch.object(app_module.blah2_client, 'get_raw', return_value=response)


def set_mode(temp_dir, mode):
    with open(os.path.join(temp_dir, 'mode.txt'), 'w') as f:
        f.write(mode)


# ── the page ────────────────────────────────────────────────────────────────

def pressed(body):
    """The view buttons that render pressed."""
    import re
    return re.findall(r'data-view="(\w+)" aria-pressed="true"', body)


@pytest.mark.parametrize('url,view', [
    ('/radar', 'live'),
    ('/radar?view=live', 'live'),
    ('/radar?view=maxhold', 'maxhold'),
])
def test_the_url_picks_the_view_the_page_opens_on(app_client, url, view):
    """So a card, a reload or a bookmark lands on the view it named."""
    body = app_client.get(url).data.decode()
    assert '<title>Passive Radar - OWL-OS</title>' in body
    assert f'window.RADAR_VIEW = "{view}"' in body
    assert pressed(body) == [view]


@pytest.mark.parametrize('value', ['', 'map', 'MAXHOLD', '<script>', 'maxhold&x'])
def test_an_unknown_view_opens_live(app_client, value):
    body = app_client.get('/radar', query_string={'view': value}).data.decode()
    assert 'window.RADAR_VIEW = "live"' in body
    assert pressed(body) == ['live']


def test_the_old_max_hold_address_still_works(app_client):
    """Max-Hold was its own page before the toggle."""
    response = app_client.get('/radar/maxhold')
    assert response.status_code == 302
    assert response.headers['Location'] == '/radar?view=maxhold'


def test_plotly_comes_from_the_node_not_a_cdn(app_client):
    """The display has to work on a node with no internet."""
    body = app_client.get('/radar').data.decode()
    assert '/static/vendor/plotly-2.20.0.min.js' in body
    assert 'plotly.min.js"' not in body


# ── the axes and the colour scale ──────────────────────────────────────────

def rendered_axes(app_client, config):
    """The axes the page hands its script, with the node's config set to this."""
    import json
    import re
    merged = MagicMock()
    merged.load_merged_config.return_value = config
    with patch.dict('sys.modules', {'app': MagicMock(config_mgr=merged)}):
        body = app_client.get('/radar').get_data(as_text=True)
    return json.loads(re.search(r'window.RADAR_AXES = (.+?);\n', body).group(1))


def test_the_axes_are_the_nodes_own_extent(app_client):
    """The same bounds the Tracker draws, so one node reads at one scale on both
    pages, and the axes stay put whatever the map holds."""
    axes = rendered_axes(app_client, {
        "capture": {"fs": 2000000},
        "process": {"ambiguity": {"dopplerMin": -1000, "dopplerMax": 1000,
                                  "delayMin": -10, "delayMax": 400}},
    })
    assert axes["doppler"] == [-1000, 1000]
    assert axes["delay"][0] == pytest.approx(-1.5, abs=0.01)
    assert axes["delay"][1] == pytest.approx(59.96, abs=0.01)


def test_axes_the_config_does_not_state_are_left_to_the_map(app_client):
    assert rendered_axes(app_client, {}) == {"doppler": None, "delay": None}


def test_an_unreadable_config_still_opens_the_page(app_client):
    """The axes are a nicety. Losing them must not lose the display."""
    merged = MagicMock()
    merged.load_merged_config.side_effect = OSError("no config")
    with patch.dict('sys.modules', {'app': MagicMock(config_mgr=merged)}):
        response = app_client.get('/radar')
    assert response.status_code == 200
    assert 'window.RADAR_AXES = {"delay": null, "doppler": null}' in response.get_data(as_text=True)


def test_the_colour_scale_does_not_follow_the_data():
    """A scale stretched to each frame's loudest cell recoloured the whole map
    every second, and made a quiet sky look busiest. The script must set the
    scale once and never pass a per-frame one."""
    path = os.path.join(os.path.dirname(__file__), '..', 'static', 'radar-map.js')
    with open(path) as f:
        script = f.read()
    assert 'var SNR_RANGE = [0, 20];' in script
    assert 'zauto: false' in script
    assert 'maxPower' not in script


@pytest.mark.parametrize('mode', ['spectrum', 'sdrconnect'])
@pytest.mark.parametrize('url', ['/radar', '/radar?view=maxhold'])
def test_a_page_goes_home_when_blah2_is_not_running(app_client, temp_dir, url, mode):
    """A bookmark must not open an empty plot that looks like a quiet sky."""
    set_mode(temp_dir, mode)
    response = app_client.get(url)
    assert response.status_code == 302
    assert response.headers['Location'] == '/'


@pytest.mark.parametrize('url', ['/radar', '/radar?view=maxhold', '/radar/data/timestamp'])
def test_a_calibration_run_does_not_hold_the_display(app_client, url):
    """Watching the display during a run worked when it had its own port. An
    open page whose data was redirected would freeze with no explanation."""
    with patch.object(app_module.calibrator, 'is_running', return_value=True), \
            relaying(upstream(b'1', content_type='text/plain')):
        assert app_client.get(url).status_code == 200


# ── the proxy ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize('source,path', [
    ('timestamp', '/api/timestamp'),
    ('detection', '/api/detection'),
    ('adsb', '/api/adsb2dd'),
    ('config', '/api/config'),
    ('map', '/api/map'),
    ('maxhold', '/stash/map'),
])
def test_a_source_reaches_its_blah2_api_path(app_client, source, path):
    with relaying(upstream(b'{"nRows":1}')) as get_raw:
        response = app_client.get(f'/radar/data/{source}')
    assert get_raw.call_args.args == (path,)
    assert response.data == b'{"nRows":1}'


@pytest.mark.parametrize('source', ['capture', 'iqdata', '..', 'api/map'])
def test_nothing_else_on_blah2_api_is_reachable(app_client, source):
    with relaying(upstream()) as get_raw:
        assert app_client.get(f'/radar/data/{source}').status_code == 404
    get_raw.assert_not_called()


def test_the_content_type_is_set_here_not_relayed(app_client):
    """Older blah2_api labels JSON text/html. Relayed, ADS-B flight names would
    be served as a page from this origin."""
    with relaying(upstream(b'{"a":{"flight":"<b>"}}', content_type='text/html')):
        response = app_client.get('/radar/data/adsb')
    assert response.content_type == 'application/json'


def test_the_timestamp_stays_plain_text(app_client):
    """The page compares it as a string."""
    with relaying(upstream(b'1762500000000', content_type='text/html')):
        response = app_client.get('/radar/data/timestamp')
    assert response.content_type.startswith('text/plain')


def test_only_the_adsb_source_forwards_a_parameter(app_client):
    with relaying(upstream()) as get_raw:
        app_client.get('/radar/data/adsb?detection_ts=1762500000000&other=1')
        assert get_raw.call_args.kwargs['params'] == {'detection_ts': '1762500000000'}
        app_client.get('/radar/data/map?detection_ts=1')
        assert get_raw.call_args.kwargs['params'] is None


def test_an_upstream_status_is_relayed(app_client):
    """blah2_api answers 400 when ADS-B truth is off, which is not an outage."""
    with relaying(upstream(b'', status=400)):
        assert app_client.get('/radar/data/adsb').status_code == 400


def test_replies_are_not_cached(app_client):
    with relaying(upstream()):
        assert app_client.get('/radar/data/map').headers['Cache-Control'] == 'no-store'


def test_an_outage_is_a_502_logged_once(app_client, caplog):
    with patch.object(app_module.blah2_client, 'get_raw',
                      side_effect=requests.ConnectionError('refused')), \
            caplog.at_level(logging.WARNING, logger='routes.radar'):
        first = app_client.get('/radar/data/map')
        app_client.get('/radar/data/map')
    assert first.status_code == 502
    assert first.get_json()['error'] == 'blah2_api /api/map unreachable: ConnectionError'
    assert len([r for r in caplog.records if r.name == 'routes.radar']) == 1


def test_the_next_outage_is_logged_too(app_client, caplog):
    failing = patch.object(app_module.blah2_client, 'get_raw',
                           side_effect=requests.Timeout('slow'))
    with caplog.at_level(logging.WARNING, logger='routes.radar'):
        with failing:
            app_client.get('/radar/data/map')
        with relaying(upstream()):
            app_client.get('/radar/data/map')
        with failing:
            app_client.get('/radar/data/map')
    assert len([r for r in caplog.records if r.name == 'routes.radar']) == 2


def test_one_failing_source_does_not_flap_the_log(app_client, caplog):
    """A slow ADS-B lookup while the map answers: each second the page asks for
    both. One warning naming the ADS-B path, not a warning and a "reachable
    again" every second. See docs/features/radar.md#the-data-proxy"""
    def only_adsb_fails(path, params=None, timeout=None):
        if path == '/api/adsb2dd':
            raise requests.Timeout('slow')
        return upstream()
    with patch.object(app_module.blah2_client, 'get_raw', side_effect=only_adsb_fails), \
            caplog.at_level(logging.INFO, logger='routes.radar'):
        for _ in range(3):
            assert app_client.get('/radar/data/timestamp').status_code == 200
            assert app_client.get('/radar/data/adsb').status_code == 502
    logged = [r for r in caplog.records if r.name == 'routes.radar']
    assert [(r.levelname, r.getMessage()) for r in logged] == [
        ('WARNING', 'blah2_api /api/adsb2dd unreachable from the radar page: Timeout')]


def test_the_end_of_an_outage_names_its_source(app_client, caplog):
    with caplog.at_level(logging.INFO, logger='routes.radar'):
        with patch.object(app_module.blah2_client, 'get_raw', side_effect=requests.Timeout('slow')):
            app_client.get('/radar/data/adsb')
        with relaying(upstream()):
            app_client.get('/radar/data/map')
            app_client.get('/radar/data/adsb')
    messages = [r.getMessage() for r in caplog.records if r.name == 'routes.radar']
    assert messages[-1] == 'blah2_api /api/adsb2dd reachable from the radar page again'
    assert len(messages) == 2


def test_the_adsb_source_waits_past_blah2_apis_own_lookup_timeout(app_client):
    """blah2_api gives up on the lookup after 5 s and answers {}; cut off at the
    client's 3 s default, the page would never draw ADS-B truth on a node
    whose lookup is slow."""
    with relaying(upstream()) as get_raw:
        app_client.get('/radar/data/adsb')
        assert get_raw.call_args.kwargs['timeout'] == 7
        app_client.get('/radar/data/map')
        assert get_raw.call_args.kwargs['timeout'] is None
    assert radar_routes.TIMEOUTS_S['adsb'] > 5


# ── the shared frame ────────────────────────────────────────────────────────

@pytest.mark.parametrize('url', ['/radar', '/tracker'])
def test_both_scope_pages_use_the_shared_frame(app_client, url):
    """Passive Radar and the Tracker share one heading, panel and toolbar."""
    body = app_client.get(url).data.decode()
    for cls in ('scope-wrap', 'scope-head', 'scope-panel', 'scope-bar', 'scope-status', 'scope-foot'):
        assert f'class="{cls}' in body, (url, cls)


def test_the_radar_page_opens_with_a_status_line(app_client):
    body = app_client.get('/radar').data.decode()
    assert 'id="radarStatus"' in body
    assert 'connecting&hellip;' in body


def test_the_shared_segmented_control_stays_on_scope_pages():
    """.seg also names the Configuration page's peak-meter segments, so a bare
    .seg rule in common.css would restyle that meter."""
    import re
    from pathlib import Path
    css = (Path(__file__).resolve().parent.parent / 'static' / 'common.css').read_text()
    for rule in re.findall(r'^([^{}\n/][^{}\n]*)\{', css, re.M):
        for selector in rule.split(','):
            selector = selector.strip()
            if re.search(r'(^|\s)\.seg\b', selector):
                assert selector.startswith(('.scope-wrap ', '.peak-meter ')), selector


# ── the Doppler and delay views ────────────────────────────────────────────

def script():
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / 'static' / 'radar-map.js').read_text()


def test_the_page_opens_on_the_trackers_default_views(app_client):
    """Narrow Doppler, where ordinary air traffic sits, and full delay, as the
    Tracker opens. The script then restores whatever this browser saved."""
    import re
    body = app_client.get('/radar').get_data(as_text=True)
    assert re.findall(r'data-dop="(\w+)" aria-pressed="true"', body) == ['narrow']
    assert re.findall(r'data-del="(\w+)" aria-pressed="true"', body) == ['full']
    assert 'id="settingsBtn"' in body
    assert '<dialog class="scope-dlg" id="settingsDlg"' in body
    assert 'id="oovAlert"' in body


def controls():
    from pathlib import Path
    return (Path(__file__).resolve().parent.parent / 'static' / 'scope-controls.js').read_text()


def test_the_views_are_saved_apart_from_the_trackers():
    """Each page keeps its own views, as the Tracker keeps its under tracker.*,
    so narrowing one page never narrows the other behind a viewer's back."""
    js = script()
    assert "prefix: 'radar'," in js
    assert "'tracker." not in js
    assert "localStorage.setItem(prefix + '.' + key" in controls()


def test_a_double_click_returns_to_the_chosen_views():
    """Plotly's own double-click goes back to the ranges the plot was built
    with, which after a toggle is the wrong view, so the page handles it."""
    js = script()
    assert "doubleClick: false" in js
    assert "gd.on('plotly_doubleclick', showViews)" in js


def test_the_settings_dialog_is_styled_once_for_both_pages():
    """The gear, dialog, alert and link styles are shared, so the two pages'
    settings cannot drift apart."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    css = (root / 'static' / 'common.css').read_text()
    for rule in ('.scope-dlg {', '.ic-btn {', '.oov-alert {', '.linkbtn {', '.dlg-foot {'):
        assert ('\n' + css).count('\n' + rule) == 1, rule
        for page in ('tracker.html', 'radar.html'):
            assert '\n' + rule not in (root / 'templates' / page).read_text(), (page, rule)


def test_the_units_toggle_is_shared_with_the_tracker(app_client):
    """One key, display.units, through static/units.js: the one setting the two
    pages share. See docs/features/tracker.md#units."""
    import re
    body = app_client.get('/radar').get_data(as_text=True)
    assert re.findall(r'data-units="(\w+)" aria-pressed="true"', body) == ['metric']
    assert '<script src="/static/units.js"></script>' in body
    assert (body.index('/static/units.js') < body.index('/static/scope-controls.js')
            < body.index('/static/radar-map.js'))
    js = script()
    assert "var U = views.units();" in js
    assert "x: data.delay.map(U.distOf)," in js
    assert "text: 'Bistatic Range (' + U.dist + ')'," in js
    # A press rebuilds the plot in the new unit from the last map.
    assert "shape = null;" in js and "if (lastMap) drawMap(lastMap);" in js
    assert "U = u;" in js
    # The toggle, the km storage and the reach clip are the Tracker's own,
    # from static/scope-controls.js. See docs/features/tracker.md#units
    shared = controls()
    assert "U = DisplayUnits.current(v);" in shared
    assert "var km = Math.round(U.distOf(state.narrowKm)) === dist ? state.narrowKm : U.kmOf(dist);" in shared
    assert "if (reach && km > reach) km = reach;" in shared
