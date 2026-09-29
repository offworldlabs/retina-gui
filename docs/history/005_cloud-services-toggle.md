# 005 - Cloud Services Toggle

## Overview
Add a "Cloud Services" toggle to retina-gui allowing users to enable/disable Mender cloud connectivity for privacy. When disabled, auth credentials are backed up offline (Level 2 security).

## User Story
As a privacy-conscious user, I want to disable cloud services so that my device doesn't phone home to Mender servers, while still being able to re-enable them later if needed.

## What Gets Toggled

**Mender Services:**
- `mender-authd` - Authentication daemon
- `mender-updated` - Update daemon
- `mender-connect` - Remote terminal daemon

**Auth Token:**
- Source: `/data/mender/authtoken`
- Backup: `/data/mender-cloud-disabled/authtoken`

## Default State
- **ON by default** - Services enabled, token in place
- Users can opt-out via toggle in the UI

---

## Implementation

### 1. Backend Endpoints (`app.py`)

#### GET `/mender/cloud-services`
Check current cloud services status.

```python
@app.route("/mender/cloud-services", methods=["GET"])
def cloud_services_status():
    """Check if Mender cloud services are enabled."""
    services = ["mender-authd", "mender-updated", "mender-connect"]
    service_status = {}

    for service in services:
        result = subprocess.run(
            ["systemctl", "is-active", service],
            capture_output=True,
            text=True,
            timeout=5
        )
        service_status[service] = result.returncode == 0

    # Check if token exists (not backed up)
    token_exists = os.path.exists("/data/mender/authtoken")
    any_service_active = any(service_status.values())

    return jsonify({
        "enabled": token_exists and any_service_active,
        "services": service_status
    })
```

#### POST `/mender/cloud-services`
Enable or disable cloud services.

```python
MENDER_SERVICES = ["mender-authd", "mender-updated", "mender-connect"]
TOKEN_PATH = "/data/mender/authtoken"
BACKUP_DIR = "/data/mender-cloud-disabled"
BACKUP_PATH = f"{BACKUP_DIR}/authtoken"

@app.route("/mender/cloud-services", methods=["POST"])
def cloud_services_toggle():
    """Enable or disable Mender cloud services."""
    data = request.get_json()
    enabled = data.get("enabled")

    if enabled is None:
        return jsonify({"success": False, "error": "Missing 'enabled' field"}), 400

    try:
        if enabled:
            # Restore token first
            if os.path.exists(BACKUP_PATH):
                shutil.move(BACKUP_PATH, TOKEN_PATH)

            # Enable and start services
            for service in MENDER_SERVICES:
                subprocess.run(["systemctl", "enable", service],
                    capture_output=True, timeout=10)
                subprocess.run(["systemctl", "start", service],
                    capture_output=True, timeout=10)
        else:
            # Stop and disable services
            for service in MENDER_SERVICES:
                subprocess.run(["systemctl", "stop", service],
                    capture_output=True, timeout=10)
                subprocess.run(["systemctl", "disable", service],
                    capture_output=True, timeout=10)

            # Backup token
            if os.path.exists(TOKEN_PATH):
                os.makedirs(BACKUP_DIR, exist_ok=True)
                shutil.move(TOKEN_PATH, BACKUP_PATH)

        return jsonify({"success": True})

    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "error": "Command timed out"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
```

### 2. Frontend UI (`templates/index.html`)

Add after "Restart Services" section, before "SSH Access":

```html
{% if retina_node_version %}
<h4 class="mt-4">Cloud Services</h4>
<div class="form-check form-switch mb-2">
    <input class="form-check-input" type="checkbox" id="cloudServicesToggle" checked>
    <label class="form-check-label" for="cloudServicesToggle">
        Enable cloud services
    </label>
</div>
<small class="text-muted d-block mb-3">
    Automatic updates and remote support. When disabled, auth credentials are stored offline.
</small>
<span id="cloudServicesStatus"></span>

<!-- Confirmation Modal -->
<div class="modal fade" id="disableCloudModal" tabindex="-1">
    <div class="modal-dialog">
        <div class="modal-content">
            <div class="modal-header">
                <h5 class="modal-title">Disable Cloud Services?</h5>
                <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
            </div>
            <div class="modal-body">
                <p>This will:</p>
                <ul>
                    <li>Stop automatic software updates</li>
                    <li>Disable remote support access</li>
                    <li>Store auth credentials offline</li>
                </ul>
                <p class="mb-0">You can re-enable at any time.</p>
            </div>
            <div class="modal-footer">
                <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Cancel</button>
                <button type="button" class="btn btn-warning" id="confirmDisableBtn">Disable</button>
            </div>
        </div>
    </div>
</div>

<script>
// Load current status
fetch('/mender/cloud-services')
    .then(r => r.json())
    .then(data => {
        document.getElementById('cloudServicesToggle').checked = data.enabled;
    });

// Handle toggle
document.getElementById('cloudServicesToggle').addEventListener('change', function() {
    const toggle = this;
    const status = document.getElementById('cloudServicesStatus');

    if (!toggle.checked) {
        // Show confirmation modal for disable
        toggle.checked = true; // Revert until confirmed
        const modal = new bootstrap.Modal(document.getElementById('disableCloudModal'));
        modal.show();
    } else {
        // Enable directly
        setCloudServices(true, toggle, status);
    }
});

// Confirm disable button
document.getElementById('confirmDisableBtn').addEventListener('click', function() {
    const toggle = document.getElementById('cloudServicesToggle');
    const status = document.getElementById('cloudServicesStatus');
    bootstrap.Modal.getInstance(document.getElementById('disableCloudModal')).hide();
    toggle.checked = false;
    setCloudServices(false, toggle, status);
});

function setCloudServices(enabled, toggle, status) {
    status.innerHTML = '<span class="spinner-border spinner-border-sm"></span>';

    fetch('/mender/cloud-services', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({enabled: enabled})
    })
    .then(r => r.json())
    .then(data => {
        if (data.success) {
            status.innerHTML = '<span class="text-success">Settings applied</span>';
            setTimeout(() => status.textContent = '', 3000);
        } else {
            status.innerHTML = '<span class="text-danger">' + data.error + '</span>';
            toggle.checked = !toggle.checked; // Revert on failure
        }
    })
    .catch(e => {
        status.innerHTML = '<span class="text-danger">Error: ' + e.message + '</span>';
        toggle.checked = !toggle.checked;
    });
}
</script>
{% endif %}
```

---

## Files to Modify

| File | Changes |
|------|---------|
| `app.py` | Add imports (shutil), constants, 2 routes (~70 lines) |
| `templates/index.html` | Add toggle UI, modal, JavaScript (~80 lines) |
| `tests/test_app.py` | Add tests for new endpoints (~50 lines) |

---

## Testing

### Manual Testing
1. **Disable flow:**
   - Toggle OFF, confirm modal
   - Verify services stopped: `systemctl status mender-authd mender-updated mender-connect`
   - Verify token moved: `ls /data/mender-cloud-disabled/`
   - Reboot, verify stays disabled

2. **Enable flow:**
   - Toggle ON
   - Verify services started
   - Verify token restored to `/data/mender/authtoken`

### Unit Tests
```python
class TestCloudServices:
    def test_get_status(self, app_client):
        with patch('subprocess.run') as mock:
            mock.return_value = MagicMock(returncode=0)
            response = app_client.get('/mender/cloud-services')
        assert response.status_code == 200
        assert 'enabled' in response.json

    def test_disable_services(self, app_client):
        with patch('subprocess.run') as mock, \
             patch('os.path.exists', return_value=True), \
             patch('shutil.move'):
            mock.return_value = MagicMock(returncode=0)
            response = app_client.post('/mender/cloud-services',
                json={"enabled": False})
        assert response.json['success'] is True

    def test_missing_enabled_field(self, app_client):
        response = app_client.post('/mender/cloud-services', json={})
        assert response.status_code == 400
```

---

## Version
- Target: retina-gui v0.1.8
- Branch: `feat/cloud-services-toggle`
