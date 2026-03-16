"""Device state machine for retina-gui.

Consolidates install locks, cloud services flag, and Mender update
status into a single class with enforced guard conditions.

State sources:
- install.lock: GUI-initiated OTA installs (JSON with version + timestamp)
- cloud-services-disabled: Mender service toggle (empty flag file)
- mender-update.status: Server-pushed updates (JSON written by Mender state scripts)

Guards:
- Can't toggle cloud services while any update is in progress
- Can't start GUI install while one is already running
"""

import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timedelta


INSTALL_LOCK_TIMEOUT = timedelta(minutes=40)
MENDER_STATUS_TIMEOUT = timedelta(hours=2)
SETUP_WIZARD_TIMEOUT = timedelta(hours=24)


class DeviceState:
    """Manages device state transitions and enforces safety guards.

    States:
        idle            — No updates running, all toggles available
        updating_gui    — GUI-initiated install in progress (install.lock)
        updating_server — Server-pushed update in progress (mender-update.status)
    """

    def __init__(self, data_dir, mender_services, mender_conf_path,
                 mender_conf_backup_dir, mender_conf_backup_path):
        self.data_dir = data_dir
        self.install_lock_file = os.path.join(data_dir, "install.lock")
        self.cloud_disabled_flag = os.path.join(data_dir, "cloud-services-disabled")
        self.mender_status_file = os.path.join(data_dir, "mender-update.status")
        self.mender_services = mender_services
        self.mender_conf_path = mender_conf_path
        self.mender_conf_backup_dir = mender_conf_backup_dir
        self.mender_conf_backup_path = mender_conf_backup_path
        self.setup_wizard_file = os.path.join(data_dir, "setup-wizard.json")

    # ── State Queries ──────────────────────────────────────────

    def get_state(self) -> str:
        """Return current device state: 'idle', 'updating_gui', or 'updating_server'."""
        locked, _ = self.is_install_locked()
        if locked:
            return "updating_gui"
        if self._is_mender_update_active():
            return "updating_server"
        return "idle"

    def is_install_locked(self) -> tuple[bool, dict | None]:
        """Check GUI install lock. Auto-clears stale locks (>30 min)."""
        if not os.path.exists(self.install_lock_file):
            return False, None
        try:
            with open(self.install_lock_file) as f:
                lock = json.load(f)
            started = datetime.fromisoformat(lock["started_at"])
            if datetime.now() - started > INSTALL_LOCK_TIMEOUT:
                os.remove(self.install_lock_file)
                return False, None
            return True, lock
        except Exception:
            return False, None

    def _is_mender_update_active(self) -> bool:
        """Check if Mender state scripts report an active update.

        Reads mender-update.status JSON written by rootfs state scripts.
        Auto-clears if >2h stale (crash recovery).
        """
        if not os.path.exists(self.mender_status_file):
            return False
        status = self._get_mender_update_status()
        if not status:
            return False
        try:
            ts = datetime.fromisoformat(status["ts"])
            if datetime.now(ts.tzinfo) - ts > MENDER_STATUS_TIMEOUT:
                os.remove(self.mender_status_file)
                return False
        except (KeyError, ValueError):
            pass  # Missing/bad ts — treat as active (fail safe)
        return True

    def _get_mender_update_status(self) -> dict | None:
        """Read Mender update status JSON: {"state": "downloading", "ts": "..."}."""
        if not os.path.exists(self.mender_status_file):
            return None
        try:
            with open(self.mender_status_file) as f:
                return json.load(f)
        except Exception:
            return None

    def is_any_update_in_progress(self) -> tuple[bool, str | None]:
        """Combined update check. Returns (in_progress, human_reason)."""
        locked, lock_info = self.is_install_locked()
        if locked:
            version = lock_info.get("version", "unknown") if lock_info else "unknown"
            return True, f"Installing {version}"
        status = self._get_mender_update_status()
        if status and self._is_mender_update_active():
            state = status.get("state", "updating")
            return True, f"System update in progress ({state})"
        return False, None

    def is_cloud_services_enabled(self) -> bool:
        """Check cloud services flag file."""
        return not os.path.exists(self.cloud_disabled_flag)

    def get_cloud_services_status(self) -> dict:
        """Full status for GET /mender/cloud-services."""
        service_status = {}
        for service in self.mender_services:
            try:
                result = subprocess.run(
                    ["systemctl", "is-active", service],
                    capture_output=True, text=True, timeout=5
                )
                service_status[service] = result.returncode == 0
            except Exception:
                service_status[service] = False

        update_in_progress, update_reason = self.is_any_update_in_progress()

        return {
            "enabled": self.is_cloud_services_enabled(),
            "services": service_status,
            "update_in_progress": update_in_progress,
            "update_reason": update_reason,
        }

    # ── Guards ─────────────────────────────────────────────────

    def can_toggle_cloud_services(self) -> tuple[bool, str | None]:
        """Guard: blocked during any update."""
        in_progress, reason = self.is_any_update_in_progress()
        if in_progress:
            return False, reason
        return True, None

    def can_start_install(self) -> tuple[bool, str | None]:
        """Guard: blocked if already updating."""
        in_progress, reason = self.is_any_update_in_progress()
        if in_progress:
            return False, reason
        return True, None

    # ── Transitions ────────────────────────────────────────────

    def acquire_install_lock(self, version: str) -> bool:
        """Try to acquire install lock. Returns False if already locked."""
        locked, _ = self.is_install_locked()
        if locked:
            return False
        lock = {"version": version, "started_at": datetime.now().isoformat()}
        os.makedirs(os.path.dirname(self.install_lock_file), exist_ok=True)
        with open(self.install_lock_file, "w") as f:
            json.dump(lock, f)
        return True

    def release_install_lock(self):
        """Release install lock."""
        if os.path.exists(self.install_lock_file):
            os.remove(self.install_lock_file)

    def set_cloud_services(self, enabled: bool) -> tuple[bool, str | None]:
        """Enable/disable cloud services. Respects guards.

        Returns (success, error). Checks can_toggle_cloud_services() first.
        """
        allowed, reason = self.can_toggle_cloud_services()
        if not allowed:
            return False, reason

        try:
            if enabled:
                if os.path.exists(self.cloud_disabled_flag):
                    os.remove(self.cloud_disabled_flag)

                if os.path.exists(self.mender_conf_backup_path):
                    shutil.move(self.mender_conf_backup_path, self.mender_conf_path)

                for service in self.mender_services:
                    subprocess.run(["systemctl", "enable", service],
                        capture_output=True, timeout=10)
                    subprocess.run(["systemctl", "start", service],
                        capture_output=True, timeout=10)
            else:
                os.makedirs(self.data_dir, exist_ok=True)
                with open(self.cloud_disabled_flag, 'w') as f:
                    f.write('')

                for service in self.mender_services:
                    subprocess.run(["systemctl", "stop", service],
                        capture_output=True, timeout=10)
                    subprocess.run(["systemctl", "disable", service],
                        capture_output=True, timeout=10)

                if os.path.exists(self.mender_conf_path):
                    os.makedirs(self.mender_conf_backup_dir, exist_ok=True)
                    shutil.move(self.mender_conf_path, self.mender_conf_backup_path)

            return True, None

        except subprocess.TimeoutExpired:
            return False, "Command timed out"
        except Exception as e:
            return False, str(e)

    def apply_startup_preferences(self):
        """Enforce cloud services preference on startup.

        Ensures the setting persists across OTA updates.
        If OTA regenerates mender.conf, we stop services and re-backup.
        """
        if not os.path.exists(self.cloud_disabled_flag):
            return  # Enabled - nothing to enforce

        for service in self.mender_services:
            try:
                subprocess.run(["systemctl", "stop", service],
                    capture_output=True, timeout=10)
                subprocess.run(["systemctl", "disable", service],
                    capture_output=True, timeout=10)
            except Exception:
                pass  # Best effort on startup

        if os.path.exists(self.mender_conf_path):
            try:
                os.makedirs(self.mender_conf_backup_dir, exist_ok=True)
                shutil.move(self.mender_conf_path, self.mender_conf_backup_path)
            except Exception:
                pass  # Best effort on startup

    # ── Setup Wizard State ────────────────────────────────────

    def get_setup_wizard_step(self) -> str | None:
        """Get current wizard step name, or None if not in progress.

        Auto-clears if older than 24h (abandoned wizard).
        """
        if not os.path.exists(self.setup_wizard_file):
            return None
        try:
            with open(self.setup_wizard_file) as f:
                data = json.load(f)
            started = datetime.fromisoformat(data["started_at"])
            if datetime.now() - started > SETUP_WIZARD_TIMEOUT:
                os.remove(self.setup_wizard_file)
                return None
            return data.get("step")
        except Exception:
            return None

    def save_setup_wizard_step(self, step: str):
        """Save current wizard step. Preserves original started_at timestamp."""
        data = {}
        if os.path.exists(self.setup_wizard_file):
            try:
                with open(self.setup_wizard_file) as f:
                    data = json.load(f)
            except Exception:
                pass
        if "started_at" not in data:
            data["started_at"] = datetime.now().isoformat()
        data["step"] = step
        os.makedirs(os.path.dirname(self.setup_wizard_file), exist_ok=True)
        with open(self.setup_wizard_file, "w") as f:
            json.dump(data, f)

    def clear_setup_wizard(self):
        """Clear wizard state (called on completion)."""
        if os.path.exists(self.setup_wizard_file):
            os.remove(self.setup_wizard_file)

    def is_setup_wizard_in_progress(self) -> bool:
        """Check if setup wizard is active."""
        return self.get_setup_wizard_step() is not None

    def ensure_cloud_services_enabled(self, get_jwt_fn) -> tuple[bool, str | None]:
        """Enable cloud services and wait for Mender auth.

        Takes get_jwt_fn callable to avoid circular dependency with MenderClient.
        Returns (success, error).
        """
        if not os.path.exists(self.cloud_disabled_flag):
            token, _ = get_jwt_fn()
            if token:
                return True, None

        if os.path.exists(self.cloud_disabled_flag):
            os.remove(self.cloud_disabled_flag)

        if os.path.exists(self.mender_conf_backup_path):
            shutil.move(self.mender_conf_backup_path, self.mender_conf_path)

        for service in self.mender_services:
            try:
                subprocess.run(["systemctl", "enable", service],
                    capture_output=True, timeout=10)
                subprocess.run(["systemctl", "start", service],
                    capture_output=True, timeout=10)
            except Exception:
                pass

        for _ in range(30):  # ~60s max
            time.sleep(2)
            token, _ = get_jwt_fn()
            if token:
                return True, None

        return False, "Timed out waiting for Mender authentication"
