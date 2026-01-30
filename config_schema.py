"""
Pydantic models for config validation and form generation.

These models define:
- Field types (int, bool, str, float) -> determines HTML input type
- Constraints (ge, le, gt) -> HTML min/max attributes
- Metadata (title, description) -> form labels and help text

Layered Config System:
- config.yml: Merged output (default + user + forced) - READ for display values
- user.yml: User overrides only - WRITE changes here
- Form shows values from config.yml, but only saves changed values to user.yml
"""
import os
import yaml
from copy import deepcopy
from pydantic import BaseModel, Field, VERSION

# Detect Pydantic version for Field() syntax
PYDANTIC_V2 = VERSION.startswith("2.")


# ============================================================================
# Config File Loading
# ============================================================================

def load_yaml_file(path):
    """Load YAML file, return empty dict if missing."""
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def save_yaml_file(path, data):
    """Save data to YAML file (atomic write)."""
    import tempfile
    config_dir = os.path.dirname(path)
    os.makedirs(config_dir, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(dir=config_dir)
    with os.fdopen(fd, 'w') as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)
    os.chmod(tmp_path, 0o644)
    os.rename(tmp_path, path)


def deep_merge(base, override):
    """
    Deep merge override into base (modifies base in place).
    - Dicts are merged recursively
    - Other values are replaced
    """
    for key, value in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(value, dict):
            deep_merge(base[key], value)
        else:
            base[key] = deepcopy(value)
    return base


def get_nested_value(data, path):
    """Get value from nested dict using dot-separated path."""
    keys = path.split('.')
    current = data
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def set_nested_value(data, path, value):
    """Set value in nested dict using dot-separated path."""
    keys = path.split('.')
    current = data
    for key in keys[:-1]:
        if key not in current:
            current[key] = {}
        current = current[key]
    current[keys[-1]] = value


def values_differ(val1, val2):
    """Check if two values are different (for change tracking)."""
    # Handle None vs missing
    if val1 is None and val2 is None:
        return False
    if val1 is None or val2 is None:
        return True
    # Compare values (handle float comparison)
    if isinstance(val1, float) or isinstance(val2, float):
        try:
            return abs(float(val1) - float(val2)) > 1e-9
        except (TypeError, ValueError):
            return val1 != val2
    return val1 != val2


# ============================================================================
# Capture Settings
# ============================================================================

# Helper for readonly fields - syntax differs between Pydantic v1 and v2
def _readonly_field(**kwargs):
    """Create a Field with readonly=True, compatible with Pydantic v1 and v2."""
    if PYDANTIC_V2:
        kwargs['json_schema_extra'] = {'readonly': True}
    else:
        kwargs['readonly'] = True
    return Field(**kwargs)


class CaptureFormConfig(BaseModel):
    """Flat capture config for form display."""
    fs: int = Field(title="Sample Rate", description="Hz")
    fc: int = Field(title="Center Frequency", description="Hz")
    device_type: str = _readonly_field(title="Device Type")
    device_agcSetPoint: int = Field(le=0, title="AGC Set Point", description="dBFS")
    device_gainReduction: int = Field(ge=20, le=59, title="Gain Reduction", description="20-59 dB, higher=less gain")
    device_lnaState: int = Field(ge=1, le=9, title="LNA State", description="1=max gain, 9=min gain")
    device_dabNotch: bool = Field(title="DAB Notch Filter")
    device_rfNotch: bool = Field(title="RF Notch Filter")
    device_bandwidthNumber: int = Field(title="Bandwidth Number")


# ============================================================================
# Location Settings
# ============================================================================
class LocationFormConfig(BaseModel):
    """Flat location config for form display."""
    rx_latitude: float = Field(ge=-90, le=90, title="Receiver Latitude", description="decimal degrees")
    rx_longitude: float = Field(ge=-180, le=180, title="Receiver Longitude", description="decimal degrees")
    rx_altitude: float = Field(title="Receiver Altitude", description="meters")
    rx_name: str = Field(title="Receiver Name", description="location name")
    tx_latitude: float = Field(ge=-90, le=90, title="Transmitter Latitude", description="decimal degrees")
    tx_longitude: float = Field(ge=-180, le=180, title="Transmitter Longitude", description="decimal degrees")
    tx_altitude: float = Field(title="Transmitter Altitude", description="meters")
    tx_name: str = Field(title="Transmitter Name", description="location name")


# ============================================================================
# ADS-B Truth Settings
# ============================================================================

class AdsbTruthConfig(BaseModel):
    """ADS-B ground truth matching settings (flat for form display)."""
    enabled: bool = Field(title="Enabled")
    tar1090: str = Field(title="tar1090 Server")
    adsb2dd: str = Field(title="adsb2dd Address")
    delay_tolerance: float = Field(gt=0, title="Delay Tolerance")
    doppler_tolerance: float = Field(gt=0, title="Doppler Tolerance")


# ============================================================================
# tar1090 Settings
# ============================================================================

class Tar1090Config(BaseModel):
    """tar1090 ADS-B viewer configuration.

    Note: adsb_source is stored as comma-separated string in YAML
    but split into 3 fields for the form.
    """
    adsb_source_host: str = Field(title="ADS-B Host", description="IP or hostname")
    adsb_source_port: int = Field(ge=1, le=65535, title="ADS-B Port")
    adsb_source_protocol: str = Field(title="Protocol", description="e.g. beast_in")
    adsblol_fallback: bool = Field(title="adsb.lol Fallback")
    adsblol_radius: int = Field(ge=1, le=500, title="adsb.lol Radius", description="nautical miles")


