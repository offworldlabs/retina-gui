# Config GUI Implementation Plan

## Overview

Add a configuration editing interface to retina-gui with **auto-generated forms from Pydantic models**. Forms render dynamically based on field metadata - adding a new field to the schema auto-adds it to the UI.

**MVP Scope: Capture settings only** - Start with `capture:` section, extend to location/tar1090/truth later.

## Goals

- **Dynamic form generation** - Forms auto-generated from Pydantic Field metadata
- **Display values from user.yml** - Form shows current config, not schema defaults
- Validate inputs before saving
- "Apply Changes" button triggers config-merger + service restart with status feedback
- Gracefully handle retina-node not installed (grey out config, show message)

## Tech Stack

| Component | Choice | Rationale |
|-----------|--------|-----------|
| Validation | Pydantic | Type-safe, built-in validation, Field metadata for form constraints |
| Config Format | YAML | Already used by retina-node config-merger |
| Form Rendering | Jinja2 + dynamic macros | Auto-generate from Pydantic schema |
| Docker Calls | Direct (no sudo) | retina-gui runs as root via systemd |
| Apply Feedback | Vanilla JS | Simple spinner + status, no extra deps |

## Architecture

### Config Flow (existing)
```
user.yml → config-merger → config.yml + tar1090.env → docker services
```

### File Locations (on device)
```
/data/retina-node/config/user.yml           # User edits (we read/write this)
/data/retina-node/config/config.yml         # Merged output (read-only)
/data/mender-app/retina-node/manifests/     # Docker compose location
```

### Schema Definition (Pydantic) - Validation & Metadata Only

Pydantic models define:
- **Field types** (int, bool, str) → determines HTML input type
- **Constraints** (ge, le) → HTML min/max attributes
- **Metadata** (title, description) → form labels and help text

**NOT defaults for the form** - form values come from `user.yml`.

```python
# config_schema.py
from pydantic import BaseModel, Field
from typing import Optional, Literal

class CaptureDevice(BaseModel):
    """Validation schema - constraints only, values come from user.yml"""
    type: Literal["RspDuo"] = Field(title="Device Type")
    agcSetPoint: int = Field(le=0, title="AGC Set Point", description="dBFS")
    gainReduction: int = Field(ge=20, le=59, title="Gain Reduction", description="20-59 dB")
    lnaState: int = Field(ge=1, le=9, title="LNA State", description="1-9")
    dabNotch: bool = Field(title="DAB Notch Filter")
    rfNotch: bool = Field(title="RF Notch Filter")
    bandwidthNumber: int = Field(title="Bandwidth Number")

class CaptureConfig(BaseModel):
    fs: int = Field(title="Sample Rate", description="Hz")
    fc: int = Field(title="Center Frequency", description="Hz")
    device: CaptureDevice = Field(title="Device Settings")

class UserConfig(BaseModel):
    capture: Optional[CaptureConfig] = Field(default=None, title="Capture Settings")
    # Future: location, tar1090, truth sections
```

### Data Flow: user.yml → Form → Validation → user.yml

```
1. Load user.yml as dict
2. Pass dict values to schema_to_form_fields()
3. Render form with values from user.yml
4. User submits form
5. Validate with Pydantic (constraints from schema)
6. Save validated dict back to user.yml
```

### Dynamic Form Utilities

```python
# form_utils.py
def get_field_input_type(field_info):
    """Map Pydantic field type to HTML input type."""
    annotation = field_info.annotation
    # Handle Optional[X] by extracting X
    if hasattr(annotation, '__origin__') and annotation.__origin__ is Union:
        annotation = [a for a in annotation.__args__ if a is not type(None)][0]

    if annotation == bool:
        return "checkbox"
    elif annotation in (int, float):
        return "number"
    else:
        return "text"

def schema_to_form_fields(model_class, values: dict):
    """
    Convert Pydantic model to form field dicts.

    Args:
        model_class: Pydantic model class (for field metadata)
        values: Current values from user.yml (what to display)

    Returns:
        List of field dicts for Jinja template
    """
    fields = []
    for name, field_info in model_class.model_fields.items():
        # Handle nested models recursively
        if hasattr(field_info.annotation, 'model_fields'):
            nested_values = values.get(name, {}) or {}
            fields.append({
                'name': name,
                'title': field_info.title or name,
                'type': 'group',
                'fields': schema_to_form_fields(field_info.annotation, nested_values)
            })
        else:
            # Get constraints from field metadata
            constraints = {}
            for meta in field_info.metadata:
                if hasattr(meta, 'ge'):
                    constraints['min'] = meta.ge
                if hasattr(meta, 'le'):
                    constraints['max'] = meta.le

            fields.append({
                'name': name,
                'title': field_info.title or name,
                'description': field_info.description,
                'type': get_field_input_type(field_info),
                'value': values.get(name),  # From user.yml, NOT schema default
                **constraints,
            })
    return fields
```

### Jinja Template (Dynamic Recursive Macro)

```html
{% macro render_field(field, prefix='') %}
  {% set field_name = prefix ~ field.name if prefix else field.name %}
  {% if field.type == 'group' %}
    <fieldset>
      <legend>{{ field.title }}</legend>
      {% for subfield in field.fields %}
        {{ render_field(subfield, field_name ~ '.') }}
      {% endfor %}
    </fieldset>
  {% elif field.type == 'checkbox' %}
    <label class="checkbox">
      <input type="checkbox" name="{{ field_name }}" {% if field.value %}checked{% endif %}>
      {{ field.title }}
    </label>
  {% else %}
    <label>{{ field.title }}
      <input type="{{ field.type }}" name="{{ field_name }}"
        value="{{ field.value if field.value is not none else '' }}"
        {% if field.min is defined %}min="{{ field.min }}"{% endif %}
        {% if field.max is defined %}max="{{ field.max }}"{% endif %}>
      {% if field.description %}<small>{{ field.description }}</small>{% endif %}
    </label>
  {% endif %}
{% endmacro %}
```

### Retina-Node Detection

```python
def is_retina_node_installed():
    """Check if retina-node stack is deployed."""
    return os.path.exists("/data/mender-app/retina-node/manifests/docker-compose.yaml")
```

If not installed:
- Config section is greyed out / disabled
- Show message: "Configuration available after retina-node is deployed"
- SSH key management still works

### Apply Changes Flow

1. User clicks "Apply Changes"
2. Frontend shows spinner/status
3. Backend runs (no sudo - retina-gui runs as root):
   ```python
   subprocess.run([
       "docker", "compose", "-p", "retina-node",
       "run", "--rm", "config-merger"
   ], cwd="/data/mender-app/retina-node/manifests", check=True)

   subprocess.run([
       "docker", "compose", "-p", "retina-node",
       "up", "-d", "--force-recreate"
   ], cwd="/data/mender-app/retina-node/manifests", check=True)
   ```
4. Return success/failure to frontend
5. User can retry on failure

## Implementation Steps

### Phase 1: Pydantic Schema + Dynamic Form Utils

**Files to create:**
- `config_schema.py` - Pydantic models (validation constraints + metadata only)
- `form_utils.py` - `schema_to_form_fields()` for model → form conversion

**Tasks:**
1. Create Pydantic models with Field constraints (ge, le) and metadata (title, description)
2. Create `schema_to_form_fields()` that takes model class + values dict
3. Add `load_user_config()` and `save_user_config()` for YAML I/O
4. Add `is_retina_node_installed()` check

### Phase 2: Config Form UI

**Files to modify:**
- `templates/index.html` - Add config section with dynamic form
- `app.py` - Add template context for form fields

**Tasks:**
1. Create recursive Jinja macro `render_field()` for dynamic form rendering
2. Add config section below SSH keys
3. Load values from user.yml, pass to `schema_to_form_fields()`
4. If retina-node not installed: greyed-out with message
5. Display validation errors inline

### Phase 3: Save + Apply Logic

**Files to modify:**
- `app.py` - Add POST routes

**Tasks:**
1. `POST /config` - Parse nested form data, validate with Pydantic, save to user.yml
2. `POST /config/apply` - Run docker compose commands
3. Frontend: "Apply Changes" button with spinner + status (vanilla JS fetch)

### Phase 4: owl-os Update (Separate PR)

**Files to modify:**
- `owl-os/plugins/playbooks/os_setup/roles/radar_packages/tasks/main.yml`

**Tasks:**
1. Add `python3-pydantic` and `python3-yaml` to apt packages

## Files Summary

| File | Action | Purpose |
|------|--------|---------|
| `config_schema.py` | Create | Pydantic models (constraints + metadata) + YAML I/O |
| `form_utils.py` | Create | Schema → form field conversion |
| `app.py` | Modify | Add config routes |
| `templates/index.html` | Modify | Add dynamic config form |

## Testing Strategy

### Unit Tests (`tests/test_config_schema.py`)

**Schema Validation:**
```python
import pytest
from config_schema import CaptureDevice, CaptureConfig, UserConfig
from pydantic import ValidationError

class TestCaptureDevice:
    def test_valid_values(self):
        """Valid values should pass validation."""
        device = CaptureDevice(
            type="RspDuo",
            agcSetPoint=-60,
            gainReduction=40,
            lnaState=4,
            dabNotch=True,
            rfNotch=True,
            bandwidthNumber=0
        )
        assert device.gainReduction == 40

    def test_gain_reduction_bounds(self):
        """gainReduction must be 20-59."""
        with pytest.raises(ValidationError):
            CaptureDevice(type="RspDuo", agcSetPoint=-60, gainReduction=19,
                         lnaState=4, dabNotch=True, rfNotch=True, bandwidthNumber=0)
        with pytest.raises(ValidationError):
            CaptureDevice(type="RspDuo", agcSetPoint=-60, gainReduction=60,
                         lnaState=4, dabNotch=True, rfNotch=True, bandwidthNumber=0)

    def test_lna_state_bounds(self):
        """lnaState must be 1-9."""
        with pytest.raises(ValidationError):
            CaptureDevice(type="RspDuo", agcSetPoint=-60, gainReduction=40,
                         lnaState=0, dabNotch=True, rfNotch=True, bandwidthNumber=0)
        with pytest.raises(ValidationError):
            CaptureDevice(type="RspDuo", agcSetPoint=-60, gainReduction=40,
                         lnaState=10, dabNotch=True, rfNotch=True, bandwidthNumber=0)

    def test_agc_set_point_max(self):
        """agcSetPoint must be <= 0."""
        with pytest.raises(ValidationError):
            CaptureDevice(type="RspDuo", agcSetPoint=1, gainReduction=40,
                         lnaState=4, dabNotch=True, rfNotch=True, bandwidthNumber=0)
```

### Unit Tests (`tests/test_form_utils.py`)

**Form Generation:**
```python
from form_utils import schema_to_form_fields, get_field_input_type
from config_schema import CaptureDevice, CaptureConfig

class TestFormGeneration:
    def test_bool_becomes_checkbox(self):
        """Boolean fields should render as checkboxes."""
        values = {'dabNotch': True, 'rfNotch': False, 'type': 'RspDuo',
                  'agcSetPoint': -60, 'gainReduction': 40, 'lnaState': 4, 'bandwidthNumber': 0}
        fields = schema_to_form_fields(CaptureDevice, values)
        dab_field = next(f for f in fields if f['name'] == 'dabNotch')
        assert dab_field['type'] == 'checkbox'

    def test_int_becomes_number(self):
        """Integer fields should render as number inputs."""
        values = {'gainReduction': 40}
        fields = schema_to_form_fields(CaptureDevice, values)
        gain_field = next(f for f in fields if f['name'] == 'gainReduction')
        assert gain_field['type'] == 'number'

    def test_constraints_from_schema(self):
        """min/max constraints from Field should appear in form."""
        values = {'gainReduction': 40}
        fields = schema_to_form_fields(CaptureDevice, values)
        gain_field = next(f for f in fields if f['name'] == 'gainReduction')
        assert gain_field['min'] == 20
        assert gain_field['max'] == 59

    def test_values_from_dict_not_defaults(self):
        """Form values should come from passed dict, not schema defaults."""
        values = {'gainReduction': 45, 'dabNotch': False}
        fields = schema_to_form_fields(CaptureDevice, values)
        gain_field = next(f for f in fields if f['name'] == 'gainReduction')
        dab_field = next(f for f in fields if f['name'] == 'dabNotch')
        assert gain_field['value'] == 45
        assert dab_field['value'] == False

    def test_missing_values_are_none(self):
        """Fields not in values dict should have None value."""
        values = {}  # Empty - simulates empty user.yml
        fields = schema_to_form_fields(CaptureDevice, values)
        gain_field = next(f for f in fields if f['name'] == 'gainReduction')
        assert gain_field['value'] is None

    def test_nested_model_becomes_group(self):
        """Nested Pydantic models should become field groups."""
        values = {'fs': 2000000, 'fc': 503000000, 'device': {'gainReduction': 40}}
        fields = schema_to_form_fields(CaptureConfig, values)
        device_field = next(f for f in fields if f['name'] == 'device')
        assert device_field['type'] == 'group'
        assert len(device_field['fields']) > 0

    def test_title_and_description(self):
        """Field metadata should be extracted."""
        values = {'gainReduction': 40}
        fields = schema_to_form_fields(CaptureDevice, values)
        gain_field = next(f for f in fields if f['name'] == 'gainReduction')
        assert gain_field['title'] == 'Gain Reduction'
        assert gain_field['description'] == '20-59 dB'
```

### Unit Tests (`tests/test_config_io.py`)

**YAML I/O:**
```python
import tempfile
import os
from config_schema import load_user_config, save_user_config

class TestConfigIO:
    def test_load_missing_file(self):
        """Loading missing file returns empty dict."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'user.yml')
            config = load_user_config(path)
            assert config == {}

    def test_save_and_load_roundtrip(self):
        """Saved config should load back identically."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'user.yml')
            original = {'capture': {'fs': 1000000, 'device': {'gainReduction': 30}}}
            save_user_config(path, original)
            loaded = load_user_config(path)
            assert loaded['capture']['fs'] == 1000000
            assert loaded['capture']['device']['gainReduction'] == 30

    def test_partial_config_preserved(self):
        """Fields not in form should be preserved on save."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, 'user.yml')
            # Start with extra fields
            original = {'capture': {'fs': 1000000}, 'location': {'rx': [1, 2, 3]}}
            save_user_config(path, original)
            # Update only capture
            updated = load_user_config(path)
            updated['capture']['fs'] = 2000000
            save_user_config(path, updated)
            # Location should still be there
            final = load_user_config(path)
            assert final['location']['rx'] == [1, 2, 3]
```

### Integration Tests (`tests/test_app.py`)

**Flask Routes:**
```python
import pytest
import tempfile
import os
from app import app

@pytest.fixture
def client():
    """Test client with mocked paths."""
    app.config['TESTING'] = True
    with tempfile.TemporaryDirectory() as tmpdir:
        app.config['USER_CONFIG_PATH'] = os.path.join(tmpdir, 'user.yml')
        app.config['RETINA_NODE_PATH'] = os.path.join(tmpdir, 'manifests')
        with app.test_client() as client:
            yield client

class TestConfigRoutes:
    def test_index_shows_config_section(self, client):
        """Index page should include config form."""
        os.makedirs(app.config['RETINA_NODE_PATH'])
        open(os.path.join(app.config['RETINA_NODE_PATH'], 'docker-compose.yaml'), 'w').close()
        # Create user.yml with values
        from config_schema import save_user_config
        save_user_config(app.config['USER_CONFIG_PATH'], {
            'capture': {'fs': 2000000, 'device': {'gainReduction': 40}}
        })

        response = client.get('/')
        assert response.status_code == 200
        assert b'Capture Settings' in response.data
        assert b'2000000' in response.data  # Value from user.yml

    def test_config_greyed_when_not_installed(self, client):
        """Config should be disabled when retina-node not installed."""
        response = client.get('/')
        assert response.status_code == 200
        assert b'Configuration available after' in response.data

    def test_post_config_validates(self, client):
        """POST /config should validate and reject invalid data."""
        response = client.post('/config', data={
            'capture.device.gainReduction': '100'  # Invalid: > 59
        })
        assert response.status_code == 400

    def test_post_config_saves(self, client):
        """POST /config should save valid data to user.yml."""
        response = client.post('/config', data={
            'capture.fs': '1500000',
            'capture.device.gainReduction': '35',
            'capture.device.type': 'RspDuo',
            'capture.device.agcSetPoint': '-60',
            'capture.device.lnaState': '4',
            'capture.device.bandwidthNumber': '0',
        })
        assert response.status_code in (200, 302)
        from config_schema import load_user_config
        config = load_user_config(app.config['USER_CONFIG_PATH'])
        assert config['capture']['fs'] == 1500000

class TestApplyRoute:
    def test_apply_fails_gracefully(self, client):
        """POST /config/apply should handle docker failure gracefully."""
        response = client.post('/config/apply')
        assert response.status_code in (200, 500)
        data = response.get_json()
        assert 'error' in data or 'success' in data
```

### Running Tests

```bash
# Install test dependencies
pip install pytest pytest-cov

# Run all tests
cd retina-gui
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=. --cov-report=html

# Run specific test file
pytest tests/test_config_schema.py -v
```

### Test Directory Structure

```
retina-gui/
├── app.py
├── config_schema.py
├── form_utils.py
├── templates/
│   └── index.html
├── tests/
│   ├── __init__.py
│   ├── conftest.py          # Shared fixtures
│   ├── test_config_schema.py # Pydantic validation tests
│   ├── test_form_utils.py    # Form generation tests
│   ├── test_config_io.py     # YAML load/save tests
│   └── test_app.py           # Flask route integration tests
└── pytest.ini
```

### What Tests Verify

| Test Area | What It Verifies |
|-----------|------------------|
| Schema validation | Pydantic rejects out-of-range values |
| Form generation | Values come from user.yml dict, not schema defaults |
| Form generation | Constraints (min/max) come from schema metadata |
| YAML I/O | Roundtrip save/load, partial config preservation |
| Flask routes | Form displays values from user.yml |
| Flask routes | Validation errors returned for invalid input |
| Apply endpoint | Docker failures handled gracefully |

## Local Development Environment

### Setup (Mac)

Use environment variables to override device paths:

```bash
# One-time setup: create test data directories
cd retina-gui
mkdir -p ./test-data/config ./test-data/manifests

# Create a test user.yml with sample values
cat > ./test-data/config/user.yml << 'EOF'
capture:
  fs: 2000000
  fc: 503000000
  device:
    type: RspDuo
    agcSetPoint: -60
    gainReduction: 40
    lnaState: 4
    dabNotch: true
    rfNotch: true
    bandwidthNumber: 0
EOF

# Create dummy docker-compose.yaml so retina-node appears "installed"
touch ./test-data/manifests/docker-compose.yaml
```

### Running Locally

```bash
cd retina-gui

# Install deps (if not already)
pip install flask pydantic pyyaml

# Run with overridden paths
USER_CONFIG_PATH=./test-data/config/user.yml \
RETINA_NODE_PATH=./test-data/manifests \
python app.py
```

Then open http://localhost:80 (or 8080 if you change the port for non-root).

### Configurable Paths in app.py

```python
# Near top of app.py
import os

# Configurable paths - override via environment for local dev
USER_CONFIG_PATH = os.environ.get('USER_CONFIG_PATH', '/data/retina-node/config/user.yml')
RETINA_NODE_PATH = os.environ.get('RETINA_NODE_PATH', '/data/mender-app/retina-node/manifests')
DATA_DIR = os.environ.get('DATA_DIR', '/data/retina-gui')

def is_retina_node_installed():
    return os.path.exists(os.path.join(RETINA_NODE_PATH, 'docker-compose.yaml'))
```

### Test Scenarios

| Scenario | How to Test |
|----------|-------------|
| Form displays user.yml values | Edit `test-data/config/user.yml`, refresh page |
| Validation error | Enter gainReduction=100, submit |
| Save works | Change a value, submit, check `test-data/config/user.yml` |
| Retina-node not installed | Delete `test-data/manifests/docker-compose.yaml`, refresh |
| Apply Changes | Will fail (no docker) - verify graceful error handling |

### Running on Non-Root Port (Mac)

Since port 80 requires root on Mac, change the port for local dev:

```python
if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))  # 8080 for local, 80 on device
    app.run(host="::", port=port, debug=True)
```

Then run:
```bash
PORT=8080 USER_CONFIG_PATH=./test-data/config/user.yml ... python app.py
```

## Verification (Manual)

1. **Local testing:**
   - Run with env vars as above
   - Verify form shows values from `test-data/config/user.yml`
   - Test validation errors (e.g., gainReduction=100)
   - Verify save updates the file correctly
   - Delete docker-compose.yaml to test "not installed" state

2. **On device:**
   - Deploy updated retina-gui
   - Test Apply Changes triggers docker commands
   - Verify config-merger runs and services restart
   - Test with retina-node not installed (greyed out)

3. **Extensibility test:**
   - Add a new field to Pydantic model
   - Verify it auto-appears in the form without template changes

## Future Enhancements

- Add location settings (rx/tx coordinates)
- Add tar1090 settings (adsb_source, adsblol)
- Add truth.adsb settings
- Separate /config page
- Show merged config.yml (read-only)
- Status dashboard: OS version, stack version, container health
