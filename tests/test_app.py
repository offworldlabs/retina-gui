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

    def test_index_shows_version_labels(self, app_client):
        """Index should show owl-os and retina-node version labels."""
        response = app_client.get('/')
        assert response.status_code == 200
        assert b'owl-os:' in response.data
        assert b'retina-node:' in response.data


class TestMenderVersions:
    """Test MenderClient.get_versions() method."""

    def test_versions_parsed_correctly(self):
        """Should parse owl-os and retina-node versions from mender output."""
        from mender import MenderClient
        mock_output = """rootfs-image.version=v0.5.0
rootfs-image.owl-os-pi5.version=v0.5.0
rootfs-image.retina-node.version=v0.3.2
artifact_name=owl-os-pi5-v0.5.0"""

        client = MenderClient()
        with patch('mender.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=mock_output)
            owl_os, retina_node = client.get_versions()

        assert owl_os == 'v0.5.0'
        assert retina_node == 'v0.3.2'

    def test_retina_node_not_installed(self):
        """Should return None for retina-node if not in mender output."""
        from mender import MenderClient
        mock_output = """rootfs-image.version=v0.5.0
rootfs-image.owl-os-pi5.version=v0.5.0
artifact_name=owl-os-pi5-v0.5.0"""

        client = MenderClient()
        with patch('mender.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=mock_output)
            owl_os, retina_node = client.get_versions()

        assert owl_os == 'v0.5.0'
        assert retina_node is None

    def test_mender_not_available(self):
        """Should return None, None if mender-update not found."""
        from mender import MenderClient

        client = MenderClient()
        with patch('mender.subprocess.run') as mock_run:
            mock_run.side_effect = FileNotFoundError()
            owl_os, retina_node = client.get_versions()

        assert owl_os is None
        assert retina_node is None

    def test_mender_command_fails(self):
        """Should return None, None if mender-update returns error."""
        from mender import MenderClient

        client = MenderClient()
        with patch('mender.subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout='', stderr='error')
            owl_os, retina_node = client.get_versions()

        assert owl_os is None
        assert retina_node is None


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
        """Should show message when retina-node not installed, but cloud services visible."""
        response = app_client_no_retina.get('/config')
        assert response.status_code == 200
        assert b'Configuration available after retina-node is deployed' in response.data
        # Should NOT show the Apply button (config form is hidden)
        assert b'Apply Changes' not in response.data
        # Cloud Services should still be visible
        assert b'Cloud Services' in response.data
        assert b'cloudServicesToggle' in response.data

    def test_config_shows_apply_button(self, app_client):
        """Config page should have Apply Changes button when retina-node installed."""
        response = app_client.get('/config')
        assert response.status_code == 200
        assert b'Apply Changes' in response.data

    def test_cloud_services_toggle_not_hardcoded_checked(self, app_client):
        """Cloud services toggle should not hardcode checked attribute."""
        response = app_client.get('/config')
        assert response.status_code == 200
        assert b'id="cloudServicesToggle" checked' not in response.data


class TestConfigSaveRoute:
    """Test the /config/save POST route.

    Note: The form uses flat field names like 'capture.device_type' not nested
    'capture.device.type' because we use flat Pydantic schemas for validation.
    """

    def test_save_valid_config(self, app_client, user_config_file):
        """Valid config should be saved to user.yml."""
        response = app_client.post('/config/save', data={
            'capture.fs': '5000000',
            'capture.fc': '500000000',
            'capture.device_type': 'RspDuo',
            'capture.device_agcSetPoint': '-40',
            'capture.device_gainReduction': '35',
            'capture.device_lnaState': '5',
            'capture.device_dabNotch': 'on',
            'capture.device_rfNotch': 'on',
            'capture.device_bandwidthNumber': '1'
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
            'capture.device_type': 'RspDuo',
            'capture.device_agcSetPoint': '-50',
            'capture.device_gainReduction': '40',
            'capture.device_lnaState': '4',
            # dabNotch and rfNotch NOT included (unchecked)
            'capture.device_bandwidthNumber': '0'
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
            'capture.device_type': 'RspDuo',
            'capture.device_agcSetPoint': '-50',
            'capture.device_gainReduction': '100',  # Invalid: > 59
            'capture.device_lnaState': '4',
            'capture.device_dabNotch': 'on',
            'capture.device_rfNotch': 'on',
            'capture.device_bandwidthNumber': '0'
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
            'capture.device_type': 'RspDuo',
            'capture.device_agcSetPoint': '-50',
            'capture.device_gainReduction': '40',
            'capture.device_lnaState': '0',  # Invalid: < 1
            'capture.device_dabNotch': 'on',
            'capture.device_rfNotch': 'on',
            'capture.device_bandwidthNumber': '0'
        })

        assert response.status_code == 200
        assert b'is-invalid' in response.data
        assert b'greater than or equal to 1' in response.data

    def test_save_invalid_agc_set_point(self, app_client):
        """Positive AGC set point should show validation error."""
        response = app_client.post('/config/save', data={
            'capture.fs': '4000000',
            'capture.fc': '503000000',
            'capture.device_type': 'RspDuo',
            'capture.device_agcSetPoint': '10',  # Invalid: > 0
            'capture.device_gainReduction': '40',
            'capture.device_lnaState': '4',
            'capture.device_dabNotch': 'on',
            'capture.device_rfNotch': 'on',
            'capture.device_bandwidthNumber': '0'
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
            'capture.device_type': 'RspDuo',
            'capture.device_agcSetPoint': '-50',
            'capture.device_gainReduction': '40',
            'capture.device_lnaState': '4',
            'capture.device_dabNotch': 'on',
            'capture.device_rfNotch': 'on',
            'capture.device_bandwidthNumber': '0'
        }, follow_redirects=False)

        assert response.status_code == 302

        with open(user_config_file) as f:
            saved = yaml.safe_load(f)
        # Other section should still exist
        assert saved.get('other_section') == {'foo': 'bar'}
        # Capture should be updated
        assert saved['capture']['fs'] == 6000000


class TestLocationSave:
    """Test saving location config.

    Note: The form uses flat field names like 'location.rx_latitude' not nested
    'location.rx.latitude' because we use flat Pydantic schemas for validation.
    """

    def test_save_valid_location(self, app_client, user_config_file):
        """Valid location should save."""
        response = app_client.post('/config/save', data={
            'location.rx_latitude': '40.7128',
            'location.rx_longitude': '-74.0060',
            'location.rx_altitude': '10',
            'location.rx_name': 'NYC',
            'location.tx_latitude': '40.0',
            'location.tx_longitude': '-74.0',
            'location.tx_altitude': '100',
            'location.tx_name': 'Transmitter',
        }, follow_redirects=False)
        assert response.status_code == 302

        with open(user_config_file) as f:
            saved = yaml.safe_load(f)
        assert saved['location']['rx']['latitude'] == 40.7128
        assert saved['location']['rx']['name'] == 'NYC'

    def test_save_invalid_latitude(self, app_client):
        """Invalid latitude should show error."""
        response = app_client.post('/config/save', data={
            'location.rx_latitude': '100',  # Invalid > 90
            'location.rx_longitude': '-74.0',
            'location.rx_altitude': '10',
            'location.rx_name': 'Test',
            'location.tx_latitude': '40.0',
            'location.tx_longitude': '-74.0',
            'location.tx_altitude': '100',
            'location.tx_name': 'Transmitter',
        })
        assert response.status_code == 200
        assert b'is-invalid' in response.data

    def test_save_invalid_longitude(self, app_client):
        """Invalid longitude should show error."""
        response = app_client.post('/config/save', data={
            'location.rx_latitude': '40.0',
            'location.rx_longitude': '200',  # Invalid > 180
            'location.rx_altitude': '10',
            'location.rx_name': 'Test',
            'location.tx_latitude': '40.0',
            'location.tx_longitude': '-74.0',
            'location.tx_altitude': '100',
            'location.tx_name': 'Transmitter',
        })
        assert response.status_code == 200
        assert b'is-invalid' in response.data


class TestTar1090Save:
    """Test saving tar1090 config with adsb_source join.

    Note: With layered config, only values that differ from merged config
    are saved to user.yml. So if adsblol_fallback=True is already in merged,
    it won't be in user.yml after save.
    """

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
        # adsb_source changed so should be saved
        assert saved['tar1090']['adsb_source'] == '10.0.0.1,30006,raw_in'
        # adsblol_radius changed (was 40, now 50) so should be saved
        assert saved['tar1090']['adsblol_radius'] == 50
        # adsblol_fallback=True is same as merged config, might not be saved
        # (only values that differ are saved)

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
    """Test saving truth.adsb config.

    Note: Truth fields use 'truth.' prefix directly (e.g., 'truth.enabled')
    since AdsbTruthConfig is a flat schema.

    With layered config, only values that differ from merged config are saved.
    """

    def test_save_valid_truth_config(self, app_client, user_config_file):
        """Valid truth config should save changed values."""
        response = app_client.post('/config/save', data={
            'truth.enabled': 'on',
            'truth.tar1090': 'server.example.com',  # Different from merged
            'truth.adsb2dd': 'localhost:49155',     # Same as merged
            'truth.delay_tolerance': '3.0',         # Different from merged (was 2.0)
            'truth.doppler_tolerance': '6.0',       # Different from merged (was 5.0)
        }, follow_redirects=False)

        assert response.status_code == 302

        with open(user_config_file) as f:
            saved = yaml.safe_load(f)
        # Changed values should be saved
        assert saved['truth']['adsb']['tar1090'] == 'server.example.com'
        assert saved['truth']['adsb']['delay_tolerance'] == 3.0
        assert saved['truth']['adsb']['doppler_tolerance'] == 6.0
        # enabled=True and adsb2dd are same as merged, might not be saved

    def test_invalid_delay_tolerance(self, app_client):
        """Delay tolerance <= 0 should show error."""
        response = app_client.post('/config/save', data={
            'truth.enabled': 'on',
            'truth.tar1090': 'server.example.com',
            'truth.adsb2dd': 'localhost:49155',
            'truth.delay_tolerance': '0',  # Invalid: must be > 0
            'truth.doppler_tolerance': '5.0',
        })
        assert response.status_code == 200
        assert b'is-invalid' in response.data

    def test_invalid_doppler_tolerance(self, app_client):
        """Doppler tolerance <= 0 should show error."""
        response = app_client.post('/config/save', data={
            'truth.enabled': 'on',
            'truth.tar1090': 'server.example.com',
            'truth.adsb2dd': 'localhost:49155',
            'truth.delay_tolerance': '2.0',
            'truth.doppler_tolerance': '-1',  # Invalid: must be > 0
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


class TestParseFlatFormData:
    """Test form data parsing with flat field names."""

    def test_parse_flat_capture_fields(self):
        """Flat capture fields should be parsed correctly."""
        import importlib
        import app as app_module
        importlib.reload(app_module)

        capture, location, truth, tar1090 = app_module.parse_flat_form_data({
            'capture.fs': '4000000',
            'capture.fc': '503000000',
            'capture.device_type': 'RspDuo',
            'capture.device_gainReduction': '40'
        })

        assert capture['fs'] == 4000000
        assert capture['fc'] == 503000000
        assert capture['device_type'] == 'RspDuo'
        assert capture['device_gainReduction'] == 40

    def test_parse_flat_location_fields(self):
        """Flat location fields should be parsed correctly."""
        import importlib
        import app as app_module
        importlib.reload(app_module)

        capture, location, truth, tar1090 = app_module.parse_flat_form_data({
            'location.rx_latitude': '37.7644',
            'location.rx_longitude': '-122.3954',
            'location.rx_altitude': '23',
            'location.rx_name': '150 Mississippi'
        })

        assert location['rx_latitude'] == 37.7644
        assert location['rx_longitude'] == -122.3954
        assert location['rx_altitude'] == 23
        assert location['rx_name'] == '150 Mississippi'

    def test_parse_integer_conversion(self):
        """String integers should be converted to int."""
        import importlib
        import app as app_module
        importlib.reload(app_module)

        capture, _, _, _ = app_module.parse_flat_form_data({
            'capture.fs': '12345'
        })
        assert capture['fs'] == 12345
        assert isinstance(capture['fs'], int)

    def test_parse_float_conversion(self):
        """String floats should be converted to float."""
        import importlib
        import app as app_module
        importlib.reload(app_module)

        _, location, _, _ = app_module.parse_flat_form_data({
            'location.rx_latitude': '37.7644'
        })
        assert location['rx_latitude'] == 37.7644
        assert isinstance(location['rx_latitude'], float)

    def test_parse_negative_values(self):
        """Negative values should be parsed correctly."""
        import importlib
        import app as app_module
        importlib.reload(app_module)

        capture, location, _, _ = app_module.parse_flat_form_data({
            'capture.device_agcSetPoint': '-50',
            'location.rx_longitude': '-122.3954'
        })
        assert capture['device_agcSetPoint'] == -50
        assert location['rx_longitude'] == -122.3954

    def test_parse_boolean_true(self):
        """Boolean true values should be converted."""
        import importlib
        import app as app_module
        importlib.reload(app_module)

        capture, _, _, _ = app_module.parse_flat_form_data({
            'capture.device_dabNotch': 'on'
        })
        assert capture['device_dabNotch'] is True

    def test_parse_empty_string_skipped(self):
        """Empty strings should be skipped."""
        import importlib
        import app as app_module
        importlib.reload(app_module)

        capture, _, _, _ = app_module.parse_flat_form_data({
            'capture.fs': '123',
            'capture.fc': ''
        })
        assert 'fs' in capture
        assert 'fc' not in capture

    def test_parse_string_preserved(self):
        """Non-numeric strings should stay as strings."""
        import importlib
        import app as app_module
        importlib.reload(app_module)

        capture, _, _, _ = app_module.parse_flat_form_data({
            'capture.device_type': 'RspDuo'
        })
        assert capture['device_type'] == 'RspDuo'
        assert isinstance(capture['device_type'], str)
