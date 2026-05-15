"""Tests for the mode (Radar / Spectrum) toggle endpoint and home page rendering."""
import json
import os
import subprocess
import pytest
from unittest.mock import patch, MagicMock, call


@pytest.fixture(autouse=True)
def reset_mode_cache():
    """Reset the in-memory mode cache before every test.

    _mode_cache is a module-level variable in routes.mode that survives
    importlib.reload(app) because Python's module cache doesn't re-execute
    already-imported submodules. Without this reset, a test that switches to
    'spectrum' would pollute the next test's default mode.
    """
    import sys
    if 'routes.mode' in sys.modules:
        sys.modules['routes.mode']._mode_cache = 'radar'
    yield


class TestGetMode:

    def test_default_mode_is_radar(self, app_client):
        response = app_client.get('/api/mode')
        assert response.status_code == 200
        assert json.loads(response.data) == {'mode': 'radar'}

    def test_returns_persisted_mode(self, app_client, temp_dir):
        with open(os.path.join(temp_dir, 'mode.txt'), 'w') as f:
            f.write('spectrum')
        response = app_client.get('/api/mode')
        assert json.loads(response.data) == {'mode': 'spectrum'}

    def test_corrupted_mode_file_falls_back_to_radar(self, app_client, temp_dir):
        with open(os.path.join(temp_dir, 'mode.txt'), 'w') as f:
            f.write('garbage')
        response = app_client.get('/api/mode')
        assert json.loads(response.data) == {'mode': 'radar'}


class TestSetMode:

    def test_invalid_mode_returns_400(self, app_client):
        response = app_client.post('/api/mode',
                                   data=json.dumps({'mode': 'invalid'}),
                                   content_type='application/json')
        assert response.status_code == 400
        assert json.loads(response.data)['success'] is False

    def test_missing_mode_returns_400(self, app_client):
        response = app_client.post('/api/mode',
                                   data=json.dumps({}),
                                   content_type='application/json')
        assert response.status_code == 400

    def test_no_retina_node_still_succeeds(self, app_client_no_retina):
        """Mode switch should succeed (skipping docker) when retina-node is absent."""
        response = app_client_no_retina.post('/api/mode',
                                             data=json.dumps({'mode': 'spectrum'}),
                                             content_type='application/json')
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True
        assert data['mode'] == 'spectrum'

    def test_no_retina_node_persists_mode_in_cache(self, app_client_no_retina):
        """After a no-docker switch the GET endpoint reflects the new mode."""
        app_client_no_retina.post('/api/mode',
                                  data=json.dumps({'mode': 'spectrum'}),
                                  content_type='application/json')
        response = app_client_no_retina.get('/api/mode')
        assert json.loads(response.data)['mode'] == 'spectrum'

    @patch('subprocess.run')
    def test_switch_to_spectrum_calls_correct_docker_commands(self, mock_run, app_client):
        mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')

        response = app_client.post('/api/mode',
                                   data=json.dumps({'mode': 'spectrum'}),
                                   content_type='application/json')

        assert response.status_code == 200
        assert json.loads(response.data)['success'] is True
        assert mock_run.call_count == 2

        stop_args = mock_run.call_args_list[0][0][0]
        assert stop_args[:4] == ['docker', 'compose', '-p', 'retina-node']
        assert 'stop' in stop_args
        for svc in ('blah2', 'blah2_api', 'blah2_web', 'blah2_host'):
            assert svc in stop_args

        up_args = mock_run.call_args_list[1][0][0]
        assert up_args[:4] == ['docker', 'compose', '-p', 'retina-spectrum']
        assert 'up' in up_args
        assert '-d' in up_args

    @patch('subprocess.run')
    def test_switch_to_radar_calls_correct_docker_commands(self, mock_run, app_client, temp_dir):
        # Pre-set mode to spectrum
        with open(os.path.join(temp_dir, 'mode.txt'), 'w') as f:
            f.write('spectrum')

        mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')

        response = app_client.post('/api/mode',
                                   data=json.dumps({'mode': 'radar'}),
                                   content_type='application/json')

        assert response.status_code == 200
        assert json.loads(response.data)['success'] is True
        assert mock_run.call_count == 2

        down_args = mock_run.call_args_list[0][0][0]
        assert down_args[:4] == ['docker', 'compose', '-p', 'retina-spectrum']
        assert 'down' in down_args

        start_args = mock_run.call_args_list[1][0][0]
        assert start_args[:4] == ['docker', 'compose', '-p', 'retina-node']
        assert 'start' in start_args
        for svc in ('blah2', 'blah2_api', 'blah2_web', 'blah2_host'):
            assert svc in start_args

    @patch('subprocess.run')
    def test_switch_to_spectrum_writes_mode_file(self, mock_run, app_client, temp_dir):
        mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')

        app_client.post('/api/mode',
                        data=json.dumps({'mode': 'spectrum'}),
                        content_type='application/json')

        with open(os.path.join(temp_dir, 'mode.txt')) as f:
            assert f.read().strip() == 'spectrum'

    @patch('subprocess.run')
    def test_docker_stop_failure_returns_500(self, mock_run, app_client):
        mock_run.return_value = MagicMock(returncode=1, stdout='', stderr='permission denied')

        response = app_client.post('/api/mode',
                                   data=json.dumps({'mode': 'spectrum'}),
                                   content_type='application/json')

        assert response.status_code == 500
        data = json.loads(response.data)
        assert data['success'] is False
        assert 'blah2' in data['error']

    @patch('subprocess.run')
    def test_docker_up_failure_returns_500(self, mock_run, app_client):
        # Stop succeeds, up fails
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout='', stderr=''),
            MagicMock(returncode=1, stdout='', stderr='image not found'),
        ]

        response = app_client.post('/api/mode',
                                   data=json.dumps({'mode': 'spectrum'}),
                                   content_type='application/json')

        assert response.status_code == 500
        data = json.loads(response.data)
        assert data['success'] is False
        assert 'retina-spectrum' in data['error']

    @patch('subprocess.run')
    def test_timeout_returns_500(self, mock_run, app_client):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd='docker', timeout=60)

        response = app_client.post('/api/mode',
                                   data=json.dumps({'mode': 'spectrum'}),
                                   content_type='application/json')

        assert response.status_code == 500
        data = json.loads(response.data)
        assert data['success'] is False
        assert 'timed out' in data['error'].lower()

    @patch('subprocess.run')
    def test_mode_file_not_written_on_failure(self, mock_run, app_client, temp_dir):
        """Mode file must not be updated when docker commands fail."""
        mock_run.return_value = MagicMock(returncode=1, stdout='', stderr='error')

        app_client.post('/api/mode',
                        data=json.dumps({'mode': 'spectrum'}),
                        content_type='application/json')

        mode_file = os.path.join(temp_dir, 'mode.txt')
        assert not os.path.exists(mode_file)


class TestHomepageModeRendering:

    def test_radar_mode_shows_services_section(self, app_client):
        response = app_client.get('/')
        assert response.status_code == 200
        assert b'Services' in response.data
        assert b'spectrumFrame' not in response.data

    @patch('subprocess.run')
    def test_spectrum_mode_shows_iframe(self, mock_run, app_client, temp_dir):
        with open(os.path.join(temp_dir, 'mode.txt'), 'w') as f:
            f.write('spectrum')

        response = app_client.get('/')
        assert response.status_code == 200
        assert b'spectrumFrame' in response.data
        assert b'Services' not in response.data

    def test_spectrum_mode_hides_passive_radar_card(self, app_client, temp_dir):
        with open(os.path.join(temp_dir, 'mode.txt'), 'w') as f:
            f.write('spectrum')

        response = app_client.get('/')
        assert b'Passive Radar' not in response.data
        assert b'49152' not in response.data
