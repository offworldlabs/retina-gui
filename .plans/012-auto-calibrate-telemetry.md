# Auto-Calibrate telemetry payload contract (schema 1)

retina-gui reports auto-calibration runs to a server-side ingest application
so tuning behaviour can be analysed fleet-wide. This document is the contract
for building that ingest service — the client side (`src/calibration_telemetry.py`)
is already implemented and versions every payload with `"schema": 1`.

## Transport

- `POST` with a JSON body to `CALIBRATION_TELEMETRY_URL` (environment variable
  on the node; empty/unset disables sending entirely).
- Fire-and-forget: 5 s timeout, one attempt, failures are swallowed. The
  ingest service must treat delivery as best-effort and idempotency as
  desirable (`node_id` + `run.started_at` uniquely identify a run).
- Two event types, distinguished by the top-level `event` field.

## Event: `run_summary`

Sent once when a run reaches a terminal state (`done`, `failed`, `cancelled`).

```json
{
  "schema": 1,
  "event": "run_summary",
  "node_id": "ret7dd2cb0d",
  "location": {"latitude": -33.86, "longitude": 151.20, "altitude": 23},
  "run": {
    "started_at": "2026-07-08T01:02:03+00:00",
    "finished_at": "2026-07-08T01:09:44+00:00",
    "state": "done",
    "error": null,
    "original": {"fc": 98000000, "gain_a": 40, "gain_b": 41},
    "progress": {"towers_tried": 2, "towers_total": 3, "retunes": 9,
                 "elapsed_seconds": 0, "budget_seconds": 600},
    "history": [
      {
        "tower_name": "ABC-FM",
        "fc": 98000000,
        "descent": [
          {"gain_a": 20, "gain_b": 20, "overload_a": true, "overload_b": false},
          {"gain_a": 30, "gain_b": 20, "overload_a": false, "overload_b": false},
          {"gain_a": 25, "gain_b": 20, "overload_a": true, "overload_b": false}
        ],
        "final_gain_a": 30,
        "final_gain_b": 20,
        "dwell_seconds": 150.2,
        "outcome": "no_confirmed_track",
        "max_evidence": 1,
        "max_detections": 3
      },
      {
        "tower_name": "XYZ-FM",
        "fc": 105100000,
        "descent": [
          {"gain_a": 20, "gain_b": 20, "overload_a": false, "overload_b": false}
        ],
        "final_gain_a": 20,
        "final_gain_b": 20,
        "dwell_seconds": 41.7,
        "outcome": "confirmed_track",
        "max_evidence": 4
      }
    ],
    "best_attempt": {"tower_name": "ABC-FM", "fc": 98000000, "gain_a": 30,
                     "gain_b": 20, "evidence": 1,
                     "reason": "detections seen, no track initiated",
                     "max_detections": 3},
    "result": {"tower_name": "XYZ-FM", "fc": 105100000, "gain_a": 20,
               "gain_b": 20, "track_id": "0A3F"}
  }
}
```

Field notes:

- `location` is the RX position from the node's merged config; may be `null`
  if setup never completed.
- `run.state`: `done` (confirmed track), `failed` (no track in budget, or a
  protocol error — see `run.error`), `cancelled` (user).
- `run.original` is the tuning the node started from (and was restored to on
  any non-`done` outcome).
- `history` has one entry per tower attempted, in order. `descent` records
  each gain candidate tried during overload backoff with the overload flags
  observed at that setting. `outcome` is one of `confirmed_track`,
  `no_confirmed_track`, `not_reached` (run ended mid-tower).
- `max_evidence` grades the best evidence seen while dwelling: 0 none,
  1 CFAR detections, 2 tentative tracks, 3 associated tracks, 4 confirmed
  (ACTIVE) track.
- `result` is only present when `state` is `done`.

## Event: `applied`

Sent when the user explicitly persists a successful result to the node's
config (an interesting signal: did the user trust the calibration?).

```json
{
  "schema": 1,
  "event": "applied",
  "node_id": "ret7dd2cb0d",
  "applied_at": "2026-07-08T01:11:20+00:00",
  "run_started_at": "2026-07-08T01:02:03+00:00",
  "result": {"tower_name": "XYZ-FM", "fc": 105100000, "gain_a": 20,
             "gain_b": 20, "track_id": "0A3F"}
}
```

`run_started_at` links the event back to its `run_summary`.
