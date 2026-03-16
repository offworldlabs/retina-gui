# 007 - Manual Test: Mender State Scripts

Verification that `mender-update.status` is correctly written/cleared
during all update types. Run after deploying an owl-os image built with
the state scripts from Phase 1.

## Prerequisites

- Device running owl-os image built with state scripts
- SSH access to device
- Mender cloud services enabled
- A retina-node release available on Mender server
- An owl-os release available on Mender server

---

## TEST 0: Verify scripts are deployed

```bash
ssh retina@<device>

# Check scripts exist with correct permissions
ls -la /etc/mender/scripts/
# Expected: 4 files, all -rwxr-xr-x
#   Download_Enter_00_retina_state
#   ArtifactInstall_Enter_00_retina_state
#   ArtifactCommit_Leave_00_retina_state
#   ArtifactFailure_Enter_00_retina_state

# Check no stale status file from build
ls /data/retina-gui/mender-update.status
# Expected: No such file or directory

# Check directory exists
ls -d /data/retina-gui/
# Expected: exists
```

- [ ] Scripts deployed with correct permissions
- [ ] No stale status file
- [ ] `/data/retina-gui/` directory exists

---

## TEST 1: Server-pushed APPLICATION update (retina-node)

```bash
# Terminal 1: Watch the status file
ssh retina@<device>
watch -n 0.5 'echo "---"; ls -la /data/retina-gui/mender-update.status 2>&1; echo "---"; cat /data/retina-gui/mender-update.status 2>&1'

# Terminal 2: Trigger deployment from Mender UI
#   Deployments > Create deployment > retina-node artifact > target device
```

Observe in Terminal 1:
1. File appears with `{"state":"downloading","ts":"..."}`
2. File changes to `{"state":"installing","ts":"..."}`
3. File disappears

- [ ] Download state detected
- [ ] Install state detected
- [ ] File cleaned up on success

---

## TEST 2: Server-pushed OS update (owl-os rootfs)

```bash
# Terminal 1: Watch the status file
ssh retina@<device>
watch -n 0.5 'cat /data/retina-gui/mender-update.status 2>&1'

# Terminal 2: Trigger OS deployment from Mender UI
#   Deployments > Create deployment > owl-os artifact > target device
```

Observe:
1. File appears with `{"state":"downloading","ts":"..."}`
2. File changes to `{"state":"installing","ts":"..."}`
3. Device reboots (SSH disconnects)

After reboot:
```bash
ssh retina@<device>

# Status file may briefly exist during commit window
cat /data/retina-gui/mender-update.status

# Wait for auto-commit, then:
ls /data/retina-gui/mender-update.status
# Expected: No such file
```

- [ ] Download state detected
- [ ] Install state detected
- [ ] File survives reboot (persisted in `/data/`)
- [ ] File cleaned up after commit

---

## TEST 3: Failed deployment (abort/rollback)

Option A: Abort from Mender UI during download
Option B: Deploy an artifact with wrong device type (will fail)

```bash
watch -n 0.5 'cat /data/retina-gui/mender-update.status 2>&1'
```

- [ ] Status file appeared during attempt
- [ ] File cleaned up on failure (`ArtifactFailure_Enter`)

---

## TEST 4: Standalone mode (GUI-initiated install)

Tests that `ArtifactInstall_Enter` fires in standalone mode but
`Download_Enter` does NOT (standalone skips download state).

```bash
# Watch the file
watch -n 0.5 'cat /data/retina-gui/mender-update.status 2>&1'

# From retina-gui, trigger a manual install
# OR from SSH:
#   mender-update install <artifact-url>
```

- [ ] No "downloading" state (correct for standalone mode)
- [ ] "installing" state detected
- [ ] File cleaned up on success

---

## Results

| Test | Status | Date | Notes |
|------|--------|------|-------|
| 0 - Scripts deployed | | | |
| 1 - App update (server-pushed) | | | |
| 2 - OS update (server-pushed) | | | |
| 3 - Failed deployment | | | |
| 4 - Standalone (GUI) | | | |
