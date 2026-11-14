"""The node's delay-Doppler extent, for the pages that plot it (Tracker, Passive Radar).

Both pages draw the same fixed axes from it, so one node always reads at one scale.
See docs/features/tracker.md#axis-bounds.
"""

# For turning blah2's delay bins into kilometres.
SPEED_OF_LIGHT = 299792458.0


def drawable(lo, hi):
    """Whether a pair is an axis rather than a typo: both present, hi > lo."""
    return lo is not None and hi is not None and hi > lo


def axis_bounds():
    """The plot's axis ranges, from the node's blah2 ambiguity bounds.

    Returns {"doppler": [lo, hi] Hz, "delay": [lo, hi] km}, with None for an
    axis the config does not state (the page then falls back). Never guess a range.
    """
    from app import app, config_mgr

    blank = {"doppler": None, "delay": None}
    try:
        config = config_mgr.load_merged_config() or {}
    except Exception as e:
        app.logger.warning(f"Plot axes unavailable, config did not load: {e}")
        return blank

    ambiguity = (config.get("process", {}) or {}).get("ambiguity", {}) or {}
    fs = (config.get("capture", {}) or {}).get("fs")

    bounds = dict(blank)
    lo, hi = ambiguity.get("dopplerMin"), ambiguity.get("dopplerMax")
    if drawable(lo, hi):
        bounds["doppler"] = [lo, hi]

    lo, hi = ambiguity.get("delayMin"), ambiguity.get("delayMax")
    if drawable(lo, hi) and fs:
        cell_km = SPEED_OF_LIGHT / float(fs) / 1000.0
        bounds["delay"] = [lo * cell_km, hi * cell_km]

    return bounds
