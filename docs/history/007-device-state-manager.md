# 007 - Device State Manager

## Overview

Introduce a `DeviceState` finite state machine to retina-gui that consolidates all scattered lock files, flag files, and status checks into a single class. Enforces guards that prevent dangerous transitions — primarily: **cannot disable cloud services while any update is in progress** (GUI-initiated, server-pushed app, or server-pushed OS).

Update detection uses **Mender state scripts** in owl-os — the official Mender mechanism for hooking into the deployment lifecycle. State scripts write a status file at each phase (download, install), and clear it on completion or failure.

## Problem

Currently the device has two independent state mechanisms with no awareness of each other:
- `install.lock` — JSON file for GUI-initiated OTA installs
- `cloud-services-disabled` — empty flag file toggling Mender services

Neither detects **server-pushed deployments**. A user can disable cloud services mid-update, killing `mender-updated` while it's writing to disk — potentially bricking the device.

## Source of Truth: File-Based State

DeviceState is a **read and guard layer** on top of file-based mechanisms. The files remain the source of truth:

| File | Type | Purpose | Survives OTA | Survives Reboot |
|------|------|---------|:---:|:---:|
| `/data/retina-gui/install.lock` | Temporary lock | Prevents concurrent GUI installs. JSON with version + timestamp. Auto-cleared after 30 min. | Yes | Yes |
| `/data/retina-gui/cloud-services-disabled` | Persistent flag | Disables Mender services. Empty file — existence = disabled. Enforced on every boot. | Yes | Yes |
| `/data/mender-cloud-disabled/mender.conf` | Backup | Preserves TenantToken while cloud services are off. | Yes | Yes |
| `/data/retina-gui/mender-update.status` | **NEW** — Mender status | Written by Mender state scripts during updates. JSON with state + timestamp. Cleared on completion/failure. | Yes | Yes |

### `mender-update.status` File Format

**Format:** JSON — overwritten at each state transition, deleted on completion/failure.

```json
{"state": "downloading", "ts": "2026-03-04T14:23:01+00:00"}
```

| Field | Type | Values | Purpose |
|-------|------|--------|---------|
| `state` | string | `"downloading"` \| `"installing"` | Current phase — shown in UI message |
| `ts` | ISO 8601 string | e.g. `"2026-03-04T14:23:01+00:00"` | Embedded timestamp for stale detection |

**File lifecycle (overwrite, not append):**

```
[no file]                                              ← IDLE
    │
    │  Download_Enter script runs
    ▼
{"state":"downloading","ts":"..."}                     ← UPDATING (download phase)
    │
    │  ArtifactInstall_Enter script runs
    ▼
{"state":"installing","ts":"..."}                      ← UPDATING (install phase)
    │
    ├── ArtifactCommit_Leave → file DELETED             ← back to IDLE (success)
    └── ArtifactFailure_Enter → file DELETED            ← back to IDLE (failure)
```

**Guard logic:** `os.path.exists()` is sufficient to block the toggle. The JSON contents are only read for:
1. UI message: "System update in progress (downloading)" vs "(installing)"
2. Stale detection: if `ts` is older than 2 hours, assume crash — delete file and treat as IDLE

**Stale detection (crash recovery):**
```python
def _is_mender_update_active(self) -> bool:
    if not os.path.exists(self.mender_status_file):
        return False
    status = self._get_mender_update_status()
    if not status:
        return False
    ts = datetime.fromisoformat(status["ts"])
    if datetime.now(ts.tzinfo) - ts > timedelta(hours=2):
        os.remove(self.mender_status_file)
        return False
    return True
```

**What DeviceState adds:**
1. Reads `mender-update.status` (written by Mender state scripts in owl-os) to detect server-pushed updates across ALL phases
2. Guard methods that read the existing files + status file before allowing transitions
3. A single entry point so routes don't need to know about individual files

```
Before:  route → is_install_locked()           (standalone function, only checks lock file)
         route → os.path.exists(flag)           (inline check, no guard)
         route → ???                            (no detection of server-pushed updates)

After:   route → device_state.can_toggle_cloud_services()
                    ├── reads install.lock             (same file, same format)
                    ├── reads mender-update.status     (NEW — written by state scripts)
                    └── returns (allowed, reason)

         route → device_state.set_cloud_services(enabled)
                    ├── checks guard first (above)
                    ├── creates/removes cloud-services-disabled  (same file, same logic)
                    ├── systemctl stop/start                     (same commands)
                    └── backs up/restores mender.conf            (same logic)
```

---

## Mender State Scripts (owl-os)

Mender's update daemon runs through a state machine for every deployment. **Rootfs state scripts** in `/etc/mender/scripts/` run for ALL deployments on the device, regardless of artifact type — this single set of scripts covers both OS updates (rootfs artifacts) and application updates (module artifacts like retina-node).

### Two types of Mender state scripts

| Type | Location | Runs For | Deployed Via |
|------|----------|----------|--------------|
| **Rootfs scripts** | `/etc/mender/scripts/` on device | ALL deployments (any artifact type) | Ansible role during OS build |
| **Artifact scripts** | Embedded in `.mender` file | Only that specific artifact | `--script` flag in `mender-artifact write` |

**We use rootfs scripts** because we need to catch updates for any artifact type. The existing owl-os artifact scripts (`ArtifactInstall_Enter_50_BootpartitionSetup`, etc.) remain unchanged — they handle artifact-specific tasks like boot partition setup.

### Standalone vs Managed mode

| Mode | Triggered By | States Executed | Download_Enter fires? |
|------|-------------|----------------|----------------------|
| **Managed** (server-pushed) | `mender-updated` daemon | Full: Idle → Sync → Download → ArtifactInstall → ArtifactReboot → ArtifactCommit | **YES** |
| **Standalone** (GUI-initiated) | `mender-update install <url>` | Partial: ArtifactInstall → ArtifactReboot → ArtifactCommit (skips Idle/Sync/Download) | **NO** |

This is fine: GUI installs are already protected by `install.lock` (acquired before download begins), so `Download_Enter` is only needed for server-pushed deployments.

### Update coverage matrix

| Scenario | Detection Mechanism | Covers Download? | Covers Install? |
|----------|-------------------|:---:|:---:|
| **Server-pushed app update** (retina-node) | `mender-update.status` via rootfs state scripts | YES (`Download_Enter`) | YES (`ArtifactInstall_Enter`) |
| **Server-pushed OS update** (owl-os rootfs) | `mender-update.status` via rootfs state scripts | YES (`Download_Enter`) | YES (`ArtifactInstall_Enter`) |
| **GUI-initiated app install** | `install.lock` file (acquired before mender-update runs) | YES (lock acquired pre-download) | YES (lock held through install) |

Both mechanisms are complementary — `install.lock` for GUI installs, state scripts for server-pushed deployments. During GUI installs, `ArtifactInstall_Enter` also fires and writes the status file, but this is harmless/redundant since `install.lock` already blocks the toggle.

### State machine lifecycle

```
Server pushes deployment
         │
         ▼
   Sync_Enter           ← Mender checks server
         │
         ▼
   Download_Enter       ← Artifact download STARTS
   ★ WRITES status file: {"state": "downloading", ...}
         │
         ▼
   ArtifactInstall_Enter ← Writing to disk STARTS
   ★ UPDATES status file: {"state": "installing", ...}
         │
         ▼
   ArtifactReboot_Enter  ← About to reboot (rootfs only)
         │                  (status file persists in /data/ across reboot)
         ▼
   ArtifactCommit_Leave  ← Update DONE, committed
   ★ REMOVES status file
         │
   ArtifactFailure_Enter ← Update FAILED or rolled back
   ★ REMOVES status file
```

### State script files

**Deployed via ansible** to `/etc/mender/scripts/` on the device.
Source: `owl-os/plugins/playbooks/board_support/roles/mender/files/state-scripts/`

**`Download_Enter_00_retina_state`**
```bash
#!/bin/sh
mkdir -p /data/retina-gui
echo "{\"state\": \"downloading\", \"ts\": \"$(date -Iseconds)\"}" \
    > /data/retina-gui/mender-update.status
exit 0
```

**`ArtifactInstall_Enter_00_retina_state`**
```bash
#!/bin/sh
mkdir -p /data/retina-gui
echo "{\"state\": \"installing\", \"ts\": \"$(date -Iseconds)\"}" \
    > /data/retina-gui/mender-update.status
exit 0
```

**`ArtifactCommit_Leave_00_retina_state`**
```bash
#!/bin/sh
rm -f /data/retina-gui/mender-update.status
exit 0
```

**`ArtifactFailure_Enter_00_retina_state`**
```bash
#!/bin/sh
rm -f /data/retina-gui/mender-update.status
exit 0
```

The `_00_` prefix ensures these run before the existing artifact `_50_` scripts (BootpartitionSetup, ConfigurationBackup).

### Ansible deployment

Add to `owl-os/plugins/playbooks/board_support/roles/mender/tasks/main.yml`:

```yaml
- name: Create Mender rootfs scripts directory
  file:
    path: /etc/mender/scripts
    state: directory
    mode: '0755'

- name: Install retina state scripts
  copy:
    src: "state-scripts/{{ item }}"
    dest: "/etc/mender/scripts/{{ item }}"
    mode: '0755'
  loop:
    - Download_Enter_00_retina_state
    - ArtifactInstall_Enter_00_retina_state
    - ArtifactCommit_Leave_00_retina_state
    - ArtifactFailure_Enter_00_retina_state
```

---

## Finite State Machine

### States

```
┌─────────────────────────────────────────────────────────────┐
│                       DEVICE STATES                         │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  IDLE                                                       │
│  ├── cloud_services: ENABLED | DISABLED                     │
│  ├── No install lock                                        │
│  └── No mender-update.status file                           │
│                                                             │
│  UPDATING                                                   │
│  ├── cloud_services: ENABLED (locked — cannot change)       │
│  ├── Source: GUI install lock OR mender status file          │
│  └── Substates:                                             │
│      ├── UPDATING_GUI        install.lock exists            │
│      ├── UPDATING_DOWNLOAD   status: "downloading"          │
│      └── UPDATING_INSTALL    status: "installing"           │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### State Detection (computed on each request)

```python
def get_state(self):
    if install.lock exists (and not stale):
        return "updating_gui"
    if mender-update.status exists:
        return "updating_server"     # substates from status JSON
    return "idle"
```

### Transitions

```
                    ┌──────────────┐
          ┌────────►│     IDLE     │◄────────┐
          │         └──────┬───┬───┘         │
          │                │   │              │
          │    GUI install │   │ state script │
          │    POST /mender│   │ writes       │
          │    /install    │   │ status file  │
          │                ▼   ▼              │
          │         ┌──────────────┐          │
          │         │   UPDATING   │          │
          │         │              │          │
          │         │ cloud toggle │          │
          │         │   BLOCKED    │          │
          │         └──────┬───┬───┘          │
          │                │   │              │
          │  lock released │   │ state script │
          │                │   │ removes      │
          │                │   │ status file  │
          └────────────────┘   └──────────────┘
```

### Guards (Transition Rules)

| Action                  | IDLE           | UPDATING_GUI   | UPDATING_SERVER |
|-------------------------|----------------|----------------|-----------------|
| Toggle cloud services   | ALLOWED        | BLOCKED        | BLOCKED         |
| Start GUI install       | ALLOWED        | BLOCKED        | BLOCKED         |
| Server pushes update    | (external)     | (concurrent)   | (already active)|

### Cloud Services Sub-States

Within IDLE, cloud services have their own state:

```
  CLOUD_ENABLED ──────► CLOUD_DISABLED
       │    ▲  user toggles  │    ▲
       │    │    off / on     │    │
       │    └─────────────────┘    │
       │                           │
       │  OTA regenerates config   │
       └───► apply_startup_preferences()
             re-enforces disabled state
```

---

## Implementation

### New File: `retina-gui/device_state.py` (~120 lines)

```python
"""Device state machine for retina-gui.

Consolidates install locks, cloud services flag, and Mender update
status into a single class with enforced guard conditions.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta

INSTALL_LOCK_TIMEOUT = timedelta(minutes=30)


class DeviceState:
    """Manages device state transitions and enforces safety guards.

    States:
        IDLE              — No updates running, all toggles available
        UPDATING_GUI      — GUI-initiated install in progress (install.lock)
        UPDATING_SERVER   — Server-pushed update in progress (mender-update.status)

    Guards:
        - Cloud services toggle blocked in any UPDATING state
        - GUI install blocked in any UPDATING state
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

    # ── State Queries ──────────────────────────────────────────

    def get_state(self) -> str:
        """Return current device state."""
        locked, _ = self.is_install_locked()
        if locked:
            return "updating_gui"
        if self._is_mender_update_active():
            return "updating_server"
        return "idle"

    def is_install_locked(self) -> tuple[bool, dict | None]:
        """Check GUI install lock. Auto-clears stale locks (>30 min)."""
        ...  # (moved from app.py)

    def _is_mender_update_active(self) -> bool:
        """Check if Mender state scripts report an active update.
        Reads mender-update.status JSON file. Auto-clears if >2h stale (crash recovery)."""
        if not os.path.exists(self.mender_status_file):
            return False
        status = self._get_mender_update_status()
        if not status:
            return False
        # Stale detection: if ts > 2 hours old, assume crash — clear and ignore
        try:
            ts = datetime.fromisoformat(status["ts"])
            if datetime.now(ts.tzinfo) - ts > timedelta(hours=2):
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
        if status:
            state = status.get("state", "updating")
            return True, f"System update in progress ({state})"
        return False, None

    def is_cloud_services_enabled(self) -> bool:
        """Check flag file."""
        return not os.path.exists(self.cloud_disabled_flag)

    def get_cloud_services_status(self) -> dict:
        """Full status for GET /mender/cloud-services."""
        ...  # systemctl checks + is_any_update_in_progress()

    # ── Guards ─────────────────────────────────────────────────

    def can_toggle_cloud_services(self) -> tuple[bool, str | None]:
        """Guard: blocked during any update."""
        ...

    def can_start_install(self) -> tuple[bool, str | None]:
        """Guard: blocked if already updating."""
        ...

    # ── Transitions ────────────────────────────────────────────

    def acquire_install_lock(self, version: str) -> bool: ...
    def release_install_lock(self): ...
    def set_cloud_services(self, enabled: bool) -> tuple[bool, str | None]: ...
    def apply_startup_preferences(self): ...
    def ensure_cloud_services_enabled(self, get_jwt_fn) -> tuple[bool, str | None]: ...
```

### Modify: `retina-gui/app.py`

**Remove** standalone functions (moved into DeviceState):
- `apply_cloud_services_preference()` (lines 51-78)
- `ensure_cloud_services_enabled()` (lines 81-116)
- `is_install_locked()` / `acquire_install_lock()` / `release_install_lock()` (lines 119-155)
- Constants: `INSTALL_LOCK_FILE`, `MENDER_CONF_PATH`, `MENDER_CONF_BACKUP_DIR`, `MENDER_CONF_BACKUP_PATH`, `CLOUD_SERVICES_DISABLED_FLAG`

**Add** at module level:
```python
from device_state import DeviceState

device_state = DeviceState(
    data_dir=DATA_DIR,
    mender_services=MENDER_SERVICES,
    mender_conf_path="/data/mender/mender.conf",
    mender_conf_backup_dir="/data/mender-cloud-disabled",
    mender_conf_backup_path="/data/mender-cloud-disabled/mender.conf",
)
device_state.apply_startup_preferences()
```

**Update routes:**

| Route | Before | After |
|-------|--------|-------|
| `GET /mender/cloud-services` | Inline flag check + systemctl | `device_state.get_cloud_services_status()` |
| `POST /mender/cloud-services` | Inline enable/disable | `device_state.set_cloud_services(enabled)` — returns 409 if blocked |
| `GET /mender/check` | `is_install_locked()` | `device_state.is_any_update_in_progress()` |
| `POST /mender/install` | `acquire/release_install_lock()` | `device_state.can_start_install()` + acquire/release |

### Modify: `retina-gui/templates/config.html` (lines 226-292)

Update cloud services JS to handle new `update_in_progress` / `update_reason` fields in GET response. Disable toggle and show warning when update is active. Handle 409 Conflict in POST response.

### New File: `retina-gui/tests/test_device_state.py`

Tests for DeviceState:
- State queries (idle, updating_gui, updating_server, stale lock cleanup)
- Guard tests (toggle allowed when idle, blocked during install, blocked during server update)
- Transition tests (set_cloud_services respects guard, acquire/release lock)
- Mender status file (present = updating, absent = idle, malformed JSON = idle)

### Modify: `retina-gui/tests/conftest.py`

Add `device_state` fixture with temp directory.

---

## Work Plan

### Phase 1: Create and Deploy Mender State Scripts (owl-os)

- [x] **1.1** Create state script files in `owl-os/plugins/playbooks/board_support/roles/mender/files/state-scripts/`:
  - `Download_Enter_00_retina_state`
  - `ArtifactInstall_Enter_00_retina_state`
  - `ArtifactCommit_Leave_00_retina_state`
  - `ArtifactFailure_Enter_00_retina_state`
- [x] **1.2** Add ansible tasks to `roles/mender/tasks/main.yml` — create `/etc/mender/scripts/` dir + copy scripts
- [ ] **1.3** Deploy test owl-os build to device
- [ ] **1.4** Trigger server-pushed **app** update (retina-node) — verify `mender-update.status` appears during download, updates during install, disappears on completion
- [ ] **1.5** Trigger server-pushed **OS** update (owl-os rootfs) — same verification, confirm status file survives reboot
- [ ] **1.6** Test failure case — abort a deployment, verify status file is cleaned up by `ArtifactFailure_Enter`

### Phase 2: Create DeviceState Class (retina-gui)

- [x] **2.1** Create `device_state.py` with class skeleton (init, file paths, constants)
- [x] **2.2** Move `is_install_locked()` / `acquire_install_lock()` / `release_install_lock()` into class
- [x] **2.3** Add `_is_mender_update_active()` and `_get_mender_update_status()` — reads status file
- [x] **2.4** Add `get_state()` — returns `"idle"`, `"updating_gui"`, or `"updating_server"`
- [x] **2.5** Add `is_any_update_in_progress()` — combined check with human-readable reason
- [x] **2.6** Add guard methods: `can_toggle_cloud_services()`, `can_start_install()`
- [x] **2.7** Move cloud services logic into class: `set_cloud_services()`, `apply_startup_preferences()`, `ensure_cloud_services_enabled()`
- [x] **2.8** Add `get_cloud_services_status()` returning full status dict

### Phase 3: Write Tests for DeviceState

- [x] **3.1** Add `device_state` fixture to `tests/test_device_state.py` (pytest `tmp_path` based — no conftest change needed)
- [x] **3.2** Create `tests/test_device_state.py` with 38 tests across 8 test classes
- [x] **3.3** Run tests — 180 passed (38 new + 142 existing), 0 failures

### Phase 4: Refactor app.py to Use DeviceState

- [x] **4.1** Add `from device_state import DeviceState` and instantiate at module level
- [x] **4.2** Replace startup call with `device_state.apply_startup_preferences()`
- [x] **4.3** Update `GET /mender/cloud-services` → `device_state.get_cloud_services_status()`
- [x] **4.4** Update `POST /mender/cloud-services` → `device_state.set_cloud_services()` (409 when blocked)
- [x] **4.5** Update `GET /mender/check` → `device_state.is_any_update_in_progress()`
- [x] **4.6** Update `POST /mender/install` → `device_state.can_start_install()` + acquire/release
- [x] **4.7** Remove old standalone functions and orphaned constants/imports (`shutil`, `time`, `datetime`/`timedelta`)
- [x] **4.8** Run full test suite — 180 passed, 0 failures

### Phase 5: Frontend Update

- [x] **5.1** Update `config.html` JS: handle `update_in_progress` / `update_reason` in GET response
- [x] **5.2** Update `setCloudServices()` to handle 409 Conflict responses
- [x] **5.3** Auto-poll every 10s while update in progress so toggle auto-unlocks

### Phase 6: End-to-End Verification

- [ ] **6.1** Deploy retina-gui + owl-os to device
- [ ] **6.2** Toggle cloud services while idle — works as before
- [ ] **6.3** Start a GUI install, navigate to config page — toggle disabled, shows "Installing retina-node-vX.X.X"
- [ ] **6.4** Trigger server-pushed app update — toggle disabled, shows "System update in progress (downloading)" then "(installing)"
- [ ] **6.5** Trigger server-pushed OS update — same behaviour, persists across reboot
- [ ] **6.6** Abort a deployment — toggle re-enables after ArtifactFailure clears status
- [ ] **6.7** POST `/mender/cloud-services` with `{"enabled": false}` during update — returns 409

---

## Future: Update Windows (natural extension)

The guard pattern + state scripts make time-gated OTA windows straightforward to add later:

```
can_start_update() → checks is_in_update_window() + is_any_update_in_progress()
```

For server-pushed updates, Mender 4.x **Update Control Maps** via D-Bus (`SetUpdateControlMap` on `io.mender.UpdateManager`) let the device tell `mender-updated` to defer deployments outside the configured window.

---

## Files Summary

| File | Repo | Action | ~Lines |
|------|------|--------|--------|
| `plugins/playbooks/board_support/roles/mender/files/state-scripts/Download_Enter_00_retina_state` | owl-os | CREATE | 5 |
| `plugins/playbooks/board_support/roles/mender/files/state-scripts/ArtifactInstall_Enter_00_retina_state` | owl-os | CREATE | 5 |
| `plugins/playbooks/board_support/roles/mender/files/state-scripts/ArtifactCommit_Leave_00_retina_state` | owl-os | CREATE | 4 |
| `plugins/playbooks/board_support/roles/mender/files/state-scripts/ArtifactFailure_Enter_00_retina_state` | owl-os | CREATE | 4 |
| `plugins/playbooks/board_support/roles/mender/tasks/main.yml` | owl-os | MODIFY — add rootfs scripts deployment | +12 |
| `device_state.py` | retina-gui | CREATE | ~120 |
| `app.py` | retina-gui | MODIFY — remove 6 functions, use DeviceState | -60, +20 |
| `templates/config.html` | retina-gui | MODIFY — handle update_in_progress in JS | ~15 changed |
| `tests/test_device_state.py` | retina-gui | CREATE | ~100 |
| `tests/conftest.py` | retina-gui | MODIFY — add device_state fixture | +15 |

## References

- [Mender State Scripts docs](https://docs.mender.io/artifact-creation/state-scripts)
- [Mender D-Bus notification example](https://github.com/mendersoftware/mender/tree/master/examples/state-scripts/dbus-notification)
- [Mender deployment lifecycle](https://docs.mender.io/overview/deployment)
- Existing owl-os state scripts: `ArtifactInstall_Enter_50_BootpartitionSetup`, `ArtifactInstall_Leave_50_ConfigurationBackup`
