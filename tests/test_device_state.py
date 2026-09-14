"""Tests for DeviceState — state machine, guards, and transitions."""
import json
import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from device_state import (
    INSTALL_LOCK_TIMEOUT,
    MENDER_STATUS_TIMEOUT,
    SETUP_WIZARD_TIMEOUT,
    TELEMETRY_CONSENT_VERSION,
    DeviceState,
)


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


class TestSetupWizardState:
    """Test setup wizard state persistence."""

    def test_no_wizard_state(self, ds):
        assert ds.get_setup_wizard_step() is None
        assert ds.is_setup_wizard_in_progress() is False

    def test_save_and_get_step(self, ds):
        ds.save_setup_wizard_step("agreements")
        assert ds.get_setup_wizard_step() == "agreements"
        assert ds.is_setup_wizard_in_progress() is True

    def test_update_step_preserves_started_at(self, ds):
        ds.save_setup_wizard_step("agreements")
        with open(ds.setup_wizard_file) as f:
            first = json.load(f)
        ds.save_setup_wizard_step("system")
        with open(ds.setup_wizard_file) as f:
            second = json.load(f)
        assert second["step"] == "system"
        assert second["started_at"] == first["started_at"]

    def test_clear_wizard(self, ds):
        ds.save_setup_wizard_step("radar")
        ds.clear_setup_wizard()
        assert ds.get_setup_wizard_step() is None
        assert not os.path.exists(ds.setup_wizard_file)

    def test_clear_noop_when_no_file(self, ds):
        ds.clear_setup_wizard()  # Should not raise

    def test_stale_wizard_auto_cleared(self, ds):
        stale_time = (datetime.now() - SETUP_WIZARD_TIMEOUT - timedelta(minutes=1)).isoformat()
        data = {"step": "system", "started_at": stale_time}
        with open(ds.setup_wizard_file, "w") as f:
            json.dump(data, f)
        assert ds.get_setup_wizard_step() is None
        assert not os.path.exists(ds.setup_wizard_file)

    def test_malformed_json_returns_none(self, ds):
        with open(ds.setup_wizard_file, "w") as f:
            f.write("not json")
        assert ds.get_setup_wizard_step() is None


class TestTowersCache:
    """Test the tower-preset cache: search-populated, manually added/removed."""

    def test_get_returns_none_when_never_cached(self, ds):
        assert ds.get_towers_cache() is None

    def test_save_and_get(self, ds):
        ds.save_towers_cache(-33.8688, 151.2093, [{"callsign": "A", "frequency_mhz": 100.0}])
        cache = ds.get_towers_cache()
        assert cache["lat"] == -33.8688
        assert cache["lon"] == 151.2093
        assert cache["towers"] == [{"callsign": "A", "frequency_mhz": 100.0}]

    def test_save_overwrites_previous_search(self, ds):
        ds.save_towers_cache(1, 1, [{"callsign": "Old", "frequency_mhz": 1.0}])
        ds.save_towers_cache(2, 2, [{"callsign": "New", "frequency_mhz": 2.0}])
        cache = ds.get_towers_cache()
        assert cache["lat"] == 2
        assert len(cache["towers"]) == 1
        assert cache["towers"][0]["callsign"] == "New"

    def test_add_tower_creates_cache_when_none_exists(self, ds):
        ds.add_tower_to_cache({"callsign": "Manual", "frequency_mhz": 95.5})
        cache = ds.get_towers_cache()
        assert cache["towers"] == [{"callsign": "Manual", "frequency_mhz": 95.5}]
        assert cache["lat"] is None

    def test_add_tower_appends_to_existing_cache(self, ds):
        ds.save_towers_cache(1, 1, [{"callsign": "A", "frequency_mhz": 100.0}])
        ds.add_tower_to_cache({"callsign": "B", "frequency_mhz": 200.0})
        towers = ds.get_towers_cache()["towers"]
        assert [t["callsign"] for t in towers] == ["A", "B"]

    def test_remove_tower_by_index(self, ds):
        ds.save_towers_cache(1, 1, [
            {"callsign": "A", "frequency_mhz": 100.0},
            {"callsign": "B", "frequency_mhz": 200.0},
        ])
        removed = ds.remove_tower_from_cache(0)
        assert removed is True
        towers = ds.get_towers_cache()["towers"]
        assert [t["callsign"] for t in towers] == ["B"]

    def test_remove_tower_out_of_range_returns_false(self, ds):
        ds.save_towers_cache(1, 1, [{"callsign": "A", "frequency_mhz": 100.0}])
        assert ds.remove_tower_from_cache(5) is False
        assert len(ds.get_towers_cache()["towers"]) == 1

    def test_remove_tower_with_no_cache_returns_false(self, ds):
        assert ds.remove_tower_from_cache(0) is False

    def test_malformed_cache_file_treated_as_none(self, ds):
        os.makedirs(os.path.dirname(ds.towers_cache_file), exist_ok=True)
        with open(ds.towers_cache_file, "w") as f:
            f.write("not json")
        assert ds.get_towers_cache() is None


class TestBackfillSetupWizardCompleted:
    """Test the flag backfill for nodes that finished setup before it existed.

    retina-telemetry gates registration on this flag, so a false negative here
    strands a fully configured node permanently. The evidence is a location in
    user.yml, which `/towers/select` has written since 4afa307 (2026-03-23),
    three months before the flag arrived in aee29a6 (2026-06-24).
    """

    CONFIGURED = {"location": {"rx": {"latitude": 42.241528, "longitude": -72.648361}}}

    def test_writes_the_flag_for_a_configured_node(self, ds):
        """The case this exists for: an old node with a real location."""
        assert ds.backfill_setup_wizard_completed(self.CONFIGURED) is True
        assert ds.has_completed_setup_wizard()

    def test_leaves_an_unconfigured_node_blocked(self, ds):
        """No evidence the owner chose anything, so no flag. This node should
        stay unregistered rather than report the Greenwich default."""
        assert ds.backfill_setup_wizard_completed({"capture": {"fc": 503000000}}) is False
        assert not ds.has_completed_setup_wizard()

    def test_empty_user_config_is_not_evidence(self, ds):
        for empty in ({}, None):
            assert ds.backfill_setup_wizard_completed(empty) is False
            assert not ds.has_completed_setup_wizard()

    def test_partial_coordinates_are_not_evidence(self, ds):
        """A half-written block does not prove a completed tower step."""
        half = {"location": {"rx": {"latitude": 42.241528}}}
        assert ds.backfill_setup_wizard_completed(half) is False
        assert not ds.has_completed_setup_wizard()

    def test_zero_is_a_real_coordinate(self, ds):
        """Null Island is a legitimate latitude and longitude. Testing these
        for truthiness rather than presence would strand a node on the equator
        or the prime meridian."""
        origin = {"location": {"rx": {"latitude": 0, "longitude": 0}}}
        assert ds.backfill_setup_wizard_completed(origin) is True

    def test_existing_flag_is_not_redated(self, ds):
        """The flag answers "when was setup finished", and a backfill has not
        finished anything. Rewriting it would also make a re-run look recent."""
        ds.mark_setup_wizard_completed()
        with open(ds.setup_wizard_completed_flag) as f:
            original = f.read()

        assert ds.backfill_setup_wizard_completed(self.CONFIGURED) is False
        with open(ds.setup_wizard_completed_flag) as f:
            assert f.read() == original

    def test_malformed_user_config_does_not_raise(self, ds):
        """Startup must not die on a config it cannot make sense of."""
        for junk in ("a string", ["a", "list"], {"location": "not a dict"},
                     {"location": {"rx": "not a dict"}}):
            assert ds.backfill_setup_wizard_completed(junk) is False

    def test_unwritable_data_dir_does_not_raise(self, ds):
        """Best effort on startup, as elsewhere in this class."""
        with patch.object(ds, "mark_setup_wizard_completed", side_effect=OSError):
            assert ds.backfill_setup_wizard_completed(self.CONFIGURED) is False


class TestTelemetryConsent:
    """Test the consent records retina-telemetry refuses to register without.

    The shape is a cross-repo contract with retina-telemetry's
    `collect/consent.py`, which fails closed on anything it does not recognise
    — so these assert the exact keys rather than round-tripping our own writer.
    """

    def test_no_consent_by_default(self, ds):
        """The state of every node that has not been through the wizard."""
        assert ds.get_telemetry_consent() is None

    def test_writes_all_three_records(self, ds):
        ds.save_telemetry_consent()
        consent = ds.get_telemetry_consent()

        assert set(consent) == {"licence", "remote_management", "publication"}
        for name in ("licence", "remote_management", "publication"):
            assert consent[name]["version"] == TELEMETRY_CONSENT_VERSION
            assert consent[name]["accepted_at"]

    def test_publication_records_a_choice(self, ds):
        """retina-telemetry discards the record unless choice is exactly
        "public" or "private"; anything else is treated as no consent at all."""
        ds.save_telemetry_consent()
        assert ds.get_telemetry_consent()["publication"]["choice"] == "public"

    def test_accepted_at_is_timezone_aware(self, ds):
        """The wire types this AwareDatetime, so a naive timestamp is rejected
        at the boundary and the node silently never registers."""
        ds.save_telemetry_consent()
        stamp = ds.get_telemetry_consent()["licence"]["accepted_at"]

        assert datetime.fromisoformat(stamp).tzinfo is not None

    def test_all_three_share_one_timestamp(self, ds):
        """One checkbox, one moment of agreement."""
        ds.save_telemetry_consent()
        consent = ds.get_telemetry_consent()

        stamps = {consent[n]["accepted_at"] for n in consent}
        assert len(stamps) == 1

    def test_reaccepting_same_version_preserves_the_original_date(self, ds):
        """A wizard re-run showing unchanged wording is not a new agreement,
        and overwriting would destroy when they first agreed."""
        ds.save_telemetry_consent()
        first = ds.get_telemetry_consent()["licence"]["accepted_at"]

        ds.save_telemetry_consent()
        assert ds.get_telemetry_consent()["licence"]["accepted_at"] == first

    def test_new_version_redates_every_record(self, ds):
        """Changed wording is a genuine re-acceptance."""
        ds.save_telemetry_consent(version="2020-01-01")
        with open(ds.telemetry_consent_file) as f:
            stale = json.load(f)
        stale["licence"]["accepted_at"] = "2020-01-01T00:00:00Z"
        stale["publication"]["accepted_at"] = "2020-01-01T00:00:00Z"
        with open(ds.telemetry_consent_file, "w") as f:
            json.dump(stale, f)

        ds.save_telemetry_consent(version="2026-12-25")
        consent = ds.get_telemetry_consent()

        assert consent["licence"]["version"] == "2026-12-25"
        assert consent["licence"]["accepted_at"] != "2020-01-01T00:00:00Z"
        assert consent["publication"]["accepted_at"] != "2020-01-01T00:00:00Z"

    def test_malformed_file_reads_as_no_consent(self, ds):
        """Fail closed, matching retina-telemetry's own posture: a record we
        cannot read is not a record."""
        with open(ds.telemetry_consent_file, "w") as f:
            f.write("{not json")
        assert ds.get_telemetry_consent() is None

    def test_write_leaves_no_temp_file_behind(self, ds):
        ds.save_telemetry_consent()
        assert not os.path.exists(ds.telemetry_consent_file + ".tmp")
