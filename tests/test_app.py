"""Tests for Flask app routes."""
import os
import json
import pytest
import yaml
from unittest.mock import patch, MagicMock


class TestIndexRoute:
    """Test the home page (/)."""

    def test_index_loads(self, app_client):
        """Index page should load successfully."""
        response = app_client.get('/')
        assert response.status_code == 200
        assert b'Retina Node' in response.data

    def test_index_shows_node_id(self, app_client):
        """Index should display node ID from config."""
        response = app_client.get('/')
        assert response.status_code == 200
        assert b'ret7dd2cb0d' in response.data

    def test_index_shows_node_id_unknown(self, app_client_no_node_id):
        """Index should show 'Unknown' when node_id is missing."""
        response = app_client_no_node_id.get('/')
        assert response.status_code == 200
        assert b'Unknown' in response.data

    def test_index_links_to_config(self, app_client):
        """Index should have link to /config page."""
        response = app_client.get('/')
        assert response.status_code == 200
        assert b'href="/config"' in response.data

    def test_services_section(self, app_client):
        """Index should show services links."""
        response = app_client.get('/')
        assert response.status_code == 200
        assert b'Services' in response.data
        assert b'blah2' in response.data
        assert b'tar1090' in response.data

    def test_ssh_keys_section(self, app_client):
        """Index should show SSH keys section."""
        response = app_client.get('/')
        assert response.status_code == 200
        assert b'SSH Access' in response.data
        assert b'Add Key' in response.data


class TestConfigPageRoute:
    """Test the config page (/config)."""

    def test_config_page_loads(self, app_client):
        """Config page should load with all sections."""
        response = app_client.get('/config')
        assert response.status_code == 200
        assert b'Capture Settings' in response.data
        assert b'Location Settings' in response.data
        assert b'ADS-B Truth' in response.data
        assert b'tar1090' in response.data

    def test_config_shows_capture_values(self, app_client):
        """Capture values from user.yml should appear."""
        response = app_client.get('/config')
        assert response.status_code == 200
        assert b'4000000' in response.data  # fs value
        assert b'503000000' in response.data  # fc value
        assert b'RspDuo' in response.data  # device type

    def test_config_shows_location_values(self, app_client):
        """Location values from user.yml should appear."""
        response = app_client.get('/config')
        assert response.status_code == 200
        assert b'37.7644' in response.data  # rx latitude
        assert b'150 Mississippi' in response.data  # rx name

    def test_config_shows_truth_values(self, app_client):
        """Truth/ADS-B values from user.yml should appear."""
        response = app_client.get('/config')
        assert response.status_code == 200
        assert b'sfo1.retnode.com' in response.data  # tar1090 server
        assert b'localhost:49155' in response.data  # adsb2dd

    def test_config_shows_tar1090_values_split(self, app_client):
        """tar1090 adsb_source should be split into 3 fields."""
        response = app_client.get('/config')
        assert response.status_code == 200
        assert b'192.168.8.183' in response.data  # host
        assert b'30005' in response.data  # port
        assert b'beast_in' in response.data  # protocol

    def test_config_no_retina_node(self, app_client_no_retina):
        """Should show message when retina-node not installed."""
        response = app_client_no_retina.get('/config')
        assert response.status_code == 200
        assert b'Configuration available after retina-node is deployed' in response.data
        # Should NOT show the Apply button
        assert b'Apply Changes' not in response.data

    def test_config_shows_apply_button(self, app_client):
        """Config page should have Apply Changes button when retina-node installed."""
        response = app_client.get('/config')
        assert response.status_code == 200
        assert b'Apply Changes' in response.data


class TestConfigSaveRoute:
    """Test the /config/save POST route."""

    def test_save_valid_config(self, app_client, user_config_file):
        """Valid config should be saved to user.yml."""
        response = app_client.post('/config/save', data={
            'capture.fs': '5000000',
            'capture.fc': '500000000',
            'capture.device.type': 'RspDuo',
            'capture.device.agcSetPoint': '-40',
            'capture.device.gainReduction': '35',
            'capture.device.lnaState': '5',
            'capture.device.dabNotch': 'on',
            'capture.device.rfNotch': 'on',
            'capture.device.bandwidthNumber': '1'
        }, follow_redirects=False)

        # Should redirect on success
        assert response.status_code == 302

        # Check file was updated
        with open(user_config_file) as f:
            saved = yaml.safe_load(f)
        assert saved['capture']['fs'] == 5000000
        assert saved['capture']['fc'] == 500000000
        assert saved['capture']['device']['gainReduction'] == 35
        assert saved['capture']['device']['lnaState'] == 5

    def test_save_unchecked_checkbox(self, app_client, user_config_file):
        """Unchecked checkboxes should be saved as False."""
        response = app_client.post('/config/save', data={
            'capture.fs': '4000000',
            'capture.fc': '503000000',
            'capture.device.type': 'RspDuo',
            'capture.device.agcSetPoint': '-50',
            'capture.device.gainReduction': '40',
            'capture.device.lnaState': '4',
            # dabNotch and rfNotch NOT included (unchecked)
            'capture.device.bandwidthNumber': '0'
        }, follow_redirects=False)

        assert response.status_code == 302

        with open(user_config_file) as f:
            saved = yaml.safe_load(f)
        assert saved['capture']['device']['dabNotch'] is False
        assert saved['capture']['device']['rfNotch'] is False

    def test_save_invalid_gain_reduction(self, app_client):
        """Invalid gain reduction should show validation error."""
        response = app_client.post('/config/save', data={
            'capture.fs': '4000000',
            'capture.fc': '503000000',
            'capture.device.type': 'RspDuo',
            'capture.device.agcSetPoint': '-50',
            'capture.device.gainReduction': '100',  # Invalid: > 59
            'capture.device.lnaState': '4',
            'capture.device.dabNotch': 'on',
            'capture.device.rfNotch': 'on',
            'capture.device.bandwidthNumber': '0'
        })

        # Should return 200 with form (not redirect)
        assert response.status_code == 200
        assert b'is-invalid' in response.data
        assert b'less than or equal to 59' in response.data

    def test_save_invalid_lna_state(self, app_client):
        """Invalid LNA state should show validation error."""
        response = app_client.post('/config/save', data={
            'capture.fs': '4000000',
            'capture.fc': '503000000',
            'capture.device.type': 'RspDuo',
            'capture.device.agcSetPoint': '-50',
            'capture.device.gainReduction': '40',
            'capture.device.lnaState': '0',  # Invalid: < 1
            'capture.device.dabNotch': 'on',
            'capture.device.rfNotch': 'on',
            'capture.device.bandwidthNumber': '0'
        })

        assert response.status_code == 200
        assert b'is-invalid' in response.data
        assert b'greater than or equal to 1' in response.data

    def test_save_invalid_agc_set_point(self, app_client):
        """Positive AGC set point should show validation error."""
        response = app_client.post('/config/save', data={
            'capture.fs': '4000000',
            'capture.fc': '503000000',
            'capture.device.type': 'RspDuo',
            'capture.device.agcSetPoint': '10',  # Invalid: > 0
            'capture.device.gainReduction': '40',
            'capture.device.lnaState': '4',
            'capture.device.dabNotch': 'on',
            'capture.device.rfNotch': 'on',
            'capture.device.bandwidthNumber': '0'
        })

        assert response.status_code == 200
        assert b'is-invalid' in response.data

    def test_save_preserves_other_sections(self, app_client, user_config_file):
        """Saving capture should preserve other config sections."""
        # Add another section to the config
        with open(user_config_file) as f:
            config = yaml.safe_load(f)
        config['other_section'] = {'foo': 'bar'}
        with open(user_config_file, 'w') as f:
            yaml.dump(config, f)

        # Save capture config
        response = app_client.post('/config/save', data={
            'capture.fs': '6000000',
            'capture.fc': '503000000',
            'capture.device.type': 'RspDuo',
            'capture.device.agcSetPoint': '-50',
            'capture.device.gainReduction': '40',
            'capture.device.lnaState': '4',
            'capture.device.dabNotch': 'on',
            'capture.device.rfNotch': 'on',
            'capture.device.bandwidthNumber': '0'
        }, follow_redirects=False)

        assert response.status_code == 302

        with open(user_config_file) as f:
            saved = yaml.safe_load(f)
        # Other section should still exist
        assert saved.get('other_section') == {'foo': 'bar'}
        # Capture should be updated
        assert saved['capture']['fs'] == 6000000


class TestLocationSave:
    """Test saving location config."""

    def test_save_valid_location(self, app_client, user_config_file):
        """Valid location should save."""
        response = app_client.post('/config/save', data={
            'location.rx.latitude': '40.7128',
            'location.rx.longitude': '-74.0060',
            'location.rx.altitude': '10',
            'location.rx.name': 'NYC',
            'location.tx.latitude': '40.0',
            'location.tx.longitude': '-74.0',
            'location.tx.altitude': '100',
            'location.tx.name': 'Transmitter',
        }, follow_redirects=False)
        assert response.status_code == 302

        with open(user_config_file) as f:
            saved = yaml.safe_load(f)
        assert saved['location']['rx']['latitude'] == 40.7128
        assert saved['location']['rx']['name'] == 'NYC'

    def test_save_invalid_latitude(self, app_client):
        """Invalid latitude should show error."""
        response = app_client.post('/config/save', data={
            'location.rx.latitude': '100',  # Invalid > 90
            'location.rx.longitude': '-74.0',
            'location.rx.altitude': '10',
            'location.rx.name': 'Test',
            'location.tx.latitude': '40.0',
            'location.tx.longitude': '-74.0',
            'location.tx.altitude': '100',
            'location.tx.name': 'Transmitter',
        })
        assert response.status_code == 200
        assert b'is-invalid' in response.data

    def test_save_invalid_longitude(self, app_client):
        """Invalid longitude should show error."""
        response = app_client.post('/config/save', data={
            'location.rx.latitude': '40.0',
            'location.rx.longitude': '200',  # Invalid > 180
            'location.rx.altitude': '10',
            'location.rx.name': 'Test',
            'location.tx.latitude': '40.0',
            'location.tx.longitude': '-74.0',
            'location.tx.altitude': '100',
            'location.tx.name': 'Transmitter',
        })
        assert response.status_code == 200
        assert b'is-invalid' in response.data


class TestTar1090Save:
    """Test saving tar1090 config with adsb_source join."""

    def test_adsb_source_joined_on_save(self, app_client, user_config_file):
        """3 adsb_source fields should be joined to comma-separated string."""
        response = app_client.post('/config/save', data={
            'tar1090.adsb_source_host': '10.0.0.1',
            'tar1090.adsb_source_port': '30006',
            'tar1090.adsb_source_protocol': 'raw_in',
            'tar1090.adsblol_fallback': 'on',
            'tar1090.adsblol_radius': '50',
        }, follow_redirects=False)

        assert response.status_code == 302

        with open(user_config_file) as f:
            saved = yaml.safe_load(f)
        assert saved['tar1090']['adsb_source'] == '10.0.0.1,30006,raw_in'
        assert saved['tar1090']['adsblol_fallback'] is True
        assert saved['tar1090']['adsblol_radius'] == 50

    def test_invalid_port(self, app_client):
        """Port > 65535 should show error."""
        response = app_client.post('/config/save', data={
            'tar1090.adsb_source_host': '10.0.0.1',
            'tar1090.adsb_source_port': '70000',  # Invalid
            'tar1090.adsb_source_protocol': 'raw_in',
            'tar1090.adsblol_fallback': 'on',
            'tar1090.adsblol_radius': '50',
        })
        assert response.status_code == 200
        assert b'is-invalid' in response.data

    def test_invalid_radius(self, app_client):
        """Radius > 500 should show error."""
        response = app_client.post('/config/save', data={
            'tar1090.adsb_source_host': '10.0.0.1',
            'tar1090.adsb_source_port': '30005',
            'tar1090.adsb_source_protocol': 'raw_in',
            'tar1090.adsblol_fallback': 'on',
            'tar1090.adsblol_radius': '600',  # Invalid > 500
        })
        assert response.status_code == 200
        assert b'is-invalid' in response.data


class TestTruthSave:
    """Test saving truth.adsb config."""

    def test_save_valid_truth_config(self, app_client, user_config_file):
        """Valid truth config should save."""
        response = app_client.post('/config/save', data={
            'truth.adsb.enabled': 'on',
            'truth.adsb.tar1090': 'server.example.com',
            'truth.adsb.adsb2dd': 'localhost:49155',
            'truth.adsb.delay_tolerance': '3.0',
            'truth.adsb.doppler_tolerance': '6.0',
        }, follow_redirects=False)

        assert response.status_code == 302

        with open(user_config_file) as f:
            saved = yaml.safe_load(f)
        assert saved['truth']['adsb']['enabled'] is True
        assert saved['truth']['adsb']['tar1090'] == 'server.example.com'
        assert saved['truth']['adsb']['delay_tolerance'] == 3.0
        assert saved['truth']['adsb']['doppler_tolerance'] == 6.0

    def test_invalid_delay_tolerance(self, app_client):
        """Delay tolerance <= 0 should show error."""
        response = app_client.post('/config/save', data={
            'truth.adsb.enabled': 'on',
            'truth.adsb.tar1090': 'server.example.com',
            'truth.adsb.adsb2dd': 'localhost:49155',
            'truth.adsb.delay_tolerance': '0',  # Invalid: must be > 0
            'truth.adsb.doppler_tolerance': '5.0',
        })
        assert response.status_code == 200
        assert b'is-invalid' in response.data

    def test_invalid_doppler_tolerance(self, app_client):
        """Doppler tolerance <= 0 should show error."""
        response = app_client.post('/config/save', data={
            'truth.adsb.enabled': 'on',
            'truth.adsb.tar1090': 'server.example.com',
            'truth.adsb.adsb2dd': 'localhost:49155',
            'truth.adsb.delay_tolerance': '2.0',
            'truth.adsb.doppler_tolerance': '-1',  # Invalid: must be > 0
        })
        assert response.status_code == 200
        assert b'is-invalid' in response.data


class TestApplyConfigRoute:
    """Test the /config/apply POST route."""

    def test_apply_not_installed(self, app_client_no_retina):
        """Apply should fail when retina-node not installed."""
        response = app_client_no_retina.post('/config/apply')
        assert response.status_code == 400
        data = json.loads(response.data)
        assert data['success'] is False
        assert 'not installed' in data['error']

    @patch('subprocess.run')
    def test_apply_success(self, mock_run, app_client):
        """Apply should run docker commands on success."""
        mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')

        response = app_client.post('/config/apply')
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True

        # Should have called docker compose twice
        assert mock_run.call_count == 2

        # Check first call (config-merger)
        first_call = mock_run.call_args_list[0]
        assert 'config-merger' in first_call[0][0]

        # Check second call (up -d --force-recreate)
        second_call = mock_run.call_args_list[1]
        assert 'up' in second_call[0][0]
        assert '--force-recreate' in second_call[0][0]

    @patch('subprocess.run')
    def test_apply_config_merger_fails(self, mock_run, app_client):
        """Apply should return error if config-merger fails."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout='',
            stderr='config-merger error message'
        )

        response = app_client.post('/config/apply')
        assert response.status_code == 500
        data = json.loads(response.data)
        assert data['success'] is False
        assert 'config-merger failed' in data['error']

    @patch('subprocess.run')
    def test_apply_restart_fails(self, mock_run, app_client):
        """Apply should return error if restart fails."""
        # First call succeeds, second fails
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout='', stderr=''),
            MagicMock(returncode=1, stdout='', stderr='restart error')
        ]

        response = app_client.post('/config/apply')
        assert response.status_code == 500
        data = json.loads(response.data)
        assert data['success'] is False
        assert 'restart failed' in data['error']

    @patch('subprocess.run')
    def test_apply_timeout(self, mock_run, app_client):
        """Apply should handle timeout gracefully."""
        import subprocess
        mock_run.side_effect = subprocess.TimeoutExpired(cmd='docker', timeout=60)

        response = app_client.post('/config/apply')
        assert response.status_code == 500
        data = json.loads(response.data)
        assert data['success'] is False
        assert 'timed out' in data['error'].lower()


class TestSSHKeysRoutes:
    """Test SSH key management routes."""

    def test_add_valid_ssh_key(self, app_client, temp_dir):
        """Valid SSH key should be added."""
        valid_key = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl test@example.com'

        response = app_client.post('/ssh-keys', data={
            'ssh_key': valid_key
        }, follow_redirects=False)

        assert response.status_code == 302

        # Check key was written
        auth_keys_file = os.path.join(temp_dir, 'authorized_keys')
        with open(auth_keys_file) as f:
            content = f.read()
        assert valid_key in content

    def test_add_invalid_ssh_key(self, app_client):
        """Invalid SSH key should show error."""
        response = app_client.post('/ssh-keys', data={
            'ssh_key': 'not-a-valid-key'
        })

        assert response.status_code == 200
        assert b'Invalid SSH key format' in response.data

    def test_add_ssh_key_with_shell_chars(self, app_client):
        """SSH key with shell metacharacters should be rejected."""
        malicious_key = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 test; rm -rf /'

        response = app_client.post('/ssh-keys', data={
            'ssh_key': malicious_key
        })

        assert response.status_code == 200
        assert b'Invalid SSH key format' in response.data

    def test_delete_ssh_key(self, app_client, temp_dir):
        """SSH key should be deletable."""
        # First add a key
        valid_key = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl test@example.com'
        app_client.post('/ssh-keys', data={'ssh_key': valid_key})

        # Then delete it
        response = app_client.post('/ssh-keys/delete', data={
            'ssh_key': valid_key
        }, follow_redirects=False)

        assert response.status_code == 302

        # Check key was removed
        auth_keys_file = os.path.join(temp_dir, 'authorized_keys')
        with open(auth_keys_file) as f:
            content = f.read()
        assert valid_key not in content


class TestParseFormToNestedDict:
    """Test form data parsing."""

    def test_parse_nested_structure(self):
        """Dot notation should become nested dict."""
        import importlib
        import app as app_module
        importlib.reload(app_module)

        result = app_module.parse_form_to_nested_dict({
            'capture.fs': '4000000',
            'capture.fc': '503000000',
            'capture.device.type': 'RspDuo',
            'capture.device.gainReduction': '40'
        })

        assert result == {
            'capture': {
                'fs': 4000000,
                'fc': 503000000,
                'device': {
                    'type': 'RspDuo',
                    'gainReduction': 40
                }
            }
        }

    def test_parse_integer_conversion(self):
        """String integers should be converted to int."""
        import importlib
        import app as app_module
        importlib.reload(app_module)

        result = app_module.parse_form_to_nested_dict({
            'value': '12345'
        })
        assert result['value'] == 12345
        assert isinstance(result['value'], int)

    def test_parse_float_conversion(self):
        """String floats should be converted to float."""
        import importlib
        import app as app_module
        importlib.reload(app_module)

        result = app_module.parse_form_to_nested_dict({
            'latitude': '37.7644',
            'tolerance': '2.5'
        })
        assert result['latitude'] == 37.7644
        assert isinstance(result['latitude'], float)
        assert result['tolerance'] == 2.5

    def test_parse_negative_integer(self):
        """Negative integers should be parsed correctly."""
        import importlib
        import app as app_module
        importlib.reload(app_module)

        result = app_module.parse_form_to_nested_dict({
            'agc': '-50'
        })
        assert result['agc'] == -50

    def test_parse_negative_float(self):
        """Negative floats should be parsed correctly."""
        import importlib
        import app as app_module
        importlib.reload(app_module)

        result = app_module.parse_form_to_nested_dict({
            'longitude': '-122.3954'
        })
        assert result['longitude'] == -122.3954

    def test_parse_boolean_true(self):
        """Boolean true values should be converted."""
        import importlib
        import app as app_module
        importlib.reload(app_module)

        result = app_module.parse_form_to_nested_dict({
            'enabled1': 'true',
            'enabled2': 'on',
            'enabled3': 'True'
        })
        assert result['enabled1'] is True
        assert result['enabled2'] is True
        assert result['enabled3'] is True

    def test_parse_boolean_false(self):
        """Boolean false values should be converted."""
        import importlib
        import app as app_module
        importlib.reload(app_module)

        result = app_module.parse_form_to_nested_dict({
            'disabled': 'false'
        })
        assert result['disabled'] is False

    def test_parse_empty_string_skipped(self):
        """Empty strings should be skipped."""
        import importlib
        import app as app_module
        importlib.reload(app_module)

        result = app_module.parse_form_to_nested_dict({
            'present': '123',
            'empty': ''
        })
        assert 'present' in result
        assert 'empty' not in result

    def test_parse_string_preserved(self):
        """Non-numeric strings should stay as strings."""
        import importlib
        import app as app_module
        importlib.reload(app_module)

        result = app_module.parse_form_to_nested_dict({
            'name': 'RspDuo'
        })
        assert result['name'] == 'RspDuo'
        assert isinstance(result['name'], str)
