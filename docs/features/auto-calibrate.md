# Auto-Calibrate

Auto-Calibrate searches for a tower (centre frequency `fc`), per-tuner gain reduction and a shared LNA state that let the
live radar run without overloading the RSPduo, and (in the full run) that produce a confirmed track. It drives blah2
through its live retune protocol, so the search itself never writes config or restarts containers. A result is only
persisted when the caller asks for it via `POST /calibrate/apply`.

The strategy is "good, not best": a confirmed track needs a real aircraft overhead, so dwell time dominates a run. The
search minimises the number of dwells, not the granularity of the gain grid. The original design notes are in
[013-auto-calibrate-design.md](../history/013-auto-calibrate-design.md) (partly stale; this page describes the code as it
is now).

## Where it lives

| File | Role |
| --- | --- |
| [src/calibrator.py](../../src/calibrator.py) | `Calibrator`: the search engine. Runs in a background thread, keeps status in an in-memory dict under a lock. Imports no Flask, subprocess or Docker. |
| [src/routes/calibrate.py](../../src/routes/calibrate.py) | `/calibrate/start`, `/status`, `/cancel`, `/apply`. Guards, candidate tower list, persisting a result to `user.yml`. |
| [src/blah2_client.py](../../src/blah2_client.py) | `Blah2Client`: thin HTTP wrapper over blah2_api (retune, retune status, overload status, detections, ADS-B). Every getter returns `None` on failure so the engine can be tested against a fake. |
| [src/retina_tracker_client.py](../../src/retina_tracker_client.py) | Shared client for the retina-tracker sidecar. The engine registers a listener for confirmed-track events and calls `reset()`. |
| [static/calibrate.js](../../static/calibrate.js) | `window.RetinaCalibrate`: shared driver for all UI entry points (run shapes, phase and outcome labels, result interpretation, fetch and poll calls). |
| [templates/setup/_calibrate.html](../../templates/setup/_calibrate.html) | The setup wizard's "Tune the receiver" step. |
| `templates/config.html`, `static/setup.js` | The Configuration page modal and the wizard step's rendering. Both use `RetinaCalibrate`. |

## Entry points and run shapes

Three UI entry points start a run:

| Entry point | Body posted to `/calibrate/start` | Shape |
| --- | --- | --- |
| Configuration page, Auto-Calibrate | `{}` | Full run: current tower plus up to two alternates, dwelling on each until a track confirms. |
| Configuration page, Quick Calibrate | `RetinaCalibrate.QUICK_RUN` | Current tower only, descend then soak, no track wait. |
| Setup wizard step | `RetinaCalibrate.QUICK_RUN` | Same as Quick Calibrate. |

`QUICK_RUN` is `{scope: "current_tower", skip_confirmation: true}` and is defined once in `calibrate.js` so the two
callers cannot drift. The route treats the two flags independently: `scope` answers "how many towers", and
`skip_confirmation` answers "whether to wait for a confirmed track". Everything except DOM rendering is shared in
`calibrate.js`; rendering stays with each caller because one is a Bootstrap modal and the other is a full-page wizard step.
`calibrate.js` is deliberately ES5-flavoured (`var`/`function`, no arrow functions) to match `setup.js`.

### Candidate towers

`routes/calibrate.start` builds the tower list. The currently configured tower is always first (named from
`location.tx.name`, else "Current tower") and is passed **without** a `tx` block. `MAX_TOWERS = 3` in total. Alternates
come from `_fetch_alternate_towers`:

1. The setup wizard's cached tower search (`device_state.get_towers_cache()`), preferred because it is RF-measurement
   informed and avoids a second live tower-finder call.
2. Otherwise a live geography lookup against the tower-finder, if `location.rx` is set.
3. Otherwise nothing: the run searches the current tower only.

`_towers_to_alternates` converts a tower-finder record to `{name, fc, tx}`, skipping the current `fc`. `_tower_tx`
returns the transmitter position, or `None` if the record has no usable coordinates. A tower without coordinates is still
searched (only `fc` is tuned) but its tuning must not move `location.tx`, since blah2 derives its bistatic geometry from
it and a new `fc` against the old tower's position is worse than either tower on its own. Only alternates carry `tx`, and
that is what tells `/calibrate/apply` that a run which stays put must not rewrite the owner's chosen location.

The tower count is deliberately small. Each tower costs a full descent (up to about 4.5 minutes on a node that never
overloads) plus its own dwell, and dwell is where success comes from. Towers past the top-ranked one are speculative, so
trading tower count for dwell time is the right way round: a run that searches five towers but dwells on none of them
searches nothing at all.

## Safe-end first

For every tower, every search axis starts at its **least sensitive** end (maximum gain reduction, maximum LNA state) and
steps toward more sensitivity. The search never starts at maximum gain or maximum sensitivity on either axis, and reverts
to the last proven-clean step the instant the front end overloads.

This is a safety requirement, not a style choice. The search always runs with the SDR's hardware AGC disabled (AGC would
fight the manual search, see [Guards and couplings](#guards-and-couplings)), so while a run is in progress nothing at the
hardware level protects the ADC. Hardware AGC reacts continuously at hardware speed; this software only checks in every
`OVERLOAD_SETTLE_SECONDS`. An earlier version started cold at maximum gain, treating a couple of seconds at an overloaded
setting as a stability inconvenience to correct afterwards. On a real deployment near a strong broadcast tower that left
the front end unprotected long enough to destabilise the SDRplay device itself, not just log an overload. Approaching from
the safe side and reverting on the first sign of trouble bounds the worst-case exposure to one step beyond an
already-proven-clean value.

The same principle appears in several other places:

- The preflight parks the device at the safe corner before any candidate ([Preflight and recovery](#preflight-and-recovery)).
- A frequency change is always preceded by a retune to the safe corner at the old frequency ([Retune protocol](#retune-protocol)).
- A device error during a probe is treated as an overload ([Device wedges](#device-wedges)).
- A mid-dwell overload backs off toward the safe end ([Dwell and overload watch](#dwell-and-overload-watch)).

## Search order

`Calibrator._descend` runs three variables in a fixed priority order per tower.

| Step | Variable | Function | Behaviour |
| --- | --- | --- | --- |
| 1 | Reference gain reduction (tuner A) | `_descend_reference` | Start at 59 dB, step `DESCENT_STEP_DB` (10 dB) toward more gain while clean, revert to the last clean value on overload. No refine step: the reference only needs to capture the illuminator cleanly, so "as hot as possible without clipping" is the goal. |
| 2 | Surveillance gain reduction (tuner B) | `_descend_surveillance` | Same walk, then one refine step once a revert has happened: claw back `REFINE_STEP_DB` (5 dB), revert again if that re-overloads. This is where MODE_ADSB's sensitivity cycling picks up from. |
| 3 | LNA state (shared) | outer loop in `_descend` | See [LNA state vs gain reduction](#lna-state-vs-gain-reduction). |

Each probe (`_probe`) applies one candidate, sleeps `OVERLOAD_SETTLE_SECONDS`, then reads overload status. Both tuners'
gains are always sent together in each retune; `_descend_reference` inspects only `overload_a`, `_descend_surveillance`
only `overload_b`.

Terminal branches:

- If a channel overloads at the 59 dB ceiling itself (no clean value yet), the stage returns `still_overloaded=True` with
  the gain still at the ceiling.
- A stage also stops at `GAIN_REDUCTION_MIN` (20 dB) or when the descent deadline passes. The deadline is checked between
  probes, so a probe already in flight finishes first.

`_descend_reference` resets the phase to `descending` on entry because `_descend_surveillance` sets it to `refining`, and
the LNA loop re-enters both stages many times per tower; without the reset the UI would show "Refining gain" through most
of a descent.

Every descent step is appended to the tower's `descent` log with a `phase` of `reference`, `reference_revert`,
`surveillance`, `surveillance_revert`, `surveillance_refine`, `lna_descent` or `lna_descent_revert`, plus
`device_error`/`device_error_detail` when a probe or revert failed.

## LNA state vs gain reduction

The RSPduo has two independent attenuation controls, both of which are safest at their highest number:

| Control | Range | Per tuner? | Stage | Safe end |
| --- | --- | --- | --- | --- |
| Gain reduction (`gainReduction`, gRdB) | 20-59 dB | Yes (A and B) | Downstream, IF stage | 59 (most attenuation) |
| LNA state (`lnaState`) | 1-9 | No, one shared register | Upstream, RF stage | 9 (most attenuation, least gain) |

LNA state 1 is maximum gain (least attenuation) and state 9 is minimum gain (most attenuation); see `RspDuo/README.md` in
blah2-arm. The bounds in `calibrator.py` (`GAIN_REDUCTION_MIN/MAX`, `LNA_STATE_MIN/MAX`) mirror blah2's RspDuo limits.

Because the device has no per-tuner LNA control, LNA state is resolved as a single outer loop around both tuners' gain
descents rather than a fourth per-tuner step:

1. Every tower starts at `lna_state = 9` and descends both tuners' gain (steps 1-2 above).
2. If both come back clean, try one step more sensitive (`lna_state - 1`) and redescend **both** tuners fresh from the
   59 dB ceiling.
3. The instant either channel overloads (or hits a device error) at the new state, the whole
   `(gain_a, gain_b, lna_state)` triple reverts together to the last state fully proven clean, and the search stops there.
4. The loop also stops at `LNA_STATE_MIN` or at the descent deadline.

Why both tuners are always reproved: within gain reduction's own descent, retreating to more attenuation can never newly
overload an already-clean channel. Moving LNA state toward more sensitivity is a different direction and can newly
overload a channel that was clean a moment ago, so there is no "only redo the triggering channel" shortcut. Why the revert
is the whole triple: LNA is one shared register, so a mismatched LNA state per tuner is not physically meaningful.

If gain reduction alone cannot clear an overload even at `lna_state = 9` (the safest corner of the whole search space),
that is terminal for the tower: there is no safer LNA state to retreat to, so the loop is never entered. In that case the
resolved point is the safe corner, or as close as the descent got. Note that the surveillance stage still runs at state 9
even when the reference stage came back overloaded, as long as the deadline allows.

## Device wedges

This hardware does not always fail safely. A bad candidate can wedge the SDRplay device outright rather than report
overload: the retune never acks, or overload status goes quiet. That surfaces as a `CalibrationError` from `_apply` or
`_read_overload`, not as a clean reading.

The descent and dwell steps treat that failure exactly like an overload reading at the candidate, rather than letting it
abort the whole multi-tower run. A wedge is, if anything, a stronger signal that the candidate is unusable.

- `_probe` catches the error and returns `overload_a = overload_b = True` plus the error text. Forcing **both** flags true
  even for a single-tuner caller is deliberate: after a device error neither channel's state is actually known, and
  assuming the worst matches the safe-descent philosophy. When the candidate's own retune never completed, `_probe`
  returns the caller's `fallback_applied_at` (the previous candidate's timestamp, or 0 for the first) because nothing new
  is known to have been applied.
- `_safe_revert` re-applies the last proven-safe value (one channel's gain, or the whole triple for LNA) and never raises.
  If even the revert fails, the dwell simply fails to confirm a track and the run moves on to the next tower like any
  other no-track outcome.
- `_safe_fc_handover` is best-effort for the same reason: raising there would turn a contained per-candidate failure into
  an aborted run, and the following retune surfaces the problem anyway.
- Any `device_error` in a tower's `descent` or `gains_tried` log marks the whole tower entry `device_error`.

Once the SDRplay API starts returning `ServiceNotResponding` (what a hard overload looks like from outside) it keeps doing
so, so a live retune cannot rescue a wedged device. Only a restart from a safe config works, which is what the preflight's
recovery branch does.

## Retune protocol

All tuning during a run goes through blah2's live retune protocol and is in-memory only:

1. `Blah2Client.retune` posts `/capture/retune` and gets back a `generation`.
2. `_apply_tuning` polls `/capture/retune/status` until that generation is acked (`ACK_TIMEOUT_SECONDS`, one retry after
   `APPLY_RETRY_DELAY_SECONDS`) and returns blah2's `appliedAt` (ms).
3. `_read_overload` polls `/capture/overload-status` for a report whose timestamp is at least `appliedAt`
   (`RF_STATUS_TIMEOUT_SECONDS`). A timeout raises, with a hint that blah2 may be too old for live tune.

All blah2-side timestamps (retune `appliedAt`, overload status, detection and tracker CPI timestamps) share blah2's system
clock, so freshness comparisons never mix clock domains.

Overload status is deliberately its own endpoint, not `/capture/rf-status`: that path belongs to the unrelated peak-dBFS
meter feature, and the two datasets have no shared consumer.

### Settle timing

`OVERLOAD_SETTLE_SECONDS` (1 s) covers the physical settle after a retune, not reporting latency. blah2's capture thread
polls the device every 250 ms and posts an overload status change as soon as it sees one, so a clipping candidate is
reported promptly. An unchanged state is only re-posted on a 2 s heartbeat, so on a clean probe the heartbeat, not this
constant, usually bounds `_read_overload`'s wait.

### Unacknowledged retunes

When a retune is never acked, the error deliberately does not guess the cause. The radar being down and the front end
being so overloaded that the SDRplay API stopped answering look identical from here, and they need opposite responses from
the user. An earlier wording ("Is the radar running?") named only the first, which misled users on strong-signal nodes
where the second is the common case.

### Safe-corner handover on a frequency change

`_apply` precedes any frequency change with a separate retune to the safe corner (59/59, LNA 9) at the **current**
frequency, via `_safe_fc_handover`. blah2's driver applies fc before gain within one retune (`RspDuo::retune` calls
`Update_Tuner_Frf`, then `Update_Tuner_Gr`), so a single call that moves both parks the front end on the new frequency at
the old frequency's gain. Reaching a strong tower from a sensitive operating point therefore saturates the device before
the requested attenuation lands, and because the driver returns early on that failure, the gain update never executes.
The retry cannot get the rescuing attenuation through either, since `ServiceNotResponding` persists.

Measured directly: the same destination frequency at (59, 59, LNA 9) was clean when reached from a safe state, and
saturated when reached from (49, 20, LNA 1) at a neighbouring tower's frequency. Going via the safe corner costs one extra
retune per tower change and removes the exposure.

`ignore_cancel` on `_apply` is only for the restore and fallback paths, which must run to completion even if the user
cancels again while they are in flight, otherwise blah2 could be left on a failed candidate.

### Seeding the current frequency

`_seed_last_applied_fc` runs first in `_run`, before any retune, and reads the frequency from blah2's retune status, not
from config. The two diverge routinely: a run that finds a track and is never persisted leaves the radio on the winning
frequency while `config.yml` still holds the old one. Seeding from config there makes the calibrator believe it is already
on the first tower's frequency, so `_apply` sees no change and skips the handover, switching the protection off exactly
when the device has drifted furthest from config. This happened on a live node: config and radio disagreed after an
unpersisted run, the first retune moved to a strong local tower at a sensitive gain with no handover, and every later
retune failed (the whole run came back `tuning_not_applied` in 42 seconds).

An empty retune status is not ambiguous: blah2 has acked no retune since boot, so it is still on `config.yml`'s frequency,
which is what the caller passed as `original["fc"]`.

### Verifying before dwelling

`_verify_applied` compares blah2's last acked retune against the resolved `(fc, gain_a, gain_b, lna_state)` before any
dwell or soak. If they differ, the tower's outcome is `tuning_not_applied` and it is not dwelt on. When blah2_api reports a
`rejected` generation (newer blah2 only, see its `MAX_RETUNE_ATTEMPTS`) the reason names that.

Without this check a dwell could look entirely healthy while measuring a different frequency: when a candidate's own
retune never completes, `_probe` returns the previous candidate's timestamp, so the dwell's `timestamp >= applied_at`
guard still passes for detections from the old tuning. This was seen live as a dwell running for minutes labelled with
one tower while blah2 was still on another. Comparing against the device's own account of its tuning catches a failed
retune, an abandoned generation, and anything else that retuned the radio behind the run's back. The dwell then uses the
verified `appliedAt`, not descent's.

## Preflight and recovery

Before the search starts, `_preflight` parks the device at the safe corner (59/59, LNA 9) at whatever frequency blah2 is
**actually** on, and requires an ack. That one retune proves the SDRplay API still answers control calls and leaves the
radio at its least sensitive setting before any candidate is tried. On a healthy node it costs a couple of seconds and
nothing else happens (`status.preflight` stays `None`).

It probes at the seeded frequency rather than `original["fc"]` because moving fc from an unknown, possibly sensitive gain
is the exact hazard the handover exists to avoid, and there is no proven-safe state to hand over from yet. Gain reduction
and LNA state only ever increase in this call, so it is safe from any starting point.

If the probe fails, the device is wedged ([Device wedges](#device-wedges)):

1. With no `ApplyService`/`ConfigManager` injected (engine tests, dev mode) the run aborts with an accurate message; there
   is no restart to attempt.
2. Otherwise phase becomes `recovering` and `status.preflight = {recovered: False, restarted: True, previous: original}` is
   recorded **before** any write, so a run that dies later still reports the intervention and `_run` still knows not to
   restore.
3. `_persist_safe_corner` writes the safe corner to `user.yml`. If this fails the run aborts: restarting without it would
   bring blah2 straight back up on the tuning that wedged it.
4. `_run_recovery_apply` runs the ordinary config-apply path (`ApplyService.request(bypass_guard=True)`: config-merger,
   `sdrplay_apiService` restart, settle, container recreate) and polls it to completion. `bypass_guard` is required
   because that guard refuses applies during a calibration, and this is the one caller that owns the run. The poll ignores
   cancel so containers are never left half-recreated; the run aborts at the next cancel check once the stack is settled.
5. The safe-corner probe is retried every `PREFLIGHT_APPLY_POLL_SECONDS` until it acks or
   `PREFLIGHT_RECOVERY_PROBE_SECONDS` expires. On success `preflight.recovered = True`. On failure the run is abandoned
   before any tower is tried, with a message naming the real fault.

Without the preflight, the wedged case was near invisible: every retune failed, every tower came back
`tuning_not_applied`, and the run finished in 30-60 seconds behind a summary saying no aircraft was overhead.

### Timing constants

| Constant | Value | Why |
| --- | --- | --- |
| `PREFLIGHT_RECOVERY_APPLY_TIMEOUT_SECONDS` | 180 | Covers config-merger, the `sdrplay_apiService` restart, `routes.mode`'s own 30 s settle and a container recreate measured at about 47 s, with headroom so a slow node is not declared dead. |
| `PREFLIGHT_RECOVERY_PROBE_SECONDS` | 60 | Covers blah2 coming up and claiming the device. SDRplay hardware needs a real 20-60 s settle before it answers again; anything shorter reports a false failure on a device about to recover. |

The preflight runs **before** the run budget clock starts, so a recovery (about 90 s) is not charged against every tower's
dwell. `_run` also rebases `_started_monotonic` after the preflight so the UI's elapsed counter does not count recovery
against a budget that has not started; `started_at` keeps the true wall-clock start. These constants are bounded from above,
see [Lock timeouts](#lock-timeouts).

### The one user.yml write, and why it is not reverted

Apart from this, nothing is written to `user.yml` during a run. This write is deliberately **not** reverted:

- It only happens on a device that was already unresponsive, where maximum attenuation is a working state, not a degraded one.
- A run that reaches `/calibrate/apply` overwrites it anyway.
- Reverting costs a second container restart that could leave the node deaf if it failed in between.

The run records that it did this, and what the previous values were, in `status["preflight"]` (`preflight.previous`).
The UI does not show a notice about it: shown next to a successful result, a note that maximum attenuation had been saved
read as a current fault rather than history. A run that cannot recover the radio still explains why in its own error.
For the same reason, a run whose preflight restarted the device never restores the original tuning on a non-success
outcome ([End of run](#end-of-run-restore-or-fallback)).

## Track confirmation

Confirmation goes through the same retina-tracker sidecar the Tracker page uses
(github.com/offworldlabs/retina-tracker, its own process, see `retina_tracker_client.py`), not an in-process tracker and
not blah2's built-in tracker, which has proved unreliable on real data. Detections reach the sidecar from blah2_api
directly (`network.tracker_forward`). The calibrator neither feeds it nor can: its ingest socket accepts one connection at
a time and blah2_api owns it.

Confirmed-track events arrive through `_on_track_event`, registered once with the shared `RetinaTrackerClient` on the
first `start()` (not in `__init__`, which runs at app boot and would start the client's tail thread that early). It runs on
the client's tail thread. The sidecar only emits an event for a track with an id, which it assigns on ACTIVE promotion
(`retina_tracker/tracker.py::process_frame`), so any event already means "confirmed". `_take_confirmed_event` only returns
an event whose timestamp is at least the current candidate's `applied_at`, since the tail thread polls on its own schedule
and an event from a previous candidate may still be in flight.

### Evidence levels

The run tracks the best attempt so far for the UI, graded `EVIDENCE_NONE` < `EVIDENCE_DETECTIONS` < `EVIDENCE_ACTIVE`
(then by detection count). This is coarser than an in-process tracker could offer: the sidecar's event stream only reports
confirmed (ACTIVE) tracks, so there is no tentative/associated distinction, the same visibility the Tracker page has. The
scale is mode-agnostic; MODE_ADSB adds a match requirement for success on top of it.

### Tracker reset per dwell

A track confirmed at one tower is meaningless at another (different fc and tx position mean different delay/Doppler
geometry). `_reset_tracker` clears the sidecar's tracker in place over its control surface (mirroring blah2's own
fc-triggered reset) and drops any held event. It runs at the start of every dwell, after every mid-dwell backoff, and for
each MODE_ADSB gain candidate, so a confirmation can only be earned from frames observed at the tuning it is reported
against. A soak skips it because it never reads the tracker.

It used to be per tower, on the reasoning that any confirmed track ends the search. That assumed the calibration was the
sidecar's only source, which it never was: blah2_api feeds it continuously, so the tracker runs through the whole descent
(150 s on one live node), and a tower-start reset let a track confirm before the dwell had observed anything. It was seen
live as a confirmed track with `dwell_seconds` 0.0. The `applied_at` guard does not close this, since it only requires the
event's latest detection to post-date the retune, which a track built across the descent satisfies.

A failed reset (now over HTTP, so visible) is recorded as `status.tracker_reset_failed` rather than raised: the dwell is
still worth running, it just can no longer promise the confirmation was earned at this tuning.

## Dwell and overload watch

`_dwell` (MODE_TRACK, and the soak with `watch_only=True`) polls every `TRACKER_FEED_POLL_SECONDS` (0.2 s). Each poll reads
the latest detection frame for evidence (de-duplicated by timestamp and required to be at least `applied_at`, so polling
faster than blah2's roughly 1 s CPI cadence costs nothing) and checks for a confirmed event.

Every `DWELL_OVERLOAD_CHECK_SECONDS` (5 s) it also re-reads overload state. One probe reading cannot tell a clean operating
point from one that clips intermittently, and the dwell is by far the longest phase, so a marginal point accepted by
descent would otherwise be sat on for minutes. This was seen on a live node: descent settled at LNA state 3, the device
cycled Overload_Detected/Corrected for the whole dwell, and by the end the SDRplay API had stopped answering control calls,
so every later retune in the run failed.

`_overload_since` needs both of blah2's signals, because each is blind to the other's case:

| Signal | Catches | Misses |
| --- | --- | --- |
| Level (`overloadA/B`) | Overload happening now, including a steady overload already in progress when the dwell began | Episodes that start and end between polls |
| Onset counts (`overloadCountA/B`, `None` on older blah2) | Clip-and-recover episodes between polls, the normal case on this hardware (9 detect/correct cycles in 90 s were measured with every level sample false) | A persistent overload whose onset predates the baseline |

On a clip, `_dwell_backoff` retreats one step toward safety: +`DESCENT_STEP_DB` on whichever channel clipped, and if a
clipping channel is already at the 59 dB ceiling (the clipping is upstream of gain), LNA state goes up one with both gains
reset to the ceiling, since moving LNA means both channels must be reproved from the safe end. It is best-effort through
`_safe_revert`. After a backoff the tracker, freshness guard and overload baseline are all reset so nothing measured at the
abandoned tuning is credited to the new one. After `MAX_DWELL_BACKOFFS` (2) backoffs, a further clip ends the tower with
outcome `unstable_overload`: each backoff costs dwell time, and a point needing several is not worth dwelling on.

Every exit from `_dwell` records `final_gain_a/b` and `final_lna_state` as the values actually in effect after any
backoff. The result offered for persisting must agree with history, and the no-track fallback reads the top tower's final
values; without this it would persist a tuning the dwell had just proved unstable.

## Time budget

MODE_TRACK is time-boxed because it has no independent way to tell "bad gain" from "no aircraft right now".

| Constant | Value | Role |
| --- | --- | --- |
| `TOTAL_BUDGET_SECONDS` | 900 | Whole-run budget, starting after the preflight. |
| `MAX_DESCENT_FRACTION` | 0.7 | Share of a tower's slice descent may use. |
| `DESCENT_BACKSTOP_SECONDS` | 300 | Hard per-tower descent ceiling. |

Per tower, `_run` computes `tower_share = time_left / towers_remaining` at the moment the tower starts, so unused time from
a quick tower rolls forward and no tower can overrun into the next. Descent's deadline is the earliest of
`tower_started + tower_share * MAX_DESCENT_FRACTION`, `tower_started + DESCENT_BACKSTOP_SECONDS`, and the run deadline. The
dwell then gets the rest of the slice (`tower_started + tower_share`, capped at the run deadline). A slow descent
therefore shortens its own dwell but can never delete it. If the dwell window is already empty the outcome is
`skipped_no_time`, meaning the tower was tuned but never watched, rather than a misleading "checked, nothing there". A
fixed `dwell_seconds` (tests) pins the dwell window to `now + dwell_seconds` instead.

The backstop is not a normal operating limit. It guards against a wedged device burning the full retune timeout on every
probe, and sits above the roughly 4.5 minute worst case for a healthy node (one that never overloads walks all 9 LNA
states at about 10-11 probes each).

History: an earlier design gave each tower a single descent-plus-dwell allowance. A descent long enough to exhaust it left
zero dwell and returned `skipped_no_time`; on any node whose descent walks more than a few LNA states that happened on
every tower, so a run could be spent retuning without ever watching for an aircraft. Separating the budgets alone was not
enough either, since three towers each taking the full backstop would still swallow a 900 s run. Capping descent at a
fraction of the slice is what guarantees every tower is watched.

MODE_ADSB has no time division: the run budget does not bound its tower loop and `progress.budget_seconds` is reported as
`null`. Its descent is still bounded by `ADSB_DESCENT_DEADLINE_SECONDS` (120) per tower, since descent is a fast,
traffic-independent overload-avoidance loop.

## Success modes

| Mode | Success signal | Time bound |
| --- | --- | --- |
| `MODE_TRACK` (default) | Any confirmed-track event after `applied_at` | Time-boxed, see [Time budget](#time-budget). |
| `MODE_ADSB` | A confirmed-track event with a non-null `adsb_hex` | None |

In MODE_ADSB the sidecar's own tracker does the matching natively: retina-tracker's `Track` initialises from the
per-detection `adsb` field that blah2_api attaches to `/api/detection` when `truth.adsb.enabled`, using the node's
`truth.adsb.delay_tolerance`/`doppler_tolerance`. Callers must check `truth.adsb.enabled` before using this mode, since the
engine has no access to config.

`_dwell_adsb` keeps reference gain fixed at descent's value (it is the surveillance channel whose sensitivity decides
whether a weak target is detected). Starting from descent's clean `gainReductionB`, it probes the candidate, resets the
tracker, then waits with no timeout for `/api/adsb2dd` to show an aircraft, since absence of traffic is never the tuning's
fault. It keeps polling (`DWELL_POLL_SECONDS`) for a matched confirmation while any aircraft stays in range. Once every
aircraft that appeared has left unmatched, that candidate has had its chance: `gainReductionB` steps `ADSB_GAIN_STEP_DB`
(5 dB) more sensitive and the loop repeats. Candidates end at the 20 dB floor or on re-overload (reverting to the previous
candidate), and the run moves to the next tower. A confirmed but unmatched track is reported in the best attempt as
"confirmed track, but doesn't match a known aircraft". Only success, exhausting every tower, or cancel ends a MODE_ADSB run.

The engine supports MODE_ADSB fully, but `/calibrate/start` refuses `mode=adsb` with 409. Exposing it to users is a
separate decision not yet made.

## Skip confirmation and the soak

`start(skip_confirmation=True)` (the wizard step and Quick Calibrate) resolves each tower's operating point, soaks it for
`SOAK_SECONDS` to prove it holds, and stops without waiting for a confirmed track. It turns a roughly 15 minute run into
the descent plus the soak. Because no confirmation is attempted, none can be spurious, which matters while
`_on_track_event` still accepts tracks the sidecar itself flags as implausible.

The soak is `_dwell(..., watch_only=True)` with a deadline of `min(now + SOAK_SECONDS, run_deadline)`: the same overload
watch and backoff as a full dwell, no detections read and no tracker reset. It must not skip overload watching. Descent
proves a candidate only over `OVERLOAD_SETTLE_SECONDS` (one second), and a point that clips intermittently only shows up
when sat on; skipping the dwell outright would persist a one-second verdict and miss exactly the failure described in
[Dwell and overload watch](#dwell-and-overload-watch). 45 s gives about 9 checks at 5 s, enough to absorb
`MAX_DWELL_BACKOFFS` retreats and still leave several clean readings at the final point.

Details that matter:

- The soak has its own phase, `soaking` ("Checking the settings hold…"). Reusing `dwelling` showed "Watching for aircraft…"
  on a wizard step whose copy promises no aircraft; this was caught in live testing.
- A soak that reaches its deadline records outcome `tuned`, not `no_confirmed_track`, and `soak_seconds` rather than
  `dwell_seconds` so it is not read as time spent looking for aircraft.
- It is deliberately not expressed as `dwell_seconds=0`, which would land in the budget-exhausted branch and report
  `skipped_no_time`, a false explanation.
- The run ends with state `failed` (no confirmed result) and a "Tuning resolved…" message rather than the "no aircraft was
  overhead" one. `status.skip_confirmation` lets consumers tell "never looked for a track" from "looked and found nothing".
  The resolved tuning is persisted through the same no-track fallback as any other run.

The wizard step's copy deliberately does not say it is "looking for aircraft", for the same reason: promising aircraft
would set the owner up to read a normal result as a failure.

## End of run: restore or fallback

| Outcome | What the device is left on |
| --- | --- |
| Confirmed track (`state: done`) | The winning tuning. `status.result` holds it. |
| No track anywhere (including a skip-confirmation run) | The top-ranked tower's own resolved operating point, via `_apply_top_tower_fallback`. `status.fallback` holds it. |
| Cancelled, `CalibrationError`, or unexpected error | The original tuning, restored with `ignore_cancel=True`. |
| Any non-success after a preflight restart | Left alone (the safe corner the recovery persisted). |

The fallback uses the top tower's final values (after any dwell backoff), which were proven not to overload by that
tower's own descent. It needs no separate safety check: the resolved triple already degrades to the safe corner whenever
the top tower's descent never found anything better. The fallback record is written before the retune and kept even if the
retune fails, because it is what `/calibrate/apply` persists, and a blah2 that missed the retune re-reads `config.yml` on
its next restart (`restart: always`). If tower 0 was never reached, the generic restore applies instead. The fallback
record includes `tx` (alternates only) because fc and tx have to be persisted together.

A cancel check runs just before the fallback, so a cancel arriving as the last dwell ends still takes the plain
cancellation path. Cancelled runs therefore never have a fallback, and cancel still means "put it back".

The original-tuning restore is skipped after a preflight restart because those are the settings the radio was stuck on;
putting a just-recovered device back on them is the one move guaranteed to undo the recovery, and `user.yml` already holds
the safe corner. It keys on `restarted`, not `recovered`, so a cancel between the restart and the first successful probe is
covered too. The restore itself uses `ignore_cancel` so a second cancel click cannot leave blah2 on the last failed
candidate. Restore failures are swallowed: if blah2 is unreachable, `restart: always` re-reads `config.yml`.

## Persisting a result

`POST /calibrate/apply` takes `status.result` when the run is `done`, and otherwise `status.fallback`. A no-track run's
tuning is live-only until written: the next stack restart re-reads `config.yml` and discards it, and in the setup wizard
that restart is seconds away (`/set-up/complete` force-recreates the stack). It is the same write as a success, from
values proven the same way.

It writes to `user.yml`:

- `capture.fc`, `capture.device.gainReduction = [gain_a, gain_b]`, `capture.device.lnaState`.
- `capture.device.bandwidthNumber = 0`, always. A calibration result is by definition a manual operating point, so it
  must never inherit a stale AGC-on bandwidth.
- When the tuning carries `tx` (an alternate tower), `location.tx` latitude, longitude, altitude and name. This mirrors
  `/towers/select`: persisting fc alone would leave blah2 processing the new tower's signal against the old tower's
  geometry, with nothing on the Configuration page showing which it really listens to. The name is truncated, not
  dropped, to `TX_NAME_MAX_LENGTH` (retina-telemetry's `tx_callsign` limit; a longer name stops the node building a
  `NodeConfig`), since keeping the old name next to new coordinates would misreport what the node is pointed at.

The `user.yml` write is synchronous; only the slow merge and restart go to the shared `ApplyService` queue, which merges
whatever is in `user.yml` when it runs. Progress is polled at `/config/apply/status` (`RetinaCalibrate.pollApply`).

The response includes `persisted`, exactly what was written, so the Configuration page can update its own form fields.
That page is not reloaded by persisting, and a Save from a stale form would post the pre-calibration values back:
`compute_user_overrides` drops an override whose submitted value matches the merged config, so the calibration would
silently vanish from `user.yml`.

## Guards and couplings

### Start guards

`/calibrate/start` refuses (409) when:

- retina-node is not installed, or the node is not in radar mode.
- `device_state.can_start_calibration()` says no (for example a Mender install in progress).
- A config apply is running. `ApplyService` already stops an apply during a run; this covers the other direction. An apply
  performs about 45 s of stack restart, and starting a run a few seconds after Apply Changes made every retune fail
  against restarting containers (all three towers `tuning_not_applied`).
- Hardware AGC is enabled: `bandwidthNumber` in `AGC_BANDWIDTHS` (5, 50, 100). AGC would fight the manual search. Users
  who want AGC set it directly in the Capture config.
- Capture config is incomplete (`fc`, `gainReduction` or `lnaState` missing). A scalar `gainReduction` is expanded to both
  tuners, and values are clamped into range to build `original`.
- The calibration lock is already held (`device_state.acquire_calibration_lock()`); it is released if `start()` fails.

`/calibrate/apply` re-checks `can_start_calibration()`, which covers a Mender install, so it needs no separate update guard.

### Server-pushed Mender deployments

A deployment pushed from the Mender server installs autonomously (mender-updated polls on its own and retina-gui is never
consulted), so unlike `/mender/install` nothing can refuse it. It replaces the containers under a run and every retune
fails. `/calibrate/status` therefore annotates `system_update` from `device_state.is_any_update_in_progress()` (a
file-exists check plus a small JSON read, cheap enough per poll), and `calibrate.js`'s `updateWarning` tells the user to
ignore the run. Preventing it instead would mean publishing Mender Update Control maps, and a map left behind by a crashed
GUI would stall fleet updates, a worse failure. This is an accepted risk: the overlap window is narrow and the run fails
safely.

### Lock timeouts

The run holds `calibrate.lock` (managed by `DeviceState`, taken by the route). Two components stop treating it as live
after 1200 s (20 minutes):

- retina-gui's own `CALIBRATE_LOCK_TIMEOUT` in `device_state.py`.
- blah2-arm's watchdog (`blah2_rspduo_restart.bash`, `CALIBRATE_LOCK_TIMEOUT_SECONDS=1200`), which also skips its restart
  while the lock is live.

Past that point the watchdog would restart the stack underneath a still-running calibration. A MODE_TRACK run's wall time
is at most the preflight recovery plus the budget: 180 + 60 + 900 = 1140 s, leaving a minute of margin. Raising
`TOTAL_BUDGET_SECONDS` or either preflight constant spends that margin; going past about 20 minutes means raising both
timeouts, in both repos, together. (MODE_ADSB has no overall bound, which is one more reason it is not exposed.)

### Dependency injection

`Calibrator` takes `blah2_client`, `retina_tracker_client`, and optionally `config_mgr` and `apply_service`. The last two
are only used by the preflight's recovery branch and are injected rather than imported so the module keeps its
no-Flask/no-subprocess/no-Docker property and stays testable against fakes.

## Status and UI

`get_status()` returns a deep copy of the status dict with `progress.elapsed_seconds` computed live. Key fields:

| Field | Meaning |
| --- | --- |
| `state` | `idle`, `running`, `done`, `failed`, `cancelled`. |
| `phase` | `preflight`, `recovering`, `descending`, `refining`, `dwelling`, `soaking`, `restoring`, or `None`. Labels in `RetinaCalibrate.PHASE_LABELS`. |
| `current` | Tower index/name, fc, gains and LNA state being tried now. |
| `rf` | Latest overload flags per tuner. |
| `progress` | Towers tried/total, retune count, elapsed, budget (`null` in MODE_ADSB). |
| `best_attempt` | Best evidence so far, for the UI. |
| `result` / `fallback` | Persistable tuning (see [End of run](#end-of-run-restore-or-fallback)). `RetinaCalibrate.tuningOf` picks between them. |
| `skip_confirmation` | Whether the run deliberately never looked for a track. |
| `preflight` | `None` on a clean probe; `{restarted, recovered, previous}` after a recovery. |
| `history` | One entry per tower: `descent` log, `outcome`, `final_*`, `dwell_seconds` or `soak_seconds`, `dwell_backoffs`, `gains_tried` (ADS-B), `tuning_error`, `device_error`. |
| `system_update` | Added by the route: the reason text if an update is installing. |

Per-tower outcomes and how `calibrate.js` (`OUTCOME_TEXT`) describes them:

| Outcome | Meaning |
| --- | --- |
| `confirmed_track` | A track confirmed. |
| `no_confirmed_track` | Watched, nothing confirmed. |
| `skipped_no_time` | Tuned but never watched (budget ran out). |
| `tuning_not_applied` | Never watched (`_verify_applied` failed). |
| `unstable_overload` | Stopped after too many mid-dwell backoffs. |
| `tuned` | Soak held; no track was waited for. |
| `not_reached` | Default before anything else is recorded. |

The run's summary message cannot express these, and without them every failure reads as "probably no aircraft". `diagnose`
lists them and adds "No tower was actually watched" only when nothing was watched **and** the run was not soak-only (a soak
did watch, for overload). `soakSummary` turns the quick run's `history[0]` into one sentence ("Held cleanly for Ns…" or
"Backed off N time(s)…"); a quick run only tries the current tower. `pollStatus` keeps polling through fetch errors (the
GUI blinks out while the stack restarts) and returns a stop function that dismissable views must call. `pollApply` gives
up after 5 consecutive misses rather than spinning forever.

`on_complete` on the `Calibrator` is called with the final status when a run ends; its exceptions are swallowed.

## History and rejected designs

- **Cold start at maximum gain.** Replaced by [safe-end first](#safe-end-first) after it destabilised a device near a
  strong tower.
- **Per-tower tracker reset.** Replaced by a reset per dwell ([Tracker reset per dwell](#tracker-reset-per-dwell)).
- **A single descent-plus-dwell allowance per tower.** Replaced by the capped descent share ([Time budget](#time-budget)).
- **Hardware AGC as a last resort.** After every manual search failed, the engine used to try one AGC-on attempt at the
  top-ranked tower. It was removed. AGC only drives the reference tuner, whose manual descent already walks to the
  highest gain that does not clip (where AGC converges anyway), while the dominant reason a run fails is that no aircraft
  was overhead, which AGC cannot influence. It also cost two full config-merger and container-recreate cycles inside a
  live run, the only Docker coupling the engine had. Do not rebuild it.
- **Skipping the soak in skip-confirmation runs.** The first version dropped the whole dwell, persisting a one-second
  descent verdict. The track wait goes; the overload watch stays.
