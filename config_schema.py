"""
Pydantic models for config validation and form generation.

These models define:
- Field types (int, bool, str, float) -> determines HTML input type
- Constraints (ge, le, gt) -> HTML min/max attributes
- Metadata (title, description) -> form labels and help text

Form VALUES come from user.yml, not from schema defaults.
"""
from pydantic import BaseModel, Field
from typing import Optional


# ============================================================================
# Capture Settings
# ============================================================================

class CaptureDevice(BaseModel):
    """SDR device settings."""
    type: str = Field(title="Device Type")
    agcSetPoint: int = Field(le=0, title="AGC Set Point", description="dBFS")
    gainReduction: int = Field(ge=20, le=59, title="Gain Reduction", description="20-59 dB, higher=less gain")
    lnaState: int = Field(ge=1, le=9, title="LNA State", description="1=max gain, 9=min gain")
    dabNotch: bool = Field(title="DAB Notch Filter")
    rfNotch: bool = Field(title="RF Notch Filter")
    bandwidthNumber: int = Field(title="Bandwidth Number")


class CaptureConfig(BaseModel):
    """Capture/SDR settings."""
    fs: int = Field(title="Sample Rate", description="Hz")
    fc: int = Field(title="Center Frequency", description="Hz")
    device: CaptureDevice = Field(title="Device Settings")


# ============================================================================
# Location Settings
# ============================================================================

class LocationPoint(BaseModel):
    """A geographic location point."""
    latitude: float = Field(ge=-90, le=90, title="Latitude", description="decimal degrees")
    longitude: float = Field(ge=-180, le=180, title="Longitude", description="decimal degrees")
    altitude: float = Field(title="Altitude", description="meters")
    name: str = Field(title="Name", description="location name")


class LocationConfig(BaseModel):
    """Receiver and transmitter locations."""
    rx: LocationPoint = Field(title="Receiver")
    tx: LocationPoint = Field(title="Transmitter")


# ============================================================================
# ADS-B Truth Settings
# ============================================================================

class AdsbTruthConfig(BaseModel):
    """ADS-B ground truth matching settings."""
    enabled: bool = Field(title="Enabled")
    tar1090: str = Field(title="tar1090 Server")
    adsb2dd: str = Field(title="adsb2dd Address")
    delay_tolerance: float = Field(gt=0, title="Delay Tolerance")
    doppler_tolerance: float = Field(gt=0, title="Doppler Tolerance")


class TruthConfig(BaseModel):
    """Ground truth settings."""
    adsb: AdsbTruthConfig = Field(title="ADS-B Truth")


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


# ============================================================================
# Top-level Config
# ============================================================================

class UserConfig(BaseModel):
    """Top-level user config schema."""
    capture: Optional[CaptureConfig] = Field(default=None, title="Capture Settings")
    location: Optional[LocationConfig] = Field(default=None, title="Location Settings")
    truth: Optional[TruthConfig] = Field(default=None, title="Truth Settings")
    tar1090: Optional[Tar1090Config] = Field(default=None, title="tar1090 Settings")
