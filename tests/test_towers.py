"""Tests for tower finder proxy routes and tower selection."""
import json
import os
import sys
import yaml

import pytest
from unittest.mock import patch, MagicMock

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

    @patch('routes.towers.http_requests.post')
    def test_search_returns_towers(self, mock_post, app_client):
        """Proxy returns tower data from Tower-Finder API."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_TOWER_RESPONSE
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        resp = app_client.post('/towers/search', json={'lat': -33.8688, 'lon': 151.2093})
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data['count'] == 1
        assert data['towers'][0]['callsign'] == 'ATN6'

        mock_post.assert_called_once()
        assert '/api/towers' in mock_post.call_args[0][0]

    def test_search_missing_body(self, app_client):
        """Returns 400 when request body is missing."""
        resp = app_client.post('/towers/search', json={})
        assert resp.status_code == 400

    def test_search_missing_lon(self, app_client):
        """Returns 400 when lon missing."""
        resp = app_client.post('/towers/search', json={'lat': -33.8688})
        assert resp.status_code == 400

    @patch('routes.towers.http_requests.post')
    def test_search_timeout(self, mock_post, app_client):
        """Returns 504 on timeout."""
        import requests
        mock_post.side_effect = requests.Timeout()

        resp = app_client.post('/towers/search', json={'lat': -33.8688, 'lon': 151.2093})
        assert resp.status_code == 504
        data = json.loads(resp.data)
        assert 'timed out' in data['error'].lower()

    @patch('routes.towers.http_requests.post')
    def test_search_upstream_error(self, mock_post, app_client):
        """Returns 502 when Tower-Finder API is unreachable."""
        import requests
        mock_post.side_effect = requests.ConnectionError()

        resp = app_client.post('/towers/search', json={'lat': -33.8688, 'lon': 151.2093})
        assert resp.status_code == 502

    @patch('routes.towers.http_requests.post')
    def test_search_forwards_body(self, mock_post, app_client):
        """Forwards entire JSON body to Tower-Finder API."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"towers": [], "count": 0}
        mock_resp.raise_for_status.return_value = None
        mock_post.return_value = mock_resp

        body = {'lat': 38.9, 'lon': -77.0, 'altitude': 50, 'limit': 10, 'source': 'us', 'radius_km': 100}
        app_client.post('/towers/search', json=body)

        forwarded = mock_post.call_args[1]['json']
        assert forwarded['lat'] == 38.9
        assert forwarded['lon'] == -77.0
        assert forwarded['altitude'] == 50
        assert forwarded['source'] == 'us'

    @patch('routes.towers.http_requests.get')
    def test_search_caches_results(self, mock_get, app_client):
        """A successful search with towers populates the device_state cache."""
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
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data['success'] is True

        # Verify user.yml was updated
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
        assert resp.status_code == 200

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
        """Setup wizard has all 6 step panels."""
        resp = app_client.get('/set-up')
        html = resp.data.decode()
        assert 'data-step="location"' in html
        assert 'data-step="towers"' in html
        assert 'data-step="complete"' in html

    def test_setup_page_includes_leaflet(self, app_client):
        """Setup wizard loads Leaflet JS and CSS."""
        resp = app_client.get('/set-up')
        html = resp.data.decode()
        assert 'leaflet.css' in html
        assert 'leaflet.js' in html
