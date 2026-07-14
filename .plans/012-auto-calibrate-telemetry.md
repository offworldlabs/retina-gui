# Config telemetry payload contract (schema 2)

retina-gui reports its node configuration to a server-side ingest application
so the server always has an up-to-date record of every node's actual config.
This document is the contract for building that ingest service — the client
side (`src/config_telemetry.py`) is already implemented and versions every
payload with `"schema": 2`.

**Superseded design**: this replaces the original per-calibration-run
telemetry (schema 1 — a `run_summary` with descent history/evidence, plus a
separate `applied` event), which only covered Auto-Calibrate. The new design
is simpler and much broader: whenever a node's configuration is actually
applied (config-merger + restart) or its mode changes, the client sends a
single event carrying the *entire* current merged config — not a diff, not a
feature-specific summary.

## Transport

- `POST` with a JSON body to `CONFIG_TELEMETRY_URL` (environment variable on
  the node; empty/unset disables sending entirely — renamed from
  `CALIBRATION_TELEMETRY_URL`).
- Fire-and-forget, dispatched on a background thread so it never adds latency
  to the request that triggered it: 5 s timeout, one attempt, failures are
  swallowed. The ingest service must treat delivery as best-effort.
- One event type: `config_snapshot`. The server should treat each snapshot as
  a full replacement of its record for that `node_id`, not a delta to merge.

## Event: `config_snapshot`

Sent whenever `routes/mode.py`'s `run_config_merger_and_restart()` succeeds
(covers `/towers/select`, `/calibrate/apply`, `/config/apply`) or `_write_mode()`
runs (covers `/api/mode`, wizard completion, and the wizard's
navigate-away-reverts-to-radar beacon).

```json
{
  "schema": 2,
  "event": "config_snapshot",
  "node_id": "ret7dd2cb0d",
  "sent_at": "2026-07-14T01:09:44+00:00",
  "trigger": "calibrate_apply",
  "mode": "radar",
  "config": {
    "capture": {"fc": 105100000, "device": {"gainReduction": [20, 20], "lnaState": 3, "...": "..."}},
    "location": {"rx": {"...": "..."}, "tx": {"...": "..."}},
    "truth": {"adsb": {"...": "..."}},
    "tar1090": {"...": "..."}
  }
}
```

Field notes:

- `trigger` is purely descriptive (server-side analytics/debugging), one of:
  `config_apply` (general `/config` page), `tower_select`, `calibrate_apply`,
  `mode_switch`, `wizard_complete`.
- `mode` is the node's current mode (`radar` / `spectrum` / `sdrconnect`) at
  the moment of the snapshot.
- `config` is the raw merged config tree exactly as `ConfigManager.load_merged_config()`
  returns it — whatever top-level keys exist there (capture, location, truth,
  tar1090, ...), unfiltered. No whitelist/blacklist is applied on the client
  side; if a field shouldn't leave the node, it needs to be handled by the
  ingest service or reconsidered as a config field.
- A snapshot from `run_config_merger_and_restart()` is only sent on success —
  a failed merge/restart doesn't reflect a real, currently-running config, so
  sending one would misrepresent node state to the server.
