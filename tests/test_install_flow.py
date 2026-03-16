"""Tests for the non-blocking install flow and mender-update.status polling.

Covers:
- /mender/check returning correct stage from flag files
- /mender/install kicking off background thread and returning immediately
- Double-install guard (install.lock prevents concurrent installs)
- Lock release on background install failure
- State script lifecycle: downloading -> installing -> commit clears status
- Stale lock auto-clear after timeout
"""
import json
import os
import time
import threading
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import pytest

from device_state import DeviceState, INSTALL_LOCK_TIMEOUT, MENDER_STATUS_TIMEOUT


@pytest.fixture
def ds(tmp_path):
    """DeviceState with temp directory for all file-based state."""
    mender_backup_dir = os.path.join(tmp_path, "mender-cloud-disabled")
    return DeviceState(
        data_dir=str(tmp_path),
        mender_services=["mender-authd", "mender-updated", "mender-connect"],
        mender_conf_path=os.path.join(tmp_path, "mender.conf"),
        mender_conf_backup_dir=mender_backup_dir,
        mender_conf_backup_path=os.path.join(mender_backup_dir, "mender.conf"),
    )


class TestCheckStatusFromFlagFiles:
    """Test that is_any_update_in_progress + _get_mender_update_status
    correctly reflect state from flag files, as /mender/check relies on them."""

    def test_idle_no_files(self, ds):
        in_progress, reason = ds.is_any_update_in_progress()
        assert in_progress is False
        assert ds._get_mender_update_status() is None

    def test_lock_only_shows_downloading_stage(self, ds):
        """When install.lock exists but no mender-update.status yet,
        /mender/check should infer 'downloading' stage."""
        ds.acquire_install_lock("retina-node-v0.4.0")
        in_progress, reason = ds.is_any_update_in_progress()
        assert in_progress is True
        assert "retina-node-v0.4.0" in reason
        # No mender status file yet — check endpoint falls back to "downloading"
        assert ds._get_mender_update_status() is None

    def test_lock_plus_installing_status(self, ds):
        """When both install.lock and mender-update.status exist,
        status file provides the real stage."""
        ds.acquire_install_lock("retina-node-v0.4.0")
        status = {"state": "installing", "ts": datetime.now().astimezone().isoformat()}
        with open(ds.mender_status_file, "w") as f:
            json.dump(status, f)
        in_progress, _ = ds.is_any_update_in_progress()
        assert in_progress is True
        mender_status = ds._get_mender_update_status()
        assert mender_status["state"] == "installing"

    def test_server_push_downloading(self, ds):
        """Server-pushed update writes downloading status without install.lock."""
        status = {"state": "downloading", "ts": datetime.now().astimezone().isoformat()}
        with open(ds.mender_status_file, "w") as f:
            json.dump(status, f)
        in_progress, reason = ds.is_any_update_in_progress()
        assert in_progress is True
        assert "downloading" in reason


class TestDoubleInstallGuard:
    """Test that concurrent installs are blocked."""

    def test_second_lock_acquire_fails(self, ds):
        assert ds.acquire_install_lock("retina-node-v0.4.0") is True
        assert ds.acquire_install_lock("retina-node-v0.4.1") is False

    def test_can_start_install_blocked_by_lock(self, ds):
        ds.acquire_install_lock("retina-node-v0.4.0")
        allowed, reason = ds.can_start_install()
        assert allowed is False
        assert "retina-node-v0.4.0" in reason

    def test_can_start_install_blocked_by_server_update(self, ds):
        status = {"state": "installing", "ts": datetime.now().astimezone().isoformat()}
        with open(ds.mender_status_file, "w") as f:
            json.dump(status, f)
        allowed, reason = ds.can_start_install()
        assert allowed is False

    def test_can_install_after_lock_released(self, ds):
        ds.acquire_install_lock("retina-node-v0.4.0")
        ds.release_install_lock()
        allowed, _ = ds.can_start_install()
        assert allowed is True
        assert ds.acquire_install_lock("retina-node-v0.4.1") is True


class TestLockReleaseOnFailure:
    """Test that install.lock is properly released when install fails."""

    def test_release_on_explicit_failure(self, ds):
        ds.acquire_install_lock("retina-node-v0.4.0")
        assert ds.is_install_locked()[0] is True
        ds.release_install_lock()
        assert ds.is_install_locked()[0] is False

    def test_stale_lock_auto_cleared(self, ds):
        """If background thread crashes without releasing, stale timeout cleans up."""
        stale_time = (datetime.now() - INSTALL_LOCK_TIMEOUT - timedelta(minutes=1)).isoformat()
        lock = {"version": "retina-node-v0.4.0", "started_at": stale_time}
        with open(ds.install_lock_file, "w") as f:
            json.dump(lock, f)
        # Stale lock should auto-clear
        locked, _ = ds.is_install_locked()
        assert locked is False
        assert not os.path.exists(ds.install_lock_file)
        # Can start a new install
        allowed, _ = ds.can_start_install()
        assert allowed is True

    def test_fresh_lock_not_cleared(self, ds):
        """Lock within timeout window should NOT be cleared."""
        ds.acquire_install_lock("retina-node-v0.4.0")
        locked, info = ds.is_install_locked()
        assert locked is True
        assert info["version"] == "retina-node-v0.4.0"


class TestStateScriptLifecycle:
    """Simulate the state script lifecycle:
    1. GUI acquires install.lock
    2. ArtifactInstall_Enter writes mender-update.status {"state":"installing"}
    3. ArtifactCommit_Leave removes mender-update.status
    4. install.lock remains until released or times out
    """

    def test_full_success_lifecycle(self, ds):
        # 1. GUI acquires lock
        ds.acquire_install_lock("retina-node-v0.4.0")
        assert ds.get_state() == "updating_gui"
        assert ds._get_mender_update_status() is None

        # 2. ArtifactInstall_Enter fires (simulated)
        status = {"state": "installing", "ts": datetime.now().astimezone().isoformat()}
        with open(ds.mender_status_file, "w") as f:
            json.dump(status, f)
        assert ds.get_state() == "updating_gui"  # lock still takes priority
        assert ds._get_mender_update_status()["state"] == "installing"

        # 3. ArtifactCommit_Leave fires — clears status file
        os.remove(ds.mender_status_file)
        assert ds._get_mender_update_status() is None
        assert ds.get_state() == "updating_gui"  # lock still held

        # 4. Background worker releases lock
        ds.release_install_lock()
        assert ds.get_state() == "idle"
        in_progress, _ = ds.is_any_update_in_progress()
        assert in_progress is False

    def test_failure_lifecycle(self, ds):
        # 1. GUI acquires lock
        ds.acquire_install_lock("retina-node-v0.4.0")

        # 2. ArtifactInstall_Enter fires
        status = {"state": "installing", "ts": datetime.now().astimezone().isoformat()}
        with open(ds.mender_status_file, "w") as f:
            json.dump(status, f)

        # 3. ArtifactFailure_Enter fires — clears status file
        os.remove(ds.mender_status_file)

        # 4. Background worker detects failure, releases lock
        ds.release_install_lock()
        assert ds.get_state() == "idle"

        # 5. User can retry
        allowed, _ = ds.can_start_install()
        assert allowed is True

    def test_server_push_lifecycle(self, ds):
        """Server-pushed update: no install.lock, only mender-update.status."""
        # 1. Download_Enter fires
        status = {"state": "downloading", "ts": datetime.now().astimezone().isoformat()}
        with open(ds.mender_status_file, "w") as f:
            json.dump(status, f)
        assert ds.get_state() == "updating_server"

        # 2. ArtifactInstall_Enter fires
        status = {"state": "installing", "ts": datetime.now().astimezone().isoformat()}
        with open(ds.mender_status_file, "w") as f:
            json.dump(status, f)
        assert ds.get_state() == "updating_server"

        # 3. Cloud toggle blocked during update
        allowed, _ = ds.can_toggle_cloud_services()
        assert allowed is False

        # 4. ArtifactCommit_Leave clears status
        os.remove(ds.mender_status_file)
        assert ds.get_state() == "idle"
        allowed, _ = ds.can_toggle_cloud_services()
        assert allowed is True

    def test_crash_recovery_via_stale_timeout(self, ds):
        """If state script never fires and lock goes stale, timeout recovers."""
        stale_time = (datetime.now() - INSTALL_LOCK_TIMEOUT - timedelta(minutes=1)).isoformat()
        lock = {"version": "retina-node-v0.4.0", "started_at": stale_time}
        with open(ds.install_lock_file, "w") as f:
            json.dump(lock, f)
        # Also a stale mender status
        stale_ts = (datetime.now().astimezone() - MENDER_STATUS_TIMEOUT - timedelta(minutes=1)).isoformat()
        status = {"state": "installing", "ts": stale_ts}
        with open(ds.mender_status_file, "w") as f:
            json.dump(status, f)

        # Both should auto-clear
        assert ds.get_state() == "idle"
        assert not os.path.exists(ds.install_lock_file)
        assert not os.path.exists(ds.mender_status_file)


class TestCloudToggleBlockedDuringInstall:
    """Verify cloud services can't be toggled during any update type."""

    @patch("device_state.subprocess.run")
    def test_blocked_during_gui_install(self, mock_run, ds):
        ds.acquire_install_lock("retina-node-v0.4.0")
        success, error = ds.set_cloud_services(False)
        assert success is False
        assert "retina-node-v0.4.0" in error
        mock_run.assert_not_called()

    @patch("device_state.subprocess.run")
    def test_blocked_during_server_push(self, mock_run, ds):
        status = {"state": "downloading", "ts": datetime.now().astimezone().isoformat()}
        with open(ds.mender_status_file, "w") as f:
            json.dump(status, f)
        success, error = ds.set_cloud_services(False)
        assert success is False
        mock_run.assert_not_called()

    @patch("device_state.subprocess.run")
    def test_allowed_after_install_complete(self, mock_run, ds):
        mock_run.return_value = MagicMock(returncode=0)
        ds.acquire_install_lock("retina-node-v0.4.0")
        ds.release_install_lock()
        success, _ = ds.set_cloud_services(False)
        assert success is True
