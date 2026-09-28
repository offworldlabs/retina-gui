"""Tests for tower finder proxy routes and tower selection."""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import yaml

# Add src to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


SAMPLE_TOWER_RESPONSE = {
    "towers": [
        {
            "rank": 1,
            "callsign": "ATN6",
            "name": "ABC Tower Gore Hill",
            "state": "NSW",
            "frequency_mhz": 177.5,
            "band": "VHF",
            "latitude": -33.820079,
            "longitude": 151.185,
            "distance_km": 5.9,
            "bearing_deg": 337.5,
            "bearing_cardinal": "NNW",
            "received_power_dbm": -7.7,
            "distance_class": "Ideal",
            "eirp_dbm": 79.1,
            "altitude_m": 122.5,
            "antenna_height_m": 77.3,
        }
    ],
    "query": {
        "latitude": -33.8688,
        "longitude": 151.2093,
        "altitude_m": 0,
        "radius_km": 80,
        "source": "au",
    },
    "count": 1,
}


class TestTowerSearch:
    """Tests for POST /towers/search proxy route."""

    @patch('routes.towers.http_requests.get')
    def test_search_returns_towers(self, mock_get, app_client):
        """Proxy returns tower data from retina-server API."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_TOWER_RESPONSE
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        resp = app_client.post('/towers/search', json={'lat': -33.8688, 'lon': 151.2093})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data['count'] == 1
        assert data['towers'][0]['callsign'] == 'ATN6'

        mock_get.assert_called_once()
        assert '/api/towers' in mock_get.call_args[0][0]

    def test_search_missing_body(self, app_client):
        """Returns 400 when request body is missing."""
        resp = app_client.post('/towers/search', json={})
        assert resp.status_code == 400

    def test_search_missing_lon(self, app_client):
        """Returns 400 when lon missing."""
        resp = app_client.post('/towers/search', json={'lat': -33.8688})
        assert resp.status_code == 400

    @patch('routes.towers.http_requests.get')
    def test_search_timeout(self, mock_get, app_client):
        """Returns 504 on timeout."""
        import requests
        mock_get.side_effect = requests.Timeout()

        resp = app_client.post('/towers/search', json={'lat': -33.8688, 'lon': 151.2093})
        assert resp.status_code == 504
        data = json.loads(resp.data)
        assert 'timed out' in data['error'].lower()

    @patch('routes.towers.http_requests.get')
    def test_search_upstream_error(self, mock_get, app_client):
        """Returns 502 when retina-server API is unreachable."""
        import requests
        mock_get.side_effect = requests.ConnectionError()

        resp = app_client.post('/towers/search', json={'lat': -33.8688, 'lon': 151.2093})
        assert resp.status_code == 502

    @patch('routes.towers.http_requests.get')
    def test_search_forwards_body(self, mock_get, app_client):
        """Forwards entire JSON body to retina-server API."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"towers": [], "count": 0}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        body = {'lat': 38.9, 'lon': -77.0, 'altitude': 50, 'limit': 10, 'source': 'us', 'radius_km': 100}
        app_client.post('/towers/search', json=body)

        # The plain (no-measurements) search forwards via query params on a GET;
        # the JSON-body form is the measurement-enriched POST branch.
        forwarded = mock_get.call_args[1]['params']
        assert forwarded['lat'] == 38.9
        assert forwarded['lon'] == -77.0
        assert forwarded['altitude'] == 50
        assert forwarded['source'] == 'us'

    @patch('routes.towers.http_requests.get')
    def test_search_caches_results_for_auto_calibrate(self, mock_get, app_client):
        """A successful search with towers populates the device_state cache
        that Auto-Calibrate's alternate-tower lookup prefers."""
        import app as app_module
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = SAMPLE_TOWER_RESPONSE
        mock_get.return_value = mock_resp

        app_client.post('/towers/search', json={'lat': -33.8688, 'lon': 151.2093})

        cached = app_module.device_state.get_towers_cache()
        assert cached is not None
        assert cached['lat'] == -33.8688
        assert cached['towers'][0]['callsign'] == 'ATN6'

    @patch('routes.towers.http_requests.get')
    def test_search_does_not_cache_empty_results(self, mock_get, app_client):
        """An empty/no-towers response must not overwrite an existing cache
        with nothing."""
        import app as app_module
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"towers": [], "count": 0}
        mock_get.return_value = mock_resp

        app_client.post('/towers/search', json={'lat': -33.8688, 'lon': 151.2093})

        assert app_module.device_state.get_towers_cache() is None

    @patch('routes.towers.http_requests.get')
    def test_search_caches_only_the_best_few(self, mock_get, app_client):
        """The tower-finder can return dozens of results (its own default,
        uncapped by the wizard's search request) — only the best
        MAX_CACHED_TOWERS get cached, keeping /config's Tower picker usable.
        The full, uncapped list still goes back to the wizard's own response."""
        import app as app_module
        many_towers = [
            {"callsign": f"T{i}", "frequency_mhz": 100.0 + i, "latitude": 0, "longitude": 0}
            for i in range(50)
        ]
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"towers": many_towers, "count": 50}
        mock_get.return_value = mock_resp

        resp = app_client.post('/towers/search', json={'lat': -33.8688, 'lon': 151.2093})

        assert len(resp.get_json()['towers']) == 50

        cached = app_module.device_state.get_towers_cache()
        assert len(cached['towers']) == 5
        assert [t['callsign'] for t in cached['towers']] == ['T0', 'T1', 'T2', 'T3', 'T4']

    @patch('routes.towers.http_requests.get')
    def test_search_trims_an_overlong_name_before_caching(self, mock_get, app_client):
        """Picking a preset assigns straight into location.tx_name, which
        maxlength does not constrain and novalidate does not catch, so an
        over-long finder result would fail the save with no user mistake."""
        import app as app_module
        from config_schema import TX_NAME_MAX_LENGTH
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"towers": [{
            "callsign": "C" * 40,
            "name": "Some Very Long Broadcast Facility Name Indeed",
            "frequency_mhz": 177.5,
            "latitude": -33.82,
            "longitude": 151.185,
        }], "count": 1}
        mock_get.return_value = mock_resp

        app_client.post('/towers/search', json={'lat': -33.8688, 'lon': 151.2093})

        cached = app_module.device_state.get_towers_cache()['towers'][0]
        assert cached['callsign'] == 'C' * TX_NAME_MAX_LENGTH
        # Trimmed too: the picker falls back callsign -> name, and a facility
        # name runs long far more readily than a callsign does.
        assert len(cached['name']) == TX_NAME_MAX_LENGTH

    @patch('routes.towers.http_requests.get')
    def test_search_drops_towers_with_unusable_coordinates(self, mock_get, app_client):
        """A tower whose position cannot be a position is no use to the preset
        picker or to Auto-Calibrate's alternate-tower list."""
        import app as app_module
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"towers": [
            {"callsign": "BAD_LAT", "frequency_mhz": 100.0, "latitude": 442.2, "longitude": 151.0},
            {"callsign": "BAD_LON", "frequency_mhz": 101.0, "latitude": -33.8, "longitude": 999.0},
            {"callsign": "NO_POS", "frequency_mhz": 102.0},
            {"callsign": "GOOD", "frequency_mhz": 103.0, "latitude": -33.8, "longitude": 151.0},
        ], "count": 4}
        mock_get.return_value = mock_resp

        resp = app_client.post('/towers/search', json={'lat': -33.8688, 'lon': 151.2093})

        # The wizard's own response is untouched; only the cache is screened.
        assert len(resp.get_json()['towers']) == 4

        cached = app_module.device_state.get_towers_cache()
        assert [t['callsign'] for t in cached['towers']] == ['GOOD']

    @patch('routes.towers.http_requests.get')
    def test_search_keeps_the_best_few_after_screening(self, mock_get, app_client):
        """Screening runs before the cap, so a dropped result does not cost a
        slot in the picker."""
        import app as app_module
        towers = [{"callsign": "BAD", "frequency_mhz": 99.0, "latitude": 442.2, "longitude": 0}]
        towers += [{"callsign": f"T{i}", "frequency_mhz": 100.0 + i, "latitude": 0, "longitude": 0}
                   for i in range(10)]
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"towers": towers, "count": len(towers)}
        mock_get.return_value = mock_resp

        app_client.post('/towers/search', json={'lat': -33.8688, 'lon': 151.2093})

        cached = app_module.device_state.get_towers_cache()
        assert [t['callsign'] for t in cached['towers']] == ['T0', 'T1', 'T2', 'T3', 'T4']

    @patch('routes.towers.http_requests.get')
    def test_search_does_not_clear_the_cache_when_nothing_survives(self, mock_get, app_client):
        """Same rule as an empty response: a stale cache beats no cache."""
        import app as app_module
        app_module.device_state.save_towers_cache(0, 0, [
            {"callsign": "KEEP", "frequency_mhz": 100.0, "latitude": 0, "longitude": 0},
        ])
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"towers": [
            {"callsign": "BAD", "frequency_mhz": 99.0, "latitude": 442.2, "longitude": 0},
        ], "count": 1}
        mock_get.return_value = mock_resp

        app_client.post('/towers/search', json={'lat': -33.8688, 'lon': 151.2093})

        cached = app_module.device_state.get_towers_cache()
        assert [t['callsign'] for t in cached['towers']] == ['KEEP']


class TestTowerCacheAdd:
    """Tests for POST /towers/cache/add route."""

    def test_add_creates_cache_when_none_exists(self, app_client):
        """Adding a tower manually works even if the wizard was never run."""
        import app as app_module
        assert app_module.device_state.get_towers_cache() is None

        resp = app_client.post('/towers/cache/add', json={
            'callsign': 'Manual FM', 'frequency_mhz': 95.5,
            'latitude': -33.9, 'longitude': 151.2, 'altitude_m': 30,
        })

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['towers'] == [{
            'callsign': 'Manual FM', 'name': 'Manual FM', 'frequency_mhz': 95.5,
            'latitude': -33.9, 'longitude': 151.2, 'altitude_m': 30.0, 'source': 'manual',
        }]

    def test_add_appends_to_existing_cache(self, app_client):
        import app as app_module
        app_module.device_state.save_towers_cache(-33.8688, 151.2093, SAMPLE_TOWER_RESPONSE['towers'])

        resp = app_client.post('/towers/cache/add', json={
            'callsign': 'Manual FM', 'frequency_mhz': 95.5, 'latitude': -33.9, 'longitude': 151.2,
        })

        assert resp.status_code == 200
        towers = resp.get_json()['towers']
        assert len(towers) == 2
        assert towers[0]['callsign'] == 'ATN6'
        assert towers[1]['callsign'] == 'Manual FM'

    def test_add_rejects_missing_callsign(self, app_client):
        resp = app_client.post('/towers/cache/add', json={
            'callsign': '', 'frequency_mhz': 95.5, 'latitude': -33.9, 'longitude': 151.2,
        })
        assert resp.status_code == 400
        assert resp.get_json()['success'] is False

    def test_add_rejects_overlong_callsign(self, app_client):
        """32 is retina-telemetry's tx_callsign cap; a longer one would be
        accepted into the cache here and only fail later, when the tower is
        selected and the whole location block stops validating."""
        resp = app_client.post('/towers/cache/add', json={
            'callsign': 'x' * 33, 'frequency_mhz': 95.5, 'latitude': -33.9, 'longitude': 151.2,
        })
        assert resp.status_code == 400
        assert '32 characters' in resp.get_json()['error']

    def test_add_accepts_callsign_at_the_cap(self, app_client):
        resp = app_client.post('/towers/cache/add', json={
            'callsign': 'x' * 32, 'frequency_mhz': 95.5, 'latitude': -33.9, 'longitude': 151.2,
        })
        assert resp.status_code == 200

    def test_add_rejects_out_of_range_latitude(self, app_client):
        resp = app_client.post('/towers/cache/add', json={
            'callsign': 'Bad', 'frequency_mhz': 95.5, 'latitude': 999, 'longitude': 151.2,
        })
        assert resp.status_code == 400
        assert 'Latitude' in resp.get_json()['error']

    def test_add_rejects_non_numeric_frequency(self, app_client):
        resp = app_client.post('/towers/cache/add', json={
            'callsign': 'Bad', 'frequency_mhz': 'not-a-number', 'latitude': -33.9, 'longitude': 151.2,
        })
        assert resp.status_code == 400
        assert resp.get_json()['success'] is False


class TestTowerCacheRemove:
    """Tests for POST /towers/cache/remove route."""

    def test_remove_by_index(self, app_client):
        import app as app_module
        app_module.device_state.save_towers_cache(-33.8688, 151.2093, [
            {'callsign': 'A', 'frequency_mhz': 100.0},
            {'callsign': 'B', 'frequency_mhz': 200.0},
        ])

        resp = app_client.post('/towers/cache/remove', json={'index': 0})

        assert resp.status_code == 200
        towers = resp.get_json()['towers']
        assert len(towers) == 1
        assert towers[0]['callsign'] == 'B'

    def test_remove_out_of_range_index_fails(self, app_client):
        import app as app_module
        app_module.device_state.save_towers_cache(-33.8688, 151.2093, [
            {'callsign': 'A', 'frequency_mhz': 100.0},
        ])

        resp = app_client.post('/towers/cache/remove', json={'index': 5})

        assert resp.status_code == 404
        assert resp.get_json()['success'] is False

    def test_remove_with_no_cache_fails(self, app_client):
        import app as app_module
        assert app_module.device_state.get_towers_cache() is None

        resp = app_client.post('/towers/cache/remove', json={'index': 0})

        assert resp.status_code == 404


class TestTowerSelect:
    """Tests for POST /towers/select route."""

    def test_select_refuses_an_empty_location(self, app_client, config_files):
        """The model now allows an empty location, because a node legitimately
        has none. This endpoint sets one, so it must never clear one."""
        resp = app_client.post(
            '/towers/select',
            data=json.dumps({"tx_callsign": "ATN6"}),
            content_type='application/json',
        )

        assert resp.status_code == 400

    def test_select_refuses_a_partial_location(self, app_client, config_files):
        resp = app_client.post(
            '/towers/select',
            data=json.dumps({"rx_latitude": -33.8688, "tx_callsign": "ATN6"}),
            content_type='application/json',
        )

        assert resp.status_code == 400

    def test_select_saves_location(self, app_client, config_files):
        """Saves RX + TX location to user.yml with node_id and callsign."""
        user_path, _ = config_files
        payload = {
            "rx_latitude": -33.8688,
            "rx_longitude": 151.2093,
            "rx_altitude": 45.0,
            "tx_latitude": -33.820079,
            "tx_longitude": 151.185,
            "tx_altitude": 122.5,
            "tx_callsign": "ATN6",
        }

        resp = app_client.post(
            '/towers/select',
            data=json.dumps(payload),
            content_type='application/json',
        )
        # Queued, not applied inline: the config write is synchronous but the
        # merge+restart goes to the shared queue (see apply_service.py).
        assert resp.status_code == 202
        data = json.loads(resp.data)
        assert data['success'] is True

        # The write must have happened before the response, not been deferred
        # to the queue — anything reading user.yml next has to see it.
        with open(user_path) as f:
            saved = yaml.safe_load(f)
        assert saved['location']['rx']['latitude'] == -33.8688
        assert saved['location']['rx']['name'] == 'ret7dd2cb0d'  # node_id
        assert saved['location']['tx']['latitude'] == -33.820079
        assert saved['location']['tx']['name'] == 'ATN6'  # callsign

    def test_select_missing_body(self, app_client):
        """Returns 400 when body is missing."""
        resp = app_client.post('/towers/select', content_type='application/json')
        assert resp.status_code == 400

    def test_select_invalid_latitude(self, app_client):
        """Returns 400 for out-of-range latitude."""
        payload = {
            "rx_latitude": 999,
            "rx_longitude": 151.2093,
            "rx_altitude": 0,
            "tx_latitude": -33.82,
            "tx_longitude": 151.185,
            "tx_altitude": 0,
            "tx_callsign": "TEST",
        }

        resp = app_client.post(
            '/towers/select',
            data=json.dumps(payload),
            content_type='application/json',
        )
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert data['success'] is False

    def test_select_preserves_other_config(self, app_client, config_files):
        """Preserves non-location fields in user.yml."""
        user_path, _ = config_files
        payload = {
            "rx_latitude": -33.8688,
            "rx_longitude": 151.2093,
            "rx_altitude": 45.0,
            "tx_latitude": -33.82,
            "tx_longitude": 151.185,
            "tx_altitude": 100.0,
            "tx_callsign": "NEW1",
        }

        resp = app_client.post(
            '/towers/select',
            data=json.dumps(payload),
            content_type='application/json',
        )
        assert resp.status_code == 202

        with open(user_path) as f:
            saved = yaml.safe_load(f)
        # network.node_id should be preserved
        assert saved.get('network', {}).get('node_id') == 'ret7dd2cb0d'


class TestSetupWizardLocationStep:
    """Tests for the Location step in the setup wizard."""

    def test_setup_page_has_location_step(self, app_client):
        """Setup wizard HTML includes the Location step."""
        resp = app_client.get('/set-up')
        html = resp.data.decode()
        assert 'data-step="location"' in html
        assert 'Find towers' in html

    def test_setup_page_has_all_steps(self, app_client):
        """Setup wizard has all 7 step panels."""
        resp = app_client.get('/set-up')
        html = resp.data.decode()
        assert 'data-step="location"' in html
        assert 'data-step="towers"' in html
        assert 'data-step="calibrate"' in html
        assert 'data-step="complete"' in html

    def test_calibrate_step_sits_between_towers_and_complete(self, app_client):
        """Steps are discovered from DOM order, so the include order in
        setup.html *is* the wizard order — tuning has to come after a tower
        is chosen and before the completion restart."""
        html = app_client.get('/set-up').data.decode()
        assert (html.index('data-step="towers"')
                < html.index('data-step="calibrate"')
                < html.index('data-step="complete"'))

    def test_calibrate_step_does_not_promise_aircraft_before_the_run(self, app_client):
        """The wizard run never waits for a track, so copy shown *before* and
        *during* it must not lead the owner to expect aircraft — a normal
        result would then read as a failure. Scoped to the pre-result half on
        purpose: the post-run explainer does discuss aircraft, to say why none
        were waited for, which is the opposite problem."""
        html = app_client.get('/set-up').data.decode()
        start = html.index('data-step="calibrate"')
        panel = html[start:html.index('data-step="complete"')]
        before_result = panel[:panel.index('id="calWizResult"')]
        assert 'aircraft' not in before_result.lower()
        assert 'gain settings' in before_result.lower()

    def test_calibrate_step_has_a_slot_for_the_post_run_explanation(self, app_client):
        """The copy itself is the author's to write; this only guards the
        wiring. setup.js reveals #calWizNext on a terminal run and hides it
        again on re-entry, so losing the id would silently drop whatever is
        written there."""
        html = app_client.get('/set-up').data.decode()
        start = html.index('data-step="calibrate"')
        panel = html[start:html.index('data-step="complete"')]
        assert 'id="calWizNext"' in panel

        with open(os.path.join(os.path.dirname(__file__), '..',
                               'static', 'setup.js')) as f:
            setup_js = f.read()
        assert "el('calWizNext').style.display = ''" in setup_js
        # Both reset paths must hide it again: re-entering the step, and
        # starting a fresh run. Not an exact count, so adding a legitimate
        # third reset later does not fail this.
        assert setup_js.count("el('calWizNext').style.display = 'none'") >= 2

    def test_setup_page_loads_shared_calibrate_driver(self, app_client):
        """The wizard step and the Configuration modal must not drift — both
        consume static/calibrate.js."""
        html = app_client.get('/set-up').data.decode()
        assert '/static/calibrate.js' in html

    def test_setup_page_includes_leaflet(self, app_client):
        """Setup wizard loads Leaflet JS and CSS."""
        resp = app_client.get('/set-up')
        html = resp.data.decode()
        assert 'leaflet.css' in html
        assert 'leaflet.js' in html


SAMPLE_GEOCODE_RESPONSE = {
    "query": "1600 Pennsylvania Ave NW, Washington, DC",
    "latitude": 38.898699,
    "longitude": -77.035188,
    "matched_address": "1600 PENNSYLVANIA AVE NW, WASHINGTON, DC, 20500",
    "provider": "census",
    "precision": "street",
}


def _upstream(status, payload):
    """A stand-in for the tower-finder response, honest about raise_for_status."""
    import requests

    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


class TestGeocode:
    """Tests for POST /towers/geocode, the address lookup proxy."""

    @patch('routes.towers.http_requests.post')
    def test_geocode_passes_through_match(self, mock_post, app_client):
        """A match is handed to the page as the service spelled it."""
        mock_post.return_value = _upstream(200, SAMPLE_GEOCODE_RESPONSE)

        resp = app_client.post('/towers/geocode',
                               json={'query': '1600 Pennsylvania Ave NW'})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data['latitude'] == 38.898699
        assert data['longitude'] == -77.035188
        assert data['precision'] == 'street'
        assert data['matched_address'].startswith('1600 PENNSYLVANIA AVE NW')

    @patch('routes.towers.http_requests.post')
    def test_geocode_strips_before_forwarding(self, mock_post, app_client):
        """Padding is the caller's, not the geocoder's problem."""
        mock_post.return_value = _upstream(200, SAMPLE_GEOCODE_RESPONSE)

        app_client.post('/towers/geocode', json={'query': '  Seattle, WA  '})
        assert mock_post.call_args.kwargs['json'] == {'query': 'Seattle, WA'}

    @patch('routes.towers.http_requests.post')
    def test_geocode_rejects_empty_query_without_asking_upstream(self, mock_post, app_client):
        """Whitespace is empty, and empty never leaves the node."""
        resp = app_client.post('/towers/geocode', json={'query': '   '})
        assert resp.status_code == 400
        mock_post.assert_not_called()

    @patch('routes.towers.http_requests.post')
    def test_geocode_rejects_over_long_query_without_asking_upstream(self, mock_post, app_client):
        """Refused here with a sentence rather than upstream with a 422."""
        resp = app_client.post('/towers/geocode', json={'query': 'x' * 201})
        assert resp.status_code == 400
        assert 'too long' in json.loads(resp.data)['error'].lower()
        mock_post.assert_not_called()

    @patch('routes.towers.http_requests.post')
    def test_geocode_no_match_stays_404(self, mock_post, app_client):
        """"Nobody knows that address" must not arrive as "try again"."""
        mock_post.return_value = _upstream(404, {'detail': 'No match for that address'})

        resp = app_client.post('/towers/geocode', json={'query': 'qqqzzz'})
        assert resp.status_code == 404
        assert json.loads(resp.data)['error'] == 'No match for that address'

    @patch('routes.towers.http_requests.post')
    def test_geocode_unavailable_stays_503(self, mock_post, app_client):
        """A provider outage is retryable, and says so with its own status."""
        mock_post.return_value = _upstream(
            503, {'detail': 'Address lookup is unavailable right now'})

        resp = app_client.post('/towers/geocode', json={'query': 'Seattle, WA'})
        assert resp.status_code == 503
        assert 'unavailable' in json.loads(resp.data)['error'].lower()

    @patch('routes.towers.http_requests.post')
    def test_geocode_ignores_non_string_detail(self, mock_post, app_client):
        """FastAPI puts a list of field errors in `detail` for a 422-shaped
        failure. That is not a sentence to show an owner."""
        mock_post.return_value = _upstream(
            404, {'detail': [{'loc': ['body', 'query'], 'msg': 'too short'}]})

        resp = app_client.post('/towers/geocode', json={'query': 'x'})
        assert resp.status_code == 404
        assert json.loads(resp.data)['error'] == 'No match for that address'

    @patch('routes.towers.http_requests.post')
    def test_geocode_timeout(self, mock_post, app_client):
        """Returns 504 on timeout."""
        import requests
        mock_post.side_effect = requests.Timeout()

        resp = app_client.post('/towers/geocode', json={'query': 'Seattle, WA'})
        assert resp.status_code == 504
        assert 'timed out' in json.loads(resp.data)['error'].lower()

    @patch('routes.towers.http_requests.post')
    def test_geocode_unreachable(self, mock_post, app_client):
        """Returns 502 when the service cannot be reached at all."""
        import requests
        mock_post.side_effect = requests.ConnectionError()

        resp = app_client.post('/towers/geocode', json={'query': 'Seattle, WA'})
        assert resp.status_code == 502

    @patch('routes.towers.http_requests.post')
    def test_geocode_never_logs_the_address(self, mock_post, app_client, caplog):
        """An address typed into this box is the most personal thing the route
        handles. The upstream keeps it out of its logs; so must we."""
        import logging

        import requests
        mock_post.side_effect = requests.ConnectionError()

        secret = '221B Baker Street, Marylebone'
        with caplog.at_level(logging.DEBUG):
            app_client.post('/towers/geocode', json={'query': secret})
        assert secret not in caplog.text
        assert 'Baker Street' not in caplog.text


class TestElevation:
    """Tests for GET /towers/elevation, the altitude prefill proxy."""

    @patch('routes.towers.http_requests.get')
    def test_elevation_passes_through(self, mock_get, app_client):
        """The page reads elevation_m straight off this."""
        mock_get.return_value = _upstream(
            200, {'latitude': 38.8977, 'longitude': -77.0365, 'elevation_m': 20.0})

        resp = app_client.get('/towers/elevation?lat=38.8977&lon=-77.0365')
        assert resp.status_code == 200
        assert json.loads(resp.data)['elevation_m'] == 20.0

    @patch('routes.towers.http_requests.get')
    def test_elevation_requires_coordinates(self, mock_get, app_client):
        """Nothing to look up without both."""
        assert app_client.get('/towers/elevation?lat=38.8977').status_code == 400
        assert app_client.get('/towers/elevation').status_code == 400
        mock_get.assert_not_called()

    @patch('routes.towers.http_requests.get')
    def test_elevation_rejects_out_of_range(self, mock_get, app_client):
        """Refused here rather than spent on a 422 upstream."""
        assert app_client.get('/towers/elevation?lat=999&lon=0').status_code == 400
        assert app_client.get('/towers/elevation?lat=0&lon=999').status_code == 400
        mock_get.assert_not_called()

    @patch('routes.towers.http_requests.get')
    def test_elevation_rejects_non_numeric(self, mock_get, app_client):
        """Returns 400, not a 500, for a coordinate that is not a number."""
        assert app_client.get('/towers/elevation?lat=abc&lon=0').status_code == 400
        mock_get.assert_not_called()

    @patch('routes.towers.http_requests.get')
    def test_elevation_unreachable_is_502(self, mock_get, app_client):
        """The page swallows this; it must still be a clean answer."""
        import requests
        mock_get.side_effect = requests.ConnectionError()

        resp = app_client.get('/towers/elevation?lat=38.8977&lon=-77.0365')
        assert resp.status_code == 502


class TestSetupWizardAddressLookup:
    """The address row on the Location step, and the promises it must keep."""

    @staticmethod
    def _setup_js():
        with open(os.path.join(os.path.dirname(__file__), '..',
                               'static', 'setup.js')) as f:
            return f.read()

    @staticmethod
    def _location_panel(app_client):
        html = app_client.get('/set-up').data.decode()
        start = html.index('data-step="location"')
        return html[start:html.index('data-step="towers"')]

    def test_address_row_is_wired(self, app_client):
        """The three ids setup.js reaches for."""
        panel = self._location_panel(app_client)
        assert 'id="rxAddress"' in panel
        assert 'id="rxAddressBtn"' in panel
        assert 'id="rxAddressMsg"' in panel

    def test_address_sits_above_the_coordinates(self, app_client):
        """Reading order is the point: type an address, watch the coordinates
        appear below it. Reversed, the box looks like an afterthought to
        fields the owner has already filled."""
        panel = self._location_panel(app_client)
        assert panel.index('id="rxAddress"') < panel.index('id="rxLat"')

    def test_address_row_states_the_us_limit_and_the_way_round_it(self, app_client):
        """The geocoder is US-only, so a non-US address comes back as "no
        match" — which reads as a typo unless the box has already said
        otherwise. Naming the manual path in the same breath is what makes
        that an inconvenience rather than a dead end."""
        panel = self._location_panel(app_client)
        assert 'US addresses only' in panel
        assert 'coordinates directly' in panel

    def test_find_towers_gate_ignores_the_address(self, app_client):
        """The load-bearing promise: an owner can always type coordinates by
        hand. updateFindBtn must therefore read the coordinate boxes and
        nothing else, so that no failure of the geocoder — or of the service
        behind it — can ever hold the step shut."""
        js = self._setup_js()
        start = js.index('function updateFindBtn()')
        body = js[start:js.index('}', js.index('findBtn.disabled', start))]
        assert 'rxLat.value' in body
        assert 'rxLon.value' in body
        assert 'rxAddress' not in body
        assert 'addressInput' not in body

    def test_filling_coordinates_refreshes_the_gate(self, app_client):
        """Setting .value programmatically does not fire an input event, so
        the lookup has to poke updateFindBtn itself. Without this the owner
        gets coordinates and a Find Towers button that stays disabled."""
        js = self._setup_js()
        start = js.index('function lookupAddress()')
        body = js[start:js.index('function fetchElevation', start)]
        assert 'rxLat.value = Number(d.latitude)' in body
        assert 'updateFindBtn();' in body

    def test_lookup_does_not_write_into_an_abandoned_step(self, app_client):
        """The wizard runs forwards while a lookup is in flight. A late answer
        must not fill coordinates on a step the owner has already left."""
        js = self._setup_js()
        start = js.index('function lookupAddress()')
        body = js[start:js.index('function fetchElevation', start)]
        assert 'if (!locationActive) return;' in body

    def test_leaving_the_step_drops_in_flight_lookups(self, app_client):
        """Same reasoning as the SDR release below it: leaving means leaving."""
        js = self._setup_js()
        leave = js[js.index('leaveHooks.location = function()'):
                   js.index('enterHooks.location = function()')]
        assert 'abortAddressLookups' in leave

    def test_altitude_is_no_longer_the_owners_job(self, app_client):
        """Nothing used to fill this box, so rx_altitude reached the radar
        config as 0 for every owner who did not type a figure."""
        panel = self._location_panel(app_client)
        assert 'manually' not in panel.lower()
        js = self._setup_js()
        assert "rxAlt.value = Math.round(d.elevation_m)" in js

    def test_elevation_fills_the_manual_path_too(self, app_client):
        """Hanging elevation off a successful geocode alone would leave
        rx_altitude at 0 for anyone typing coordinates by hand, which is the
        path that always has to work."""
        js = self._setup_js()
        assert 'function scheduleElevation()' in js
        assert "rxLat.addEventListener('input', scheduleElevation);" in js
        assert "rxLon.addEventListener('input', scheduleElevation);" in js

    def test_typed_altitude_is_never_overwritten(self, app_client):
        """We may replace a figure we put there when the coordinates move. We
        must never replace one the owner typed."""
        js = self._setup_js()
        assert "rxAlt.addEventListener('input', function() { altAutoFilled = false; });" in js
        start = js.index('function fetchElevation(')
        body = js[start:js.index('function scheduleElevation', start)]
        assert "if (!force && rxAlt.value.trim() !== '' && !altAutoFilled) return;" in body

    def test_precision_below_street_is_warned_about(self, app_client):
        """A city-centre fix can sit 10 km from the real receiver, and these
        coordinates are written to the radar config, not just searched with."""
        js = self._setup_js()
        assert 'PRECISION_WARNINGS' in js
        assert 'Postcode centre only' in js
        assert 'City centre only' in js
