# 008 - First-Boot OS Update Wizard

## Problem

Currently, when a user first boots a Retina Node, the home page immediately
offers to install the retina-node radar stack. A shipped SD card may have an
outdated or incompatible owl-os version. Installing the radar stack on an
old OS can cause breakage.

## Goal

Add a two-step setup wizard to the first-boot experience:
1. Check owl-os version and update if needed
2. Only then allow radar stack installation

---

## Design

### Stateless Step Detection

Determine the active wizard step from device state — no extra files needed:

```python
owl_os, retina_node = mender.get_versions()
latest_os = get_latest_owl_os_from_github()

if retina_node is not None:
    wizard = None          # hide wizard, show normal home
elif os_needs_update(owl_os, latest_os):
    wizard = "step1"       # OS update needed
else:
    wizard = "step2"       # OS current, install radar stack
```

After an OS update + reboot, the device comes back with the new version →
Step 1 auto-completes → Step 2 is immediately active.

### Step 1: System Update

- Shows current vs available OS version
- Single "Update System" button, no consent checkboxes
- Warning: "Device will reboot after update. Reconnect to retina.local."
- Cloud services enabled silently (no checkbox) to authenticate with Mender
- Downloads `.mender` artifact from **Mender server** (same flow as application install)
- Runs `mender-update install <mender-signed-url>`
- Mender handles the reboot automatically

### Step 2: Radar Stack Install

- Same as current flow: EULA, export compliance, cloud services checkboxes
- "Install Software" button enabled only when all checked
- Existing `/mender/install` endpoint handles the install

---

## UI Mockups (Bootstrap 5, no extra libraries)

**Step 1 active:**
```
┌──────────────────────────────────────────────────────────┐
│  Welcome to Retina Node                                  │
│  Let's get you set up.                                   │
│                                                          │
│  ● 1. System Update          ○ 2. Radar Software         │
│  ─────────────────────────────────────────────────────── │
│                                                          │
│  A system update is available.                           │
│  Current: v0.1.0  →  Available: v0.2.0                   │
│                                                          │
│  [Update System]                                         │
│                                                          │
│  ℹ Device will reboot after update. Reconnect to         │
│    retina.local to continue setup.                       │
└──────────────────────────────────────────────────────────┘
```

**Step 1 complete, Step 2 active:**
```
┌──────────────────────────────────────────────────────────┐
│  Welcome to Retina Node                                  │
│  Let's get you set up.                                   │
│                                                          │
│  ✓ 1. System Update          ● 2. Radar Software         │
│  ─────────────────────────────────────────────────────── │
│                                                          │
│  Now let's install the passive radar software.           │
│  Available: v0.3.5 (~600MB, takes 5-10 minutes)          │
│                                                          │
│  ☐ Accept End User License Agreement                     │
│  ☐ Device will not be exported from the USA              │
│  ☐ Enable cloud services for updates and remote support  │
│                                                          │
│  [Install Software]                                      │
└──────────────────────────────────────────────────────────┘
```

**OS already current on first visit:**
- Step 1 shows ✓ (auto-completed)
- Step 2 immediately active

---

## Backend Changes

### `mender.py` — New functions

```python
def parse_os_version(tag: str) -> tuple[int, ...] | None:
    """Parse 'os-v0.1.0' or '0.1.0' → (0, 1, 0). Returns None for rc/dev."""
    match = re.match(r'^(?:os-)?v?(\d+)\.(\d+)\.(\d+)$', tag)
    if match:
        return tuple(int(x) for x in match.groups())
    return None

def get_latest_owl_os_from_github(
    repo: str = "offworldlabs/owl-os",
) -> tuple[str | None, str | None]:
    """Get latest stable owl-os version from GitHub releases.
    Returns (version_tag, error). version_tag like 'os-v0.2.0'."""
    # Query GitHub releases API
    # Filter for tags matching os-v*.*.*  (exclude rc, dev)
    # Return highest semver

def get_owl_os_download_url(version_tag: str, repo="offworldlabs/owl-os") -> str | None:
    """Get .mender asset download URL from GitHub release.
    Queries release assets, finds owl-os-pi5-v{version}.mender file."""
    # GET /repos/{repo}/releases/tags/{version_tag}
    # Find asset named owl-os-pi5-v{version}.mender
    # Return browser_download_url
```

### `app.py` — New endpoints + modified index route

```python
@app.route("/mender/check-os")
def mender_check_os():
    """Check owl-os version: current vs latest available."""
    owl_os, _ = mender.get_versions()
    latest_tag, error = get_latest_owl_os_from_github()
    if error:
        return jsonify({"error": error})

    current_ver = parse_os_version(owl_os) if owl_os else None
    latest_ver = parse_os_version(latest_tag) if latest_tag else None
    update_needed = current_ver is None or (latest_ver and current_ver < latest_ver)

    return jsonify({
        "current_version": owl_os,
        "latest_version": latest_tag,
        "update_available": update_needed,
    })

@app.route("/mender/install-os", methods=["POST"])
def mender_install_os():
    """Install owl-os update from GitHub release."""
    # 1. Get latest version from GitHub
    # 2. Get .mender download URL from GitHub release assets
    # 3. Acquire install lock
    # 4. Run mender-update install <url>
    # 5. Mender handles reboot
```

Modified index route — pass OS version info to template:
```python
@app.route("/")
def index():
    owl_os, retina_node = mender.get_versions()
    # ... existing code ...

    os_update_available = False
    latest_owl_os = None
    if retina_node is None:
        latest_tag, _ = get_latest_owl_os_from_github()
        if latest_tag:
            latest_owl_os = latest_tag
            current = parse_os_version(owl_os)
            latest = parse_os_version(latest_tag)
            os_update_available = current is None or (latest and current < latest)

    return render_template("index.html", ...,
        os_update_available=os_update_available,
        latest_owl_os=latest_owl_os)
```

### `index.html` — Stepper wizard

Replace the `{% if not retina_node_version %}` block with a stepper:
- CSS step indicators (circles with numbers/checkmarks)
- Show Step 1 content if `os_update_available`
- Show Step 2 content if not `os_update_available` (OS is current)
- Step 1 JS: `fetch('/mender/install-os')` on button click
- Step 2 JS: existing install flow (checkboxes + `/mender/install`)

### `device_state.py` — No changes

Reuse existing install lock mechanism. Lock with `owl-os-pi5-v{version}`.

---

## Files to Modify

| File | Change |
|------|--------|
| `retina-gui/src/mender.py` | Add `parse_os_version()`, `get_latest_owl_os_from_github()`, `get_owl_os_download_url()` |
| `retina-gui/src/app.py` | Add `/mender/check-os`, `/mender/install-os` endpoints; pass OS data to template |
| `retina-gui/templates/index.html` | Replace install section with stepper wizard |
| `retina-gui/tests/` | Tests for OS version parsing, GitHub query, new endpoints |

---

## Scope

- **First-boot only** — wizard shows when `retina_node_version is None`
- Returning users with outdated OS (retina-node already installed) are out of scope

---

## Verification

1. **Local dev**: Wizard renders with mock version data. Step indicators work.
2. **On device (old OS)**: Flash old image → Step 1 shows → Update → reboot → Step 2 shows → install stack
3. **On device (current OS)**: Step 1 auto-completes → Step 2 immediately active
4. **Edge cases**: GitHub unreachable → graceful error message

---

## Open Questions

- [ ] Is `offworldlabs/owl-os` repo public? (needed for unauthenticated GitHub release downloads)
- [ ] Confirm `.mender` asset filename convention: `owl-os-pi5-v{version}.mender`
- [ ] Should Step 1 show a progress bar / spinner during the download+install phase?
