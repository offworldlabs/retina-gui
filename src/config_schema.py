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
from copy import deepcopy
from typing import Literal

import yaml
from pydantic import VERSION, BaseModel, Field

# Detect Pydantic version for Field() syntax
PYDANTIC_V2 = VERSION.startswith("2.")

# Transmitter names travel to the server as retina-telemetry's `tx_callsign`,
# which the node-ingest spec caps at 32. Tower-Finder never returns anything
# near it — the only way to exceed it is a hand-typed name, either in the
# config form or the manual Add Tower dialog, so both are checked against this.
TX_NAME_MAX_LENGTH = 32


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
    fs: Literal[62500, 125000, 250000, 500000, 1000000, 2000000] = Field(
        title="Sample Rate",
        description="Hz. How much of the broadcast signal the radar captures. 2000000 gives the sharpest "
                    "range detail (recommended); lower values blur range but reduce processing load."
    )
    fc: int = Field(title="Center Frequency", description="Hz. Set based on the tower you have chosen.")
    device_type: str = _readonly_field(title="Device Type")
    device_agcSetPoint: int = Field(le=0, title="AGC Set Point", description="dBFS. Not recommended to be changed.")
    device_gainReductionA: int = Field(ge=20, le=59, title="Reference Gain Reduction", description="20-59 dB, higher=less gain")
    device_gainReductionB: int = Field(ge=20, le=59, title="Surveillance Gain Reduction", description="20-59 dB, higher=less gain")
    device_lnaState: int = Field(ge=1, le=9, title="Low Noise Amplifier State", description="1=max gain, 9=min gain. RF attenuator block also used similarly to gain.")
    device_dabNotch: bool = Field(title="DAB Notch Filter", description="Not recommended to enable unless you are sure.")
    device_rfNotch: bool = Field(title="RF Notch Filter", description="Not recommended to enable unless you are sure.")
    device_bandwidthNumber: Literal[0, 5, 50, 100] = Field(
        title="Bandwidth Number",
        description="AGC loop bandwidth (Hz). 0 disables AGC: gain is fixed by Gain Reduction/LNA State. "
                     "5/50/100 enable AGC: lower is slower and more stable, higher reacts faster but chases noise more."
    )


# ============================================================================
# Location Settings
# ============================================================================
#: The six numbers that make a bistatic geometry. Names are excluded: they are
#: labels, and a position without one is still a position.
LOCATION_COORDINATE_FIELDS = (
    "rx_latitude", "rx_longitude", "rx_altitude",
    "tx_latitude", "tx_longitude", "tx_altitude",
)


class LocationFormConfig(BaseModel):
    """Flat location config for form display.

    Optional, because a node has no location until its owner picks a tower and
    retina-node ships these null rather than defaulting to a plausible site.
    Optional is not partial, though: see the validator below.
    """
    rx_latitude: float | None = Field(None, ge=-90, le=90, title="Receiver Latitude", description="decimal degrees")
    rx_longitude: float | None = Field(None, ge=-180, le=180, title="Receiver Longitude", description="decimal degrees")
    rx_altitude: float | None = Field(None, title="Receiver Altitude", description="meters")
    rx_name: str | None = Field(None, title="Receiver Name", description="location name")
    tx_latitude: float | None = Field(None, ge=-90, le=90, title="Transmitter Latitude", description="decimal degrees")
    tx_longitude: float | None = Field(None, ge=-180, le=180, title="Transmitter Longitude", description="decimal degrees")
    tx_altitude: float | None = Field(None, title="Transmitter Altitude", description="meters")
    # 32 chars is retina-telemetry's tx_callsign limit, not a display concern:
    # a longer name means it cannot build a NodeConfig, so registration and
    # every config resend fail and the node never reaches the server.
    tx_name: str | None = Field(None, max_length=TX_NAME_MAX_LENGTH, title="Transmitter Name", description="location name")

    # The all-or-nothing rule is enforced on save in routes/config.py, not
    # here: it is a cross-field rule like the ADS-B source trio, so each
    # complaint can be attached to the box it concerns. A model validator would
    # also need pydantic v2, and nodes run v1.

    @property
    def is_located(self) -> bool:
        """Whether this carries a usable geometry. Never partially true."""
        return all(getattr(self, f) is not None for f in LOCATION_COORDINATE_FIELDS)


# ============================================================================
# ADS-B Truth Settings
# ============================================================================

class AdsbTruthConfig(BaseModel):
    """ADS-B ground truth matching settings (flat for form display)."""
    enabled: bool = Field(title="Enabled")
    tar1090: str = Field(title="tar1090 Server", description="Used to access ADSB data")
    adsb2dd: str = Field(title="adsb2dd Address", description="Local URL to view your local ADSB data")
    delay_tolerance: float = Field(gt=0, title="Delay Tolerance", description="km. Maximum bistatic range error allowed when matching a radar detection to an ADS-B aircraft position.")
    doppler_tolerance: float = Field(gt=0, title="Doppler Tolerance", description="Hz. Maximum bistatic Doppler error allowed when matching a radar detection to an ADS-B aircraft position.")


# ============================================================================
# retina-tracker Settings
# ============================================================================

class RetinaTrackerConfig(BaseModel):
    """retina-tracker sidecar settings (flat for form display).

    Unrelated to blah2's own built-in tracker (process.tracker in
    capture config) - this tunes github.com/offworldlabs/retina-tracker.
    """
    min_snr: float = Field(gt=0, title="Minimum SNR", description="dB. Detections below this are discarded before tracking even begins - too high and the tracker will never confirm a track. Tune to this node's real noise floor.")


# ============================================================================
# tar1090 Settings
# ============================================================================

class Tar1090Config(BaseModel):
    """tar1090 ADS-B viewer configuration.

    Note: adsb_source is stored as comma-separated string in YAML
    but split into 3 fields for the form.

    The three source fields are optional here so that a node with no local
    beast feed can be described at all: one fed from adsb.lol via the tar1090
    proxy has nothing to point them at, and blanking adsb_source is how that
    is expressed. Optional does not mean free-standing, though. When they may
    actually be left empty depends on adsblol_fallback, and a partial set is
    never valid, so both rules are enforced on save in routes/config.py, where
    each complaint can be attached to the box it concerns.
    """
    adsb_source_host: str | None = Field(None, title="ADS-B Host", description="IP or hostname")
    adsb_source_port: int | None = Field(None, ge=1, le=65535, title="ADS-B Port")
    adsb_source_protocol: str | None = Field(None, title="Protocol", description="e.g. beast_in")
    adsblol_fallback: bool = Field(title="adsb.lol Fallback")
    adsblol_radius: int = Field(ge=1, le=500, title="adsb.lol Radius", description="nautical miles")


