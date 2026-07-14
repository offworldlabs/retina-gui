"""Config telemetry: a full config snapshot is sent to CONFIG_TELEMETRY_URL
every time a node's configuration is actually applied (config-merger +
restart) or its mode changes, so the server always has an up-to-date record
of every node's real configuration — not just a narrow slice of one feature's
activity. Replaces the earlier per-calibration-run telemetry (descent
history, evidence), which only covered auto-calibrate.

Client-side only, unconditional (no consent gate). Fire-and-forget on a
background thread — these hooks live inside synchronous request handlers
(tower select, config apply, mode switch), so a slow or unreachable
telemetry endpoint must never add latency to the action that triggered it,
let alone affect its outcome. Failures are swallowed. An empty URL disables
sending entirely.
"""

import threading
from datetime import datetime, timezone

import requests

SCHEMA_VERSION = 2
TELEMETRY_TIMEOUT_SECONDS = 5


def build_config_snapshot(node_id, mode, merged_config, trigger):
    """Assemble a full-config snapshot payload.

    trigger: what caused this snapshot — e.g. "config_apply", "tower_select",
    "calibrate_apply", "mode_switch", "wizard_complete". Purely descriptive,
    for the server's own analytics/debugging.
    """
    return {
        "schema": SCHEMA_VERSION,
        "event": "config_snapshot",
        "node_id": node_id,
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "trigger": trigger,
        "mode": mode,
        "config": merged_config,
    }


def _post(url, payload):
    if not url:
        return False
    try:
        requests.post(url, json=payload, timeout=TELEMETRY_TIMEOUT_SECONDS)
        return True
    except Exception:
        return False


def send_config_snapshot(url, node_id, mode, merged_config, trigger):
    """Fire-and-forget: dispatches the POST on a daemon thread and returns
    immediately. Returns False without spawning anything if url is empty."""
    if not url:
        return False
    payload = build_config_snapshot(node_id, mode, merged_config, trigger)
    threading.Thread(target=_post, args=(url, payload), daemon=True).start()
    return True
