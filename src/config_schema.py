"""Pydantic models for config validation and form generation.

Display from config.yml (merged), write only changed values to user.yml.
Field title/description strings are UI copy. See docs/features/config-editor.md.
"""
import os
from copy import deepcopy
from typing import Literal

import yaml
from pydantic import VERSION, BaseModel, Field

# Detect Pydantic version for Field() syntax
PYDANTIC_V2 = VERSION.startswith("2.")

# retina-telemetry's `tx_callsign` cap in the node-ingest spec. Must match the
# spec and maxlength="32" in config.html.
# See docs/features/config-editor.md#transmitter-name-length
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


def model_values(model):
    """A validated model's values as a dict (Pydantic v1 and v2)."""
    return model.model_dump() if PYDANTIC_V2 else model.dict()


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
    device_dabNotch: bool = Field(title="DAB Notch Filter", description="Blocks 155-235 MHz (DAB and VHF TV). Leave on unless your tower is in this range. Changing it changes signal level a lot, so recalibrate afterwards.")
    device_rfNotch: bool = Field(title="RF Notch Filter", description="Blocks 77-115 MHz (FM radio). Leave on unless your tower is in this range. Changing it changes signal level a lot, so recalibrate afterwards.")
    device_bandwidthNumber: Literal[0, 5, 50, 100] = Field(
        title="Bandwidth Number",
        description="AGC loop bandwidth (Hz). 0 disables AGC: gain is fixed by Gain Reduction/LNA State. "
                     "5/50/100 enable AGC: lower is slower and more stable, higher reacts faster but chases noise more."
    )


# ============================================================================
# Processing Settings
# ============================================================================

class ProcessingFormConfig(BaseModel):
    """Flat process config for the Processing section.

    For testing detection: every field changes what the radar reports. The
    copy says what each does and what to expect; the page adds live readouts
    in physical units. See docs/features/config-editor.md#processing-section
    and blah2-arm's config/README.md for the measurements behind it.
    """
    detection_minDoppler: float = Field(
        ge=0, le=1000, title="Minimum Doppler",
        description="Hz. Slower detections are not reported. Keeps slowly moving clutter out, and "
                    "hides every slow target with it. Expect: lowering it adds detections in the newly "
                    "tested Doppler rows first. If those rows fill up while the rest of the map does not, "
                    "this site has moving clutter there.")
    detection_maxReportedDoppler: float | None = Field(
        None, ge=0, le=1000, title="Maximum Reported Doppler",
        description="Hz. Faster detections are not reported. The radar still processes them, so a fast "
                    "aircraft cannot fold into the reported band as a fake slow target, which is what "
                    "narrowing the Doppler span would do. Expect: detections above it disappear and "
                    "nothing below it changes.")
    detection_minDelay: int = Field(
        ge=0, le=400, title="Minimum Delay",
        description="Delay bins. Nearer detections are not reported. Keeps out the noise next to the "
                    "direct signal, and hides targets close to the receiver. Expect: lowering it adds "
                    "detections at the nearest delays, where false alarms are most likely.")
    ambiguity_delayMax: int = Field(
        ge=10, le=600, title="Maximum Delay",
        description="Delay bins. How far out the radar processes at all. Lowering it costs nothing "
                    "for nearer targets and means fewer cells, so fewer false alarms. Expect: nothing "
                    "is detected or shown on the map beyond it.")
    detection_pfa: float = Field(
        ge=1e-7, le=1e-3, title="False Alarm Probability",
        description="Chance that a cell of pure noise is reported, for example 0.00001. Higher reports "
                    "weaker targets and more false alarms. Expect: each 10x increase gives about 10x the "
                    "false alarms, spread evenly over the map, for about 1.4 dB more sensitivity.")
    detection_nTrain: int = Field(
        ge=1, le=32, title="Training Cells",
        description="Delay bins each side used to estimate the local noise. More gives a steadier, lower "
                    "threshold but follows changes in the noise less closely. Expect: slightly more "
                    "detections far out, slightly fewer at the nearest delays.")
    detection_nGuard: int = Field(
        ge=0, le=10, title="Guard Cells",
        description="Delay bins each side skipped between a cell and its training cells, so a target "
                    "does not raise its own threshold.")
    detection_nCentroid: int = Field(
        ge=0, le=20, title="Merge Distance",
        description="Bins. A detection is dropped when a stronger one lies within this many delay and "
                    "Doppler bins of it. Expect: lowering it keeps close targets apart but can split one "
                    "strong target into several detections.")
    ambiguity_dopplerWindow: Literal["none", "hann"] | None = Field(
        None, title="Doppler Window",
        description="hann stops strong targets and slow clutter leaking into neighbouring Doppler bins, "
                    "and costs every target 1.76 dB. Expect: fewer detections next to strong targets, "
                    "and slightly fewer of the weakest targets.")
    clutter_enable: bool = Field(
        title="Clutter Filter",
        description="Removes the direct signal and still clutter. Turn it off only to see them: the "
                    "detector will fire heavily near 0 Hz while it is off.")
    ambiguity_dopplerMin: int | None = _readonly_field(
        default=None, title="Doppler Span Minimum",
        description="Hz. Fixed. A narrower span folds fast targets back in as fake slow ones. Use "
                    "Maximum Reported Doppler instead.")
    ambiguity_dopplerMax: int | None = _readonly_field(
        default=None, title="Doppler Span Maximum",
        description="Hz. Fixed, for the same reason.")
    data_cpi: float | None = _readonly_field(
        default=None, title="Integration Time",
        description="Seconds. Fixed: the clutter filter is tuned for 0.5 s. Sets the Doppler bin size.")
    clutter_delayMax: int | None = _readonly_field(
        default=None, title="Clutter Filter Extent",
        description="Delay bins. Fixed: shorter leaves far clutter uncancelled.")


#: The processing fields this page writes, as (form field, path under
#: `process`). The read-only fields post too but are never written.
PROCESSING_EDITABLE_FIELDS = (
    ("detection_minDoppler", ("detection", "minDoppler")),
    ("detection_maxReportedDoppler", ("detection", "maxReportedDoppler")),
    ("detection_minDelay", ("detection", "minDelay")),
    ("ambiguity_delayMax", ("ambiguity", "delayMax")),
    ("detection_pfa", ("detection", "pfa")),
    ("detection_nTrain", ("detection", "nTrain")),
    ("detection_nGuard", ("detection", "nGuard")),
    ("detection_nCentroid", ("detection", "nCentroid")),
    ("ambiguity_dopplerWindow", ("ambiguity", "dopplerWindow")),
    ("clutter_enable", ("clutter", "enable")),
)

#: Shown locked, with the reason in their description.
PROCESSING_READONLY_FIELDS = (
    ("ambiguity_dopplerMin", ("ambiguity", "dopplerMin")),
    ("ambiguity_dopplerMax", ("ambiguity", "dopplerMax")),
    ("data_cpi", ("data", "cpi")),
    ("clutter_delayMax", ("clutter", "delayMax")),
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

    Optional, but the six coordinates are all-or-nothing, enforced in
    routes/config.py. See docs/features/config-editor.md#location-all-or-nothing.
    """
    rx_latitude: float | None = Field(None, ge=-90, le=90, title="Receiver Latitude", description="decimal degrees")
    rx_longitude: float | None = Field(None, ge=-180, le=180, title="Receiver Longitude", description="decimal degrees")
    rx_altitude: float | None = Field(None, title="Receiver Altitude", description="meters")
    rx_name: str | None = Field(None, title="Receiver Name", description="location name")
    tx_latitude: float | None = Field(None, ge=-90, le=90, title="Transmitter Latitude", description="decimal degrees")
    tx_longitude: float | None = Field(None, ge=-180, le=180, title="Transmitter Longitude", description="decimal degrees")
    tx_altitude: float | None = Field(None, title="Transmitter Altitude", description="meters")
    # Contract limit, not display: a longer name blocks registration.
    tx_name: str | None = Field(None, max_length=TX_NAME_MAX_LENGTH, title="Transmitter Name", description="location name")

    # No model validator: nodes run pydantic v1. The all-or-nothing rule is in
    # routes/config.py:_location_errors.

    @property
    def is_located(self) -> bool:
        """Whether this carries a usable geometry. Never partially true."""
        return all(getattr(self, f) is not None for f in LOCATION_COORDINATE_FIELDS)


# ============================================================================
# Owner Contact Details
# ============================================================================
#: Copied from the node-ingest spec's `NodeContact`; move only when the spec
#: does. See docs/features/config-editor.md#how-we-reach-you.
CONTACT_NAME_MAX_LENGTH = 64
CONTACT_EMAIL_MAX_LENGTH = 255
CONTACT_PHONE_MAX_LENGTH = 32

#: ISO 3166-1 alpha-2 country of the *phone number*, not of the owner.
#: Checked in routes/setup.py: `regex=`/`pattern=` each work on only one
#: pydantic version. See docs/features/config-editor.md#pydantic-v1-and-v2.
CONTACT_COUNTRY_PATTERN = r"^[A-Za-z]{2}$"

CONTACT_FIELDS = ("first_name", "last_name", "email", "phone", "country")


class ContactFormConfig(BaseModel):
    """Whom to contact about this node, as its owner gave them.

    All optional; empty is the steady state. Kept out of the agreement records'
    file so a malformed contact cannot stop registration.
    See docs/features/config-editor.md#how-we-reach-you.
    """
    first_name: str | None = Field(None, max_length=CONTACT_NAME_MAX_LENGTH, title="First Name")
    last_name: str | None = Field(None, max_length=CONTACT_NAME_MAX_LENGTH, title="Last Name")
    email: str | None = Field(None, max_length=CONTACT_EMAIL_MAX_LENGTH, title="Email")
    phone: str | None = Field(None, max_length=CONTACT_PHONE_MAX_LENGTH, title="Phone")
    country: str | None = Field(
        None, max_length=2, title="Country",
        description="two-letter country the phone number belongs to",
    )

    @property
    def is_empty(self) -> bool:
        """Whether there is anything at all to report (skipped and cleared are the same)."""
        return all(getattr(self, f) is None for f in CONTACT_FIELDS)


#: Copied from the node-ingest spec's `NodeClaimRequest.email`.
CLAIM_EMAIL_MAX_LENGTH = 255

#: Deliberately thin, no stricter than the server. Checked in routes/setup.py
#: (pydantic v1/v2). See docs/features/config-editor.md#node-claim.
CLAIM_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+$"


class ClaimFormConfig(BaseModel):
    """The address that owns this node: the server mails it a claim link.

    **Not the contact email.** Kept in its own file; never copy one into the
    other. See docs/features/config-editor.md#node-claim.
    """
    email: str | None = Field(None, max_length=CLAIM_EMAIL_MAX_LENGTH, title="Email")


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
# tar1090 Settings
# ============================================================================

class Tar1090Config(BaseModel):
    """tar1090 ADS-B viewer configuration.

    adsb_source is one "host,port,protocol" string in YAML, split into three
    fields here. They are all-or-nothing, and may be empty only with
    adsblol_fallback on (enforced in routes/config.py).
    See docs/features/config-editor.md#ads-b-source.
    """
    adsb_source_host: str | None = Field(None, title="ADS-B Host", description="IP or hostname")
    adsb_source_port: int | None = Field(None, ge=1, le=65535, title="ADS-B Port")
    adsb_source_protocol: str | None = Field(None, title="Protocol", description="e.g. beast_in")
    adsblol_fallback: bool = Field(title="adsb.lol Fallback")
    adsblol_radius: int = Field(ge=1, le=500, title="adsb.lol Radius", description="nautical miles")


