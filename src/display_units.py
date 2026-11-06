"""The node's display units: metric or imperial, as `display.units` in user.yml.

A presentation setting kept with the rest of the node's configuration so
config-merger carries it into config.yml, where the whole stack can read
it. Writing it never applies: the radar keeps working in hertz and
kilometers whichever is chosen, so nothing needs restarting, and the
merger sweeps the key in at the next apply or boot.
See docs/features/config-editor.md#display-units.
"""

METRIC = "metric"
IMPERIAL = "imperial"
CHOICES = (METRIC, IMPERIAL)
DEFAULT = METRIC
SECTION = "display"
KEY = "units"


class DisplayUnits:
    """Reads and writes the owner's choice of units in user.yml."""

    def __init__(self, config_mgr):
        self.config_mgr = config_mgr

    def get(self):
        """The stored choice, or metric when nothing valid is stored.

        Reads user.yml, not config.yml: the choice shows at once, while the
        merged file only carries it after the next apply.
        """
        try:
            section = self.config_mgr.load_user_config().get(SECTION) or {}
            value = section.get(KEY)
        except Exception:
            return DEFAULT
        return value if value in CHOICES else DEFAULT

    @staticmethod
    def validate(units):
        """Return (ok, error). Only the two names are accepted."""
        if units not in CHOICES:
            return False, "Units must be metric or imperial"
        return True, None

    def set(self, units):
        """Store the choice in user.yml. Returns (ok, error). Never applies."""
        ok, error = self.validate(units)
        if not ok:
            return False, error
        try:
            user_config = dict(self.config_mgr.load_user_config())
            section = dict(user_config.get(SECTION) or {})
            section[KEY] = units
            user_config[SECTION] = section
            self.config_mgr.save_user_config(user_config)
        except Exception as e:
            return False, f"Could not save the units: {e}"
        return True, None
