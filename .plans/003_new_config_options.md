# New Config Options Planning

## Overview

Planning document for additional settings to add to the config GUI.

User settings sections:
1. **capture** - DONE
2. **location** - TODO (rx + tx)
3. **adsb** - TODO (truth.adsb + tar1090)

---

## Page Structure

Split into two pages to keep things tidy:

### Home Page (`/`)
- **Node ID** display (read-only, at top)
- **Services** links (blah2, tar1090, adsb2dd)
- **SSH Keys** management
- **Config** link → takes you to `/config`

### Config Page (`/config`) - NEW
- Capture settings
- Location settings
- ADS-B Truth settings
- tar1090 settings
- Apply Changes button

---

## 0. Display Node ID (read-only)

Show the node ID near the top of home page. Auto-generated from RPi serial by config-merger, stored in `user.yml` under `network.node_id`.

```yaml
network:
  node_id: "ret7dd2cb0d"
```

**Implementation:**
- Read `network.node_id` from user.yml in `index()` route
- Display prominently near top of home page (not editable)
- Show "Unknown" or similar if not yet generated

---

## 1. Location Settings - APPROVED

Both receiver (rx) and transmitter (tx) locations.

```yaml
location:
  rx:                          # Receiver location
    latitude: 37.7644          # decimal degrees
    longitude: -122.3954       # decimal degrees
    altitude: 23               # meters
    name: "150 Mississippi"    # human-readable name
  tx:                          # Transmitter location
    latitude: 37.49917
    longitude: -121.87222
    altitude: 783
    name: "KSCZ-LD"
```

### Fields:

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| rx.latitude | float | -90 to 90 | Receiver latitude (decimal degrees) |
| rx.longitude | float | -180 to 180 | Receiver longitude (decimal degrees) |
| rx.altitude | float | none | Receiver altitude (meters) |
| rx.name | string | none | Receiver name |
| tx.latitude | float | -90 to 90 | Transmitter latitude (decimal degrees) |
| tx.longitude | float | -180 to 180 | Transmitter longitude (decimal degrees) |
| tx.altitude | float | none | Transmitter altitude (meters) |
| tx.name | string | none | Transmitter name |

---

## 2. ADS-B Settings - APPROVED

Two separate config sections that work together:

### truth.adsb - Ground Truth Matching

Uses ADS-B data to validate radar detections.

```yaml
truth:
  adsb:
    enabled: true
    tar1090: 'sfo1.retnode.com'
    adsb2dd: 'localhost:49155'
    delay_tolerance: 2.0
    doppler_tolerance: 5.0
```

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| enabled | bool | - | Enable ADS-B truth matching |
| tar1090 | string | - | tar1090 server to fetch ADS-B from |
| adsb2dd | string | - | adsb2dd service address |
| delay_tolerance | float | > 0 | Acceptable delay error for matching |
| doppler_tolerance | float | > 0 | Acceptable doppler error for matching |

### tar1090 - ADS-B Viewer Config

Configures the tar1090 ADS-B map viewer.

```yaml
tar1090:
  adsb_source: "192.168.8.183,30005,beast_in"
  adsblol_fallback: true
  adsblol_radius: 40
```

**Note:** `adsb_source` is stored as comma-separated string but displayed as 3 separate inputs in the UI.

| Field | Type | Constraints | Description |
|-------|------|-------------|-------------|
| adsb_source_host | string | - | ADS-B source IP/hostname |
| adsb_source_port | int | 1-65535 | ADS-B source port |
| adsb_source_protocol | string | - | Protocol (e.g. beast_in) |
| adsblol_fallback | bool | - | Use adsb.lol if local fails |
| adsblol_radius | int | 1-500 | Radius for adsb.lol feed (nm) |

**Form → YAML conversion:** Join the 3 fields as `"{host},{port},{protocol}"` when saving.

---

## Implementation Order

1. **Refactor: Split into two pages** (home + config)
2. **Add Node ID display** to home page
3. **Add Location settings** to config page
4. **Add truth.adsb settings** to config page
5. **Add tar1090 settings** to config page
6. **Update tests**

---

## Test Plan

Extend existing test suite in `tests/` to cover new functionality.

### test_config_schema.py - Add validation tests

```python
class TestLocationPoint:
    """Test LocationPoint validation."""

    def test_valid_location(self):
        """Valid coordinates should pass."""
        point = LocationPoint(latitude=37.7644, longitude=-122.3954, altitude=23, name="Test")
        assert point.latitude == 37.7644

    def test_latitude_bounds(self):
        """Latitude must be -90 to 90."""
        with pytest.raises(ValidationError):
            LocationPoint(latitude=91, longitude=0, altitude=0, name="Test")
        with pytest.raises(ValidationError):
            LocationPoint(latitude=-91, longitude=0, altitude=0, name="Test")

    def test_longitude_bounds(self):
        """Longitude must be -180 to 180."""
        with pytest.raises(ValidationError):
            LocationPoint(longitude=181, latitude=0, altitude=0, name="Test")
        with pytest.raises(ValidationError):
            LocationPoint(longitude=-181, latitude=0, altitude=0, name="Test")

    def test_negative_altitude_allowed(self):
        """Negative altitude (below sea level) should be valid."""
        point = LocationPoint(latitude=0, longitude=0, altitude=-50, name="Dead Sea")
        assert point.altitude == -50


class TestAdsbTruthConfig:
    """Test ADS-B truth config validation."""

    def test_valid_config(self):
        """Valid config should pass."""
        config = AdsbTruthConfig(
            enabled=True, tar1090="server.com", adsb2dd="localhost:49155",
            delay_tolerance=2.0, doppler_tolerance=5.0
        )
        assert config.enabled is True

    def test_tolerance_must_be_positive(self):
        """Tolerances must be > 0."""
        with pytest.raises(ValidationError):
            AdsbTruthConfig(
                enabled=True, tar1090="x", adsb2dd="x",
                delay_tolerance=0, doppler_tolerance=5.0
            )
        with pytest.raises(ValidationError):
            AdsbTruthConfig(
                enabled=True, tar1090="x", adsb2dd="x",
                delay_tolerance=-1, doppler_tolerance=5.0
            )


class TestTar1090Config:
    """Test tar1090 config validation."""

    def test_valid_config(self):
        """Valid config should pass."""
        config = Tar1090Config(
            adsb_source_host="192.168.1.1", adsb_source_port=30005,
            adsb_source_protocol="beast_in", adsblol_fallback=True, adsblol_radius=40
        )
        assert config.adsb_source_port == 30005

    def test_port_bounds(self):
        """Port must be 1-65535."""
        with pytest.raises(ValidationError):
            Tar1090Config(
                adsb_source_host="x", adsb_source_port=0,
                adsb_source_protocol="x", adsblol_fallback=True, adsblol_radius=40
            )
        with pytest.raises(ValidationError):
            Tar1090Config(
                adsb_source_host="x", adsb_source_port=65536,
                adsb_source_protocol="x", adsblol_fallback=True, adsblol_radius=40
            )

    def test_radius_bounds(self):
        """Radius must be 1-500."""
        with pytest.raises(ValidationError):
            Tar1090Config(
                adsb_source_host="x", adsb_source_port=30005,
                adsb_source_protocol="x", adsblol_fallback=True, adsblol_radius=0
            )
        with pytest.raises(ValidationError):
            Tar1090Config(
                adsb_source_host="x", adsb_source_port=30005,
                adsb_source_protocol="x", adsblol_fallback=True, adsblol_radius=501
            )
```

### test_app.py - Add route tests

```python
class TestHomePage:
    """Test home page with node ID."""

    def test_node_id_displayed(self, app_client):
        """Node ID should be shown on home page."""
        response = app_client.get('/')
        assert b'ret7dd2cb0d' in response.data

    def test_node_id_unknown(self, app_client_no_node_id):
        """Should show 'Unknown' when node_id not set."""
        response = app_client_no_node_id.get('/')
        assert b'Unknown' in response.data

    def test_config_link(self, app_client):
        """Home page should link to /config."""
        response = app_client.get('/')
        assert b'href="/config"' in response.data


class TestConfigPage:
    """Test config page."""

    def test_config_page_loads(self, app_client):
        """Config page should load with all sections."""
        response = app_client.get('/config')
        assert response.status_code == 200
        assert b'Capture Settings' in response.data
        assert b'Location Settings' in response.data
        assert b'ADS-B Truth' in response.data
        assert b'tar1090' in response.data

    def test_config_shows_location_values(self, app_client):
        """Location values from user.yml should appear."""
        response = app_client.get('/config')
        assert b'37.7644' in response.data  # rx latitude
        assert b'150 Mississippi' in response.data  # rx name


class TestLocationSave:
    """Test saving location config."""

    def test_save_valid_location(self, app_client, user_config_file):
        """Valid location should save."""
        response = app_client.post('/config', data={
            # ... existing capture fields ...
            'location.rx.latitude': '40.7128',
            'location.rx.longitude': '-74.0060',
            'location.rx.altitude': '10',
            'location.rx.name': 'NYC',
            'location.tx.latitude': '40.0',
            'location.tx.longitude': '-74.0',
            'location.tx.altitude': '100',
            'location.tx.name': 'Transmitter',
        }, follow_redirects=False)
        assert response.status_code == 302

    def test_save_invalid_latitude(self, app_client):
        """Invalid latitude should show error."""
        response = app_client.post('/config', data={
            'location.rx.latitude': '100',  # Invalid > 90
            # ...
        })
        assert response.status_code == 200
        assert b'is-invalid' in response.data


class TestAdsbSourceParsing:
    """Test adsb_source split/join logic."""

    def test_adsb_source_split_on_load(self, app_client):
        """adsb_source should be split into 3 fields."""
        response = app_client.get('/config')
        assert b'192.168.8.183' in response.data  # host
        assert b'30005' in response.data  # port
        assert b'beast_in' in response.data  # protocol

    def test_adsb_source_join_on_save(self, app_client, user_config_file):
        """3 fields should be joined to adsb_source on save."""
        response = app_client.post('/config', data={
            'tar1090.adsb_source_host': '10.0.0.1',
            'tar1090.adsb_source_port': '30006',
            'tar1090.adsb_source_protocol': 'raw_in',
            'tar1090.adsblol_fallback': 'on',
            'tar1090.adsblol_radius': '50',
            # ... other required fields ...
        }, follow_redirects=False)

        with open(user_config_file) as f:
            saved = yaml.safe_load(f)
        assert saved['tar1090']['adsb_source'] == '10.0.0.1,30006,raw_in'
```

### conftest.py - Add fixtures

```python
@pytest.fixture
def sample_full_config():
    """Sample config with all sections."""
    return {
        'capture': { ... },
        'network': {'node_id': 'ret7dd2cb0d'},
        'location': {
            'rx': {'latitude': 37.7644, 'longitude': -122.3954, 'altitude': 23, 'name': '150 Mississippi'},
            'tx': {'latitude': 37.49917, 'longitude': -121.87222, 'altitude': 783, 'name': 'KSCZ-LD'}
        },
        'truth': {
            'adsb': {
                'enabled': True, 'tar1090': 'sfo1.retnode.com', 'adsb2dd': 'localhost:49155',
                'delay_tolerance': 2.0, 'doppler_tolerance': 5.0
            }
        },
        'tar1090': {
            'adsb_source': '192.168.8.183,30005,beast_in',
            'adsblol_fallback': True, 'adsblol_radius': 40
        }
    }

@pytest.fixture
def app_client_no_node_id(temp_dir, test_manifests_dir):
    """Flask client with config missing node_id."""
    # ... setup without network.node_id ...
```

---

## Pydantic Schema Preview

```python
# Location
class LocationPoint(BaseModel):
    latitude: float = Field(ge=-90, le=90, title="Latitude", description="decimal degrees")
    longitude: float = Field(ge=-180, le=180, title="Longitude", description="decimal degrees")
    altitude: float = Field(title="Altitude", description="meters")
    name: str = Field(title="Name", description="location name")

class LocationConfig(BaseModel):
    rx: LocationPoint = Field(title="Receiver")
    tx: LocationPoint = Field(title="Transmitter")

# ADS-B Truth
class AdsbTruthConfig(BaseModel):
    enabled: bool = Field(title="Enabled")
    tar1090: str = Field(title="tar1090 Server")
    adsb2dd: str = Field(title="adsb2dd Address")
    delay_tolerance: float = Field(gt=0, title="Delay Tolerance")
    doppler_tolerance: float = Field(gt=0, title="Doppler Tolerance")

class TruthConfig(BaseModel):
    adsb: AdsbTruthConfig = Field(title="ADS-B Truth")

# tar1090 - adsb_source split into 3 fields for the form
class Tar1090Config(BaseModel):
    adsb_source_host: str = Field(title="ADS-B Host", description="IP or hostname")
    adsb_source_port: int = Field(ge=1, le=65535, title="ADS-B Port")
    adsb_source_protocol: str = Field(title="Protocol", description="e.g. beast_in")
    adsblol_fallback: bool = Field(title="adsb.lol Fallback")
    adsblol_radius: int = Field(ge=1, le=500, title="adsb.lol Radius", description="nautical miles")

    # Note: When saving to YAML, combine as: f"{host},{port},{protocol}"
    # When loading from YAML, split adsb_source on comma into 3 fields

# Top-level
class UserConfig(BaseModel):
    capture: Optional[CaptureConfig] = None
    location: Optional[LocationConfig] = None
    truth: Optional[TruthConfig] = None
    tar1090: Optional[Tar1090Config] = None
```
