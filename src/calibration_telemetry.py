"""Fire-and-forget telemetry for auto-calibration runs.

One summary POST when a run reaches a terminal state, plus a small event when
the user persists a successful result. Failures are swallowed — telemetry must
never affect the run outcome or the UI. An empty URL disables sending.

The payload contract for the (separately built) server-side ingest app is
documented in .plans/012-auto-calibrate-telemetry.md.
"""

from datetime import datetime, timezone

import requests

SCHEMA_VERSION = 1
TELEMETRY_TIMEOUT_SECONDS = 5


def _post(url, payload):
    if not url:
        return False
    try:
        requests.post(url, json=payload, timeout=TELEMETRY_TIMEOUT_SECONDS)
        return True
    except Exception:
        return False


def build_run_report(status, node_id, rx_location):
    """Assemble the end-of-run summary from a terminal calibrator status."""
    return {
        "schema": SCHEMA_VERSION,
        "event": "run_summary",
        "node_id": node_id,
        "location": rx_location,
        "run": {
            "started_at": status.get("started_at"),
            "finished_at": status.get("finished_at"),
            "state": status.get("state"),
            "error": status.get("error"),
            "original": status.get("original"),
            "progress": status.get("progress"),
            "history": status.get("history"),
            "best_attempt": status.get("best_attempt"),
            "result": status.get("result"),
        },
    }


def send_run_report(url, status, node_id, rx_location):
    return _post(url, build_run_report(status, node_id, rx_location))


def build_applied_event(status, node_id):
    """The user chose to persist the calibration result."""
    return {
        "schema": SCHEMA_VERSION,
        "event": "applied",
        "node_id": node_id,
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "run_started_at": status.get("started_at"),
        "result": status.get("result"),
    }


def send_applied_event(url, status, node_id):
    return _post(url, build_applied_event(status, node_id))
