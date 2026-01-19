"""
Pydantic models for config validation and form generation.

These models define:
- Field types (int, bool, str) -> determines HTML input type
- Constraints (ge, le) -> HTML min/max attributes
- Metadata (title, description) -> form labels and help text

Form VALUES come from user.yml, not from schema defaults.
"""
from pydantic import BaseModel, Field
from typing import Optional


class CaptureDevice(BaseModel):
    """SDR device settings."""
    type: str = Field(title="Device Type")
    agcSetPoint: int = Field(le=0, title="AGC Set Point", description="dBFS")
    gainReduction: int = Field(ge=20, le=59, title="Gain Reduction", description="20-59 dB")
    lnaState: int = Field(ge=1, le=9, title="LNA State", description="1-9")
    dabNotch: bool = Field(title="DAB Notch Filter")
    rfNotch: bool = Field(title="RF Notch Filter")
    bandwidthNumber: int = Field(title="Bandwidth Number")


class CaptureConfig(BaseModel):
    """Capture/SDR settings."""
    fs: int = Field(title="Sample Rate", description="Hz")
    fc: int = Field(title="Center Frequency", description="Hz")
    device: CaptureDevice = Field(title="Device Settings")


class UserConfig(BaseModel):
    """Top-level user config schema."""
    capture: Optional[CaptureConfig] = Field(default=None, title="Capture Settings")
    # Future sections:
    # location: Optional[LocationConfig] = Field(default=None, title="Location Settings")
    # tar1090: Optional[Tar1090Config] = Field(default=None, title="ADS-B Settings")
