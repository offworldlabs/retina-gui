"""Tests for network_manager.py and the /network routes."""
import json
import subprocess
from unittest.mock import patch, MagicMock

from network_manager import NetworkManager, _split_terse_line


class TestSplitTerseLine:

    def test_simple_split(self):
        assert _split_terse_line("ethernet:connected", 2) == ["ethernet", "connected"]

    def test_unescapes_colon_in_field(self):
        # nmcli escapes literal colons within a field as '\:'
        assert _split_terse_line("My\\:SSID:80:WPA2", 3) == ["My:SSID", "80", "WPA2"]

    def test_pads_missing_trailing_fields(self):
        assert _split_terse_line("OpenNet:60", 3) == ["OpenNet", "60", ""]


class TestGetNetworkStatus:

    @patch('subprocess.run')
    def test_ethernet_and_wifi_disconnected(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ethernet:disconnected\nwifi:disconnected\n")
        mgr = NetworkManager(dev_mode=False)
        status = mgr.get_network_status()
        assert status == {"ethernet_connected": False, "wifi_connected": False, "wifi_ssid": None, "client_on_wifi": False}

    @patch('subprocess.run')
    def test_ethernet_connected(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ethernet:connected\nwifi:disconnected\n")
        mgr = NetworkManager(dev_mode=False)
        status = mgr.get_network_status()
        assert status["ethernet_connected"] is True
        assert status["wifi_connected"] is False

    @patch('subprocess.run')
    def test_wifi_connected_resolves_ssid(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="ethernet:disconnected\nwifi:connected\n"),
            MagicMock(returncode=0, stdout="no:OtherNet\nyes:HomeNet\n"),
        ]
        mgr = NetworkManager(dev_mode=False)
        status = mgr.get_network_status()
        assert status["wifi_connected"] is True
        assert status["wifi_ssid"] == "HomeNet"

    @patch('subprocess.run')
    def test_nmcli_failure_returns_safe_defaults(self, mock_run):
        mock_run.side_effect = Exception("nmcli not found")
        mgr = NetworkManager(dev_mode=False)
        status = mgr.get_network_status()
        assert status == {"ethernet_connected": False, "wifi_connected": False, "wifi_ssid": None, "client_on_wifi": False}

    def test_dev_mode_reflects_simulated_connect(self):
        mgr = NetworkManager(dev_mode=True)
        assert mgr.get_network_status()["wifi_connected"] is False
        mgr._dev_wifi_ssid = "DevNet"
        status = mgr.get_network_status()
        assert status["wifi_connected"] is True
        assert status["wifi_ssid"] == "DevNet"


class TestClientOnWifiDetection:
    """Warns the user when their own browser session is riding the WiFi
    network they're about to reconfigure (it won't survive the switch)."""

    @patch('subprocess.run')
    def test_client_ip_inside_wifi_subnet_is_flagged(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="ethernet:disconnected\nwifi:connected\n"),
            MagicMock(returncode=0, stdout="yes:HomeNet\n"),
            MagicMock(returncode=0, stdout="wlan0:wifi\neth0:ethernet\n"),
            MagicMock(returncode=0, stdout="IP4.ADDRESS[1]:192.168.1.50/24\n"),
        ]
        mgr = NetworkManager(dev_mode=False)
        status = mgr.get_network_status(client_ip="192.168.1.77")
        assert status["client_on_wifi"] is True

    @patch('subprocess.run')
    def test_client_ip_outside_wifi_subnet_is_not_flagged(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="ethernet:disconnected\nwifi:connected\n"),
            MagicMock(returncode=0, stdout="yes:HomeNet\n"),
            MagicMock(returncode=0, stdout="wlan0:wifi\neth0:ethernet\n"),
            MagicMock(returncode=0, stdout="IP4.ADDRESS[1]:192.168.1.50/24\n"),
        ]
        mgr = NetworkManager(dev_mode=False)
        status = mgr.get_network_status(client_ip="10.0.0.5")
        assert status["client_on_wifi"] is False

    @patch('subprocess.run')
    def test_no_client_ip_is_not_flagged(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="ethernet:disconnected\nwifi:connected\n"),
            MagicMock(returncode=0, stdout="yes:HomeNet\n"),
        ]
        mgr = NetworkManager(dev_mode=False)
        status = mgr.get_network_status(client_ip=None)
        assert status["client_on_wifi"] is False
        assert mock_run.call_count == 2  # never bothers checking subnet without a client IP

    @patch('subprocess.run')
    def test_wifi_not_connected_short_circuits_without_extra_calls(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="ethernet:disconnected\nwifi:disconnected\n")
        mgr = NetworkManager(dev_mode=False)
        status = mgr.get_network_status(client_ip="192.168.1.77")
        assert status["client_on_wifi"] is False
        assert mock_run.call_count == 1  # only the initial device-status check

    def test_dev_mode_never_flags_client(self):
        mgr = NetworkManager(dev_mode=True)
        status = mgr.get_network_status(client_ip="192.168.1.77")
        assert status["client_on_wifi"] is False


class TestScanWifi:

    @patch('subprocess.run')
    def test_parses_and_sorts_by_signal(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="Weak:20:WPA2\nStrong:90:\nMid:50:WPA2\n",
        )
        mgr = NetworkManager(dev_mode=False)
        networks = mgr.scan_wifi()
        assert [n["ssid"] for n in networks] == ["Strong", "Mid", "Weak"]
        assert networks[0]["secured"] is False
        assert networks[1]["secured"] is True

    @patch('subprocess.run')
    def test_dedupes_by_ssid_keeping_strongest(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="SameNet:30:WPA2\nSameNet:75:WPA2\n",
        )
        mgr = NetworkManager(dev_mode=False)
        networks = mgr.scan_wifi()
        assert len(networks) == 1
        assert networks[0]["signal"] == 75

    @patch('subprocess.run')
    def test_drops_hidden_blank_ssid(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout=":40:WPA2\nVisible:60:\n")
        mgr = NetworkManager(dev_mode=False)
        networks = mgr.scan_wifi()
        assert [n["ssid"] for n in networks] == ["Visible"]

    @patch('subprocess.run')
    def test_scan_failure_returns_empty_list(self, mock_run):
        mock_run.side_effect = Exception("boom")
        mgr = NetworkManager(dev_mode=False)
        assert mgr.scan_wifi() == []

    def test_dev_mode_returns_fake_networks(self):
        mgr = NetworkManager(dev_mode=True)
        networks = mgr.scan_wifi()
        assert len(networks) > 0
        assert all("ssid" in n for n in networks)


class TestConnectWifi:

    @patch('subprocess.run')
    def test_successful_connect_verifies_active_before_marking_connected(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),  # nmcli connect
            MagicMock(returncode=0, stdout="no:Other\nyes:NewNet\n"),  # verification scan
        ]
        mgr = NetworkManager(dev_mode=False)
        mgr._run_connect("NewNet", "secret", False)
        status = mgr.get_connect_status()
        assert status == {"state": "connected", "ssid": "NewNet", "error": None}

    @patch('subprocess.run')
    def test_connect_command_failure_marks_failed_and_keeps_message(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Secrets were required, but not provided")
        mgr = NetworkManager(dev_mode=False)
        mgr._run_connect("NewNet", "wrong", False)
        status = mgr.get_connect_status()
        assert status["state"] == "failed"
        assert "Secrets" in status["error"]

    @patch('subprocess.run')
    def test_connect_exit_zero_but_not_active_marks_failed(self, mock_run):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="yes:SomeOtherNet\n"),  # never activated the requested SSID
        ]
        mgr = NetworkManager(dev_mode=False)
        mgr._run_connect("NewNet", "secret", False)
        status = mgr.get_connect_status()
        assert status["state"] == "failed"

    @patch('subprocess.run')
    def test_connect_timeout_marks_failed(self, mock_run):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd='nmcli', timeout=45)
        mgr = NetworkManager(dev_mode=False)
        mgr._run_connect("NewNet", "secret", False)
        status = mgr.get_connect_status()
        assert status["state"] == "failed"
        assert "timed out" in status["error"].lower()

    @patch('subprocess.run')
    def test_old_profile_not_touched_on_connect(self, mock_run):
        """connect_wifi only ever calls nmcli for the new SSID — never touches another profile."""
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="", stderr=""),
            MagicMock(returncode=0, stdout="yes:NewNet\n"),
        ]
        mgr = NetworkManager(dev_mode=False)
        mgr._run_connect("NewNet", "secret", False)
        connect_call = mock_run.call_args_list[0][0][0]
        assert connect_call == ["nmcli", "device", "wifi", "connect", "NewNet", "password", "secret"]

    def test_dev_mode_connect_eventually_succeeds(self):
        mgr = NetworkManager(dev_mode=True)
        mgr._dev_connect("DevNet")
        status = mgr.get_connect_status()
        assert status == {"state": "connected", "ssid": "DevNet", "error": None}


class TestNetworkRoutes:

    @patch('subprocess.run')
    def test_status_endpoint(self, mock_run, app_client):
        mock_run.return_value = MagicMock(returncode=0, stdout="ethernet:connected\nwifi:disconnected\n")
        response = app_client.get('/network/status')
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["ethernet_connected"] is True

    @patch('subprocess.run')
    def test_scan_endpoint(self, mock_run, app_client):
        mock_run.return_value = MagicMock(returncode=0, stdout="Net1:80:WPA2\n")
        response = app_client.get('/network/wifi/scan')
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["networks"][0]["ssid"] == "Net1"

    def test_connect_missing_ssid_returns_400(self, app_client):
        response = app_client.post('/network/wifi/connect',
                                   data=json.dumps({}),
                                   content_type='application/json')
        assert response.status_code == 400
        assert json.loads(response.data)["success"] is False

    @patch('subprocess.run')
    def test_connect_accepts_request_and_returns_immediately(self, mock_run, app_client):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        response = app_client.post('/network/wifi/connect',
                                   data=json.dumps({"ssid": "NewNet", "password": "secret"}),
                                   content_type='application/json')
        assert response.status_code == 200
        assert json.loads(response.data)["success"] is True

    def test_connect_status_endpoint_defaults_to_idle(self, app_client):
        response = app_client.get('/network/wifi/connect/status')
        assert response.status_code == 200
        assert json.loads(response.data)["state"] == "idle"
