# 010: Managed OTA for OS Updates

## Context

Standalone `mender-update install` for rootfs OTA doesn't work reliably — the daemon doesn't run post-reboot state scripts (WiFi/config restore, auto-commit) for standalone installs. The Mender docs also state standalone mode requires the daemon to be stopped, which we weren't doing.

Managed OTA from the Mender server works perfectly — the daemon handles the full state machine (download, install, config backup, tryboot reboot, config restore, commit, rollback on failure).

We're on Mender Basic/Professional (not Enterprise), so dynamic deployments aren't available. Per-device deployments must be created server-side.

**Decision**: OS updates use managed mode (server-side deployment via node-infra). App updates remain standalone (no reboot needed).

Branch: `feat/set-up`

## Architecture

### Current flow (broken)
```
Wizard → mender-update install (standalone) → manual reboot → WiFi lost → manual commit
```

### New flow
```
Wizard → enable cloud services → daemon authenticates
→ node-infra auto-approves + creates per-device deployment (every 30s timer)
→ daemon polls → finds deployment → handles everything
→ wizard polls flag file + shows progress
```

### Why managed mode is most robust
- Daemon's state machine is battle-tested (WiFi restore, commit, rollback — all built in)
- Zero custom glue services or systemd oneshots
- No standalone mode edge cases (reboot handling, manual commit, state script gaps)
- Mender docs say standalone requires daemon stopped — we were running both
- Rollback on failure is automatic
- Deployment history visible in Mender dashboard

### Component responsibilities

| Component | Role |
|-----------|------|
| **retina-gui** | Enable cloud services, poll flag file, show progress to user |
| **node-infra** | Auto-approve new devices + create per-device deployment if OS outdated (existing timer, extended) |
| **Mender daemon** | Download, install, config backup, tryboot reboot, config restore, commit, rollback |
| **Mender server** | Host artifacts, manage deployments, device authentication |

## Detailed Step-by-Step Onboarding Flow

```
DEVICE (retina-gui)                    SERVER (node-infra)                 MENDER SERVER
─────────────────                      ───────────────────                 ─────────────

1. User in setup wizard Step 1:
   checks EULA + export compliance + cloud services
   (checkboxes stay as-is)

2. User advances to Step 2 (System Update)
   → retina-gui checks GitHub for latest OS
   → shows "Current: v0.7.3 → Available: v0.7.31"
   → shows [Update System] and [Skip] buttons

3. User clicks "Update System" (or "Skip" to advance without updating)
   → POST /mender/install-os
   → acquire install.lock ("owl-os-pi5-v0.7.31")
   → retina-gui enables cloud services
   → mender-updated + mender-authd start

4. mender-authd authenticates with
   Mender server
   ──────────────────────────────────────────────────────────────────► Device appears
                                                                      as PENDING
5. retina-gui returns:
   {success: true, state: "waiting"}
   → install.lock prevents duplicate installs + blocks cloud toggle
   → wizard shows "Connecting to
     update server..." with spinner

6.                                     Timer fires (every 30s)
                                       │
                                       GET /devauth/devices?status=pending ──►
                                       ◄── [{device_id, auth_sets, identity}]
                                       │
                                       Found new device → accept it
                                       PUT /devauth/devices/{id}/auth/{aid} ─►
                                       ◄── 204 OK                           Device now
                                       │                                    ACCEPTED
                                       │
                                       Get device inventory (OS version)
                                       GET /inventory/devices/{id} ──────────►
                                       ◄── {artifact_name: "owl-os-pi5-v0.7.3"}
                                       │
                                       Get latest artifact for device type
                                       GET /deployments/artifacts ───────────►
                                       ◄── [{name: "owl-os-pi5-v0.7.31"}]
                                       │
                                       OS outdated → create deployment
                                       POST /deployments/deployments ────────►
                                         {name: "onboard-{device_id}-{ts}",
                                          artifact_name: "owl-os-pi5-v0.7.31",
                                          devices: [{device_id}]}
                                       ◄── 201 Created

7. Daemon polls server (every 60s)
   Finds pending deployment              ◄──────────────────────────────── Deployment
   │                                                                      assigned

8. Download_Enter state script runs
   → writes flag file: {state: "downloading"}
   │
   retina-gui polls flag file (every 5s)
   → shows "Downloading OS update..."

9. Download completes
   ArtifactInstall state scripts run:
   → ConfigurationBackup (saves WiFi to /data/backup/)
   → retina_state writes: {state: "installing"}
   │
   retina-gui polls
   → shows "Installing OS update..."

10. Install completes
    Daemon triggers reboot via pi-uboot
    → tryboot into new partition
    → device goes offline
    │
    retina-gui shows "Rebooting..."
    (connection lost)

11. NEW ROOTFS BOOTS:
    → mender-updated daemon starts
    → Runs ArtifactReboot_Leave state scripts
    → ConfigurationRestore (restores WiFi from /data/backup/)
    → NetworkManager restarts → WiFi reconnects
    → avahi announces retina.local
    → Daemon verifies health → auto-commits
    → ArtifactCommit_Leave clears flag file + install.lock

12. User reconnects to retina.local/set-up
    → Wizard resumes at Step 2 (from saved state)
    → GET /mender/check-os → {current: "v0.7.31", update_available: false}
    → "Update complete!" → auto-advance to Step 3
```

### Timing expectations
- Steps 3-5: ~5 seconds (enable services, auth)
- Steps 5-6: up to 30 seconds (waiting for node-infra timer)
- Steps 6: ~5 seconds (approve + create deployment)
- Steps 6-7: up to 60 seconds (waiting for daemon poll)
- Steps 8-9: ~30 seconds (download ~450MB compressed artifact)
- Steps 9-10: ~30 seconds (write rootfs)
- Steps 10-11: ~60 seconds (reboot, restore, commit)
- **Total: ~3-4 minutes** from clicking "Update" to completion

User sees: "Connecting..." (30-90s) → "Downloading..." (30s) → "Installing..." (30s) → "Rebooting..." → "Complete!"

## Implementation Steps

### Step 1: node-infra — Extend auto-approve to create deployments

Extend `auto_accept.py` (~30 lines of new code):

After accepting a device, check its OS version and create a deployment if outdated.

```python
# New function: get device inventory
def get_device_inventory(device_id):
    """Get device inventory attributes from Mender."""
    resp = requests.get(
        f"{MENDER_SERVER}/api/management/v1/inventory/devices/{device_id}",
        headers=HEADERS,
    )
    resp.raise_for_status()
    return resp.json()

# New function: get latest STABLE artifact version for device type
def get_latest_stable_artifact(device_type):
    """Get latest stable artifact name for a device type.

    Only considers stable releases matching vX.X.X format.
    Filters out rc, dev, beta, alpha, etc.
    """
    resp = requests.get(
        f"{MENDER_SERVER}/api/management/v1/deployments/artifacts",
        headers=HEADERS,
    )
    resp.raise_for_status()
    artifacts = resp.json()
    # Filter for device type
    matching = [a for a in artifacts if device_type in a.get("device_types_compatible", [])]
    # Filter for stable only: name must match owl-os-pi5-vX.X.X (no -rc, -dev, -beta, etc.)
    stable = [a for a in matching if re.match(r'^owl-os-pi5-v\d+\.\d+\.\d+$', a["name"])]
    if not stable:
        return None
    # Sort by semver, return latest
    stable.sort(key=lambda a: tuple(int(x) for x in re.findall(r'\d+', a["name"])[-3:]))
    return stable[-1]["name"]

# New function: create deployment for device
def create_onboard_deployment(device_id, artifact_name):
    """Create a one-off deployment targeting a single device."""
    resp = requests.post(
        f"{MENDER_SERVER}/api/management/v1/deployments/deployments",
        headers=HEADERS,
        json={
            "name": f"onboard-{device_id[:8]}-{int(time.time())}",
            "artifact_name": artifact_name,
            "devices": [device_id],
        },
    )
    resp.raise_for_status()
```

In `main()`, after accepting each device:
```python
# After accept...
inventory = get_device_inventory(device_id)
current_artifact = extract_artifact_name(inventory)
latest = get_latest_stable_artifact(device_type)
if current_artifact != latest:
    create_onboard_deployment(device_id, latest)
    log(f"Created deployment for {node_id}: {current_artifact} → {latest}")
```

**File**: `node-infra/mender-auto-accept/auto_accept.py`

### Step 2: retina-gui — Simplify install-os endpoint

Enable cloud services, acquire lock, return. No subprocess, no reboot, no background thread.
`install.lock` kept as GUI-level guard — prevents duplicate installs, blocks cloud toggle during
update. State scripts clear it on commit or failure.

```python
@app.route("/mender/install-os", methods=["POST"])
def mender_install_os():
    # Guard: block if any update already in progress
    can_install, reason = device_state.can_start_install()
    if not can_install:
        return jsonify(success=False, error=reason), 409

    # Get target version for the lock
    latest_tag, error = get_latest_owl_os_from_github()
    if error:
        return jsonify(success=False, error=error)
    release_name = f"owl-os-pi5-{latest_tag.removeprefix('os-')}"

    # Acquire lock — prevents duplicate installs + blocks cloud toggle
    if not device_state.acquire_install_lock(release_name):
        return jsonify(success=False, error="Install already in progress"), 409

    # Enable cloud services (starts daemon, begins auth)
    success, error = device_state.ensure_cloud_services_enabled(mender.get_jwt)
    if not success:
        device_state.release_install_lock()
        return jsonify(success=False, error=error)

    # Save wizard state before potential reboot
    device_state.save_setup_wizard_step("system")

    # That's it! node-infra will auto-approve + create deployment.
    # Daemon will find it on next poll and handle everything.
    # install.lock cleared by ArtifactCommit_Leave or ArtifactFailure_Enter state scripts.
    return jsonify(success=True, state="waiting")
```

- Remove `mender-update install` subprocess call
- Remove `--reboot-exit-code` handling
- Remove `_run_install` background thread (for OS updates)
- Remove reboot logic
- Keep `install.lock` as GUI guard (cleared by state scripts on commit/failure)
- Keep `install_from_url` for app updates only (standalone, no reboot)

**File**: `retina-gui/src/app.py`

### Step 3: retina-gui — Update check-os for polling

```python
@app.route("/mender/check-os")
def mender_check_os():
    # 1. Check flag file for in-progress install (managed deployment)
    status = device_state.get_mender_update_status()
    if status:
        return jsonify(installing=True, state=status["state"], ...)

    # 2. Compare current vs latest (GitHub for version discovery)
    current = mender.get_versions()[0]  # owl-os version
    latest, err = get_latest_owl_os_from_github()
    if err:
        return jsonify(error=err)

    update_available = parse_os_version(latest) > parse_os_version(current)
    return jsonify(
        current_version=current,
        latest_version=latest,
        update_available=update_available,
        installing=False,
    )
```

No new states needed — existing flag file mechanism works for managed mode. State scripts write it during managed deployments just like they would for standalone.

**File**: `retina-gui/src/app.py`

### Step 4: retina-gui — Update setup.html wizard

Step 2 (System Update) changes:
- Shows version comparison: "Current: v0.7.3 → Available: v0.7.31"
- Two buttons: **[Update System]** and **[Skip]**
- Skip advances to Step 3 without updating (user can update later)
- Cloud services enabled silently when user clicks Update (not in Step 1)
- Polling handles waiting period (30-90s for node-infra + daemon)

Update Step 2 polling to handle the waiting period:

```javascript
// After clicking "Update System"
async function installOs() {
    const resp = await fetch('/mender/install-os', {method: 'POST'});
    const data = await resp.json();
    if (!data.success) { showError(data.error); return; }

    // Poll for progress
    showStatus("Connecting to update server...");
    const poll = setInterval(async () => {
        const check = await fetch('/mender/check-os').then(r => r.json());

        if (check.installing) {
            if (check.state === 'downloading') showStatus("Downloading OS update...");
            else if (check.state === 'installing') showStatus("Installing OS update...");
        } else if (!check.update_available) {
            // Update complete (after reboot)
            clearInterval(poll);
            showStatus("Update complete!");
            wizard.advance();
        }
        // Otherwise still waiting for deployment — keep showing "Connecting..."
    }, 5000);
}
```

**File**: `retina-gui/templates/setup.html`

### Step 5: retina-gui — Clean up standalone OS code
- [ ] Remove `_run_install` reboot logic (OS path only — keep for app installs)
- [ ] Remove `/usr/share/mender/integration/reboot` reference
- [ ] Remove `needs_reboot` from `install_from_url` return (simplify to 2-tuple)
- [ ] Remove `--reboot-exit-code` flag from `install_from_url`
- [ ] Keep `install_from_url` for app updates only (standalone, no reboot)
- [ ] Keep `install.lock` for both OS and app updates (GUI guard)
- [ ] Clean up unused imports

**Files**: `retina-gui/src/app.py`, `retina-gui/src/mender.py`

### Step 6: owl-os — Verify (no changes needed)
- [ ] Confirm state scripts write flag file during managed OTA
- [ ] Confirm ConfigurationBackup/Restore runs (WiFi persists)
- [ ] Confirm debugfs fix enables mender services on OTA rootfs
- [ ] No systemd restore service needed

### Step 7: Testing
- [ ] Unit tests: mock responses in test_app.py (simplified install-os endpoint)
- [ ] node-infra tests: mock Mender API responses
- [ ] On-device test: fresh flash → wizard → managed OS update → verify new version
- [ ] Test: node-infra slow (30s wait) → wizard shows "Connecting..." gracefully
- [ ] Test: deployment fails → daemon rolls back → wizard shows error on resume
- [ ] Test: user power-cycles mid-install → daemon handles rollback
- [ ] Test: device already has latest OS → wizard shows "up to date" → auto-advance
- [ ] Test: app update still works via standalone mode (unaffected)

## Edge Cases

1. **node-infra 30s timer delay** — user sees "Connecting to update server..." for up to 90s (30s approve + 60s daemon poll). Normal first-boot UX.
2. **Device already has latest OS** — node-infra sees matching versions, skips deployment. Wizard shows "up to date".
3. **WiFi drops mid-download** — daemon retries automatically. Wizard polls and shows resumed progress.
4. **Deployment fails / rollback** — daemon rolls back, ArtifactFailure_Enter clears flag file. Wizard shows error on resume.
5. **User power-cycles during install** — daemon handles on next boot (rollback or retry).
6. **Multiple nodes onboarding simultaneously** — node-infra creates per-device deployments (unique names with timestamp). No conflicts.
7. **node-infra down** — device authenticates but never gets approved or deployed. Wizard stays on "Connecting..." until timeout → shows retry button.
8. **Mender server down** — daemon can't authenticate. `ensure_cloud_services_enabled` times out → wizard shows error.
9. **Artifact not on Mender server** — node-infra can't find artifact, skips deployment creation. Logs error. Wizard stays on "Connecting...".
10. **Device re-onboarding (already approved)** — node-infra skips approval but still checks OS version and creates deployment if outdated.

## Key Decisions

- **OS = managed, apps = standalone** — OS needs reboot + full state machine. Apps don't.
- **No `check-update` needed** — daemon polls naturally. 30-90s wait is acceptable for first-boot.
- **node-infra extended, not new service** — ~30 lines added to existing auto-approve script. Same timer, same deployment model.
- **No new API endpoints** — node-infra stays as a timer-based script, not a web server.
- **install.lock kept for both OS and app** — GUI-level guard prevents duplicate installs + blocks cloud toggle. State scripts clear on commit/failure. 40-min stale timeout as safety net.
- **retina-gui gets simpler** — install-os acquires lock, enables cloud services, returns. No subprocess, no reboot, no commit.
- **Wizard checkboxes stay as-is** — EULA, export compliance, cloud services in Step 1. Cloud services actually enabled in Step 2 when user clicks Install.
- **Skip button on OS update** — user can skip OS update and proceed to app install. Update can happen later via Mender dashboard.
- **Flag file for progress** — existing `mender-update.status` works for managed mode. State scripts write it.
- **GitHub for version discovery, Mender for deployment** — wizard checks GitHub for "is update available?", Mender handles the actual install.

## Dependencies

- node-infra running on server with systemd timer (already deployed)
- Mender management API PAT in node-infra .env (already configured)
- OS artifact uploaded to Mender server before new nodes can update
- mender-updated daemon enabled on OTA rootfs (debugfs fix — already done)
