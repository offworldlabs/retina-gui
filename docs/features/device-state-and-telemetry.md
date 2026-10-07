# Device state and telemetry

retina-gui keeps a small set of files about the node itself under its data directory (`/data/retina-gui` on a node, `dev_data/` in dev mode). Some are private to the GUI (install and calibration locks, the wizard's resume point, the tower cache). Others are cross-repo contracts read by the retina-telemetry container, which will not register a node until the GUI has written them. In the other direction, retina-telemetry's only channel out is a status document it writes, which the GUI reads and presents on the home page, the configuration page, the setup wizard and the fleet `/healthz` payload.

## Where it lives

| File | Role |
| --- | --- |
| [src/device_state.py](../../src/device_state.py) | `DeviceState`: update/calibration locks and their guards, cloud services toggle, wizard step and completion flag, tower cache, telemetry consent, contact and claim records. |
| [src/telemetry_status.py](../../src/telemetry_status.py) | `TelemetryStatus`: reads retina-telemetry's `status.json` and keeps the node reference cache. |
| [src/services.py](../../src/services.py) | Constructs both objects: `DATA_DIR`, `TELEMETRY_STATUS_PATH`, the Mender conf paths. |
| [src/app.py](../../src/app.py) | Startup: `apply_startup_preferences`, stale calibration lock release, `backfill_setup_wizard_completed`. |
| [src/routes/setup.py](../../src/routes/setup.py) | `/set-up/save-step`, `/set-up/consent`, `/set-up/contact`, `/set-up/claim`. |
| [src/routes/home.py](../../src/routes/home.py), [templates/index.html](../../templates/index.html) | Home page Telemetry card. |
| [src/routes/config.py](../../src/routes/config.py), [templates/config.html](../../templates/config.html) | Node claim and contact sections on the configuration page. |
| [src/routes/fleet.py](../../src/routes/fleet.py) | `telemetry_payload`, the telemetry part of `/healthz`. |
| [templates/setup/_agreements.html](../../templates/setup/_agreements.html) | The agreements step whose wording the consent record describes. |

See also [setup-wizard.md](setup-wizard.md) for the wizard flow that writes most of these records.

## Files under the data dir

All paths are relative to `DATA_DIR` (`/data/retina-gui` on a node) unless stated.

| File | Format | Written by | Read by |
| --- | --- | --- | --- |
| `install.lock` | JSON `{version, started_at, stage?}` | `acquire_install_lock`, `update_install_stage` | GUI only |
| `mender-update.status` | JSON `{state, ts}` | Mender state scripts in owl-os (`Download_Enter`, `ArtifactInstall_Enter`); removed by `ArtifactCommit_Leave` / `ArtifactFailure_Enter` | GUI |
| `cloud-services-disabled` | Empty flag file | `set_cloud_services(False)` | GUI (`apply_startup_preferences` on every boot) |
| `calibrate.lock` | JSON `{started_at}` | `acquire_calibration_lock` | GUI, and blah2-arm's `blah2_rspduo_restart.bash` watchdog |
| `setup-wizard.json` | JSON `{started_at, step}` | `save_setup_wizard_step` | GUI only |
| `setup-wizard-completed` | Plain text, an ISO timestamp | `mark_setup_wizard_completed`, `backfill_setup_wizard_completed` | GUI, and retina-telemetry (registration gate) |
| `towers-cache.json` | JSON `{lat, lon, cached_at, towers[]}` | `save_towers_cache`, `add_tower_to_cache`, `remove_tower_from_cache` | GUI (wizard, config tower picker, Auto-Calibrate) |
| `telemetry-consent.json` | JSON, three records (see below) | `save_telemetry_consent` | retina-telemetry (registration gate) |
| `telemetry-contact.json` | JSON, the wire's `NodeContact` | `save_telemetry_contact` | GUI, retina-telemetry |
| `telemetry-claim.json` | JSON `{email, send_requested_at}` | `save_telemetry_claim` | GUI, retina-telemetry |
| `telemetry-node-ref` | Plain text, one node reference | `TelemetryStatus._remember_node_ref` | GUI only |
| `/data/retina-telemetry/status.json` | JSON status document | retina-telemetry | GUI only (read-only to us) |

The paths marked as read by another repo are contracts: retina-telemetry hard-codes `/data/retina-gui/...` for each of them (`collect/wizard.py`, `collect/consent.py`, `collect/contact.py`, `collect/claim.py`), and the blah2 watchdog hard-codes `calibrate.lock`. Renaming any of them on this side breaks the other repo silently.

The Mender conf is also moved around by the cloud services toggle: `/data/mender/mender.conf` is moved to `/data/mender-cloud-disabled/mender.conf` when cloud services are turned off, and back when they are turned on.

## Device state machine

`DeviceState.get_state` reports one of three states:

| State | Source |
| --- | --- |
| `idle` | No lock or status file is active. |
| `updating_gui` | A GUI-initiated OTA install holds `install.lock`. |
| `updating_server` | A server-pushed Mender update is in progress (`mender-update.status`). |

### Guards

| Guard | Blocked when |
| --- | --- |
| `can_toggle_cloud_services` | Any update is in progress. |
| `can_start_install` | Any update is in progress, or a calibration holds `calibrate.lock`. |
| `can_start_calibration` | Any update is in progress (the containers would restart under the run), or another calibration holds the lock. |

`is_any_update_in_progress` combines the two update sources and returns a human reason (`Installing <version>` or `System update in progress (<state>)`).

### Stale lock timeouts

Every lock clears itself when read past its timeout, so a crash never leaves the node stuck.

| Constant | Value | Applies to | Compared against |
| --- | --- | --- | --- |
| `INSTALL_LOCK_TIMEOUT` | 40 min | `install.lock` | `started_at`, naive local time |
| `MENDER_STATUS_TIMEOUT` | 2 h | `mender-update.status` | `ts` (the state scripts write `date -Iseconds`, so it carries an offset) |
| `CALIBRATE_LOCK_TIMEOUT` | 20 min | `calibrate.lock` | `started_at`, naive local time |
| `SETUP_WIZARD_TIMEOUT` | 24 h | `setup-wizard.json` (an abandoned wizard) | `started_at` |

A `mender-update.status` with a missing or unparseable `ts` is treated as active: failing safe means blocking toggles rather than allowing one mid-update.

`CALIBRATE_LOCK_TIMEOUT` is mirrored by `CALIBRATE_LOCK_TIMEOUT_SECONDS=1200` in blah2-arm's watchdog script, which skips its restart while the lock is younger than that (it uses the file's mtime rather than `started_at`). If the two disagree, a crashed calibration either silences the watchdog indefinitely or lets it restart the stack under a live run. The GUI also releases any calibration lock at startup, since a run cannot survive a GUI restart.

### Cloud services toggle

`set_cloud_services` respects `can_toggle_cloud_services`. Disabling writes the flag, stops and disables the Mender services, and moves `mender.conf` into the backup directory. Enabling reverses all three. In dev mode only the flag is touched.

`apply_startup_preferences` runs on every non-dev boot. If the flag is present it stops and disables the services again and re-backs-up `mender.conf`, because an OTA update can regenerate `mender.conf` and would otherwise silently re-enable cloud services.

`ensure_cloud_services_enabled` is the stronger form used when a flow needs Mender: it re-enables everything and polls the supplied `get_jwt_fn` every 2 s for up to about 60 s. It takes a callable rather than a `MenderClient` to avoid a circular dependency.

## Setup wizard state

### Resume point

`setup-wizard.json` holds `{started_at, step}`. `save_setup_wizard_step` preserves the original `started_at` and overwrites `step`. Because the wizard is forward-only, the stored step is both the resume point and the furthest step reached.

An earlier version also tracked a separate `highest_step` so a reloaded page could offer back-navigation to finished steps. Nothing navigates backwards now, and that ordering never listed the calibrate step, so a run that reached it recorded itself as being back at the start. It was removed.

`is_setup_wizard_in_progress` is true when a step is stored and is not `complete`. When the page posts `step: "complete"`, `/set-up/save-step` calls `clear_setup_wizard` and then `mark_setup_wizard_completed`.

### Completion flag

`setup-wizard-completed` records that the wizard has been completed at least once. It is distinct from retina-node being installed: a node can ship with retina-node pre-installed and never have had the wizard run, in which case it is still a first run.

retina-telemetry also reads this flag and will not register a node without it. The flag is what proves the owner has been through the tower step, so the config being reported is theirs rather than the shipped default.

### Setup completion backfill

The completion flag only arrived in commit aee29a6 (2026-06-24). Nodes that completed the wizard before then have none, and without a repair they would be blocked from registering forever, which is the same failure the gate exists to prevent.

`backfill_setup_wizard_completed` is called from `app.py` on every boot (there is nowhere to record that it has run, and it is a no-op once the flag exists). It writes the flag when `user.yml` records a receiver position:

- `location.rx.latitude` and `location.rx.longitude` must both be present. A partial block is not evidence of a completed tower step, and `0` is a legitimate latitude, so `_has_user_chosen_location` tests presence rather than truthiness.
- `user.yml` is the evidence because it is the override layer. The merged `config.yml` always carries a location, so a location there proves nothing, whereas an entry in `user.yml` means someone chose it. `/towers/select` has written it since commit 4afa307 (2026-03-23), three months before the flag, so every node in the gap is covered.
- It is deliberately not a coordinate check against the shipped default location. A node genuinely sited near that default would be refused registration for life, and it would make a config default load-bearing across two repos.
- An existing flag is never rewritten: its timestamp answers "when was setup finished", and a backfill has not finished anything.

The call in `app.py` is wrapped in a broad `try`, because it is the only thing that parses `user.yml` at import time. An unparseable file must not stop the GUI booting, which would be worse than the missing flag.

## Tower cache

`towers-cache.json` holds the wizard's tower-finder search. Auto-Calibrate reuses it as its alternate-tower list: the wizard's search is informed by RF measurement (a better ranking than a plain geography lookup), and reusing it avoids a second live tower-finder call at calibration time. It never expires, because broadcast tower frequencies and locations essentially never change, and re-running the wizard's tower step overwrites it with a fresh search.

The configuration page can add a manually entered tower (`add_tower_to_cache`, which creates the cache with null coordinates if the wizard's location step was never run) or remove one by index (`remove_tower_from_cache`). The wizard also reads the cache on reload to recover the coordinates of the last search.

## Telemetry consent

retina-telemetry refuses to register without `telemetry-consent.json`, and it will never write a default of its own, because a record it invented would claim an owner agreed to something they were never shown. That discipline only holds if the GUI never writes one speculatively either, so `save_telemetry_consent` is called from the agreements step (`POST /set-up/consent`) and nowhere else.

The file holds three records, mirroring the wire's `Agreements` object one-for-one so there is no translation between what the owner saw and what the server is told:

```json
{
  "licence":           {"version": "2026-08-15", "accepted_at": "2026-08-16T09:49:52Z"},
  "publication":       {"version": "2026-08-15", "accepted_at": "2026-08-16T09:49:52Z", "choice": "public"},
  "remote_management": {"version": "2026-08-15", "accepted_at": "2026-08-16T09:49:52Z"}
}
```

- `accepted_at` is timezone-aware UTC on purpose. The wire types it `AwareDatetime`, so a naive timestamp is rejected at the boundary and the node silently never registers. Every other timestamp in `DeviceState` uses a naive `datetime.now()`, which makes copying the surrounding idiom the natural way to break this.
- Re-accepting the same version keeps the original `accepted_at` (read from `licence`). The record answers "when did they agree to this text", and a wizard re-run showing unchanged wording has not produced a new agreement. A changed version is a genuine re-acceptance and re-dates all three.
- Re-running `/set-up` is the re-consent path for nodes already in the field. The consent is posted from the agreements step rather than at completion, so an owner can tick, continue and close the tab without doing the location, tower or docker work.
- retina-telemetry re-reads the file on every state derivation rather than caching it, so a write takes effect within seconds with no restart.

### Consent version and wording

`TELEMETRY_CONSENT_VERSION` identifies the terms the owner was actually shown, so it is possible later to say what a given owner agreed to. It must change whenever that text changes, and "that text" is the whole agreements screen: the two checkbox labels in `templates/setup/_agreements.html` as much as `templates/eula.html`, because the publication disclosure lives in the checkbox wording rather than in the EULA. A version that does not move when the wording does makes the record a lie.

Deferred deliberately: nothing re-prompts an owner whose stored version is older than the current one. Changing the text today leaves existing nodes holding their original record, which is correct but silent.

### Publication choice

`TELEMETRY_PUBLICATION_CHOICE` is `"public"`. Publishing is a condition of participation rather than a choice, so there is no UI for it. Recording `"public"` is honest only because the second checkbox on the agreements step ("Enable cloud services and data publication") discloses that detections and the receiver location are published in a public archive.

The checkbox wording and this constant are a matched pair across a template and a Python file: change the wording and you must bump `TELEMETRY_CONSENT_VERSION`, and if the wording ever stops saying data is public, `"public"` stops being true. The wire also supports `"private"`, so making publication optional later is a UI change and nothing more.

## Contact details

`telemetry-contact.json` records whom to contact about the node. It is written by one route, `/set-up/contact`, used by both the wizard step and the How We Reach You section on the configuration page, so the two cannot drift into storing different shapes. The shape mirrors the wire's `NodeContact`.

- It is kept out of `telemetry-consent.json` on purpose. The consent records are versioned acceptances neither end may invent, and retina-telemetry refuses to register without all three. A mutable optional document sharing that file would let a malformed contact stop a node registering. The wire treats them as separate endpoints too.
- Empty values are dropped rather than written as null, and a document with nothing left in it removes the file. The spec says a node with nothing to report never calls the endpoint, so no file is the honest way to say that. It is what an owner who skipped the step and an owner who cleared every box both mean, which is also why `get_telemetry_contact` returns `{}` rather than `None`: "never given" and "cleared" are the same downstream. Clearing is a real outcome rather than a no-op.

## Node claim

`telemetry-claim.json` holds the address that owns the node. It is not the contact email, however alike the boxes look. The contact email answers "whom do we ring"; the claim answers "who owns this node": the server mails the address a link, and opening it binds the node to the account behind the address. A wrong value mails a stranger a link that hands them somebody's node. So the two live in separate files and nothing copies one into the other. See `ClaimFormConfig` in [config_schema.py](../../src/config_schema.py).

### Every write is an ask

The page has one button (Send link), and pressing it means "send a link to this address". Every write therefore stores `email` and stamps `send_requested_at` (UTC), so one press produces one email whatever state the claim is in.

There used to be a separate Save button, and it was a trap: saving an address the node already held wrote an identical file, so the press could not be seen and nothing was mailed. A released node showed its owner exactly that.

`email` is state and `send_requested_at` is the event, because retina-telemetry needs both to choose its call:

| Situation | Call retina-telemetry makes |
| --- | --- |
| Changed address | `PUT /nodes/claim`, which mails. The stamp beside it counts as answered by it. |
| Same address | `POST /nodes/claim/resend`. The only way out after a declined link, since re-offering an address the node already holds mails nothing. |
| Same address on a node whose owner released it | `PUT` again, because a release clears the address and a resend would have nothing on file to mail. |

Which call to make is deliberately decided only in retina-telemetry. It is the only side that knows where the claim actually stands, and putting the rule in both places is how they drift.

An empty box removes the file. That does not unclaim the node, which only its owner can do from the dashboard.

## Atomic writes

The consent, contact and claim files are written by `_write_json_atomically`: a `.tmp` file, `fsync`, then `os.replace`. A half-written consent file reads as "not accepted" to retina-telemetry, so the node goes quiet and looks like a telemetry bug rather than an interrupted write. The other files in the data dir are written directly.

## Telemetry status document

retina-telemetry binds no ports and nothing pushes to it, so `/data/retina-telemetry/status.json` is the only channel out of it. Its logs are inside a container the owner cannot see, and there is no endpoint to ask. retina-gui is its only reader, and treats the file as read-only.

The shape deliberately mirrors `mender-update.status`: a JSON object carrying its own timestamp, treated as stale past a timeout. It lives in its own module rather than in `DeviceState` because telemetry status is not part of the device state machine.

`TelemetryStatus.read` never raises. It returns `None` when the file is absent, unreadable, malformed or not a JSON object: absent is the ordinary state on a node without the telemetry package, and malformed is indistinguishable from absent to an operator, so neither is worth an alarm. Otherwise it returns:

| Key | Meaning |
| --- | --- |
| `state` | The raw state string from the document, or `None`. |
| `detail` | Prose to show verbatim with its first character uppercased, or `None` for transient states. |
| `node_ref` | The live value, falling back to the cached one. |
| `node_id` | As reported by telemetry, which reads it directly. |
| `stale` | `True` when the document is too old to believe, meaning the container is not running. |
| `last_report` | How long ago it last wrote, in words, or `None` if the timestamp was missing or unreadable. |
| `claim` | Where the claim stands, as `{state, email, undeliverable}`, or `None`. |

### Staleness

retina-telemetry writes the document about every 10 s (`STATUS_INTERVAL_S` in its settings). `STALE_AFTER` is 2 minutes, generous enough that an ordinary restart never trips it.

`written_at` is RFC 3339 in UTC (e.g. `2026-08-16T09:49:52Z`). A naive value is assumed to be UTC, because reading it as local time on a node in another timezone would make a healthy service look hours stale. A document with no parseable `written_at` is stale: retina-telemetry writes it on every write, so its absence means this is not a document we understand.

`last_report` is relative prose ("less than a minute ago", "5 minutes ago", "2 hours ago", "3 days ago") rather than the raw timestamp. The raw value makes the operator do arithmetic to answer the only question it is there for (how long the service has been quiet), and relative phrasing sidesteps timezones, since the node writes UTC and the reader is not necessarily in it.

### Transient states

`TRANSIENT_STATES` (`registering`, `awaiting_config`, `starting`) are normal and pass on their own, so their `detail` is suppressed and a healthy node has nothing to say for itself.

It is deliberately an exclusion list rather than a list of states worth showing. A fault state added to retina-telemetry later would be silently hidden by an allow list, whereas here anything unrecognised surfaces by default. The only states this list ever needs are "normal and transient", a far more stable set than the faults.

`detail` strings arrive as lowercase fragments meant to follow a state word, which read as a typo on a card of their own. `_sentence` uppercases only the first character. Jinja's `capitalize` is not used for this because it lowercases the remainder, turning "Mender" into "mender" and "MAC-based" into "mac-based", and these strings are meant to be shown verbatim.

### Node reference cache

`node_ref` is the owner's public identifier: what they need to find their node on the server's views. The server assigns it, and it arrives only in a registration or heartbeat response, so this document is the sole path by which the node ever learns it.

retina-telemetry holds it in memory only. `State.store_token` persists the token and nothing else, on the grounds that `node_ref` is re-obtainable from the server without an operator. So for up to one heartbeat interval (60 s by default) after that container restarts, the document legitimately carries `node_ref: null` on a healthy registered node.

The GUI therefore keeps a last-known-good copy in `telemetry-node-ref`:

- It is not for noticing rotation. retina-telemetry already does that (`State.apply_levels`), and the document always carries the live value, so the document is the source of truth and the cache is only consulted when the document says null. A non-null value is written to the cache whenever it differs from what is cached.
- It is on disk rather than in memory because the case that matters is a node reboot, where both services come back at once and an in-memory copy would be empty at exactly the moment the owner is looking.
- It never expires. A `node_ref` stays valid indefinitely, and blanking it when telemetry dies would remove the identifier precisely when someone needs to quote it to support.
- Writes are best effort. It is a display convenience, so a failure to write must not stop the page rendering the value it was given.

### Claim state

`claim` is restated on every heartbeat, so it is never more than a beat behind. It is `None` on a node whose telemetry predates the claim (spec 1.4.0) and on one that has not yet had a response carrying it. Both mean the same thing to a reader: nothing to show yet. It is not defaulted to `unclaimed`, because telling an owner nobody owns their node is a claim in itself, and the document has not made it.

## How the GUI presents it

| Surface | What it shows |
| --- | --- |
| Home page Telemetry card (`index.html`) | Absent entirely when `read()` returns `None` (not a fault, so no card rather than an empty one). Otherwise the node reference (or "no identifier yet"), a status pill (Not running when stale, else the state), and either the stale message using `last_report` or `detail` verbatim. |
| Configuration page, Node Claim section (`config.html`) | The stored claim address (`get_telemetry_claim`) alongside what the server has actually done with it (`claim` from the status document). The two disagree until the next heartbeat. When telemetry is not reporting the section says so rather than guessing. Send link is disabled once the node is owned. |
| Configuration page, contact section | `get_telemetry_contact`. |
| Setup wizard (`routes/setup.py`) | Prefills the claim step from the stored address, or, on an owned node (a wizard re-run), from the owner's address in the status document, and then only lets the step be skipped. |
| Fleet `/healthz` (`routes/fleet.py`) | `telemetry_payload`: just `node_ref`, `state` and `stale`. Local file reads only, because peers use `/healthz` to decide whether this node exists, and anything slow would make a busy node look absent. |

The GUI shows retina-telemetry's prose verbatim rather than mapping its states to its own wording, which would be a second vocabulary to keep in step with a file it does not own.
