"""Reader for the status document retina-telemetry writes.

That service binds no ports and nothing pushes to it, so this file is the only
channel out of it — its logs are inside a container the owner cannot see, and
there is no endpoint to ask. We are its only reader.

Its own docstring says the shape deliberately mirrors `mender-update.status`,
which device_state already handles: a JSON object carrying its own timestamp,
treated as stale past a timeout. The logic here is the same; it lives in its
own module because device_state is the *device state machine* and telemetry
status is not part of it.

## node_ref, and why it is cached

`node_ref` is the owner's public identifier — the thing they need to find their
node on the server's views. It is assigned by the server and arrives only in a
registration or heartbeat response, so this document is the sole path by which
the node ever learns it.

retina-telemetry holds it in memory only: `State.store_token` persists the
token and nothing else, on the grounds that node_ref is re-obtainable from the
server without an operator. So for up to one heartbeat interval (60s by
default) after that container restarts, the document legitimately carries
`node_ref: null` on a perfectly healthy registered node.

That is why we keep a last-known-good copy on disk. It is *not* for noticing
rotation — telemetry already does that in `State.apply_levels`, and the file
always carries the live value, so the file is the source of truth and this
cache is only consulted when it says null. On disk rather than in memory
because the case that matters is a node reboot, where both services come back
at once and an in-memory copy would be empty at exactly the moment the owner is
looking. It never expires: a node_ref stays valid indefinitely, and blanking it
when telemetry dies would remove the identifier precisely when someone needs to
quote it to support.
"""

import json
import os
from datetime import datetime, timedelta, timezone

# Past this, the document describes a service that is no longer running. It
# writes every ~10s (STATUS_INTERVAL_S in retina-telemetry's settings), so this
# is generous enough that an ordinary restart never trips it.
STALE_AFTER = timedelta(minutes=2)

# States that are normal and will pass on their own. Their `detail` is
# suppressed so a healthy node has nothing to say for itself.
#
# Deliberately an exclusion list rather than a list of states worth showing: a
# fault state added to retina-telemetry later would be silently hidden by the
# latter, whereas here anything unrecognised surfaces by default. The only
# thing this list ever needs to contain is states meaning "this is normal and
# transient", which is a far more stable set than the faults.
TRANSIENT_STATES = frozenset({"registering", "awaiting_config", "starting"})


def _sentence(detail):
    """Uppercase the first character and change nothing else.

    retina-telemetry writes these as lowercase fragments meant to follow a
    state word, which reads as a typo on a card of its own. Only the first
    character moves: Jinja's `capitalize` lowercases the remainder, which turns
    "Mender" into "mender" and "MAC-based" into "mac-based" — and these strings
    are meant to be shown verbatim.
    """
    if not detail:
        return detail
    return detail[0].upper() + detail[1:]


class TelemetryStatus:
    """Reads retina-telemetry's status document. Never raises.

    An unreadable or absent document is reported as "not installed" rather than
    as a fault: on a node that has never had the telemetry package, there is no
    file and nothing is wrong.
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
            written_at: the raw timestamp, for display alongside `stale`
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

        return {
            "state": state,
            "detail": None if state in TRANSIENT_STATES else _sentence(detail),
            "node_ref": node_ref,
            "node_id": document.get("node_id"),
            "stale": self._is_stale(document.get("written_at")),
            "written_at": document.get("written_at"),
        }

    # ── The document ───────────────────────────────────────────

    def _load(self) -> dict | None:
        try:
            with open(self.status_path) as f:
                document = json.load(f)
        except (OSError, ValueError):
            # Absent is the ordinary state on a node without the telemetry
            # package; malformed is indistinguishable from absent to an
            # operator, and neither is worth an alarm on the home page.
            return None
        return document if isinstance(document, dict) else None

    def _is_stale(self, written_at) -> bool:
        """Whether the document is too old to describe a running service.

        A document we cannot date is treated as stale: retina-telemetry writes
        `written_at` on every write, so its absence means this is not a
        document we understand.
        """
        if not written_at:
            return True
        try:
            written = datetime.fromisoformat(written_at)
        except (TypeError, ValueError):
            return True
        if written.tzinfo is None:
            written = written.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - written > STALE_AFTER

    # ── The node_ref cache ─────────────────────────────────────

    def _remember_node_ref(self, node_ref):
        """Store the current node_ref, if it isn't what we already have.

        Best-effort: this is a display convenience, so a failure to write it
        must not stop the page rendering the value we were given.
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
