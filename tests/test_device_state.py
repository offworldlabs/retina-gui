"""Tests for DeviceState — state machine, guards, and transitions."""
import json
import os
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest

from device_state import DeviceState, INSTALL_LOCK_TIMEOUT, MENDER_STATUS_TIMEOUT


@pytest.fixture
def ds(tmp_path):
    """Create a DeviceState with temp directory for all file-based state."""
    mender_backup_dir = os.path.join(tmp_path, "mender-cloud-disabled")
    return DeviceState(
        data_dir=str(tmp_path),
        mender_services=["mender-authd", "mender-updated", "mender-connect"],
        mender_conf_path=os.path.join(tmp_path, "mender.conf"),
        mender_conf_backup_dir=mender_backup_dir,
        mender_conf_backup_path=os.path.join(mender_backup_dir, "mender.conf"),
    )


class TestGetState:
    """Test get_state() returns correct state based on files."""

    def test_idle_when_no_files(self, ds):
        assert ds.get_state() == "idle"

    def test_updating_gui_when_lock_exists(self, ds):
        lock = {"version": "v0.3.5", "started_at": datetime.now().isoformat()}
        with open(ds.install_lock_file, "w") as f:
            json.dump(lock, f)
        assert ds.get_state() == "updating_gui"

    def test_updating_server_when_status_exists(self, ds):
        status = {"state": "downloading", "ts": datetime.now().astimezone().isoformat()}
        with open(ds.mender_status_file, "w") as f:
            json.dump(status, f)
        assert ds.get_state() == "updating_server"

    def test_gui_lock_takes_priority_over_server_status(self, ds):
        """If both lock and status exist, GUI lock wins (checked first)."""
        lock = {"version": "v0.3.5", "started_at": datetime.now().isoformat()}
        with open(ds.install_lock_file, "w") as f:
            json.dump(lock, f)
        status = {"state": "installing", "ts": datetime.now().astimezone().isoformat()}
        with open(ds.mender_status_file, "w") as f:
            json.dump(status, f)
        assert ds.get_state() == "updating_gui"


class TestInstallLock:
    """Test install lock acquire/release/stale detection."""

    def test_not_locked_when_no_file(self, ds):
        locked, info = ds.is_install_locked()
        assert locked is False
        assert info is None

    def test_locked_when_file_exists(self, ds):
        lock = {"version": "v0.3.5", "started_at": datetime.now().isoformat()}
        with open(ds.install_lock_file, "w") as f:
            json.dump(lock, f)
        locked, info = ds.is_install_locked()
        assert locked is True
        assert info["version"] == "v0.3.5"

    def test_stale_lock_auto_cleared(self, ds):
        stale_time = (datetime.now() - INSTALL_LOCK_TIMEOUT - timedelta(minutes=1)).isoformat()
        lock = {"version": "v0.3.5", "started_at": stale_time}
        with open(ds.install_lock_file, "w") as f:
            json.dump(lock, f)
        locked, info = ds.is_install_locked()
        assert locked is False
        assert not os.path.exists(ds.install_lock_file)

    def test_acquire_lock(self, ds):
        assert ds.acquire_install_lock("v0.3.5") is True
        assert os.path.exists(ds.install_lock_file)
        with open(ds.install_lock_file) as f:
            lock = json.load(f)
        assert lock["version"] == "v0.3.5"

    def test_acquire_fails_when_already_locked(self, ds):
        ds.acquire_install_lock("v0.3.5")
        assert ds.acquire_install_lock("v0.3.6") is False

    def test_release_lock(self, ds):
        ds.acquire_install_lock("v0.3.5")
        ds.release_install_lock()
        assert not os.path.exists(ds.install_lock_file)

    def test_release_noop_when_not_locked(self, ds):
        ds.release_install_lock()  # Should not raise

    def test_malformed_lock_treated_as_unlocked(self, ds):
        with open(ds.install_lock_file, "w") as f:
            f.write("not json")
        locked, info = ds.is_install_locked()
        assert locked is False


class TestMenderUpdateStatus:
    """Test mender-update.status file reading and stale detection."""

    def test_no_status_file(self, ds):
        assert ds._is_mender_update_active() is False
        assert ds._get_mender_update_status() is None

    def test_active_downloading(self, ds):
        status = {"state": "downloading", "ts": datetime.now().astimezone().isoformat()}
        with open(ds.mender_status_file, "w") as f:
            json.dump(status, f)
        assert ds._is_mender_update_active() is True
        result = ds._get_mender_update_status()
        assert result["state"] == "downloading"

    def test_active_installing(self, ds):
        status = {"state": "installing", "ts": datetime.now().astimezone().isoformat()}
        with open(ds.mender_status_file, "w") as f:
            json.dump(status, f)
        assert ds._is_mender_update_active() is True

    def test_stale_status_auto_cleared(self, ds):
        stale_time = (datetime.now().astimezone() - MENDER_STATUS_TIMEOUT - timedelta(minutes=1)).isoformat()
        status = {"state": "installing", "ts": stale_time}
        with open(ds.mender_status_file, "w") as f:
            json.dump(status, f)
        assert ds._is_mender_update_active() is False
        assert not os.path.exists(ds.mender_status_file)

    def test_malformed_json_treated_as_inactive(self, ds):
        with open(ds.mender_status_file, "w") as f:
            f.write("not json")
        assert ds._is_mender_update_active() is False

    def test_missing_ts_treated_as_active(self, ds):
        """Fail safe: if ts is missing, assume update is active."""
        status = {"state": "installing"}
        with open(ds.mender_status_file, "w") as f:
            json.dump(status, f)
        assert ds._is_mender_update_active() is True


class TestIsAnyUpdateInProgress:
    """Test combined update detection."""

    def test_idle(self, ds):
        in_progress, reason = ds.is_any_update_in_progress()
        assert in_progress is False
        assert reason is None

    def test_gui_install(self, ds):
        ds.acquire_install_lock("v0.3.5")
        in_progress, reason = ds.is_any_update_in_progress()
        assert in_progress is True
        assert "v0.3.5" in reason

    def test_server_downloading(self, ds):
        status = {"state": "downloading", "ts": datetime.now().astimezone().isoformat()}
        with open(ds.mender_status_file, "w") as f:
            json.dump(status, f)
        in_progress, reason = ds.is_any_update_in_progress()
        assert in_progress is True
        assert "downloading" in reason

    def test_server_installing(self, ds):
        status = {"state": "installing", "ts": datetime.now().astimezone().isoformat()}
        with open(ds.mender_status_file, "w") as f:
            json.dump(status, f)
        in_progress, reason = ds.is_any_update_in_progress()
        assert in_progress is True
        assert "installing" in reason


class TestGuards:
    """Test guard methods that prevent dangerous transitions."""

    def test_can_toggle_when_idle(self, ds):
        allowed, reason = ds.can_toggle_cloud_services()
        assert allowed is True
        assert reason is None

    def test_cannot_toggle_during_gui_install(self, ds):
        ds.acquire_install_lock("v0.3.5")
        allowed, reason = ds.can_toggle_cloud_services()
        assert allowed is False
        assert "v0.3.5" in reason

    def test_cannot_toggle_during_server_update(self, ds):
        status = {"state": "downloading", "ts": datetime.now().astimezone().isoformat()}
        with open(ds.mender_status_file, "w") as f:
            json.dump(status, f)
        allowed, reason = ds.can_toggle_cloud_services()
        assert allowed is False
        assert "downloading" in reason

    def test_can_start_install_when_idle(self, ds):
        allowed, reason = ds.can_start_install()
        assert allowed is True

    def test_cannot_start_install_during_update(self, ds):
        ds.acquire_install_lock("v0.3.5")
        allowed, reason = ds.can_start_install()
        assert allowed is False


class TestSetCloudServices:
    """Test set_cloud_services() with guard enforcement."""

    @patch("device_state.subprocess.run")
    def test_disable_creates_flag(self, mock_run, ds):
        mock_run.return_value = MagicMock(returncode=0)
        success, error = ds.set_cloud_services(False)
        assert success is True
        assert os.path.exists(ds.cloud_disabled_flag)

    @patch("device_state.subprocess.run")
    def test_enable_removes_flag(self, mock_run, ds):
        # Start disabled
        with open(ds.cloud_disabled_flag, "w") as f:
            f.write("")
        mock_run.return_value = MagicMock(returncode=0)
        success, error = ds.set_cloud_services(True)
        assert success is True
        assert not os.path.exists(ds.cloud_disabled_flag)

    @patch("device_state.subprocess.run")
    def test_blocked_during_install(self, mock_run, ds):
        ds.acquire_install_lock("v0.3.5")
        success, error = ds.set_cloud_services(False)
        assert success is False
        assert "v0.3.5" in error
        mock_run.assert_not_called()

    @patch("device_state.subprocess.run")
    def test_blocked_during_server_update(self, mock_run, ds):
        status = {"state": "installing", "ts": datetime.now().astimezone().isoformat()}
        with open(ds.mender_status_file, "w") as f:
            json.dump(status, f)
        success, error = ds.set_cloud_services(False)
        assert success is False
        assert "installing" in error
        mock_run.assert_not_called()

    @patch("device_state.subprocess.run")
    def test_disable_backs_up_mender_conf(self, mock_run, ds):
        mock_run.return_value = MagicMock(returncode=0)
        # Create a mender.conf to be backed up
        with open(ds.mender_conf_path, "w") as f:
            f.write('{"TenantToken": "abc123"}')
        success, _ = ds.set_cloud_services(False)
        assert success is True
        assert os.path.exists(ds.mender_conf_backup_path)
        assert not os.path.exists(ds.mender_conf_path)

    @patch("device_state.subprocess.run")
    def test_enable_restores_mender_conf(self, mock_run, ds):
        mock_run.return_value = MagicMock(returncode=0)
        # Start disabled with backed up conf
        with open(ds.cloud_disabled_flag, "w") as f:
            f.write("")
        os.makedirs(ds.mender_conf_backup_dir, exist_ok=True)
        with open(ds.mender_conf_backup_path, "w") as f:
            f.write('{"TenantToken": "abc123"}')
        success, _ = ds.set_cloud_services(True)
        assert success is True
        assert os.path.exists(ds.mender_conf_path)
        assert not os.path.exists(ds.mender_conf_backup_path)


class TestCloudServicesStatus:
    """Test get_cloud_services_status() response shape."""

    @patch("device_state.subprocess.run")
    def test_status_includes_update_fields(self, mock_run, ds):
        mock_run.return_value = MagicMock(returncode=0)
        result = ds.get_cloud_services_status()
        assert "enabled" in result
        assert "services" in result
        assert "update_in_progress" in result
        assert "update_reason" in result

    @patch("device_state.subprocess.run")
    def test_status_shows_update_in_progress(self, mock_run, ds):
        mock_run.return_value = MagicMock(returncode=0)
        status = {"state": "downloading", "ts": datetime.now().astimezone().isoformat()}
        with open(ds.mender_status_file, "w") as f:
            json.dump(status, f)
        result = ds.get_cloud_services_status()
        assert result["update_in_progress"] is True
        assert "downloading" in result["update_reason"]


class TestApplyStartupPreferences:
    """Test apply_startup_preferences() on boot."""

    @patch("device_state.subprocess.run")
    def test_noop_when_enabled(self, mock_run, ds):
        ds.apply_startup_preferences()
        mock_run.assert_not_called()

    @patch("device_state.subprocess.run")
    def test_stops_services_when_disabled(self, mock_run, ds):
        with open(ds.cloud_disabled_flag, "w") as f:
            f.write("")
        mock_run.return_value = MagicMock(returncode=0)
        ds.apply_startup_preferences()
        assert mock_run.call_count > 0

    @patch("device_state.subprocess.run")
    def test_rebackups_conf_after_ota(self, mock_run, ds):
        """If OTA regenerates mender.conf, startup should re-backup it."""
        with open(ds.cloud_disabled_flag, "w") as f:
            f.write("")
        with open(ds.mender_conf_path, "w") as f:
            f.write('{"TenantToken": "new"}')
        mock_run.return_value = MagicMock(returncode=0)
        ds.apply_startup_preferences()
        assert os.path.exists(ds.mender_conf_backup_path)
        assert not os.path.exists(ds.mender_conf_path)
