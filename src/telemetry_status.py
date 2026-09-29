"""Reader for the status document retina-telemetry writes.

That service binds no ports, so this file is the only channel out of it, and
retina-gui is its only reader. `node_ref` falls back to an on-disk
last-known-good copy while the document says null (up to one heartbeat after a
telemetry restart).

See docs/features/device-state-and-telemetry.md#telemetry-status-document.
"""

import json
import os
from datetime import datetime, timedelta, timezone

# Past this, the service is no longer running. It writes every ~10s
# (retina-telemetry's STATUS_INTERVAL_S), so a restart never trips it.
STALE_AFTER = timedelta(minutes=2)

# Normal states that pass on their own; their `detail` is suppressed. An
# exclusion list on purpose, so a fault state added later surfaces by default.
# See docs/features/device-state-and-telemetry.md#transient-states.
TRANSIENT_STATES = frozenset({"registering", "awaiting_config", "starting"})


def _parse_timestamp(written_at):
    """Parse retina-telemetry's `written_at`, or None if we cannot.

    A naive value is assumed to be UTC, not local time, or a node in another
    timezone would make a healthy service look hours stale.
    """
    if not written_at:
        return None
    try:
        written = datetime.fromisoformat(written_at)
    except (TypeError, ValueError):
        return None
    return written.replace(tzinfo=timezone.utc) if written.tzinfo is None else written


def _in_words(written):
    """How long ago that was, in prose (e.g. "5 minutes ago"), or None."""
    if written is None:
        return None

    seconds = max(0, int((datetime.now(timezone.utc) - written).total_seconds()))
    if seconds < 90:
        return "less than a minute ago"
    for amount, unit in ((3600, "minute"), (86400, "hour"), (None, "day")):
        if amount is None or seconds < amount:
            divisor = {"minute": 60, "hour": 3600, "day": 86400}[unit]
            count = seconds // divisor
            return f"{count} {unit}{'s' if count != 1 else ''} ago"


def _sentence(detail):
    """Uppercase the first character and change nothing else.

    Not Jinja's `capitalize`, which lowercases the rest ("Mender" -> "mender").
    """
    if not detail:
        return detail
    return detail[0].upper() + detail[1:]


class TelemetryStatus:
    """Reads retina-telemetry's status document. Never raises.

    An unreadable or absent document means "not installed", not a fault.
    """

    def __init__(self, status_path, node_ref_cache_path):
        self.status_path = status_path
        self.node_ref_cache_path = node_ref_cache_path

    def read(self) -> dict | None:
        """Return what to show the operator, or None if telemetry isn't installed.

        Keys:
            state:      the raw state string from the document, or None
            detail:     prose to show verbatim, or None when there is nothing
                        worth saying (see TRANSIENT_STATES)
            node_ref:   live value, falling back to the last known one
            node_id:    as reported by telemetry, which reads it directly
            stale:      True when the document is too old to believe, meaning
                        the container is not running
            last_report: how long ago it last wrote, in words, or None if the
                        timestamp was missing or unreadable
            claim:      where the node's claim stands, as {state, email,
                        undeliverable}, or None
        """
        document = self._load()
        if document is None:
            return None

        node_ref = document.get("node_ref")
        if node_ref:
            self._remember_node_ref(node_ref)
        else:
            node_ref = self._recall_node_ref()

        state = document.get("state")
        detail = document.get("detail")
        written = _parse_timestamp(document.get("written_at"))

        return {
            "state": state,
            "detail": None if state in TRANSIENT_STATES else _sentence(detail),
            "node_ref": node_ref,
            "node_id": document.get("node_id"),
            # Undatable means stale: retina-telemetry always writes `written_at`.
            "stale": written is None or datetime.now(timezone.utc) - written > STALE_AFTER,
            "last_report": _in_words(written),
            # None until a heartbeat response carries it (or on telemetry
            # older than spec 1.4.0). Never default this to "unclaimed".
            # See docs/features/device-state-and-telemetry.md#claim-state.
            "claim": document.get("claim"),
        }

    # ── The document ───────────────────────────────────────────

    def _load(self) -> dict | None:
        try:
            with open(self.status_path) as f:
                document = json.load(f)
        except (OSError, ValueError):
            # Absent is normal without the telemetry package; malformed is
            # treated the same.
            return None
        return document if isinstance(document, dict) else None

    # ── The node_ref cache ─────────────────────────────────────

    def _remember_node_ref(self, node_ref):
        """Store the current node_ref, if it isn't what we already have.

        Best effort: a failed write must not stop the page rendering the value.
        See docs/features/device-state-and-telemetry.md#node-reference-cache.
        """
        if node_ref == self._recall_node_ref():
            return
        try:
            os.makedirs(os.path.dirname(self.node_ref_cache_path), exist_ok=True)
            with open(self.node_ref_cache_path, "w") as f:
                f.write(node_ref)
        except OSError:
            pass

    def _recall_node_ref(self):
        try:
            with open(self.node_ref_cache_path) as f:
                return f.read().strip() or None
        except OSError:
            return None
