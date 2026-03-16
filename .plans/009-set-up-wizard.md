# 009: First-Boot Setup Wizard

## Context

Shipped SD cards may have an outdated owl-os. The current first-boot flow jumps straight to installing retina-node. We need a modular, full-page setup wizard at `/set-up` that walks the user through: agreements → OS update → app install → done.

Branch: `feat/set-up` from latest main.

## Design Overview

- **Homepage** (`/`): When `retina_node_version is None`, show a "Welcome to Retina Node — Launch Setup Wizard" button linking to `/set-up`
- **Wizard** (`/set-up`): Full-page, 4-step flow in a new `setup.html` template
  - Step 1: **Agreements & Cloud Services** — EULA, export compliance, enable cloud (all 3 checkboxes, "Continue" when all checked)
  - Step 2: **System Update** — Check owl-os version, update via Mender if needed, auto-advance if up to date
  - Step 3: **Radar Software** — Check retina-node version, install via Mender if needed, auto-advance if up to date
  - Step 4: **Setup Complete** — Success message + "Go to Dashboard" button → redirects to `/`
- Steps advance automatically on completion. User cannot scroll between steps freely. If a step (OS/app) is already up to date, show brief "Up to date" then auto-advance (or show a "Next" arrow to continue).

## UI Design

Consistent with homepage styling: Bootstrap 5, same navbar, same footer. The wizard content area replaces the normal page body with centered step content.

```
┌─────────────────────────────────────────────────────────┐
│  Retina Node                        Home | Config       │  ← same navbar as homepage
│  ─────────────────────────────────────────────────────  │
│                                                         │
│              ● ─── ○ ─── ○ ─── ○                        │  ← progress dots
│                                                         │
│         Agreements & Cloud Services                     │
│                                                         │
│         ☐ I accept the End User License Agreement       │
│         ☐ Export compliance acknowledgment              │
│         ☐ Enable cloud services                         │
│                                                         │
│                              [Continue →]               │
│                                                         │
│  ─────────────────────────────────────────────────────  │
│  Node: abc123 • owl-os: v0.1.0 • retina-node: N/A      │  ← same footer as homepage
└─────────────────────────────────────────────────────────┘
```

### Version comparison display (Steps 2 & 3)
```
         System Update

         Current: v0.1.0  →  Available: v0.2.0

         [Update System]
```
Always show both installed and available versions so the user knows what's happening.

### CSS approach
- Same navbar/footer as index.html (consistent branding)
- Max-width container (~600px) for wizard content
- Smooth fade transitions between steps (CSS on opacity)
- Progress indicator: small dots at top
- Bootstrap 5 base, minimal custom CSS

## Wizard State Persistence

The OS update causes a reboot. We need to save wizard progress so the user resumes at the right step after reconnecting.

**Mechanism**: Add wizard state methods to `DeviceState` class (`src/device_state.py`). Uses `{data_dir}/setup-wizard.json` — same directory as `install.lock` and other state files.

```python
# New file path in __init__:
self.setup_wizard_file = os.path.join(data_dir, "setup-wizard.json")

# New methods:
def get_setup_wizard_step(self) -> str | None:
    """Read saved wizard step. Returns None if no wizard in progress.
    Auto-clears if older than 24h (abandoned wizard)."""

def save_setup_wizard_step(self, step: str) -> None:
    """Save current wizard step to disk. JSON: {step, started_at}"""

def clear_setup_wizard(self) -> None:
    """Delete wizard state file (wizard complete)."""

def is_setup_wizard_in_progress(self) -> bool:
    """True if setup-wizard.json exists and is not stale."""
```

State file format:
```json
{
    "step": "system",
    "started_at": "2026-03-16T12:00:00Z"
}
```

- **Step 1 complete** → `device_state.save_setup_wizard_step("system")` before advancing
- **After reboot** → `GET /set-up` calls `device_state.get_setup_wizard_step()`, passes to template
- **Wizard complete** → `device_state.clear_setup_wizard()`
- **Stale timeout** → auto-clear after 24h in `get_setup_wizard_step()` (same pattern as install lock)
- **Homepage** → `device_state.is_setup_wizard_in_progress()` drives "Continue Setup" vs "Start Setup"

File: `src/device_state.py`

## Implementation Steps

### Step 0: Create plan file
Create `retina-gui/.plans/009-set-up-wizard.md` with this plan (version-tracked).

### Step 1: mender.py — Add OS version functions

Add below existing `get_latest_stable_from_github()`:

**`parse_os_version(tag)`** — Parse `os-v0.1.0`, `v0.1.0`, `0.1.0` → `(0, 1, 0)`. Returns `None` for rc/dev.
Regex: `r'^(?:os-)?v?(\d+)\.(\d+)\.(\d+)$'`

**`get_latest_owl_os_from_github(repo="offworldlabs/owl-os")`** — Query GitHub releases, filter `os-v*` tags, return highest stable semver. Returns `(tag, error)`.

File: `src/mender.py`

### Step 2: app.py — New routes

**`GET /set-up`** — Call `device_state.get_setup_wizard_step()` for saved step. Render `setup.html` with `resume_step` (defaults to 'agreements' if None). Pass `owl_os_version`, `retina_node_version`, `node_id` for the footer.

**`GET /mender/check-os`** — Mirrors `/mender/check` (reference: `feat/owl-os-update:src/app.py` `mender_check_os()`):
1. `is_any_update_in_progress()` → if true, return `{installing, version, started_at, reason}` (same shape as `/mender/check`)
2. Get current owl-os via `mender.get_versions()[0]`
3. Get latest from **GitHub** via `get_latest_owl_os_from_github()` (version discovery only)
4. Compare with `parse_os_version()`, return `{current_version, latest_version, update_available}`
5. On GitHub error → return `{error: "..."}` (surface to user like the app install does)

**`POST /mender/install-os`** — Mirrors `/mender/install` (reference: `feat/owl-os-update:src/app.py` `mender_install_os()`):
1. `ensure_cloud_services_enabled(mender.get_jwt)` — enable services, wait for JWT
2. `can_start_install()` guard — block if update in progress
3. Get latest OS tag from **GitHub** (version discovery)
4. Check if update needed: compare `parse_os_version(current)` vs `parse_os_version(latest)` — return error if already up to date
5. Map tag to Mender release name: `os-v0.2.0` → `owl-os-pi5-v0.2.0` (strip `os-`, prepend `owl-os-pi5-`)
6. `acquire_install_lock(release_name)`
7. Query **Mender server** for artifact: `list_artifacts(release_name=...)` → `get_download_url(artifact_id)` (signed URL)
8. Kick off `_run_install(url)` in **background thread** (reuse existing function — unlike the WIP which ran synchronously, we need the thread for polling to work)
9. Return `{success: true, version: ...}` immediately

Error handling at each step (same pattern as `/mender/install`): return `{success: false, error: "..."}` with appropriate messages for auth failure, no artifact found, already in progress, etc.

**Modified `GET /`** — Use the same existing condition (`retina_node_version is None`) to show the launcher button. If `setup-wizard.json` exists with step past agreements, the launcher text can say "Continue Setup" instead of "Start Setup".

**`POST /set-up/save-step`** — Call `device_state.save_setup_wizard_step(step)`. Called by JS before advancing steps.

**`POST /set-up/complete`** — Call `device_state.clear_setup_wizard()`. Called when wizard finishes.

File: `src/app.py`

### Step 3: setup.html — Full-page modular wizard template

**Architecture: step registry pattern.** Each step is a self-contained module registered with the wizard. Adding a new step requires only:
1. Add a `<div class="wizard-step" data-step="my-step">` HTML block
2. Register an enter hook: `wizard.registerStep('my-step', { enter: function() { ... } })`

That's it. No changes to wizard core code, no touching other steps.

```javascript
const wizard = {
    steps: [],           // ordered step names, built from DOM data-step attributes
    handlers: {},        // step name → {enter, exit} callbacks
    currentStep: 0,

    // Core (never needs changing)
    registerStep(name, handler) {
        this.handlers[name] = handler;
    },
    init(resumeStep) {
        // Discover steps from DOM: all elements with data-step attribute
        this.steps = Array.from(document.querySelectorAll('.wizard-step'))
            .map(el => el.dataset.step);
        // Resume at saved step or start at 0
        this.currentStep = resumeStep ? this.steps.indexOf(resumeStep) : 0;
        if (this.currentStep < 0) this.currentStep = 0;
        this.showStep(this.currentStep);
    },
    advance() {
        this.saveStep(this.steps[this.currentStep + 1]);
        this.currentStep++;
        this.showStep(this.currentStep);
    },
    showStep(index) {
        // Hide all steps, show target, update progress dots, call enter hook
        document.querySelectorAll('.wizard-step').forEach((el, i) => {
            el.style.display = i === index ? '' : 'none';
        });
        this.updateProgress(index);
        var name = this.steps[index];
        if (this.handlers[name] && this.handlers[name].enter) {
            this.handlers[name].enter();
        }
    },
    updateProgress(index) { /* update dot classes based on index */ },
    saveStep(stepName) {
        fetch('/set-up/save-step', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({step: stepName})
        });
    }
};
```

**HTML structure** — each step is a standalone block, order in DOM = order in wizard:
```html
<div class="wizard-step" data-step="agreements"> ... </div>
<div class="wizard-step" data-step="system"> ... </div>
<div class="wizard-step" data-step="radar"> ... </div>
<div class="wizard-step" data-step="complete"> ... </div>
```

**Progress dots** — auto-generated from step count:
```html
<div id="wizardProgress">
    <!-- JS generates dots from this.steps.length -->
</div>
```

**To add a future step** (e.g. "network" config between agreements and system):
1. Insert `<div class="wizard-step" data-step="network">` in the HTML at desired position
2. Add `wizard.registerStep('network', { enter: function() { ... } })`
3. Done — wizard discovers it from DOM, progress dots auto-update

#### Step behaviors:
- **Agreements**: "Continue →" enabled when all 3 checked. POSTs `/mender/cloud-services` `{enabled: true}`. Calls `wizard.advance()`.
- **System Update**: auto-checks on enter via `/mender/check-os`. Shows "Current: v0.1.0 → Available: v0.2.0" with "Update System" button. If up to date → show "Up to date (v0.2.0)" + "Next →" arrow. On install: poll `/mender/check-os` every 5s, show spinner. On complete → "Updated! Device is rebooting. Reconnect to retina.local/set-up" (wizard state saved, resumes here after reboot).
- **Radar Software**: same pattern via `/mender/check` and `/mender/install`. Shows "Current: Not installed → Available: v0.3.5" or "Radar software v0.3.5 installed" + "Next →". Polls every 5s during install. Auto-advances on complete.
- **Complete**: "Setup complete!" + "Go to Dashboard →". POSTs `/set-up/complete` to clear wizard state, navigates to `/`.

#### Shared utilities (reusable across steps):
```javascript
// Polling helper — any step can use this
function pollUntilDone(url, onUpdate, onDone, interval) { ... }

// Status display helper
function showStatus(el, type, message) { ... }  // type: 'info', 'warning', 'success', 'danger'
```

File: `templates/setup.html`

### Step 4: index.html — Simplify

Keep the existing `{% if not retina_node_version %}` condition (same logic as current homepage). Replace the install section contents with a launcher card:
```html
{% if not retina_node_version %}
<div class="card mb-4 text-center py-5">
    <div class="card-body">
        <h4>Welcome to Retina Node</h4>
        <p class="text-muted">Launch the setup wizard to get started.</p>
        <a href="/set-up" class="btn btn-primary btn-lg">
            {{ 'Continue Setup' if setup_in_progress else 'Start Setup' }}
        </a>
    </div>
</div>
{% endif %}
```
Remove all install JS (checkStatus, startPolling, checkbox logic) — moved to setup.html. Pass `setup_in_progress` (from `device_state.is_setup_wizard_in_progress()`) from the index route.

File: `templates/index.html`

### Step 5: Tests

**test_mender.py**: `TestParseOsVersion` (~8 tests), `TestGetLatestOwlOsFromGitHub` (~5 tests)
**test_app.py**: `TestSetupRoute`, `TestMenderCheckOs`, `TestMenderInstallOs`, `TestIndexSetupNeeded`

## Files to modify

| File | Change |
|------|--------|
| `.plans/009-set-up-wizard.md` | Create (this file) |
| `src/mender.py` | Add `parse_os_version()`, `get_latest_owl_os_from_github()` |
| `src/device_state.py` | Add wizard state methods: `get/save/clear_setup_wizard_step`, `is_setup_wizard_in_progress` |
| `src/app.py` | Add `/set-up`, `/mender/check-os`, `/mender/install-os`; modify index route |
| `templates/setup.html` | **Create** — full-page wizard template |
| `templates/index.html` | Replace install section with launcher card, remove install JS |
| `tests/test_mender.py` | Add OS version tests |
| `tests/test_app.py` | Add wizard + endpoint tests |

## Key decisions

- **Separate template** — `setup.html` keeps index clean and wizard self-contained
- **Modern form UI** — full-page, centered, one step at a time, smooth transitions
- **Step registry pattern** — wizard discovers steps from DOM `data-step` attributes. Adding a step = 1 HTML block + 1 `registerStep()` call. No wizard core changes needed.
- **Auto-advance** — steps that don't need action skip automatically
- **Shared utilities** — `pollUntilDone()` and `showStatus()` reusable across any step
- **Cloud services in Step 1** — needed before Steps 2/3 can talk to Mender
- **GitHub for version discovery, Mender for download** — GitHub releases API determines latest available version; Mender server provides signed artifact URL for actual install
- **Background thread for installs** — HTTP returns quickly, polling shows progress (fixes WIP which ran synchronously)
- **Homepage launcher** — clean entry point using existing `{% if not retina_node_version %}` condition, no wizard logic on index page
- **Wizard state on disk** — `setup-wizard.json` survives OS update reboot, resumes at correct step

## Phased Workplan

### Phase 1: Backend — Version Discovery (`src/mender.py`) ✅
- [x] Add `parse_os_version(tag)` — regex for `os-v*`, `v*`, bare version strings
- [x] Add `get_latest_owl_os_from_github(repo)` — GitHub releases API, filter `os-v*`, highest semver
- [x] Write tests: `TestParseOsVersion` (8 tests) in `tests/test_mender.py`
- [x] Write tests: `TestGetLatestOwlOsFromGitHub` (5 tests) in `tests/test_mender.py`
- [x] Run tests, confirm all pass (38/38)

### Phase 2: Backend — Wizard State (`src/device_state.py`)
- [ ] Add `self.setup_wizard_file` path in `__init__`
- [ ] Add `get_setup_wizard_step()` — read JSON, return step name or None, 24h stale timeout
- [ ] Add `save_setup_wizard_step(step)` — write `{step, started_at}` JSON
- [ ] Add `clear_setup_wizard()` — delete file
- [ ] Add `is_setup_wizard_in_progress()` — bool wrapper
- [ ] Write tests: `TestSetupWizardState` in `tests/test_device_state.py`
- [ ] Run tests, confirm all pass

### Phase 3: Backend — API Endpoints (`src/app.py`)
- [ ] Add `GET /set-up` route — read wizard step, render `setup.html` with `resume_step` + footer vars
- [ ] Add `GET /mender/check-os` — mirror `/mender/check` for owl-os (GitHub version discovery, install lock check)
- [ ] Add `POST /mender/install-os` — mirror `/mender/install` for owl-os (GitHub version → Mender artifact → background thread)
- [ ] Add `POST /set-up/save-step` — call `device_state.save_setup_wizard_step()`
- [ ] Add `POST /set-up/complete` — call `device_state.clear_setup_wizard()`
- [ ] Modify `GET /` — pass `setup_in_progress` to template
- [ ] Add imports: `parse_os_version`, `get_latest_owl_os_from_github`
- [ ] Write tests: `TestSetupRoute`, `TestMenderCheckOs`, `TestMenderInstallOs` in `tests/test_app.py`
- [ ] Run tests, confirm all pass

### Phase 4: Frontend — Setup Wizard (`templates/setup.html`)
- [ ] Create `setup.html` with same navbar/footer as `index.html`
- [ ] Add progress dots (4 steps) with CSS
- [ ] Add wizard JS object: `steps[]`, `advance()`, `showStep()`, enter hooks
- [ ] Step 1 HTML: 3 checkboxes + "Continue →" button
- [ ] Step 1 JS: `enterAgreements()` — checkbox listeners, POST `/mender/cloud-services`, save step, advance
- [ ] Step 2 HTML: version comparison display + "Update System" button + status span
- [ ] Step 2 JS: `enterSystem()` — fetch `/mender/check-os`, show update or auto-advance, polling on install
- [ ] Step 3 HTML: version comparison display + "Install Software" button + status span
- [ ] Step 3 JS: `enterRadar()` — fetch `/mender/check`, show install or auto-advance, polling on install
- [ ] Step 4 HTML: success message + "Go to Dashboard →" button
- [ ] Step 4 JS: `enterComplete()` — POST `/set-up/complete`, navigate to `/`
- [ ] Resume logic: read `resume_step` from template, start wizard at correct step on page load
- [ ] Visual test: boot dev server, walk through wizard manually

### Phase 5: Frontend — Homepage Update (`templates/index.html`)
- [ ] Replace `{% if not retina_node_version %}` install section with launcher card
- [ ] Show "Continue Setup" vs "Start Setup" based on `setup_in_progress`
- [ ] Remove all install JS (checkStatus, startPolling, checkbox handlers)
- [ ] Visual test: confirm launcher renders, links to `/set-up`

### Phase 6: Integration Testing
- [ ] Run full test suite — all existing + new tests pass
- [ ] Manual walkthrough: homepage → wizard → agreements → system → radar → complete → dashboard
- [ ] Test page refresh mid-install (both OS and app) — resumes correctly
- [ ] Test auto-advance when OS/app already up to date
- [ ] Test error states: GitHub unreachable, Mender auth failure, no artifact found

## Verification

1. `PORT=5050 python src/app.py` — homepage shows launcher, `/set-up` renders wizard
2. Step 1: check all boxes → Continue advances to Step 2
3. Step 2: mock old OS → update button; mock current OS → auto-advances
4. Step 3: mock no retina-node → install button; mock installed → auto-advances
5. Step 4: "Go to Dashboard" → homepage shows normal dashboard
6. Page refresh mid-install → wizard resumes at correct step with spinner
7. All tests pass
