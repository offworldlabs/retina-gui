# Config editor

The `/config` page is a form over the node's radar configuration (capture, location, ADS-B truth,
tar1090 and retina-tracker settings) plus a set of administration panels that save on their own.
The radar fields are generated from Pydantic models, display the merged `config.yml` that the stack
is actually running, and write only the user's overrides to `user.yml`. Saving is followed
automatically by an apply, which runs retina-node's config-merger and restarts the stack.

## Where it lives

| File | Role |
| --- | --- |
| [src/config_schema.py](../../src/config_schema.py) | Pydantic form models (`CaptureFormConfig`, `LocationFormConfig`, `AdsbTruthConfig`, `Tar1090Config`, `RetinaTrackerConfig`, plus `ContactFormConfig` and `ClaimFormConfig` used by the setup routes), length caps, YAML load/save and dict helpers. |
| [src/config_manager.py](../../src/config_manager.py) | `ConfigManager`: reads `config.yml` and `user.yml`, flattens nested YAML into form fields and back, parses the posted form, computes the overrides to write. |
| [src/form_utils.py](../../src/form_utils.py) | `schema_to_form_fields`: turns a Pydantic model plus current values into the field dicts the template renders. Works on Pydantic v1 and v2. |
| [src/routes/config.py](../../src/routes/config.py) | Blueprint: `/config`, `/config/save`, `/config/apply`, `/config/apply/status`, `/config/rf-status`, plus `/ssh-keys`, `/ssh-keys/delete` and `/node-name`. Cross-field validation (location, ADS-B source). |
| [templates/config.html](../../templates/config.html) | The page: `render_field` macro, hand-built Location and tar1090 sections, tower presets, mode switch, save bar, auto-apply, peak meter, Auto-Calibrate modal, administration panels. |
| [src/routes/towers.py](../../src/routes/towers.py) | `/towers/cache/add` and `/towers/cache/remove`, used by the Tower section. |
| [static/calibrate.js](../../static/calibrate.js) | `RetinaCalibrate`, shared with the setup wizard's calibrate step. |
| retina-node `config-merger/script/merge_config.py` | Builds `config.yml` from `default.yml`, `user.yml` and `forced.yml` (separate repo). |

The apply service internals (queueing, locking, the settle window) are covered in
[architecture.md](../architecture.md).

## Layered config

Three input layers are merged, later layers winning:

```
default.yml -> user.yml -> forced.yml   =>   config.yml
```

| File | Owner | What retina-gui does with it |
| --- | --- | --- |
| `default.yml`, `forced.yml` | Baked into the retina-node containers | Nothing directly. |
| `user.yml` (`/data/retina-node/config/user.yml`) | The node owner, via retina-gui | Writes overrides here. Also written by `/towers/select` and `/calibrate/apply`. |
| `config.yml` (`/data/retina-node/config/config.yml`) | config-merger output | Reads it to show what is running. Never writes it. |

Paths come from `USER_CONFIG_PATH` and `MERGED_CONFIG_PATH` in `services.py` (both under `dev_data/`
in dev mode). `ConfigManager.is_retina_node_installed` checks for `docker-compose.yaml` under
`RETINA_NODE_PATH`; without it (and outside dev mode or `?demo=1`) the page greys out the radar
sections.

The rule the whole page follows: **display from `config.yml`, write to `user.yml`**. The form shows
the merged values because those are what blah2 is running; writing the whole merged config back
would pin every default into `user.yml` and stop future `default.yml` changes from reaching the node.

### config-merger

config-merger lives in retina-node and runs as part of every apply. Relevant behaviour, from its
`merge_config.py`:

- If `user.yml` does not exist it is created as a copy of `default.yml`. So on most nodes `user.yml`
  starts out holding a full copy of the defaults, not just real overrides. The merger carries several
  migrations that recognise an unchanged first-boot copy of an old default and move it forward.
- `migrate_gain_reduction` converts a legacy scalar `capture.device.gainReduction` into a
  `[reference, surveillance]` pair after the merge. See [Per-tuner gain and LNA](#per-tuner-gain-and-lna).
- It also generates `tar1090.env` from the `tar1090` section and `retina-tracker.yaml` from the
  `retina_tracker` section, which is how those two form sections reach their containers.

## Form generation

`routes/config.py:config_page` loads `config.yml`, turns each section into a flat dict, and passes
it with the matching model to `form_utils.schema_to_form_fields`:

| Section | Source in `config.yml` | Flattened by | Model |
| --- | --- | --- | --- |
| `capture` | `capture` | `ConfigManager.flatten_capture_for_form` | `CaptureFormConfig` |
| `location` | `location.rx`, `location.tx` | `ConfigManager.flatten_location_for_form` | `LocationFormConfig` |
| `truth` | `truth.adsb` | (used as is) | `AdsbTruthConfig` |
| `tar1090` | `tar1090` | `ConfigManager.parse_tar1090_adsb_source` | `Tar1090Config` |
| `retina_tracker` | `retina_tracker` | (used as is) | `RetinaTrackerConfig` |

The models are flat on purpose: nested YAML such as `capture.device.lnaState` becomes a flat field
(`device_lnaState`) so one model maps to one form section.

`schema_to_form_fields` produces one dict per field:

| Pydantic | Form dict |
| --- | --- |
| Annotation `Literal[...]` | `type: "select"`, `options` from the literal values |
| `bool` | `type: "checkbox"` |
| `int`, `float` | `type: "number"`; `float` also gets `step: "any"` |
| anything else | `type: "text"` |
| `ge` / `gt` | `min` (a `gt` bound is rendered as an inclusive `min`) |
| `le` | `max` |
| `title`, `description` | Label and help text, shown in the UI |
| `json_schema_extra={'readonly': True}` (v2) or `readonly=True` (v1) | `readonly`, via `config_schema._readonly_field` |
| A nested model | `type: "group"` with its own `fields` |

`value` always comes from the values passed in (the merged config, or the rejected submission on a
validation error), never from a schema default. `Optional[X]` and `X | None` are unwrapped to `X`
first. Both spellings must be recognised: `get_origin` returns `typing.Union` for one and
`types.UnionType` for the other, and matching only the first made PEP 604 fields lose their input
type (a port rendered as a text box). The repo's ruff config enforces `X | None` (UP045).

The `render_field` macro in `config.html` draws each dict. A readonly text field is shown locked
with a hidden input so its value still posts; a readonly checkbox posts through a hidden input too.
Location and tar1090 are hand-built rather than rendered through the macro (see
[Validation](#validation) for what that costs).

The Pydantic `Field(description=...)` strings are UI copy. Changing them changes the page.

### Pydantic v1 and v2

Nodes run Pydantic v1 (Debian Bookworm's apt package); development environments usually have v2.
`form_utils` and `config_schema` detect the version and read field metadata the right way for each.
This shapes the schema in a few ways:

- No model validators: cross-field rules live in `routes/config.py` instead (which is also where
  they can attach errors to fields).
- No `regex=` / `pattern=` constraints, because each is only understood by one version. Pattern
  checks such as `CONTACT_COUNTRY_PATTERN` and `CLAIM_EMAIL_PATTERN` are applied in
  `routes/setup.py`. Length caps are portable and stay on the model.

## Sections and schema models

| Section | Fields | Notes |
| --- | --- | --- |
| Capture | `fs`, `fc`, `device_type` (readonly), `device_agcSetPoint`, `device_gainReductionA/B`, `device_lnaState`, `device_dabNotch`, `device_rfNotch`, `device_bandwidthNumber` | `fs` and `device_bandwidthNumber` are `Literal` selects. |
| Location | `rx_*` and `tx_*` latitude, longitude, altitude, name | All optional. See [Location: all or nothing](#location-all-or-nothing). |
| ADS-B truth | `enabled`, `tar1090`, `adsb2dd`, `delay_tolerance`, `doppler_tolerance` | Stored under `truth.adsb`. |
| tar1090 | `adsb_source_host/port/protocol`, `adsblol_fallback`, `adsblol_radius` | See [ADS-B source](#ads-b-source). |
| Retina Tracker | `min_snr` | Tunes the retina-tracker sidecar, not blah2's built-in tracker (`process.tracker`). Reaches the sidecar via config-merger's `retina-tracker.yaml`. |

### Transmitter name length

`TX_NAME_MAX_LENGTH` (32) is not a display limit. Transmitter names travel to the server as
retina-telemetry's `tx_callsign`, which the node-ingest spec caps at 32. A longer name means
retina-telemetry cannot build a `NodeConfig`, so registration and every config resend fail and the
node never reaches the server. Tower-Finder results never come close; the only way over is a
hand-typed name, so the limit is checked in three places: `LocationFormConfig.tx_name`, the manual
Add Tower dialog (`/towers/cache/add`), and `maxlength="32"` on the inputs in `config.html`.
`routes/towers.py` also trims over-long names from finder results before caching them.

## Per-tuner gain and LNA

The RSPduo has two tuners: A is the reference channel, B the surveillance channel. `config.yml`
stores gain reduction as a list, `capture.device.gainReduction: [A, B]`, and the form splits it
into `device_gainReductionA` ("Reference Gain Reduction") and `device_gainReductionB`
("Surveillance Gain Reduction"), each 20-59 dB.

- `flatten_capture_for_form` accepts both the list and the older scalar form. A scalar (from a
  config written before the split) fills both boxes with the same value.
- `unflatten_capture_from_form` always writes the list form.
- config-merger's `migrate_gain_reduction` converts any scalar that survives into the merged config
  into a `[g, g]` pair, since a scalar in `user.yml` would otherwise clobber `default.yml`'s list and
  break blah2's per-tuner support.

LNA state (`device_lnaState`, 1-9, 1 = maximum gain) is a single value shared by both tuners.
There is no per-tuner LNA state in the form or the schema.

`device_bandwidthNumber` is the AGC loop bandwidth: 0 disables AGC so gain is fixed by the gain
reduction and LNA state fields; 5, 50 and 100 enable it.

## Validation

`/config/save` validates each posted section against its model and collects errors into
`config_errors`, keyed `"<section>.<field>"` (built by `ConfigManager.format_validation_errors`).
A special `_form` key carries a message that belongs to no field. On any error the page is
re-rendered from the submitted values (not from disk), so the user's input is kept.

The banner at the top shows `config_errors['_form']` if present, otherwise "Please fix the
highlighted fields below". The `render_field` macro highlights its own field. The Location and
tar1090 sections are hand-built, so every input in them looks up its own `config_errors` key.
Without that the banner claimed fields were highlighted when none were. All eight Location inputs
are wired, not only the ones that can fail today, so a future constraint cannot silently
reintroduce the problem.

### Location: all or nothing

A node has no location until its owner picks a tower, and retina-node ships the location as null
rather than defaulting to a plausible site, so every `LocationFormConfig` field is optional. But
the six coordinates (`LOCATION_COORDINATE_FIELDS`) must be all present or all absent.
blah2 derives its whole bistatic solution from them, and a missing one becomes NaN rather than an
error: the radar runs and silently associates nothing, and the form is the only place an owner
finds out. Names are excluded; a position without a label is still a position.
`LocationFormConfig.is_located` reports whether all six are set.

The rule is enforced in `routes/config.py:_location_errors`, not in the model, because it is a
cross-field rule and nodes run Pydantic v1 (no model validators). It returns a single `_form`
message naming the missing coordinates rather than per-field errors. The inputs would highlight
per-field keys if sent; one sentence about the group reads better than up to five boxes repeating
the same complaint. `save_config` adds it with `setdefault`, so a calibration refusal already in
`_form` wins.

### ADS-B source

tar1090's source is one YAML value, `tar1090.adsb_source: "host,port,protocol"`, split into three
boxes by `parse_tar1090_adsb_source`. The three only mean something as a set:

| Boxes filled | `adsblol_fallback` | Result |
| --- | --- | --- |
| All three | any | Valid. |
| None | on | Valid: tar1090 is fed from adsb.lol through the tar1090 proxy. |
| None | off | Error on each box: a source is required unless the fallback is on. |
| Some | any | Error on each empty box. A partial set joins into a malformed `"host,,"` that tar1090 accepts and then never connects on. |

`routes/config.py:_adsb_source_errors` enforces this. It is not a model validator because a
model-level error carries no field name, so the banner would appear with nothing highlighted.

An emptied source is written as `adsb_source: ""`, not omitted. `default.yml` ships a source of its
own, so dropping the key would let the next merge put the default straight back and silently undo
the clearing. `compute_user_overrides` still discards the `""` when it matches what is already
merged.

## Save and apply

```
Apply changes (submit)
  -> [mode changed?] POST /api/mode, then form.submit()
  -> POST /config/save          validate, write user.yml, redirect to /config?saved=1
  -> page load sees ?saved=1    POST /config/apply (202), poll /config/apply/status every 1 s
  -> state done                 "Applied successfully!", reload /config
```

### Parsing the posted form

`ConfigManager.parse_flat_form_data` splits `request.form` by prefix (`capture.`, `location.`,
`truth.`, `tar1090.`, `retina_tracker.`). Values are typed by their shape, not by the schema:
`true`/`false`/`on` become booleans, strings containing `.` are tried as floats, others as ints,
and anything that fails stays a string. Empty strings are dropped. Unchecked checkboxes are not
posted, so the parser fills `device_dabNotch`, `device_rfNotch`, `truth.enabled` and
`tar1090.adsblol_fallback` with `False` when the rest of their section was posted.

### Override computation

`ConfigManager.compute_user_overrides` walks the submitted section and keeps a value when either:

- it differs from the merged `config.yml` value (floats compared with a 1e-9 tolerance by
  `config_schema.values_differ`), or
- `user.yml` already holds that key with the same value (an existing override is kept, not
  dropped because it now matches the merged config it produced).

A submitted value equal to the merged value, with no existing override, is not written.

`save_config` then rebuilds `user.yml`: top-level keys outside the five form sections are copied
over unchanged, and each form section is replaced by its computed overrides. Keys inside those
sections that the form does not post are not carried over (for `truth`, only `truth.adsb` is
posted). The file is written atomically by `config_schema.save_yaml_file` (temp file, `chmod 644`,
rename).

### Mode switch

The Mode control (Radar, Spectrum, SDRconnect) is staged, not immediate. On submit, if the selected
mode differs from the one `/api/mode` reported on load, the page POSTs `/api/mode` first and only
then calls `form.submit()` (which bypasses the submit handler). On failure the control stays on the
target mode and the message asks the user to press Apply again to retry.

### Apply

`/config/apply` starts the apply on a background thread and returns 202 at once; the page polls
`/config/apply/status` and shows the phase label, the settle countdown, and whether a later change
is queued. Running it off the request is what stops a slow apply (around 45 s, mostly the SDR
settle window) looking like a hang and being re-clicked into a second, colliding
`docker compose` run. In spectrum mode only config-merger runs: blah2 is intentionally stopped and
must not be restarted until the user switches back to radar. See [architecture.md](../architecture.md)
for the apply service.

### Guards

| Guard | Where | Why |
| --- | --- | --- |
| Setup wizard in progress | `routes/config.py:_check_wizard_not_active` redirects every route in the blueprint to `/set-up`, except `/config/apply/status` | The wizard's tower step polls that status. A redirect would hand `fetch()` an HTML page: the request succeeds, `.json()` rejects, and the step re-polls forever with its spinner up and skip button hidden. Matched exactly, not by prefix, because `/config/apply` and `/config/save` mutate and must stay blocked. The same rule applies to `_CALIBRATION_ALLOWED_PREFIXES` in `app.py`. |
| Auto-Calibrate running | `/config/save` (form-level error) and `ApplyService.request()` (raises `ConfigChangeRefused`, 409) | The save is refused as well as the apply. Guarding only the apply would leave changes in `user.yml` that were never applied, and they would be swept silently into the next merge, including the one `/calibrate/apply` runs after a successful calibration. The apply check lives in `request()` so no future route can skip it. |
| Mender install in progress | `/config/apply`, 409 | The install replaces the compose manifests config-merger runs against, and mender-update's own docker commands are outside the restart lock, so the two can genuinely run at once. The apply would also report success while skipping the restart, since the install sets `mode.txt` to `spectrum`. An install can end in rollback or reboot, so the user is asked to retry rather than having the apply held across it. |
| retina-node missing | `/config/apply`, 400 | Nothing to apply to. |

The page also mirrors the calibration guard: while a run is going, `setConfigLockedOut` disables
Apply changes and the tower preset and remove controls, so the user is not refused after filling in
a form.

### Unsaved changes

The save bar counts inputs whose value differs from a snapshot taken on load. Discard reloads the
page. `window.RetinaConfigForm.adoptSaved(values)` lets other code on the page change fields and
move the snapshot with them; today only Auto-Calibrate uses it (see
[Auto-Calibrate on this page](#auto-calibrate-on-this-page)). The snapshot records the element's
value read back after setting it, so a select that rejects a value is not recorded as holding it.

## Tower presets

The Tower section offers the towers cached by the setup wizard's search (`towers-cache.json`, read
through `device_state.get_towers_cache`). Two different save models share the section:

- **Picking a preset** only fills form fields: `location.tx_name` (callsign, else name),
  `location.tx_latitude`, `location.tx_longitude`, `location.tx_altitude`, and `capture.fc`
  (the tower's MHz times 1e6, rounded). Nothing is saved until Apply changes, like any other edit.
  The receiver position is not touched.
- **Add tower / Remove** edit the cache file immediately, via `/towers/cache/add` and
  `/towers/cache/remove`, like SSH keys. The response carries the new list and `renderTowers`
  rebuilds the dropdown and list without a reload. Remove takes the tower's position in the list.

The manage list highlights the row whose latitude, longitude (within 1e-5) and frequency match the
current transmitter fields. It updates on preset picks, hand edits and add/remove, since
`setField` fires the same `input` events a hand edit does.

## Signal peak meter

The Capture section shows a live peak level for each tuner (Reference / A, Surveillance / B),
polled every second from `/config/rf-status`. That route returns blah2's `/capture/rf-status`
(the peaks), plus the overload flags and onset counts from `/capture/overload-status` while they
are fresh (see [Overload indicator](#overload-indicator)). blah2_api keeps the two on separate
endpoints; the meter is the one consumer that needs both, so the route merges them.

- Scale -60 to 0 dBFS in 24 segments; 0 dBFS is saturated.
- PPM-style ballistics: instant attack up to a louder report, decay at 14 dB/s toward a quieter one.
  The raw value is the latest report, not a running maximum, so there is something to decay toward.
- blah2 clamps the peak to at least one sample before taking log10, so a device that delivered no
  samples reads as 20*log10(1/32767), about -90.3 dBFS. A real front end's thermal noise never gets
  that low, so anything at or below -89 dBFS is treated as no data.
- Missing data alone is not an alarm: blah2 is stopped on purpose in spectrum and SDRconnect modes,
  and a dropped poll or container restart is normal. The meter shows "no signal" only when
  `/api/mode` says radar and data has been missing for 4 s or more (for example a hung SDRplay
  service or an unplugged SDR). The mode is only fetched while data is missing.

### Overload indicator

A tuner's value reads **overload** in red, in place of its dBFS figure, while either holds:

- its overload flag has stayed set for 3 s (`OVERLOAD_HOLD_MS`), so a momentary clip does not
  flicker the label, or
- the tuner has had 3 or more overload onsets within the last 60 s (`CLIP_ONSETS`,
  `CLIP_WINDOW_MS`). The RSPduo clips and recovers faster than the flag can be sampled: a whole
  episode can pass with every reading of the flag false, and only blah2's onset count shows it.

How the onsets are tallied:

- Each poll adds the rise in the tuner's onset count since the previous poll, so a single poll
  where the count jumped by 3 is enough.
- The first poll after the page loads is the baseline. Onsets from before the page was opened do
  not count, however many blah2 has recorded.
- A drop in the count is a restarted blah2 counting from 0 again, and is taken as a new baseline.
- A blah2 that does not report counts (older than the onset counters) leaves only the flag rule.

When it clears: on the flag rule, as soon as the flag drops. On the onset rule, once fewer than 3
onsets remain inside the last 60 s, so the label can stay up to a minute after the last clip. That
is deliberate: a radar clipping every few seconds is overloaded, even though every individual
reading between clips looks clean. The ladder itself still shows the peak level throughout.

The flag comes from the SDRplay API's own overload detector, separate from the peak. They usually
agree: measured on a test node with a strong tower and the DAB notch off, the overloaded tuner's
peak sat at about -2 dBFS. But the flag is the authoritative signal, so the label does not wait
for the peak to reach the top of the scale.

`/config/rf-status` only passes the overload state through while blah2_api received it within the
last 10 s (`OVERLOAD_STALE_MS` in `routes/config.py`). blah2 re-posts it at least every 2 s, so an
older state means blah2 has stopped, and a last-known "overloaded" must not keep showing. It skips
the overload lookup altogether when there are no peaks, since the meter is then on its no-signal
path.

### Overload help

While either tuner reads as overloaded, a yellow box under the meter says which input is clipping
("the Reference input (Tuner A)", "the Surveillance input (Tuner B)" or "both inputs") and what to
do: run Quick Calibrate to find the most sensitive settings that do not overload, or restart on
the safest settings to stop it straight away. It shows and hides with the meter's label, by the
rules in [Overload indicator](#overload-indicator). Its **Quick Calibrate** button opens the
same run as the button above it, and **Use safe settings** behaves as in
[No-signal help](#no-signal-help). It stays hidden while a calibration or an apply runs: a
calibration overloads the radio on purpose while it searches, and an apply restarts it.

Why the page needs this: until blah2-arm#79, an overload while blah2 applied its start-up gains,
or during a live retune onto a strong tower, deadlocked the SDRplay API. blah2 exited, the
service hung, and the meter showed "no signal", so the No-signal help was in practice the overload
help. blah2 now acknowledges overloads outside the API's event callback, so an overloaded radio
keeps streaming. Without this box it would run clipped, with degraded detections, and nothing on
the page would prompt the owner to act.

### No-signal help

When "no signal" has lasted 60 s, a yellow box under the meter tells the owner what to do: check
the USB cable, restart the radar, and if there is still no signal, restart on the safest settings
and then run Quick Calibrate once a signal is back. If nothing helps, contact support.

- **Restart radar** reloads the page with `?saved=1`, which runs the page's normal
  [apply](#apply) on the saved settings: config-merger, stopping blah2, a forced `sdrplay_apiService`
  restart, the settle window and a container recreate, with progress on the Apply button. If the page has
  unsaved changes, it asks first, because the reload discards them.
- **Use safe settings** sets both gain reductions to 59 and the LNA state to 9
  (`calibrator.GAIN_REDUCTION_MAX` and `LNA_STATE_MAX`, which the page script mirrors in
  `SAFE_SETTINGS`) and submits the form, so it takes the normal [save and apply](#save-and-apply)
  path. If the page already has unsaved changes, it asks first, because they are submitted with it.

Why it says this and not something more specific:

- **"No signal" cannot say why.** Once blah2 stops delivering samples, an unplugged USB cable, a
  hung SDRplay service and a blah2 that failed for some other reason look the same from here.
- **A restart comes before safe settings.** The usual cause of a hung service is now something
  other than overload, and the apply's forced service restart and settle clear it. The cron
  watchdog restarts the stack every 5 minutes while blah2 crash-loops, but without a settle window,
  so a restart from this page is still worth trying.
- **Safe settings are still the next step.** A node on a blah2 older than blah2-arm#79 still goes
  down when it overloads at start-up, and restarts on the same tuning go down again. Restarting on
  59/59/9 removes overload as a cause. The LNA state matters most: a node near a strong tower can
  overload at 59/59 with a sensitive LNA state.
- **The safe settings are almost deaf.** That is why Quick Calibrate follows.
- **It waits 60 s and stays hidden while a calibration or an apply runs**, because each of those
  restarts the radar and shows "no signal" for 30-60 s. `/calibrate/status` and
  `/config/apply/status` are only fetched once the minute has passed, not on every poll.

## Auto-Calibrate on this page

The Capture section has two buttons, deliberately separate because they answer different questions
and take very different times:

| Button | Run | Time |
| --- | --- | --- |
| Auto-Calibrate | Searches towers and gain until a confirmed track appears (`{mode: "track"}`) | about 10 min |
| Quick Calibrate | Current tower only: find the best gain, then soak 45 s to check it holds without overloading. No aircraft needed. Posts `RetinaCalibrate.QUICK_RUN`, the same object the setup wizard posts, so the two stay the same run. | about 4 min |

The Quick Calibrate prompt offers no mode choice, since the run never confirms anything, and says
plainly that its tuning was never checked against a real aircraft. The ADS-B verified mode is
benched, not removed: its radio button is commented out and `/calibrate/start` rejects
`mode: "adsb"` with a 409, so it is not only hidden in the UI.

Vocabulary and interpretation shared with the wizard (phase labels, `diagnose`, `updateWarning`,
`soakSummary`) live in `static/calibrate.js`. Page-side behaviour:

- **Reattaching.** A run is a background thread on the node and outlives the page. On load, and
  whenever either button is opened, the page checks `/calibrate/status` and attaches to a running
  run (started by the other button, another tab, or the wizard) instead of offering Start, which
  would only earn a 409. Before this, refreshing left a run going with no way to see or cancel it.
  The modal title and mode line follow the run's own `skip_confirmation`, not the button pressed,
  so a quick run is never labelled as one that will wait for a track.
- **Leaving.** A `beforeunload` prompt fires while a run is going. Leaving does not stop it, but
  people assume it does, and a run holds the SDR and blocks config changes for its whole budget.
- **Deployments.** A deployment warning is shown as soon as one starts, not after the run has died
  of it, and leads the failure report, because a deployment restarting the radar explains the
  per-tower failures better than the tuning or the sky does.
- **Quick run results.** A quick run ends in state `failed` by design (there is nothing to confirm).
  It still resolved tuning and proved it holds, so it is shown as "Tuning resolved", not
  "No calibration found", and the "Closest attempt" advice is suppressed.
- **Failure diagnosis.** A run's own message assumes the towers were watched. When they were not
  (tuning never reached the device, the operating point kept clipping, time ran out mid-search),
  "no aircraft was overhead" is wrong, so the page adds `diagnose(history)`, what the engine
  determined per tower.
- **Fallback.** A run that found no track still resolved an operating point the descent proved the
  device tolerates, and the radar is running on it, but only until the next restart unless it is
  written to config. The modal saves it automatically, just as for a confirmed result. This is
  separate from "Closest attempt", which is informational and may name a different tower.
- **Saving.** A finished run with tuning (a confirmed result or the fallback) is saved without
  asking. `saveAutomatically` POSTs `/calibrate/apply` once per run (keyed on the run's
  `started_at`), which writes `user.yml` synchronously and queues the merge and restart on the
  shared apply queue. Progress shows under the result (`calSaveStatus`) while it polls
  `/config/apply/status`. There used to be a "Persist to config" button, but it read as optional
  when leaving the tuning unsaved only meant the next restart threw the run away. A failed save
  says the radar is running with the tuning until it next restarts. Cancelled runs have no tuning
  (see `RetinaCalibrate.tuningOf`), so they save nothing.

### Adopting the persisted values

The Capture and Location fields were rendered from `config.yml` before the run, so after a save
they show the old tuning. That is not only confusing (a user saw the calibrated gains in the modal
and the old ones in the form), it is destructive: Save posts every field. Before the merge finishes,
`config.yml` still holds the old values, so the stale submission matches it and
`compute_user_overrides` drops the calibration's override from `user.yml`; after the merge, the stale
values differ and overwrite it. Either way one Save undoes the calibration.

So as soon as `/calibrate/apply` answers, `adoptPersisted` calls `RetinaConfigForm.adoptSaved` with
`fc`, both gain reductions, LNA state and bandwidth number, and, only when the run moved to a
different tower, the four transmitter fields. Without the transmitter fields in that case, `fc` alone
would leave the page and blah2's geometry describing the wrong transmitter. Both halves of
`adoptSaved` matter: the fields change, and the unsaved-changes baseline moves with them so the
save bar does not claim changes that are already on disk.

## Administration sections

Everything after the radar form (`#configForm` closes after Retina Tracker) saves on its own
endpoint, immediately, and is not part of Apply changes.

| Section | Endpoint | Notes |
| --- | --- | --- |
| This node | `POST /node-name` | See below. |
| SSH access | `POST /ssh-keys`, `/ssh-keys/delete` | Form posts that redirect back to `/config`. |
| Remote support | `/remote-access/toggle`, `/remote-access/shell` | Two independent toggles. |
| How we reach you | `POST /set-up/contact` | Optional owner contact details. |
| Node claim | `POST /set-up/claim` | The address that owns the node. |
| Cloud services | (page script) | Disabling also switches off Remote support. |
| Setup wizard | link to `/set-up` | |
| Network | (page script) | Status, scan, manual entry, connect. |

The Remote support, contact and claim template variables come from the blueprint's context
processor `_remote_access_context`, not from each `render_template` call. `config.html` has two
render points (`/config` and the validation-error branch of `/config/save`), and passing the same
arguments by hand is how the second one once shipped without them. A context processor cannot drift
when a third render point appears.

### This node

The node name is only a label. The fleet page shows it on the node's card instead of the
`ret<node_id>` identifier, and it reaches other nodes through the DNS-SD TXT record. Nothing
addresses the node by it, so a rename cannot break a bookmark or an SSH config.

The address shown is the node's own `http://<node_id>.local`, deliberately not `owl.local`: that
name is answered by every node on the network at once, so checking it proves only that some node is
reachable, and on a network with more than one node it leads to the fleet list instead.

### Remote support

- `remote_host` is derived as `<node_id>.<REMOTE_ACCESS_DOMAIN>`, not reported, so the page can name
  the address before anything has provisioned it.
- `remote_tunnel` comes from systemd (`remote_access.tunnel_status`). Nothing on the node is told
  whether provisioning worked, so whether the connector is up is the honest answer to "is it
  reachable". After enabling, the page gives no time estimate: that depends on node-infra's
  scheduling, which the node knows nothing about.
- `shell_enforced` is read back from mender-connect's own config, not from what was recorded. The two
  can disagree (a failed enforcement, a hand-edited config), and the page would otherwise show a
  choice the node is not honouring. `None` means the config could not be read. After a toggle, the
  response can be trusted as is: the route enforces before it records and errors if enforcement
  failed.
- The two toggles drive separate endpoints, each reverting itself on failure, and are deliberately
  not linked: the point of two settings is that changing one says nothing about the other.

### How we reach you

The other half of Remote support: those settings are how support reaches the node, this is how
support reaches the owner. It is its own section because it is the only block about a person rather
than the radar, and because the `name`/`desc`/`help` classes it used inside Remote support are only
styled within a `toggle-row`, so it rendered as unstyled text there.

`ContactFormConfig` is optional and nullable throughout, and empty is the steady state: a node with
nothing to report never calls the contact endpoint. Nothing in it is verified, identifies anyone to
the server, or grants an account. It exists so that a fault support can see and the owner cannot
has somewhere to go. An owner who skips the step and one who clears every box mean the same thing
(`is_empty`). When every box is empty the endpoint removes the record (`stored: false`), and the
page says "Details removed", not "Saved".

It is deliberately kept out of the file holding the three agreement records. Those are versioned
acceptances neither end may invent, and retina-telemetry refuses to register without all three; a
mutable optional document beside them would let a malformed contact stop a node registering.

`country` is ISO 3166-1 alpha-2 and belongs to the phone number, not the owner (the server added it
to record which country a contact's phone number is in). Asking it as "where do you live" would put
a wrong answer against a real person.

The length caps (`CONTACT_NAME_MAX_LENGTH` 64, `CONTACT_EMAIL_MAX_LENGTH` 255,
`CONTACT_PHONE_MAX_LENGTH` 32) are copied from the node-ingest spec's `NodeContact`. Past one of
them retina-telemetry cannot build the payload, so the whole document is refused and the owner is
unreachable. They move only when the spec does.

### Node claim

**Not the contact email**, however alike the boxes look. The contact answers "whom do we ring about
this node", is optional and grants nothing. The claim answers "who owns it": the server mails the
address a link, and opening it binds the node to the account behind that address. A wrong value
mails a stranger a link that hands them somebody's node. So `ClaimFormConfig` lives in a file of its
own, and nothing copies one address into the other, even though an owner may well give the same
address twice.

- `CLAIM_EMAIL_MAX_LENGTH` (255) is copied from `NodeClaimRequest.email` in the spec, for the same
  reason as the contact caps.
- `CLAIM_EMAIL_PATTERN` is deliberately thin (one `@`, something either side, no whitespace), no
  stricter than the server. Nothing can verify the address before the link is clicked; the check
  exists so a mistyped address is caught in the box rather than by waiting for a link that never
  arrives. It is applied in `routes/setup.py` (see [Pydantic v1 and v2](#pydantic-v1-and-v2)).
- The claim state belongs to retina-telemetry, the only thing on the node that talks to the server,
  and is read from its status document (`telemetry_status.read()`, as the home page does). `claim`
  is the address stored here; `claim_state` is what the server has done with it. They disagree for
  as long as it takes retina-telemetry to notice a change and be answered. When the telemetry
  service is not running or predates the claim, the section says it cannot tell rather than
  guessing.
- There is one button, Send link, and every press asks for a link. Nothing is claimed from the page:
  retina-telemetry reads the file on its next pass (within a minute) and decides between an offer
  and a resend. A separate Save could store an address the node already held without anyone seeing
  the press, which after a release meant nothing was mailed. See
  `device_state.save_telemetry_claim`.
- Send link is disabled while the node is owned: a resend is refused there, and a new address would
  be offering somebody else's node.
- `unclaimed` with an address on file is what a declined or expired link leaves. Nothing more
  arrives unless the owner asks again.

## See also

- [towers.md](towers.md): tower search, the tower cache and `/towers/select`.
- [auto-calibrate.md](auto-calibrate.md): the calibration engine behind the Capture buttons.
- [remote-access.md](remote-access.md): the remote support toggles, remote shell and SSH keys.
- [fleet-and-naming.md](fleet-and-naming.md): node names and addresses.
- [setup-wizard.md](setup-wizard.md): the wizard, which shares the contact, claim and calibrate endpoints.
