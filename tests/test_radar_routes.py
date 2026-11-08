"""The Passive Radar and Max-Hold pages and the proxy behind them.

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
    radar_routes._unreachable = False
    yield
    radar_routes._unreachable = False


def relaying(response):
    return patch.object(app_module.blah2_client, 'get_raw', return_value=response)


def set_mode(temp_dir, mode):
    with open(os.path.join(temp_dir, 'mode.txt'), 'w') as f:
        f.write(mode)


# ── the pages ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize('url,title,source', [
    ('/radar', 'Passive Radar', '/radar/data/map'),
    ('/radar/maxhold', 'Max-Hold', '/radar/data/maxhold'),
])
def test_each_page_draws_its_own_map(app_client, url, title, source):
    """The two pages are one display fed by different data."""
    body = app_client.get(url).data.decode()
    assert f'<title>{title} - OWL-OS</title>' in body
    assert f'window.RADAR_MAP_URL = "{source}"' in body


def test_plotly_comes_from_the_node_not_a_cdn(app_client):
    """The display has to work on a node with no internet."""
    body = app_client.get('/radar').data.decode()
    assert '/static/vendor/plotly-2.20.0.min.js' in body
    assert 'plotly.min.js"' not in body


@pytest.mark.parametrize('mode', ['spectrum', 'sdrconnect'])
@pytest.mark.parametrize('url', ['/radar', '/radar/maxhold'])
def test_a_page_goes_home_when_blah2_is_not_running(app_client, temp_dir, url, mode):
    """A bookmark must not open an empty plot that looks like a quiet sky."""
    set_mode(temp_dir, mode)
    response = app_client.get(url)
    assert response.status_code == 302
    assert response.headers['Location'] == '/'


@pytest.mark.parametrize('url', ['/radar', '/radar/maxhold', '/radar/data/timestamp'])
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
    assert first.get_json()['error'] == 'blah2_api unreachable: ConnectionError'
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
