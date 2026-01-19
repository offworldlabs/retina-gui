"""
Pydantic models for config validation and form generation.

These models define:
- Field types (int, bool, str, float) -> determines HTML input type
- Constraints (ge, le, gt) -> HTML min/max attributes
- Metadata (title, description) -> form labels and help text

Form VALUES come from user.yml, not from schema defaults.
"""
from pydantic import BaseModel, Field


# ============================================================================
# Capture Settings
# ============================================================================
class CaptureFormConfig(BaseModel):
    """Flat capture config for form display."""
    fs: int = Field(title="Sample Rate", description="Hz")
    fc: int = Field(title="Center Frequency", description="Hz")
    device_type: str = Field(title="Device Type")
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


