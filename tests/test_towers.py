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
    """Tests for GET /towers/search proxy route."""

    @patch('routes.towers.http_requests.get')
    def test_search_returns_towers(self, mock_get, app_client):
        """Proxy returns tower data from Tower-Finder API."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_TOWER_RESPONSE
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        resp = app_client.get('/towers/search?lat=-33.8688&lon=151.2093')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data['count'] == 1
        assert data['towers'][0]['callsign'] == 'ATN6'

        # Verify we called the Tower-Finder API
        mock_get.assert_called_once()
        call_args = mock_get.call_args
        assert '/api/towers' in call_args[0][0]

    def test_search_missing_params(self, app_client):
        """Returns 400 when lat/lon missing."""
        resp = app_client.get('/towers/search')
        assert resp.status_code == 400
        data = json.loads(resp.data)
        assert 'error' in data

    def test_search_missing_lon(self, app_client):
        """Returns 400 when lon missing."""
        resp = app_client.get('/towers/search?lat=-33.8688')
        assert resp.status_code == 400

    @patch('routes.towers.http_requests.get')
    def test_search_timeout(self, mock_get, app_client):
        """Returns 504 on timeout."""
        import requests
        mock_get.side_effect = requests.Timeout()

        resp = app_client.get('/towers/search?lat=-33.8688&lon=151.2093')
        assert resp.status_code == 504
        data = json.loads(resp.data)
        assert 'timed out' in data['error'].lower()

    @patch('routes.towers.http_requests.get')
    def test_search_upstream_error(self, mock_get, app_client):
        """Returns 502 when Tower-Finder API is unreachable."""
        import requests
        mock_get.side_effect = requests.ConnectionError()

        resp = app_client.get('/towers/search?lat=-33.8688&lon=151.2093')
        assert resp.status_code == 502

    @patch('routes.towers.http_requests.get')
    def test_search_forwards_params(self, mock_get, app_client):
        """Forwards all query params to Tower-Finder API."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"towers": [], "count": 0}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        app_client.get('/towers/search?lat=38.9&lon=-77.0&altitude=50&limit=10&source=us&radius_km=100')

        call_kwargs = mock_get.call_args
        params = call_kwargs[1]['params']
        assert params['lat'] == '38.9'
        assert params['lon'] == '-77.0'
        assert params['altitude'] == '50'
        assert params['limit'] == '10'
        assert params['source'] == 'us'
        assert params['radius_km'] == '100'


class TestTowerElevation:
    """Tests for GET /towers/elevation proxy route."""

    @patch('routes.towers.http_requests.get')
    def test_elevation_returns_data(self, mock_get, app_client):
        """Returns elevation from Tower-Finder API."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"elevation_m": 45.2, "latitude": -33.8688, "longitude": 151.2093}
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        resp = app_client.get('/towers/elevation?lat=-33.8688&lon=151.2093')
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data['elevation_m'] == 45.2

    def test_elevation_missing_params(self, app_client):
        """Returns 400 when lat/lon missing."""
        resp = app_client.get('/towers/elevation')
        assert resp.status_code == 400

    @patch('routes.towers.http_requests.get')
    def test_elevation_upstream_error(self, mock_get, app_client):
        """Returns 502 when elevation API fails."""
        import requests
        mock_get.side_effect = requests.ConnectionError()

        resp = app_client.get('/towers/elevation?lat=-33.8688&lon=151.2093')
        assert resp.status_code == 502


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
        assert 'data-step="location"' in html
        assert 'Find Towers' in html

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
