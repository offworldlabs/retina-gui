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
| [templates/setup/_agreements.html](../../templates/setup/_agreements.html) | Agreements step (EULA, Privacy Policy, cloud services / publication) |
| [static/legal/](../../static/legal/) | The EULA and Privacy Policy PDFs the agreements step links to and offers for download |
| [templates/setup/_contact.html](../../templates/setup/_contact.html) | Contact details and support step |
| [templates/setup/_system.html](../../templates/setup/_system.html) | OWL-OS update step |
| [templates/setup/_packages.html](../../templates/setup/_packages.html) | RETINA package install step (`data-step="radar"`) |
| [templates/setup/_while_you_wait.html](../../templates/setup/_while_you_wait.html) | `while_you_wait` macro shared by the two install steps |
| [templates/setup/_location.html](../../templates/setup/_location.html) | Receiver location step |
| [templates/setup/_towers.html](../../templates/setup/_towers.html) | Tower selection step |
| [templates/setup/_calibrate.html](../../templates/setup/_calibrate.html) | Auto-Calibrate step markup |
| [templates/setup/_name.html](../../templates/setup/_name.html) | Name your node step |
| [templates/setup/_claim.html](../../templates/setup/_claim.html) | Node claim step |
| [templates/setup/_complete.html](../../templates/setup/_complete.html) | Final step |
| [static/setup.js](../../static/setup.js) | `initSetupWizard`: step engine, enter/leave hooks for every step, tower presentation helpers |
| [static/calibrate.js](../../static/calibrate.js) | `window.RetinaCalibrate`, shared with the Configuration page, used by the calibrate step |
| [src/device_state.py](../../src/device_state.py) | Wizard step file, completion flag, towers cache, consent/contact/claim records |

Related docs: [auto-calibrate.md](auto-calibrate.md), [device-state-and-telemetry.md](device-state-and-telemetry.md),
[towers.md](towers.md), [ota-updates.md](ota-updates.md), [remote-access.md](remote-access.md),
[tutorial.md](tutorial.md).

## Step order

Step order is the include order in `templates/setup.html`. `setup.js` builds its step list from
the `[data-step]` elements in document order, so reordering the includes reorders the wizard.

| # | `data-step` | Title | Skippable | What it writes |
| --- | --- | --- | --- | --- |
| 1 | `agreements` | Agreements | No | Consent records, then enables cloud services |
| 2 | `contact` | How can we reach you? | Yes | Contact record, or nothing on Skip; the two support switches write themselves |
| 3 | `system` | Update OWL-OS | No (waits for the update) | Nothing; reports a server-pushed update |
| 4 | `radar` | RETINA packages | No on first run | Installs retina-node via `/mender/install` |
| 5 | `location` | Where is your receiver? | Yes (skips towers too) | Nothing directly; switches the SDR to spectrum mode |
| 6 | `towers` | Choose a tower | Yes | Receiver and transmitter location, frequency (`/towers/select`) |
| 7 | `calibrate` | Tune the receiver | Yes | Gain settings, persisted automatically |
| 8 | `name` | Name your node | Yes | The node's name (`/node-name`), or nothing on Skip |
| 9 | `account` | Connect your node to your account | Yes | Claim record, or nothing on Skip |
| 10 | `complete` | You're all set | n/a | Completion flag; the receiver name; forces radar mode |

The order is deliberate. The owner answers about themselves first (agreements, contact,
support), while the node is still a box on a desk; then the node is brought up to date, which
is as early as it can be because the OS update arrives through the cloud services the
agreements step enables; then it is sited, tuned and named; and connecting it to an account is
the very last thing, so the claim link binds a node that is already set up and the owner can
see on the claim step which account and which site it will appear under.

The step comments in the templates and in `setup.js` name steps rather than number them,
because the numbering changed each time a step was added. `tests/test_setup_flow.py` asserts
the order.

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
| Current step and `started_at` | `setup-wizard.json` in the data dir | `/set-up/save-step` on every step entry | `/set-up` (resume, through `resume_point`), home and config redirects |
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
altitude is rehydrated as 0. The location step reads the same cached position into its empty
boxes on a reload (and refreshes the altitude from it), so an owner who reloads there finds the
map and pin where they left them rather than blank boxes.

### Prefilled answers

`/set-up` also prefills the contact, support, name and claim steps. Contact comes from the
stored contact record, and an empty record renders as empty boxes, which is the ordinary case.
The support switches come from `remote_access.status()` and the name from `node_name.get()`. Claim comes from the stored claim address, except
when `status.json` from retina-telemetry reports the node as `owned`: then the owner's address
from that status is shown instead and the step can only be skipped (see
[Node claim](#node-claim)).

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

Two sections. **Agreements** has one checkbox per document: the End User Licence Agreement and
the Privacy Policy, each with a "Read" link (`/eula`, `/privacy-policy`, which redirect to the
PDF so it opens in a new tab) and a "Download PDF" link (the same routes with `?download=1`,
which send the file as an attachment named `RETINA-EULA.pdf` / `Offworld-Labs-Privacy-Policy.pdf`
through `Content-Disposition`, because a link's `download` attribute is advice that some
browsers ignore). The documents themselves are the PDFs under `static/legal/`; replacing a document
means replacing the file and bumping `TELEMETRY_CONSENT_VERSION`. The files ship in their own
change, so a build can carry this step before them: when a file is not there, both of its links
render `legal_pending.html`, a page saying the document is still being finalised and will come
with an OWL-OS update, rather than a 404 (`_legal` in `routes/home.py` checks for the file
first). **Cloud Services** has the
third checkbox, "Enable cloud services and data publication", with the publication disclosure
under it. Continue is enabled only when all three are ticked. On Continue the page posts
`/set-up/consent`, then `/mender/cloud-services` with `enabled: true`, then advances. On failure
the button is restored.

- One consent record covers both documents. `save_telemetry_consent` writes the shape the wire
  expects (`licence`, `remote_management`, `publication`), and `TELEMETRY_CONSENT_VERSION` names
  the whole screen the owner saw: both PDFs and the checkbox wording. The privacy policy has no
  record of its own because the wire has no field for one.
- The cloud services box also says that updates include this setup flow, because the next step
  that matters, the OS update, is what brings the wizard itself up to date (see
  [System update](#system-update)).

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
  of what `TELEMETRY_CONSENT_VERSION` in `device_state.py` identifies, as are the two PDFs.
  Changing any of them must bump that constant. Recording publication as `"public"` is only
  honest while the text says so. See
  [device-state-and-telemetry.md](device-state-and-telemetry.md).
- Demo mode pre-ticks all three boxes and skips the consent write. A write there would record an
  acceptance nobody gave, which is the one thing the versioned-record design exists to prevent.

## Contact details

Two sections on one step: **Contact** and **Support** (the latter below). Contact is an optional
name, email, phone and a two-letter phone country, placed straight after the agreements so it is
asked while the owner is still answering questions about themselves rather than about the radar.
Nothing downstream blocks on it.

- **Save and continue** posts to `/set-up/contact`, the same route the "How We Reach You"
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

## Remote support

The second section of the contact step: the two Remote Support switches from the Configuration
page, with the same wording. "Let support open this node's interface" posts
`/remote-access/toggle`; "Let support open a command line" posts `/remote-access/shell`. Each
switch posts the moment it is flipped (form-encoded, as those routes read `request.form`),
shows the route's answer under the step, and reverts on failure, exactly as `config.html`'s
section does. They are independent of Save and Skip: both buttons belong to the contact
details, and a flipped switch has already been recorded. The initial state comes from
`remote_access.status()`, so a re-run shows what the node is set to.

The shell switch keeps the enforce-then-record order that
[remote-access.md](remote-access.md#order-enforce-then-record) describes: an ok answer means
mender-connect was changed, not merely that a wish was noted. The gate that refuses
`/remote-access/*` on the tunnel hostname is unaffected; the wizard runs on the LAN.

## Name your node

Optional, after tuning and before the claim. One box, prefilled with the current name, posting
to `/node-name`, the Configuration page's rename route, so the wizard cannot store a different
shape or apply a different rule; `_WIZARD_ALLOWED_PATHS` in `routes/config.py` lets that one
route through the redirect that otherwise sends every config route back to `/set-up` while the
wizard is in progress. Save with an empty box is the same as Skip: the stored name, if any, is
left alone rather than cleared. The help text says the id stays underneath, because an owner
who has just typed "Garage roof" should not expect to reach the node by it.

The naming step comes after the tower step, which wrote `location.rx.name` while the node was
still unnamed, so at that point the receiver name in `user.yml` is the node id. `/set-up/complete`
refreshes it from `node_name.receiver_name` (the one rule, see
[config-editor.md](config-editor.md#receiver-name)) before the stack restart it was already going
to do. That restart merges the name on its own: its `up` runs `config-merger` first (see
[sdr-mode.md](sdr-mode.md#returning-to-radar)), so Home's subtitle carries the name once the
radar is back rather than after the next Apply. An earlier version ran the merger separately
first; on two test nodes Docker's events showed the merger running twice per completion, and
with the separate run forced to fail the name still reached `config.yml` through the restart
alone, so it was removed. If the restart lock is busy for longer than the restart waits, the
restart is skipped and the lock holder's own restart merges the name, since every path that
brings the stack up uses `up`. Nothing on this step waits on any of it, and nothing can fail
over it. An unsited node (no `location.rx`) is left alone.

Because nothing waits on it, every way it can fall short is logged, or nobody would know: the
page has moved on and completion still answers success. `_refresh_receiver_name` logs an error
when `user.yml` cannot be written, and `enforce_radar_mode` logs a busy lock, an exception, and
a failed `up` with its stderr (a failed merge shows up there, since the stack does not start
without it). The lines go to retina-gui's log, which on a node is its systemd journal
(`journalctl -u retina-gui`). This was found on a test node with the restart lock held for the
watchdog's 130 s: completion answered success after 100 s, the name was in `user.yml` but not
`config.yml`, no container had restarted, and the journal held only the access log.

## Node claim

Asks for an email address to send a claim link to. Opening the link binds the node to the
account behind that address, creating one if needed. The last step before completion: the step
names the Network Map and Dashboard (`DASHBOARD_URL` in `routes/fleet.py`, the same address as
the Resources card and the fleet bar's button) and says that the node will appear there under
the account for the address typed, so the owner knows which account and which site the link
binds the node to before sending it. An owned node's "release it from your dashboard" links
there too.

- Posts to `/set-up/claim`, the same route as the Node Claim section on the Configuration page.
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

This is also where the setup flow itself is brought up to date, and the step says so. retina-gui
ships inside the OWL-OS image, so there is no separate "configuration flow" package to fetch
first: the wizard a node boots with is the one its OS carries, and the OS update is the way to
the current one. It cannot be the first step, because the Mender client that receives the
deployment is among the cloud services the agreements step enables; it is the first step after
the two that only ask the owner about themselves. After the update the node reboots, the wizard
resumes at the recorded step (`system`), the new wizard reports the OS up to date, and the owner
continues through the new flow. A step name the new wizard no longer has resumes at the first
step, since `initSetupWizard` falls back to index 0 when the recorded name matches nothing.

The saved step is only a name, and the build that reads it can be newer than the one that wrote
it. That is not an edge case: the wizard ships inside OWL-OS and updates itself at this step, so
every node flashed before a release starts the wizard on the old build and finishes it on the
new one. node-infra pushes the OS update as soon as the agreements step turns cloud services on,
and on the old build the owner's next two steps were contact and the claim. The old order had
the claim third; it is ninth now. A saved `claim` from the old build matched the moved step by
name and opened at step 9 of 10, with completion the next click, skipping the update, the
packages, siting, tuning and naming. Two things stop that, both in `routes/setup.py`:

- A step that moves gets a new key, so an old saved value cannot match it: the claim panel's
  `data-step` is `account`, not `claim` (`setup.js`'s `stepNames` and hooks use the same key;
  the `/set-up/claim` route and `_claim.html`'s name are unchanged).
- `LEGACY_RESUME_STEPS` maps the retired key to where a node that saved it should resume:
  `claim` to `system`, the first step after the two the owner had already done. `resume_point`
  applies it where the resume step is read. Every other old name still means the same place,
  because the rest of the old order is the new order with steps inserted.

A later rework that moves a step does the same two things. `tests/test_setup_flow.py`
(`TestResumingFromAnOlderBuild`) seeds the old build's file and expects `system`, and checks
the `claim` key is not reused.

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
- Coordinates are written as the finder returned them (it fixes the precision at six decimals,
  about 0.1 m, finer than any geocoder claims) and never rounded here; see
  [Coordinates](#coordinates).
- A postcode-level or locality-level match shows a warning instead of "Matched: ...". These
  coordinates are not only a search input: `/towers/select` writes them to the radar config as
  the receiver position, and a city-centre fix can sit 10 km from the real receiver.
- A ZIP in the typed address that the matched address does not carry is pointed out beside the
  match, with a nudge to check the pin. The finder's second geocoder (OSM Nominatim, which
  answers when the Census geocoder has no match) takes its postcode from the surrounding map
  data, and that is sometimes the neighbouring town's; the coordinates can still be right, and
  the map is where to tell. `zipOf` takes the last five-digit group after dropping a leading
  house number, because a rural house number is often five digits too ("12345 County Road 5")
  and the first such group would be read as the ZIP.
- The help text says a lookup is street-level and points at the map. Both geocoders place a
  house number along its street segment, not on the roof, so a lookup is where to start and the
  pin is where to finish.
- Each lookup gets an `AbortController`, so a late answer cannot write coordinates into a step
  the owner has already left. An aborted request leaves the button label to whichever request
  replaced it.

### Coordinates

The latitude and longitude boxes are text (`inputmode="decimal"`), not `type=number`, and
nothing in the step rounds a value the owner gave:

- A pair pasted from a map ("38.8895, -77.0353", with a comma, semicolon, space or tab
  between) lands in one box and is split across both (`splitPair`). Right-clicking a map
  application gives such a pair at a dozen decimals, which `type=number` would have refused as
  a whole and an earlier `toFixed(6)` would have cut. Only a paste splits at once
  (`inputType === 'insertFromPaste'`); a pair typed by hand splits when the box loses focus
  (`change`). Splitting on every key press cut a typed pair at "38.8977, -7" and sent the rest
  of it into the latitude box, a valid-looking point with no warning.
- `coordValue` accepts a box only when the whole of it is one decimal number (a Unicode minus is
  allowed), so "38.8977abc", or a pair still in one box, is refused with a message rather than
  read as its first number, as `parseFloat` would. `coordsValid` is the one gate: both
  present, in range. Find Towers reads nothing else, so no lookup or map failure can hold the
  step shut, and an out-of-range figure is explained under the boxes rather than silently
  disabling the button.
- A map (`#rxMap`, Leaflet, the same CARTO tiles as the tower step) appears under the boxes
  once there is a valid point, with a draggable pin. Clicking the map or dragging the pin writes
  the boxes at seven decimals (about a centimetre), the one place the step rounds, and only
  because a click has no precision of its own. Typing or pasting moves the pin; a lookup zooms to
  the match. The pin is a `divIcon`, as the tower step's markers are, so nothing is fetched for
  it. Without Leaflet (no internet on the LAN) `ensureMap` does nothing and the boxes work alone.
- The map is created while its panel is hidden on a reload, so the enter hook calls
  `invalidateSize` on every entry (`refreshLocationMap`).
- The geocoder is pinned to the US, and the help text says so. A non-US address usually returns
  "no match", which reads as a typo unless the page has already said otherwise. Worse, a foreign
  place name that collides with a US one returns a confident street-level match in the wrong
  country. That has been measured, so the owner still has to check the matched address.
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

Find Towers stores the coordinates (parsed from the boxes, see [Coordinates](#coordinates)),
altitude and RF measurements in `window._towerSearchParams` and advances. Skip jumps two steps,
past both location and towers, to the calibrate step.

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

Entering the complete step removes the `beforeunload` guard and runs `finishSetup`, which is the
one place the wizard ends. Every other step posts `save-step` from `showStep` and ignores the
result, because a failed save there only costs the resume point. The last step is excluded from
that, because its save is what ends the wizard rather than recording a place in it: `save-step`
with `complete` clears the step file and writes the completion flag.

### Nothing on the last step is clickable before the save lands

`is_setup_wizard_in_progress` is true for any saved step other than `complete`, and both
`home.index` and the config blueprint redirect to `/set-up` while it is, as does
`routes/tutorial.start`. So until that save returns, **every way out of the wizard leads back
into it.**

`stepBtns-complete` therefore starts with both its links hidden and a status line reading
"Finishing setup…". `finishSetup` awaits `save-step {complete}`; on success it reveals **Go to
dashboard** and **Start tutorial**, and on failure it shows the server's reason and a **Try
again** button that calls `finishSetup` again. Nothing is lost by a failure: the step file still
holds the previous step, so a reload resumes there.

This used to be two fire-and-forget posts with their failures swallowed (`.catch(function(){})`)
while both links sat in the page from the moment the step rendered. An owner who clicked Start
tutorial before the save landed — or at all, if it had failed — was sent straight back into the
wizard they had just finished. `tests/test_tutorial.py` asserts both that the links start hidden
and that the complete save is not fire-and-forget.

`/set-up/complete` is still posted without being awaited, after the gate. It writes `radar` to
`mode.txt` before any docker work, so the home page cannot race and see spectrum mode while
`enforce_radar_mode` is still running, then calls `enforce_radar_mode` if retina-node is
installed. That recreates the stack, which is why the calibrate step saves its tuning and waits
for the restart before letting the owner reach this step, and why it is not what the buttons
wait on. It takes tens of seconds, nothing on this step depends on it having finished, and
`enforce_radar_mode` never raises, so there would be nothing to report.

It is sent before the links appear and with `keepalive` (`postJSON`'s third argument). Both
links navigate away, and a navigation cancels an ordinary `fetch` that has not left the browser
yet. Posted plainly at the moment the links appeared, a slow request was cancelled by an
immediate click on a test node that was still in Spectrum: the wizard finished, but the node
stayed in Spectrum with no radar running. The wizard's spectrum release on unload uses
`navigator.sendBeacon` for the same reason. `tests/test_tutorial.py` asserts the `keepalive`.

The step also invites the owner to the guided tutorial, saying it is recommended for anyone
setting up a node for the first time. The tour is covered in [tutorial.md](tutorial.md); in demo
mode the link carries `demo=1` so the tour runs in demo mode too.

## Demo and dev modes

| Mode | Trigger | Behaviour |
| --- | --- | --- |
| Demo | `/set-up?demo=1` | Treated as a re-run. Starts at the first step, pre-ticks the agreements, skips the consent write, prefills location with fixed example coordinates and seeds the tower search. `window.fetch` is wrapped to fake `/mender/cloud-services`, `/mender/check-os`, `/mender/check`, `/mender/install` (an install that finishes after 8 seconds), `/towers/select`, `/api/mode`, `/set-up/save-step`, `/set-up/complete`, `/node-name`, `/remote-access/toggle` and `/remote-access/shell`. Everything else goes to the real server. |
| Dev | `DEV_MODE` environment variable | Starts at the location step instead of the recorded one. |

Demo mode is meant to be safe to show on a live node, which is why it skips the consent write
and also fakes `/node-name` and the
two `/remote-access/*` switches. Requests it does not fake still reach the node, so in demo mode
Save on the contact step, Send on the claim step, the tower search, the address and elevation
lookups, and Optimise reception are all real actions.
