"""The node's display units: metric or imperial.

A presentation setting only, stored on /data so every browser that opens
this node sees the same units. Nothing in the radar config changes with it:
blah2 keeps working in Hz and kilometres, and the pages convert what they
show. See docs/features/config-editor.md#display-units.
"""

import os
import tempfile

METRIC = "metric"
IMPERIAL = "imperial"
CHOICES = (METRIC, IMPERIAL)
DEFAULT = METRIC


class DisplayUnits:
    """Reads and writes the owner's choice of units for this node."""

    def __init__(self, units_file):
        self.units_file = units_file
        self.data_dir = os.path.dirname(units_file)

    def get(self):
        """The stored choice, or metric when nothing valid is stored."""
        try:
            with open(self.units_file) as f:
                value = f.read().strip()
        except OSError:
            return DEFAULT
        return value if value in CHOICES else DEFAULT

    @staticmethod
    def validate(units):
        """Return (ok, error). Only the two names are accepted."""
        if units not in CHOICES:
            return False, "Units must be metric or imperial"
        return True, None

    def set(self, units):
        """Store the choice. Returns (ok, error)."""
        ok, error = self.validate(units)
        if not ok:
            return False, error
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(dir=self.data_dir)
            with os.fdopen(fd, "w") as f:
                f.write(units)
            os.chmod(tmp_path, 0o644)
            os.rename(tmp_path, self.units_file)
        except OSError as e:
            return False, f"Could not save the units: {e}"
        return True, None
