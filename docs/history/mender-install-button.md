# Plan: Mender Install Button for Device-Initiated OTA

## Goal
Add an "Install Software" button to retina-gui that allows the device to pull and install the retina-node artifact from Mender, using the device's existing authentication.

## Context
- Device already has mender-auth running, which stores a JWT token locally
- Mender Device API allows listing and downloading artifacts
- Button should only appear when `retina_node_version` is None (not yet installed)

## User Flow
```
┌─────────────────────────────────────────────────────┐
│  Retina Node                           [Home|Config]│
├─────────────────────────────────────────────────────┤
│  [Install Software]  ← Only shown if not installed  │
│  ─────────────────────────────────────────────────  │
│  Services (greyed out until installed)              │
│  • blah2 - Passive Radar                            │
│  • tar1090 - ADS-B Map                              │
├─────────────────────────────────────────────────────┤
│  node123 • owl-os: v0.5.0 • retina-node: Not installed │
└─────────────────────────────────────────────────────┘
```

After clicking Install:
- Spinner + "Installing... this may take a few minutes"
- On success: page refresh shows services, footer shows version
- On error: show error message

## Artifact Version Selection
- Artifacts named like: `retina-node-v0.3.2`
- Must filter out release candidates (e.g., `retina-node-v0.3.2-rc1`)
- Auto-select latest stable version (no user selection needed)

```python
import re

def parse_retina_node_version(artifact_name):
    """Extract version from 'retina-node-v0.3.2' format. Returns None if not stable."""
    match = re.match(r'^retina-node-v(\d+\.\d+\.\d+)$', artifact_name)
    if match:
        return tuple(int(x) for x in match.group(1).split('.'))
    return None  # Not a stable version (has suffix like -rc1)
```

## Technical Approach

### 1. Backend Functions (app.py)

```python
# Mender configuration
MENDER_SERVER_URL = os.environ.get('MENDER_SERVER_URL', 'https://hosted.mender.io')
MENDER_AUTH_TOKEN_PATH = '/var/lib/mender/authtoken'

def get_mender_jwt():
    """Read device's Mender JWT token from mender-auth."""
    try:
        with open(MENDER_AUTH_TOKEN_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None

def get_available_artifacts():
    """List artifacts available for this device from Mender."""
    jwt = get_mender_jwt()
    if not jwt:
        return None, "Device not authenticated with Mender"

    try:
        resp = requests.get(
            f"{MENDER_SERVER_URL}/api/devices/v1/deployments/artifacts",
            headers={"Authorization": f"Bearer {jwt}"},
            timeout=30
        )
        if resp.status_code != 200:
            return None, f"Mender API error: {resp.status_code}"
        return resp.json(), None
    except Exception as e:
        return None, str(e)

def install_artifact(artifact_id):
    """Download and install artifact via mender-update."""
    jwt = get_mender_jwt()
    if not jwt:
        return False, "Device not authenticated with Mender"

    # Get download URL for artifact
    try:
        resp = requests.get(
            f"{MENDER_SERVER_URL}/api/devices/v1/deployments/artifacts/{artifact_id}/download",
            headers={"Authorization": f"Bearer {jwt}"},
            timeout=30
        )
        if resp.status_code != 200:
            return False, f"Failed to get download URL: {resp.status_code}"

        download_url = resp.json().get('uri')
        if not download_url:
            return False, "No download URL in response"

        # Install via mender-update
        result = subprocess.run(
            ['mender-update', 'install', download_url],
            capture_output=True, text=True, timeout=600  # 10 min timeout
        )

        if result.returncode != 0:
            return False, result.stderr or "Install failed"

        return True, None
    except subprocess.TimeoutExpired:
        return False, "Installation timed out"
    except Exception as e:
        return False, str(e)
```

### 2. API Endpoint (app.py)

```python
@app.route('/mender/install', methods=['POST'])
def mender_install():
    """Install retina-node artifact from Mender."""
    # Check if already installed
    _, retina_node_version = get_mender_versions()
    if retina_node_version:
        return jsonify({"success": False, "error": "Already installed"})

    # Get available artifacts
    artifacts, error = get_available_artifacts()
    if error:
        return jsonify({"success": False, "error": error})

    # Find retina-node artifact
    retina_artifact = None
    for artifact in artifacts:
        if 'retina-node' in artifact.get('artifact_name', ''):
            retina_artifact = artifact
            break

    if not retina_artifact:
        return jsonify({"success": False, "error": "No retina-node artifact found"})

    # Install it
    success, error = install_artifact(retina_artifact['id'])
    if not success:
        return jsonify({"success": False, "error": error})

    return jsonify({"success": True})
```

### 3. UI (index.html)

Add install button section (only shown when not installed):

```html
{% if not retina_node_version %}
<div class="alert alert-info mb-4">
    <h5>Welcome to Retina Node</h5>
    <p>Click below to download and install the radar software.</p>
    <button type="button" class="btn btn-primary" id="installBtn">
        Install Software
    </button>
    <span id="installStatus" class="ms-2"></span>
</div>

<script>
document.getElementById('installBtn').addEventListener('click', function() {
    const btn = this;
    const status = document.getElementById('installStatus');

    if (btn.disabled) return;

    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Installing...';
    status.textContent = 'This may take a few minutes';

    fetch('/mender/install', { method: 'POST' })
        .then(r => r.json())
        .then(data => {
            if (data.success) {
                status.className = 'text-success';
                status.textContent = 'Installed! Reloading...';
                setTimeout(() => location.reload(), 2000);
            } else {
                status.className = 'text-danger';
                status.textContent = data.error || 'Installation failed';
                btn.disabled = false;
                btn.textContent = 'Install Software';
            }
        })
        .catch(e => {
            status.className = 'text-danger';
            status.textContent = 'Error: ' + e.message;
            btn.disabled = false;
            btn.textContent = 'Install Software';
        });
});
</script>
{% endif %}
```

## Files to Modify

1. **retina-gui/app.py**
   - Add `requests` import (if not already)
   - Add Mender config constants
   - Add `get_mender_jwt()` function
   - Add `get_available_artifacts()` function
   - Add `install_artifact()` function
   - Add `/mender/install` endpoint

2. **retina-gui/templates/index.html**
   - Add install button section (conditional on `retina_node_version`)
   - Add JavaScript for install button

3. **retina-gui/requirements.txt** (if needed)
   - Ensure `requests` is listed

## Testing

### Local (dev environment)
- Button should appear (no mender-update)
- Click should fail gracefully with "Device not authenticated" or similar
- Services section should be visible (for testing layout)

### On Device
1. Flash fresh owl-os image
2. Connect to retina.local
3. Verify footer shows "retina-node: Not installed"
4. Click "Install Software"
5. Wait for installation (may take several minutes)
6. Page should refresh, footer shows version
7. Services should now be accessible
8. Config page should unlock

## Open Questions
- [ ] Verify JWT token path on actual device (`/var/lib/mender/authtoken` or different?)
- [ ] Confirm artifact filtering logic (name contains 'retina-node'?)
- [ ] Need to add `requests` to requirements.txt?
