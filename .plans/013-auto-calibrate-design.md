# Auto-Calibrate: design summary

Status: implemented, uncommitted. `blah2-arm:20260708-auto-calibrate-live-tune`,
`retina-gui:20260708-auto-calibrate`. Verification in progress (see bottom).

**2026-07-12 update — ADS-B mode shipped, and reworked to have no time
division.** The "Non-goals (v1)" ADS-B line below is stale; see "Two success
modes" further down for what actually shipped. MODE_ADSB's dwell
(`Calibrator._dwell_adsb`) doesn't share MODE_TRACK's time-boxed strategy at
all: since `/api/adsb2dd` gives ground truth on whether an aircraft is
actually observable, absence of traffic is never treated as a reason to give
up. It waits for an ADS-B-confirmed aircraft with no timeout, and only
concludes a gain candidate failed once a real aircraft was in range and left
unmatched — then steps `gainReductionB` toward more sensitivity
(`ADSB_GAIN_STEP_DB`, re-checking overload) and tries again. Once candidates
are exhausted for a tower (sensitivity floor or re-overload), it moves to the
next tower. No overall run budget applies to this mode either — only success,
exhausting every tower's gain candidates, or the user cancelling ends it.

## Problem

After the setup wizard picks a tower (which only sets `fc`), the user has to
hand-tune Gain Reduction A/B in `/config`, apply, restart, and eyeball the
Max-Hold/Controller views to see if a target shows up — manual trial and
error, no feedback loop, and no signal telling them whether a bad result
means "wrong gain" or "no aircraft overhead right now."

## Goal

An on-demand, algorithmic process that searches three variables — tower/`fc`,
`gainReductionA`, `gainReductionB` — until blah2's tracker confirms a real
track: one that reaches `ASSOCIATED`/`ACTIVE` state via M-of-N confirmation,
as opposed to a single-CPI CFAR blob that could be noise. Optimized for
**good, not best** — a ~10 minute run that gets the user to working reception,
not an exhaustive search for the optimal setting.

### Non-goals (v1)

- Per-tuner LNA state is not searched (stays fixed — a separate, pre-existing
  gap where only gain reduction was ever split per-tuner).
- Success criterion is "any confirmed track," not ADS-B-verified. blah2_api
  already computes expected delay/Doppler for known aircraft (`adsb2dd`) and
  correlates it against raw detections, but not against confirmed tracks yet
  — a natural v2 extension, not built here.
- No detection/tracker algorithm tuning (`pfa`, `nGuard`, `M`/`N`, etc.) —
  out of scope; those stay hand-edited YAML.
- No RF-sweep-driven tower discovery mid-run — that needs spectrum mode,
  which conflicts with blah2 holding the SDR. Tower alternates come from the
  tower-finder's plain geography lookup instead (see below).

## Core architecture: "search live, persist once"

Today every config change costs a full container restart (~60-90s) because
blah2 reads its entire config once at process startup. Searching even a
coarse gain grid this way would take hours. So this project's first and
largest piece of work is a **live-tune control channel**: blah2's SDR capture
device can be retuned (fc and per-tuner gain reduction) while the process
keeps running, via `sdrplay_api_Update()` on the already-open device — the
same technique the sibling `retina-spectrum` repo already uses in production
for its live RF sweep.

Every candidate the search tries is applied through this channel and never
touches `user.yml`. Only the winning configuration (or a user-accepted
best-effort result) is written to disk and applied via the existing one-time
config-merger-and-restart path — so the search is fast, but persistence still
goes through the same durable, restart-based mechanism as every other config
change in the app.

```
retina-gui Calibrator --POST /capture/retune--> blah2_api (server.js)
                                                      |  (stores it, bumps a
                                                      |   generation counter)
                                                      v
blah2 (Capture's poll thread, every 250ms) --GET /capture/retune-->
      |
      v
RspDuo::retune()  --sdrplay_api_Update()-->  real SDR hardware
      |
      +-- if fc changed: signal blah2.cpp's processing loop to reset the
      |   tracker (old geometry's tracks are meaningless under a new tower)
      v
Capture's poll thread --POST /capture/retune/ack--> blah2_api
                                                      ^
retina-gui Calibrator <--GET /capture/retune/status-- (polls for the ack)
```

blah2 never becomes a network server for this — it stays the HTTP *client* it
already was for the pre-existing `/capture/toggle` (IQ-save) feature.

## Search algorithm: greedy descent + dwell

**Per tower:**
1. **Descend from max gain.** Retune to (20, 20) dB gain reduction — maximum
   sensitivity. Watch per-tuner RF overload events (a new signal, fed by the
   SDRplay API's `PowerOverloadChange` callback). While a tuner reports
   overload, back off *only that tuner* in 10dB jumps (a few seconds each,
   converges in ≤4 steps), then one 5dB refine step, reverting if it
   re-overloads. Result: the highest clean gain per tuner, found in well
   under a minute — "good, not best," per the stated goal.
2. **Dwell** (~2.5 min) at that setting, polling `/api/tracker`. Success the
   moment a track reports `ACTIVE`/`ASSOCIATED` state (`nActive > 0` in the
   API response — `Track::to_json()` already excludes unconfirmed `TENTATIVE`
   tracks). Also samples `/api/detection` throughout for a graded
   best-attempt signal (none → detections seen → tentative → associated →
   confirmed) so a failed run still tells the user something useful.
3. Absence of a track is **never** treated as proof of bad tuning — it may
   just mean no aircraft is overhead right now — so only overload drives fast
   rejection; everything else is patient dwelling.

**Outer loop:** the currently-configured tower first (already chosen by the
wizard from a measured signal sweep), then up to 4 more from the
tower-finder's plain geography lookup (`GET /api/towers?lat&lon`, no RF sweep
— avoids the SDR-ownership conflict with spectrum mode), which is already
rank-ordered by expected signal strength. **Capped at 5 towers total per run**
to fit the ~10 minute budget (roughly descent <1 min + dwell ~2.5 min per
tower).

**Cleanup invariant:** the original `(fc, gainA, gainB)` is captured at the
start of a run. On any non-success terminal state (failed, cancelled, error)
it's restored via one more live retune, so a failed run never leaves blah2
parked on a random candidate. On success, blah2 stays live-tuned to the
winner; persisting to disk is a separate, explicit user action.

## Implementation — blah2-arm (engine + Node API)

- `RspDuo::retune(fc, gainA, gainB, fcChanged&)` — validates bounds, applies
  the same `sdrplay_api_Update()` calls the startup path already made once,
  now callable repeatedly. Returns whether fc actually changed.
- `RspDuo::get_overload(overloadA&, overloadB&)` — reads two atomics set by
  the existing `PowerOverloadChange` event callback (previously just logged).
- A new mutex (`sdrplay_update_mutex`) guards every `sdrplay_api_Update()`
  call — including the pre-existing overload-ack call, which had no locking
  before this change and would otherwise race the new retune path.
- `Tracker::set_lambda()` / `Tracker::reset()` — fixes a latent bug where
  `lambda` (wavelength, `c/fc`) was computed once at startup and never
  refreshed; a live fc change would otherwise silently corrupt kinematic
  predictions. `reset()` clears tracks that belong to the now-irrelevant old
  transmitter geometry after a tower switch.
- A tiny `TuneState` (two atomics) hands the "fc changed" event from the
  capture-control thread to blah2's main processing thread, consumed once
  per CPI.
- `Capture.cpp` gains a second poll thread (250ms, tighter than the existing
  1Hz `/capture` toggle poll since this sits on a timed search's critical
  path) that applies pending retunes and reports overload state.
- `server.js` gains `/capture/retune` (GET/POST/ack/status) and
  `/capture/rf-status`, with a **server-assigned, monotonic generation
  counter** so a late ack can't be misattributed to a different candidate
  after a timeout/retry. The retune POST also fixes a pre-existing staleness
  bug: `server.js` cached `config.capture.fc` once at boot for its ADS-B
  bistatic-Doppler math and never updated it — a live fc retune now updates
  that value too.
- `RspDuo::replay()` additionally sets a `replay_mode_fg` flag so a live
  retune request *simulates* success against local state instead of
  dereferencing the SDRplay pointers that replay mode never initializes —
  added specifically so the whole ack/`TuneState`/tracker-reset chain is
  testable without real hardware (see Tier 2 below).

## Implementation — retina-gui (orchestrator, UI, telemetry)

- `Blah2Client` — the first-ever HTTP link from retina-gui to blah2_api
  (previously all interaction was `docker compose` lifecycle management).
- `Calibrator` — a background-thread state machine implementing the search
  above (in-memory status dict + lock, mirroring the existing WiFi-connect
  pattern in `network_manager.py`).
- `DeviceState` gains a second, independent `calibrate.lock` — mutual
  exclusion with Mender OTA installs in both directions (can't calibrate
  mid-update, can't update mid-calibration).
- `routes/calibrate.py` — `start`/`status`/`cancel`/`apply`, with guards:
  refuses to run if hardware AGC is enabled (`bandwidthNumber` 5/50/100 —
  AGC on the reference channel would fight the manual gain search), requires
  radar mode and a completed setup.
- UI: an "Auto-Calibrate" button in `/config`'s Capture section, a progress
  modal (2s poll cadence — tighter than the 5s install-progress convention,
  since this is an actively-watched run with fast-changing detail) showing
  phase/tower/gain/live-overload/elapsed, a best-attempt summary on
  failure/timeout, and an explicit "Persist to config" button on success
  (never auto-applied, since a restart is involved).
- Telemetry (2026-07-14 redesign, superseding the original per-run version):
  no longer calibration-specific. A full config-snapshot POST to
  `CONFIG_TELEMETRY_URL` (empty = disabled, renamed from
  `CALIBRATION_TELEMETRY_URL`) fires app-wide, fire-and-forget, whenever any
  config-applying action succeeds (`/calibrate/apply` included, alongside
  `/towers/select`, `/config/apply`, and mode switches) — see
  [012-auto-calibrate-telemetry.md](012-auto-calibrate-telemetry.md). The
  server-side ingest app itself is out of scope for this project.

## Key decisions and why

- **Live-tune investment over restart-per-candidate.** Restart cost
  (~60-90s) made any real search infeasible; `retina-spectrum` already proved
  live SDR retuning works reliably on this exact hardware/software stack.
- **Success = any confirmed track, not ADS-B-verified.** Simpler v1, matches
  the original ask directly; ADS-B correlation is flagged as a natural v2
  since the building blocks (`adsb2dd`) already exist.
- **On-demand button, not a wizard step.** Keeps the wizard's scope
  unchanged; calibration is also something worth re-running later if
  reception degrades, not just a one-time setup action.
- **Greedy descent from max gain, big jumps, no grid search.** Waiting for a
  track is the expensive step (needs a real aircraft overhead) — the design
  minimizes the *number of dwells*, not the granularity of the gain search.
- **Max 5 towers, best-ranked.** Bounds the ~10 minute budget; reuses the
  tower-finder's existing rank ordering rather than inventing new scoring.
- **Telemetry sent unconditionally, one summary per run.** Keeps the client
  side simple; the ingest service is a deliberately separate, later project.

## Verification strategy (tiered, no hardware until the last one)

- **Tier 0 — done.** `testTracker` unit tests (`reset`/`set_lambda`
  correctness), `api/test_retune.js` (22/22, full endpoint round-trip against
  a real running `server.js`), retina-gui `pytest` (301 passed, 28 new —
  descent/dwell/cleanup logic, route guards, lock exclusion, telemetry
  payloads, all against a scripted fake blah2 client).
- **Tier 1 — real build.** An ARM64 Docker build (via `buildx`/QEMU,
  matching the Pi5 production target and the repo's own CI) rather than a
  syntax-check against hand-assembled headers — validates every touched file
  compiles under the project's actual vcpkg toolchain, and (as a bonus)
  builds and links the Catch2 test binaries too.
- **Tier 2 — replay-mode integration test, still no hardware.** The real
  compiled `blah2` binary (from the Tier 1 image) running in file-replay mode
  against synthesized noise IQ data, alongside the real `blah2_api`, driving
  an actual retune HTTP round-trip end to end — proves the whole chain (poll
  → apply → ack → status) without ever touching an SDR, enabled by the
  `replay_mode_fg` addition above.
- **Tier 3 — real hardware.** Done on the desk node: gain-only retune, fc
  retune with a clean tracker reset, and a cancelled run's restore all
  confirmed directly against real SDRplay hardware. Not yet done: a full
  end-to-end run against real air traffic in either mode.

## Must investigate before this is finished

Flagged 2026-07-10, not yet looked into — recorded here so they survive a
context switch, not because either is confirmed a bug:

- **Watchdog interaction.** `blah2-arm/script/blah2_rspduo_restart.bash`
  (cron, every 5 min) restarts blah2 if `/api/map` looks stale, with a 60s
  post-start grace period, and avoids racing a `docker compose` operation it
  can see via `pgrep`. Auto-Calibrate never runs `docker compose` — it
  live-retunes — so that race-avoidance check can't see a calibration in
  progress. Does a live fc retune (tracker reset, brief relock gap) ever
  look stale enough by the watchdog's own criteria to trigger a restart
  mid-search?
- **Container startup/sequencing.** The Capture.cpp poll thread assumes
  blah2_api is reachable when blah2 starts polling `/capture/retune` —
  only informally covered by retry-next-cycle, never checked against the
  compose `depends_on` graph (config-merger → blah2_api → blah2) when both
  are force-recreated together by the Persist/Apply flow.
- **Calibration vs. mode-switching race.** Starting a calibration while not
  in radar mode is refused (confirmed, in `routes/calibrate.py`). Not
  confirmed: whether switching *to* spectrum/sdrconnect mode is blocked
  while a calibration is actively running — if not, the SDR could be yanked
  out from under the calibrator thread mid-run.

## Other known follow-ups (not blocking this project)

- Per-tuner LNA state remains a separate, pre-existing gap.
- Descent/refine step sizes and dwell budget are principled estimates from
  CPI/M-N timing — expect to tune them empirically once real hardware data
  is available.
- The server-side telemetry ingest application is a separate project.
