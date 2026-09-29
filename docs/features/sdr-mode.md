# SDR mode and stack restarts

The node's single SDRplay RSPduo can be owned by one of three consumers at a time: the blah2 radar stack (`radar`), the retina-spectrum container (`spectrum`), or the SDRconnect systemd service (`sdrconnect`). `routes/mode.py` switches between them, persists the choice, and also owns the shared "merge config and restart the radar stack" path used by config apply, tower selection and Auto-Calibrate. Every transition restarts `sdrplay_apiService` before handing the device over, and that restart needs more than a plain `systemctl restart`.

## Where it lives

| File | Role |
| --- | --- |
| [routes/mode.py](../../src/routes/mode.py) | Mode routes, `restart_sdrplay_service`, `run_config_merger_and_restart`, `enforce_radar_mode` |
| [restart_lock.py](../../src/restart_lock.py) | Cross-process lock every `docker compose` caller must hold |
| [stack_reconcile.py](../../src/stack_reconcile.py) | Repairs half-recreated containers after a failed recreate |
| [apply_service.py](../../src/apply_service.py) | Background config apply, calls `run_config_merger_and_restart` with the long lock timeout |
| [routes/mender_routes.py](../../src/routes/mender_routes.py) | Calls `enforce_radar_mode` to recover a failed install |

## Persisted mode

The mode lives in `mode.txt` in the data dir. `get_current_mode` returns `radar` if the file holds anything else. If the file cannot be read (a dev machine with no `/data`, or before a mode has ever been written) it returns an in-memory cache, which defaults to `radar`. `_write_mode` updates the cache first, so dev machines still behave consistently.

The blah2-arm watchdog reads this file: it leaves blah2 alone when the mode is not `radar`.

## Switching modes

`POST /api/mode` with `{"mode": "radar" | "spectrum" | "sdrconnect"}`.

It refuses with 409 when:

- Auto-Calibrate is running. Every transition stops or restarts blah2, which would take the SDR away from a calibration run. `calibrator.is_running()` is checked as well as the lock file, because an ADS-B calibration run has no time limit and can outlive the lock file's 20-minute staleness window.
- A Mender install is in progress. It replaces the very containers and manifests the transition manipulates, and `mender-update`'s docker commands are outside the restart lock. See [ota-updates.md](ota-updates.md).
- The restart lock is busy past its default timeout.

If retina-node is not installed (dev or pre-deployment), it just persists the mode and runs nothing.

Otherwise `_set_mode_locked` runs the transition with the restart lock held for the whole transition, not just one command, because every branch stops or starts containers that a config apply or the watchdog might be recreating at the same moment.

| Target | Order of operations |
| --- | --- |
| `spectrum` | Write mode first. Stop `sdrconnect.service` or the blah2 containers. Start `retina-spectrum` (compose profile `spectrum`). |
| `sdrconnect` | Write mode first. Stop and remove `retina-spectrum`, or stop the blah2 containers. `restart_sdrplay_service`. Start `sdrconnect.service`. |
| `radar` | Stop `sdrconnect.service`, or stop and remove `retina-spectrum`. `restart_sdrplay_service`. Force-recreate blah2, blah2_api, blah2_web, blah2_host and retina-tracker. Write mode last. |

Leaving radar, the mode is written first so the watchdog's guard applies immediately and it cannot see blah2 stopped mid-transition and trigger a spurious stack restart. Returning to radar, the mode is written only once the stack is up.

`retina-spectrum` is removed, not just stopped, so it cannot be auto-restarted and so the SDR is cleanly released before the next owner claims it.

### Readiness probes

| Route | Ready when |
| --- | --- |
| `GET /api/spectrum/ready` | retina-spectrum answers on its URL with any HTTP response, including an error status |
| `GET /api/sdrconnect/ready` | `systemctl is-active sdrconnect.service` succeeds (SDRconnect has no HTTP port to probe) |

### Returning to radar

- `enforce_radar_mode` stops spectrum and SDRconnect, restarts the SDRplay service and force-recreates the radar containers. It is called on wizard completion, so the node is always left in radar mode whatever happened during the wizard, and by the Mender install path to recover a failed install from a background thread, where nothing else coordinates it with a concurrent apply. It takes the restart lock and swallows every error, including failing to get the lock: whoever holds the lock is already restarting the stack.
- `POST /api/mode/release-spectrum` is sent by `navigator.sendBeacon` when the user leaves the wizard's location step mid-flow. It stops and removes retina-spectrum and writes `radar`, returning 204. It uses the opportunistic lock timeout: nobody waits on a beacon, and every restart path already stops retina-spectrum defensively, so giving up when the lock is busy loses nothing.

## Restarting sdrplay_apiService

`restart_sdrplay_service` restarts `sdrplay.service` so the USB device is re-initialised before blah2 or SDRconnect claims it. It mirrors what the watchdog does.

### Why a plain restart is not enough

`systemctl restart sdrplay.service` is not reliable on the failure that matters most. When the SDRplay device has wedged (the state Auto-Calibrate exists to recover from), the unit hangs in `deactivating (stop-sigterm)` because `sdrplay_apiService` ignores SIGTERM, and the restart never returns.

This used to be a bare `subprocess.run(..., timeout=30)`. On a real node with a wedged RSPduo (blah2 crash-looping on `MaxDevs=1023 NumDevs=0 / Error: No devices found`), the timeout fired, `TimeoutExpired` propagated out of `run_config_merger_and_restart` and reached `ApplyService` as "Command timed out", and the container recreate never ran. The one path that can recover the device aborted half-way, and Auto-Calibrate's preflight reported that it could not restart the radio.

### The forced reset

If the restart times out or returns non-zero, the function falls back to the sequence that worked by hand on that node:

1. `pkill -9 -f '[s]drplay_apiService'`. Killing the process is what releases the stuck systemd job.
2. Sleep 3 s so systemd can reap the process before the unit is actionable again.
3. `systemctl reset-failed sdrplay.service`.
4. `systemctl start sdrplay.service`. Often a no-op, since the restart queued earlier usually completes on its own once the process dies.

The device stays enumerated on USB throughout, so no USB unbind/rebind is needed: only the API service is stuck.

Two details of the `pkill` call matter:

- It uses `-f` (full command line), not `-x`. The kernel truncates a process's `comm` to 15 characters, so the 18-character name never matches an `-x` pattern. That bug is why blah2-arm's own `sdrplay-restart.sh` is a silent no-op, while `script/blah2_rspduo_restart.bash` works.
- The pattern is bracketed so it cannot match a shell command line that merely contains it. Running the unbracketed form over ssh matches the invoking shell and kills the session.

### Contract

- Never raises. Every caller treats the restart as best-effort, and a node without `sdrplay.service` (a dev machine) must be a no-op.
- Returns `None` normally, or a short description of what it had to do (for example that it forced a reset, or that the forced reset also failed).
- Reports the phases `restarting_sdr` and, if it has to force a reset, `resetting_sdr` through the optional `on_phase` callback.

## Config apply restart path

`run_config_merger_and_restart` is shared by config apply (through `ApplyService`), tower selection and calibrate apply. It holds the restart lock for the whole operation, settle window included, so the watchdog and every other caller queue behind it instead of interleaving `docker compose` runs against the same project.

`lock_timeout` defaults to the short, request-shaped wait, because `/towers/select` and `/calibrate/apply` still call this inline on a Flask request thread and must not hang a browser for minutes. `ApplyService` runs off-request and passes the long timeout.

Phases reported through `on_phase(phase, detail)`:

| Phase | Step |
| --- | --- |
| `waiting_for_lock` | Waiting for the restart lock |
| `merging` | `docker compose run --rm config-merger`. Stops here in spectrum or SDRconnect mode |
| `stopping_spectrum` | Defensive stop and remove of retina-spectrum, non-fatal |
| `restarting_sdr`, `resetting_sdr` | `restart_sdrplay_service` |
| `settling` | Settle window, `detail` is the seconds remaining |
| `recreating` | `docker compose up -d --force-recreate` |
| `repairing` | Only after a failed recreate |

It returns an error string or `None`. `TimeoutExpired` and `FileNotFoundError` from the config-merger step propagate to the caller.

### Settle window

`SDRPLAY_RESTART_SETTLE_SECONDS` (30 s) separates the SDRplay service restart from the container recreate. The two used to run back to back with no settle time, and that race was found on a real node to be a repeated cause of the SDRplay device wedging outright (not just a bad gain candidate). The window gives the service time to finish reinitialising the USB device before blah2 claims it again.

`_settle` sleeps in 1 s chunks only so callers can show a countdown. The window is about two thirds of a whole apply's wall time, and a progress display that sits silent through it looks like a hang. That is what led users to click Apply a second time and collide two `docker compose` runs in the first place.

### Recreate timeout and repair

`RECREATE_TIMEOUT_SECONDS` is 300 s. The recreate measured 13.5 s on a loaded 4-core Pi (load average 4.3), so this is about 20x headroom. The earlier 120 s was also generous: what actually blew it was two compose runs contending, which the restart lock now prevents. The remaining reasons to exceed the budget are a genuinely stuck device or daemon, where killing the CLI mid-recreate does real damage, so it is worth waiting longer before doing so.

When the recreate times out, the compose CLI has just been killed, possibly between renaming a container and removing the old one. When it fails with a name conflict ("already in use" or "error when allocating new name"), a half-recreated container still holds the name. In both cases `_repair` runs `stack_reconcile.reconcile` before returning, otherwise every later apply fails on the same conflict. It runs with the restart lock still held, which is what makes removing containers there safe. The returned message says whether containers were cleaned up or the repair failed.
