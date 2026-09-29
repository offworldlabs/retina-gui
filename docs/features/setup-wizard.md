# Setup wizard

The setup wizard at `/set-up` is the full-page, first-boot flow that takes a new node from
accepting the terms to a sited, tuned radar. It is a single page load that shows one step at a
time, runs forwards only, and records the current step on the node so a reload or a reboot
resumes where it left off. Re-running it on a node that has already finished setup is also the
re-consent path, and most steps then become informational.

## Where it lives

| File | Role |
| --- | --- |
| [src/routes/setup.py](../../src/routes/setup.py) | `/set-up` page render, plus the step routes: `save-step`, `consent`, `contact`, `claim`, `complete` |
| [templates/setup.html](../../templates/setup.html) | Page shell: header, progress dots, footer buttons for every step, script bootstrap |
| [templates/setup/_agreements.html](../../templates/setup/_agreements.html) | Agreements step (EULA and cloud services / publication) |
| [templates/setup/_contact.html](../../templates/setup/_contact.html) | Contact details step |
| [templates/setup/_claim.html](../../templates/setup/_claim.html) | Node claim step |
| [templates/setup/_system.html](../../templates/setup/_system.html) | OWL-OS update step |
| [templates/setup/_packages.html](../../templates/setup/_packages.html) | RETINA package install step (`data-step="radar"`) |
| [templates/setup/_while_you_wait.html](../../templates/setup/_while_you_wait.html) | `while_you_wait` macro shared by the two install steps |
| [templates/setup/_location.html](../../templates/setup/_location.html) | Receiver location step |
| [templates/setup/_towers.html](../../templates/setup/_towers.html) | Tower selection step |
| [templates/setup/_calibrate.html](../../templates/setup/_calibrate.html) | Auto-Calibrate step markup |
| [templates/setup/_complete.html](../../templates/setup/_complete.html) | Final step |
| [static/setup.js](../../static/setup.js) | `initSetupWizard`: step engine, enter/leave hooks for every step, tower presentation helpers |
| [static/calibrate.js](../../static/calibrate.js) | `window.RetinaCalibrate`, shared with the Configuration page, used by the calibrate step |
| [src/device_state.py](../../src/device_state.py) | Wizard step file, completion flag, towers cache, consent/contact/claim records |

Related docs: [auto-calibrate.md](auto-calibrate.md), [device-state-and-telemetry.md](device-state-and-telemetry.md),
[towers.md](towers.md), [ota-updates.md](ota-updates.md), [remote-access.md](remote-access.md).

## Step order

Step order is the include order in `templates/setup.html`. `setup.js` builds its step list from
the `[data-step]` elements in document order, so reordering the includes reorders the wizard.

| # | `data-step` | Title | Skippable | What it writes |
| --- | --- | --- | --- | --- |
| 1 | `agreements` | Let's get started | No | Consent records, then enables cloud services |
| 2 | `contact` | How can we reach you? | Yes | Contact record, or nothing on Skip |
| 3 | `claim` | Connect your node to your account | Yes | Claim record, or nothing on Skip |
| 4 | `system` | Update OWL-OS | No (waits for the update) | Nothing; reports a server-pushed update |
| 5 | `radar` | RETINA packages | No on first run | Installs retina-node via `/mender/install` |
| 6 | `location` | Where is your receiver? | Yes (skips towers too) | Nothing directly; switches the SDR to spectrum mode |
| 7 | `towers` | Choose a tower | Yes | Receiver and transmitter location, frequency (`/towers/select`) |
| 8 | `calibrate` | Tune the receiver | Yes | Gain settings, persisted automatically |
| 9 | `complete` | You're all set | n/a | Completion flag; forces radar mode |

The step comments in the templates and in `setup.js` name steps rather than number them,
because the numbering changed each time a step was added.

## Navigation

`showStep(index)` in `setup.js` is the only transition:

1. It runs the current step's leave hook (if any) and disables every footer button until that
   hook's promise settles. The location step's leave hook is asynchronous (it reverts the SDR
   mode), and the transition waits for it whether it succeeds or fails.
2. It clears the shared `pollTimer`, shows the new panel and its `stepBtns-<name>` footer group,
   widens the card on the towers step, and updates the progress dots.
3. It posts the step name to `/set-up/save-step`. This is best effort: a failed save only costs
   the resume point, and `postJSON` already raises the session notice if an expired session is
   the cause.
4. It runs the new step's enter hook.

`advance()` moves to the next step. There is no Back button: the wizard is forward-only, so a
reload is the whole recovery story and every step has to be a usable landing point.

Enter hooks run on every entry. Each hook guards its one-time setup (event listeners, closures)
with `hookInitialized[name]` or a `listenersAdded` flag, and re-derives its visible state
(button labels, disabled state, messages) on every entry. That split is deliberate: an earlier
agreements hook set the button to "Connecting..." imperatively and only the failure path put it
back, so re-entering a step whose consent had already succeeded found a disabled button with
nothing in flight to clear it.

The progress label reads "Step N of M" and dots behind the current step are marked complete.
`#progressFill` is hidden but still required by `updateProgress`.

## Persistence and resume

| State | Where | Written by | Read by |
| --- | --- | --- | --- |
| Current step and `started_at` | `setup-wizard.json` in the data dir | `/set-up/save-step` on every step entry | `/set-up` (resume), home and config redirects |
| Completion flag | `setup-wizard-completed` | `/set-up/save-step` with `complete` | `/set-up` (`is_rerun`), `/mender/check`, `/mender/check-os`, retina-telemetry |
| Last tower search | `towers-cache.json` | `/towers/search` | `/set-up` (rehydration), Auto-Calibrate |
| Consent, contact, claim | See [device-state-and-telemetry.md](device-state-and-telemetry.md) | The step routes below | retina-telemetry, prefills |

The step file holds the resume point, and because the wizard is forward-only it is also the
furthest step reached. `save_setup_wizard_step` keeps the original `started_at`, and
`get_setup_wizard_step` deletes the file once `started_at` is more than 24 hours old
(`SETUP_WIZARD_TIMEOUT`). The timeout is measured from the start of the wizard, not from the last
step entry. When `step` is `complete`, `save_step` clears the step file and writes the completion
flag instead.

While a step is recorded and it is not `complete`, the home page and every `/config` route
redirect to `/set-up` (`/config/apply/status` is exempt, because the tower step polls it). The
home page shows "Resume Setup" or "Start Setup" in its setup banner. The calibration GET lock in
`app.py` stands aside while the wizard is in progress, because the wizard redirects `/config`
back to itself and the two would otherwise bounce the browser forever.

On load, `initSetupWizard` starts at the recorded step. In dev mode it starts at `location`, and
in demo mode at the first step (see [Demo and dev modes](#demo-and-dev-modes)).

### First run or re-run

`is_rerun` comes from the completion flag, not from whether retina-node is installed. A node can
ship with retina-node pre-installed and never have had the wizard run on it, which is still a
first run. On a re-run the system and packages steps only report that updates are managed
remotely (see [System update](#system-update) and [Packages](#packages)). `/mender/check` and
`/mender/check-os` use the same flag to stop offering GitHub versions once setup has completed.

### Rehydrating the tower search

The tower step normally searches with `window._towerSearchParams`, which only the location
step's Find Towers button sets. A reload onto the tower step would therefore have nothing to
search with and, with no Back button, no way to reach the location step again. `/set-up` passes
the towers cache to the template, and `setup.html` sets `_towerSearchParams` from the cached
coordinates before `initSetupWizard` runs. RF measurements are not cached, so the rehydrated
search is location-only, the same fallback used when the spectrum analyser is unavailable. The
altitude is rehydrated as 0.

### Prefilled answers

`/set-up` also prefills the contact and claim steps. Contact comes from the stored contact
record, and an empty record renders as empty boxes, which is the ordinary case. Claim comes from
the stored claim address, except when `status.json` from retina-telemetry reports the node as
`owned`: then the owner's address from that status is shown instead and the step can only be
skipped (see [Node claim](#node-claim)).

## Page chrome

The wizard has its own header, full-height layout and footer, so `setup.html` empties base.html's
`navbar`, `subnav` and `footer` blocks. The fleet bar is included as the first child of `.wiz`
instead. `.wiz` is a 100vh flex column and `.wiz-body` is `flex: 1`, so the bar takes its natural
height and the body shrinks to fit; placed outside `.wiz` it would push a full-height element
down and overflow the viewport. `setup.html` imports the `fleet_bar` macro itself because
base.html's import lives in its own namespace and is not inherited by child templates.

The split is deliberate. Home and Config must not be reachable on a node that is mid-setup, and
the redirects enforce that. Leaving for another node is a different thing: without the fleet bar,
a node part-way through setup becomes a dead end in every other node's banner, and an abandoned
wizard keeps it that way until the 24 hour timeout clears it.

## Session expiry

The wizard reads its CSRF token once at page load and reuses it for the whole run, which can
last hours and survive a reboot (agreements, an OS update, about 600 MB of packages, then
calibration). `app.py` sets `WTF_CSRF_TIME_LIMIT = None` for that reason, so the token lives as
long as the session.

`postJSON` in `setup.js` rejects on any non-2xx status. `fetch()` resolves for every status, so a
caller that only chains `.then()` treats a 400 as a success. An expired CSRF token once reached
the owner as two different bugs: the location step's mode switch "succeeded", so its catch never
ran and the step sat on "Waiting for sweep to start" with Find Towers disabled forever, while
steps that parse the body handed the HTML error page to `r.json()` and reported
`Failed to save: Unexpected token '<'`. Rejecting in one place makes every existing catch block
correct. The rejection carries `status`, the server's `error` message, and per-field `errors`
from a 400, so a step can show the server's reason rather than a connection error.

If the response body has `session_expired`, `showSessionExpired` shows a one-time overlay
("This page timed out") with a Reload button. Reload is the only fix and it resumes at the step
the server last recorded. The overlay removes the `beforeunload` guard first so the owner is not
also challenged with the browser's "leave site?" dialog.

## Leaving the page

One `beforeunload` handler covers the whole wizard lifetime and shows the browser's native
"leave site?" dialog on every step. On the location step it also sends a `sendBeacon` to
`/api/mode/release-spectrum`, so retina-spectrum stops even if the owner confirms and leaves. The
handler is removed on the complete step and when the session-expired overlay appears.

## Agreements

Two checkboxes: the EULA and "Enable cloud services and data publication". Continue is enabled
only when both are ticked. On Continue the page posts `/set-up/consent`, then
`/mender/cloud-services` with `enabled: true`, then advances. On failure the button is restored.

- Consent is recorded before the Mender toggle because it is the half that unblocks telemetry:
  if cloud services fail, the owner has still accepted and retina-telemetry can register.
- Consent is posted from this step rather than at completion. Every node already in the field
  has completed the wizard and will never see it again, so re-running `/set-up` is the
  re-consent path, and writing here means an owner can tick, continue and close the tab without
  going through the location and tower steps or the docker work `/set-up/complete` triggers.
- retina-telemetry re-reads the consent file on every state derivation rather than caching it,
  so the change takes effect within seconds with no restart or ordering between containers. The
  same holds for the contact and claim files.
- The publication disclosure is in this template rather than in the EULA, so its wording is part
  of what `TELEMETRY_CONSENT_VERSION` in `device_state.py` identifies. Changing the wording must
  bump that constant. Recording publication as `"public"` is only honest while the text says so.
  See [device-state-and-telemetry.md](device-state-and-telemetry.md).
- Demo mode pre-ticks both boxes and skips the consent write. A write there would record an
  acceptance nobody gave, which is the one thing the versioned-record design exists to prevent.

## Contact details

Optional name, email, phone and a two-letter phone country, placed straight after the agreements
so it is asked while the owner is still answering questions about themselves rather than about
the radar. Nothing downstream blocks on it.

- **Save and continue** posts to `/set-up/contact`, the same route the "How we reach you"
  section on the Configuration page uses, so the two surfaces cannot drift into storing
  different shapes.
- **Skip** writes nothing and moves on. Skipping is an answer, not an abandonment, and anything
  already stored is left alone: an owner skipping past details they gave earlier has not
  withdrawn them. The node-ingest spec says a node with nothing to report never calls the
  contact endpoint, so an absent file is how the node says it has nothing, and an empty
  document would be indistinguishable from an owner who cleared theirs.
- The route blanks whitespace-only values to `None` (`_blank_to_none`), since a space would
  otherwise be stored, sent and shown back as if it were a detail. It checks the country against
  `CONTACT_COUNTRY_PATTERN` itself, because the model cannot carry that check portably, and
  returns the error on the `country` field so the page can mark that box. A valid country is
  uppercased. `ContactFormConfig` then validates the rest.
- The route stores the submitted dict rather than the model's dump, because `.dict()` is
  pydantic v1, `.model_dump()` is v2, and this runs against both. The two carry identical values
  by construction. `stored` in the reply is false when every field was empty (the record is
  then removed).
- The boxes are prefilled server-side, so a re-run shows the owner their details rather than
  empty boxes that would read as "we hold nothing".

## Node claim

Asks for an email address to send a claim link to. Opening the link binds the node to the
account behind that address, creating one if needed.

- Posts to `/set-up/claim`, the same route as the Node claim section on the Configuration page.
- An empty box is filled with the contact email from the previous step as a suggestion only.
  Nothing is sent until **Send link and continue** is pressed.
- Send with an empty box is refused on the page ("Enter an email address, or skip this step"),
  because on this route an empty value clears the stored address, which is not what Send means.
  Skip is the way past with nothing, and writes nothing.
- This is the only box on the page whose value reaches a stranger if it is wrong, so the route
  checks the shape against `CLAIM_EMAIL_PATTERN` and `ClaimFormConfig` rather than leaving the
  server to refuse it thirty seconds later with nothing on screen to explain it. Nothing verifies
  that the address exists, and nothing can.
- Every submitted address is an ask for a link, and there is no way to store an address without
  asking, because a press the node cannot see is one that mails nothing. See
  `device_state.save_telemetry_claim` and [device-state-and-telemetry.md](device-state-and-telemetry.md).
- A node already claimed (retina-telemetry reports `owned`) shows the owner's address read-only,
  has no Send button, and can only be skipped. The Configuration page applies the same rule: a
  new address would be offering somebody else's node. To hand it over, the owner releases it
  from their dashboard first.

## System update

Fully automatic: OS updates are pushed by the server as a deployment and applied by the
managed-mode Mender client, so this page only reports progress. See
[ota-updates.md](ota-updates.md).

On a first run the step polls `/mender/check-os` every 5 seconds:

| Response | Shown |
| --- | --- |
| `installing` | Stage text (connecting, downloading, installing, rebooting), spinner, "Do not power off the device." The response's `version` is a generic placeholder for server-pushed updates, so it is not shown. |
| `error` | "Unable to check: ... retrying". Polling continues rather than letting the owner past an unconfirmed update state. |
| `update_available` | "Preparing system update..." with the target version. After 2 minutes, "Taking longer than expected. Please keep waiting." |
| Otherwise | Up to date: polling stops and Continue appears. This is also what the page sees after the update finishes and the node reboots back in. |

There is deliberately no way past "Preparing": the update is on its way but the deployment has
not started downloading, and the Packages step is not safe to enter until it either starts or
turns out not to be needed.

On a re-run the step makes one `/mender/check-os` call for the current version, says updates are
managed remotely, and shows Continue.

## Packages

Installs retina-node (`data-step="radar"`). On a first run the owner must tick the export
declaration ("This device will not be exported from the USA") before Install is enabled.

- `checkAvailability` asks `/mender/check`. GitHub is the only source of truth for what to
  install on a fresh node and the step cannot be skipped, so a failure retries every 5 seconds
  rather than being a dead end. The backend caches the GitHub call for 60 seconds
  (`_STABLE_RELEASE_CACHE_TTL` in `mender.py`), so the retries stay inside GitHub's
  unauthenticated rate limit.
- If the installed version equals the latest, Continue appears. Otherwise Install appears,
  including on a node that shipped with an older retina-node pre-installed: installing the
  latest is mandatory on first run.
- If an install is already running (for example after a reload), the step resumes polling.
  `latestVersion` was never set by that page load, so it is recovered from the install lock's
  release name (`retina-node-vX`) to keep the success check accurate.
- Install posts `/mender/install`. A 409 carries a reason worth reading ("Auto-calibration is
  running", "Install already in progress"), so the error message is shown rather than a generic
  "try again" for something that cannot succeed yet.
- `startRadarPoll` polls `/mender/check` every 5 seconds. When the install finishes: the latest
  version running advances automatically; a different version running means the install failed
  and the previous version came back, so the page offers "Try again" and "Continue without
  updating" rather than trapping the owner if updates keep failing; no version at all shows
  "Install may have failed. Try again."
- On a re-run the declaration and radio are hidden, the step says package updates are managed
  remotely, and Continue is shown. The version lookup there is cosmetic and never gates Continue.

The package card's size and duration come from `formatSize` ("~600 MB · 5-10 minutes").

## While You Wait checklist

`templates/setup/_while_you_wait.html` defines the `while_you_wait(block_id, duration)` macro used
by the system and packages steps: an optional duration line and a checklist of physical checks
(antenna inputs, cable length, grounding). One copy serves both callers so a reworded bullet
cannot drift between them.

Both blocks ship hidden. `setup.js` reveals them only once the step knows a wait is coming
(`showUpdateWait` from `showStage` and `showPreparing`, `showRadarWait` when an install is
required or running) and hides them again when the node turns out to need nothing, so a node
with nothing to do is never told to set aside half an hour. The packages step passes no
duration: its card already carries one from `formatSize`, and a second figure a few lines below
is the kind of thing that later disagrees with the first.

## Location

Collects receiver coordinates and, in parallel, runs a spectrum sweep so the tower search can be
ranked against what the site actually receives.

### Spectrum scan

On every entry the enter hook reads `/api/mode`, remembers it in `wizardWasMode`, and posts
`/api/mode` with `spectrum`. That is idempotent: a no-op if already in spectrum mode or if
retina-node is not installed yet. Once the switch lands it opens an `EventSource` on
`/towers/spectrum/events`.

- Find Towers is gated (`spectrumGating`) until the first full sweep pass completes, or until the
  mode switch fails, in which case the step says "will search by location only" and ungates.
- On a sweep `start` event the scan begins; `step` events add one measurement per channel
  (frequency, normalised band, score, and SNR and occupied bandwidth for non-pilot channels or
  pilot power for pilot channels); `complete` finishes a pass.
- retina-spectrum averages each sweep step over a ring of passes and exports no channels until
  that ring holds `METRICS_MIN_ENTRIES` (5) of them, about 3 minutes on a freshly started
  container. An empty early pass is normal, so the status text counts passes towards 5 and the
  scan re-arms for the next sweep instead of freezing on an empty profile. Find Towers is ungated
  once any full pass is in: more passes only sharpen the profile.
- The SSE reconnects after 3 seconds on error while the step is active.

The leave hook sets `locationActive = false` (so a late fetch resolving after leave does
nothing), aborts address lookups, closes the SSE, and reverts to the mode recorded on entry if
that was not spectrum. It waits for any in-flight spectrum switch (`pendingModeSwitch`) before
sending the revert, so two docker operations never race each other.

### Address lookup

The address row comes first because it is the only way to fill the coordinates without reading
them off a map. The node is served over plain HTTP, so the browser Geolocation API behind the
"Use my location" button is unavailable; that button is commented out in the template until the
node is served over HTTPS, and its handler in `setup.js` is inert while the button is absent.
The lookup runs server-side through `/towers/geocode` (see [towers.md](towers.md)), which that
constraint does not reach.

- The lookup only fills the coordinate boxes. Find Towers reads only those boxes, so typing
  coordinates by hand works exactly as before and no failure here can block the step.
- It does not use `postJSON`: the route reports "no such address" and "could not ask" as
  different statuses, each with its own sentence, and both are shown as returned.
- Enter in the address box runs the lookup, not the step. Find Towers is a deliberate second
  action once the coordinates look right.
- Coordinates are written to six decimals (about 0.1 m, finer than any geocoder claims).
- A postcode-level or locality-level match shows a warning instead of "Matched: ...". These
  coordinates are not only a search input: `/towers/select` writes them to the radar config as
  the receiver position, and a city-centre fix can sit 10 km from the real receiver.
- The geocoder is pinned to the US, and the help text says so. A non-US address usually returns
  "no match", which reads as a typo unless the page has already said otherwise. Worse, a foreign
  place name that collides with a US one returns a confident street-level match in the wrong
  country. That has been measured, so the owner still has to check the matched address.
- Each lookup gets an `AbortController`, so a late answer cannot write coordinates into a step
  the owner has already left. An aborted request leaves the button label to whichever request
  replaced it.

### Altitude prefill

Nothing used to fill the altitude box, which left `rx_altitude` at 0 in the radar config for
every owner who did not type a figure. `fetchElevation` now fills it from `/towers/elevation`, in
whole metres (a site altitude, not a survey figure).

- An address lookup forces a refresh: it means "the node is here", so the old altitude is stale.
- Typing coordinates by hand also fills it, debounced by 800 ms and only for in-range values,
  because the manual path is the one that always has to work.
- `altAutoFilled` records whether the figure is ours or the owner's. A figure we filled may be
  replaced when the coordinates move; one the owner typed is never replaced by the manual path.
- Failures are silent. Nothing gates on altitude, and an error here would be noise on a step
  that already has a sweep reporting into it.

### Leaving the step

Find Towers stores the coordinates, altitude and RF measurements in `window._towerSearchParams`
and advances. Skip jumps two steps, past both location and towers, to the calibrate step.

## Tower selection

On entry the step tears down any previous map, resets the selection, and posts `/towers/search`
with the stored search parameters. If there are none (no search has ever been cached on this
device, since the page rehydrates from that cache on load), it says so and offers Skip, pointing
at the Config page and at Auto-Calibrate, rather than naming a Back button that does not exist.
Tower search itself is covered in [towers.md](towers.md).

Results render as a summary row, a Leaflet map and a table. Selecting a tower (map marker or
table row) enables Save, shows the selected card and draws the antenna aiming guide.

### Tower presentation

The table columns and their order follow tower-finder's own results table, so a tower reads the
same on both surfaces. Detect Area sits beside the rank because the ranking is built on it. The
`hide-mobile` class is ours: tower-finder renders the table on a desk, the wizard may be on a
phone in a field, and sixteen columns do not fit. What survives on mobile is what a choice is
actually made on.

The helpers at the top of `setup.js` are ports of tower-finder's `frontend/src/utils/rankTier.ts`,
`format.ts` and `basemap.ts`, kept as close to the originals as ES5 allows, because they are the
only place the two surfaces could drift.

- `isNum` is `Number.isFinite` without ES6. The global `isFinite` coerces, so a string would
  pass every guard.
- `rankTier` places a tower in one of five tiers (Best to Worst) by its quintile of rank within
  the returned list. It is derived from rank, not from `expected_area_km2`: the finder ranks with
  diversity folded in (`query.ranking` is `expected_area_mmr`), so a tower with the larger area
  can sit below one with a smaller area, and a tier taken from rank can never contradict the #
  beside it. It is relative to the list so a short list still spreads across the ramp.
- `bearingCardinal` names a 16-point compass direction. The finder sends a cardinal for
  `bearing_deg` but not for `best_azimuth_deg`, so the pointing advice is named here from the
  same table as `bearing_to_cardinal` in tower-finder's `tower_ranking.py`. Keep them in step.
- `formatAreaKm2` renders whole km² with thousands separators, and an absent value as empty.
  These fields are additive and an older finder omits them, which must not read as a measured
  zero.
- `beyondHorizon` marks towers past their own radio horizon. The finder penalises them but still
  returns them, so the row is muted rather than dropped: an owner who can see why a transmitter
  they know is strong ranks low learns more than one who cannot find it at all.

Other rendering details:

- The summary shows towers found, the best tower's detectable area, the bands, and the top pick.
  The detect area tile replaced an "Ideal Range" tile that counted `distance_class === 'Ideal'`
  and read 0 once the finder stopped sending that field.
- The search circle uses the `radius_km` the finder reports (80 km if absent), so it matches the
  area the list covers.
- Markers get `zIndexOffset` from rank. Co-located stations share a mast and a position, Leaflet
  gives them equal z-indexes, and DOM order would otherwise paint the worst-ranked on top.
- Band and rank chips are CSS classes (`.tower-badge.band-*`, `.tower-badge.rank-*` in
  `common.css`). Marker icons take a colour instead, because they are inline styles inside a
  `divIcon`, one of the few places a `var()` still resolves (the same approach as tower-finder's
  `TowerMap`).

### Basemap key

`withCartoKey` appends the CARTO API key to basemap tile URLs. The key is public by nature,
because tile requests come from the browser, so `setup.html` passes it to the page as
`window._cartoApiKey` from `CARTO_API_KEY` in `services.py`.

- It is not optional in practice. CARTO answers an unkeyed request with HTTP 200 and a tile
  stamped "API KEY REQUIRED", so a missing key defaces the map rather than breaking it.
- The parameter must be `key`. Getting it wrong fails silently: an unkeyed tile, one with a bogus
  `key`, and one with `api_key` all come back as the same watermarked PNG.
- It only touches URLs on `basemaps.cartocdn.com`, so swapping a layer to another provider cannot
  start appending the key to somebody else's CDN.

### Antenna aiming guide

A passive radar has two antennas doing different jobs, and they do not point the same way. The
reference antenna (input 1) looks at the illuminator for a clean copy of its transmission. The
surveillance antenna (input 2) looks wherever the detectable area is largest.

`renderAntennaGuide` shows both bearings on a small compass rose with a card for each.
`best_azimuth_deg` is the finder's own answer for the surveillance antenna and is taken exactly
as given, never derived from the tower bearing: against live data the separation between the two
ran from 58 to 180 degrees, so "point it the other way" would be wrong about a third of the time.

- No tower bearing means no guide at all, since a panel of dashes would read as a finding.
- No `best_azimuth_deg` (an older finder) shows a dash and says no azimuth was returned, rather
  than inventing one.
- The footer gives the smallest angle between the two bearings (350 and 10 read as 20 apart) and
  warns when the tower is beyond the radio horizon.
- The surveillance arm is drawn first so the reference arm paints on top where they nearly
  coincide.

### Saving the tower

Save posts `/towers/select` with the receiver position (from the search parameters) and the
tower's position, altitude, callsign and frequency.

- If the reply has a `status`, the restart runs on the server's shared apply queue, and the step
  polls `/config/apply/status` until it finishes before advancing, because later steps expect the
  radar to be back up. Five consecutive poll failures stop the poll with "Configuration saved,
  but progress could not be read" and give back Retry and "Continue anyway". A miss or two is
  normal while the stack restarts under the GUI, but polling forever would leave the step frozen
  with Skip hidden.
- A failed restart shows Retry and "Continue anyway".
- With retina-node not installed yet, the config is saved and the step advances; services start
  when retina-node is installed.

## Auto-Calibrate step

Optional tuning against the tower just chosen. It never blocks completion. The engine and the
shared client are covered in [auto-calibrate.md](auto-calibrate.md); this section covers only the
wizard's use of them.

- The run shape is `RetinaCalibrate.QUICK_RUN`: scope `current_tower` (the owner picked a tower
  one step ago, and re-searching alternates would contradict that and triple the time) plus
  `skip_confirmation` (descend, then soak the resolved point for overload, but never wait for a
  confirmed track). That takes about 4 minutes instead of about 15, and nothing can be falsely
  confirmed because nothing is confirmed. The Configuration page's Quick Calibrate posts the same
  object, so the shape lives in `calibrate.js`. The soak is not optional: descent proves a point
  for one second only, and intermittent clipping shows only when the point is held.
- The hook never auto-starts. It asks the calibrator for its status and reattaches to a running
  run, shows a finished run's result, or shows the start prompt. The run is a background thread
  on the node, not something the tab owns. "Step says calibrate, calibrator says idle" is the
  normal state after a reboot mid-run, and the start prompt is the honest thing to show.
- A result with tuning (confirmed, or the no-track fallback) is persisted without asking, as the
  Configuration page's calibrate window also does: the stack restart at `/set-up/complete` would
  discard anything unsaved seconds later. The step waits for
  the merge and restart to finish before showing Continue, because two overlapping restarts race
  the 90 second restart lock, and `enforce_radar_mode` swallows that failure silently. A save
  failure is reported but still lets the owner continue: the tuning is live either way.
- The result shows what the run changed as well as what it ended on (previous gains and LNA
  state), since the owner has no other way to see that these are not the generic shipped
  settings, and the soak summary shared with Quick Calibrate.
- The leave hook stops the status poll timer. The run continues on the node, and re-entering
  reattaches to it.

## Completion

Entering the complete step removes the `beforeunload` guard and posts `/set-up/complete`. The
step entry itself also posts `save-step` with `complete`, which clears the step file and writes
the completion flag.

`/set-up/complete` writes `radar` to `mode.txt` before any docker work, so the home page cannot
race and see spectrum mode while `enforce_radar_mode` is still running, then calls
`enforce_radar_mode` if retina-node is installed. That recreates the stack, which is why the
calibrate step saves its tuning and waits for the restart before letting the owner reach this
step.

## Demo and dev modes

| Mode | Trigger | Behaviour |
| --- | --- | --- |
| Demo | `/set-up?demo=1` | Treated as a re-run. Starts at the first step, pre-ticks the agreements, skips the consent write, prefills location with fixed example coordinates and seeds the tower search. `window.fetch` is wrapped to fake `/mender/cloud-services`, `/mender/check-os`, `/mender/check`, `/mender/install` (an install that finishes after 8 seconds), `/towers/select`, `/api/mode`, `/set-up/save-step` and `/set-up/complete`. Everything else goes to the real server. |
| Dev | `DEV_MODE` environment variable | Starts at the location step instead of the recorded one. |

Demo mode is meant to be safe to show on a live node, which is why it skips the consent write.
Requests it does not fake still reach the node, so in demo mode Save on the contact step, Send
on the claim step, the tower search, the address and elevation lookups, and Optimise reception
are all real actions.
