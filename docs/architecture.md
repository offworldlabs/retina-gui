# Architecture

retina-gui is a single Flask process that serves a node's web interface on port 80. It owns
the node's configuration UI and setup wizard, and it drives the retina-node Docker Compose
stack (config-merger, blah2, the tracker and friends) whenever a change needs a restart. This
doc covers the shape of the app: how it starts, how its shared services are built, how
requests are gated, and the machinery that keeps stack restarts from colliding.

## Where it lives

| File | Role |
| --- | --- |
| [src/app.py](../src/app.py) | Flask app, cookie and CSRF setup, startup repairs, blueprint registration, request hooks |
| [src/services.py](../src/services.py) | Configuration (paths, URLs, env vars) and every shared service singleton |
| [src/apply_service.py](../src/apply_service.py) | `ApplyService`: runs config apply (config-merger plus stack restart) on a background thread |
| [src/stack_reconcile.py](../src/stack_reconcile.py) | Repairs a compose project left half-recreated |
| [src/restart_lock.py](../src/restart_lock.py) | Cross-process `flock` mutex around anything that drives the stack |
| [src/routes/home.py](../src/routes/home.py) | `/` (home page) and `/eula` |
| [src/routes/sdr.py](../src/routes/sdr.py) | `run_config_merger_and_restart`, the restart body that `ApplyService` runs (see [sdr-mode.md](features/sdr-mode.md)) |
| [templates/base.html](../templates/base.html) | Page shell: fleet bar, node sub-nav, footer, CSRF meta tag |
| [templates/index.html](../templates/index.html) | Home page |
| [templates/summary.html](../templates/summary.html) | Fleet summary page, including the flight-path diagram |
| [templates/eula.html](../templates/eula.html) | Placeholder EULA linked from the wizard |
| [static/flight-sim.js](../static/flight-sim.js) | Makes the summary page's diagram interactive |
| [static/common.css](../static/common.css) | Shared design-system styles for every page |

Where the files live on a node, and what the GUI reads and writes there, is in
[operations/node-files.md](operations/node-files.md).

## One instance per process

`app.py` is executed twice in one process. systemd starts it as `python3 src/app.py`, so its
module body runs under the name `__main__`. Route handlers then do `from app import ...` at
request time, and because `__main__` and `app` are distinct entries in `sys.modules`, Python
imports the same file a second time and runs the whole body again. Anything constructed at
`app.py`'s module level therefore exists twice.

For stateless helpers that is only wasteful. For anything owning a socket, a thread or
in-memory run state it is a bug, and it has bitten before. retina-tracker's sidecar accepted a
single TCP connection at a time, so two `RetinaTrackerClient` instances meant two connections:
one was served and the other sat unread in the kernel's backlog. Which one won was a startup
race. When the calibrator held the losing one, its detection frames and its tracker RESET went
into a socket nobody read. Nothing failed visibly, because confirmed-track events arrive over a
file tail that kept working, so Auto-Calibrate appeared to work while its own feed reached
nothing, and credited tracks it had never observed.

The fix is that every shared object is built in [services.py](../src/services.py). `services`
is only ever reachable under one name, so it executes once however many times `app.py` does.
`app.py` re-exports the names, so `from app import calibrator` still works.

Rules that follow from this:

- Construct new singletons (clients, threads, anything with state) in `services.py`, never in
  `app.py`.
- Anything in `app.py`'s body that has side effects must be safe to run twice. The startup
  repairs below are idempotent for this reason.
- Background threads are started from `app.py`, not `services.py`, and never under pytest
  (see [Startup sequence](#startup-sequence)).

### Shared services

`services.py` builds these, in dependency order:

| Name | Class | Notes |
| --- | --- | --- |
| `ssh_keys` | `SSHKeyManager` | `DATA_DIR/authorized_keys` |
| `network_mgr` | `NetworkManager` | |
| `config_mgr` | `ConfigManager` | `user.yml`, merged `config.yml`, compose manifests |
| `mender` | `MenderClient` | Server URL, release name and device type from env |
| `device_state` | `DeviceState` | Lock and flag files under `DATA_DIR` |
| `telemetry_status` | `TelemetryStatus` | Reads retina-telemetry's `status.json` |
| `node_name` | `NodeName` | |
| `remote_access` | `RemoteAccess` | Records what the owner chose for remote access |
| `access_identity` | `AccessIdentity` | Verifies Cloudflare Access assertions |
| `mender_connect` | `MenderConnect` | Makes the owner's shell choice true in `mender-connect.conf` |
| `peers` | from `peer_directory_from_env` | mDNS peer directory; owns a browse and a probe thread |
| `apply_service` | `ApplyService` | Guarded by `config_change_guard` |
| `blah2_client` | `Blah2Client` | |
| `retina_tracker_client` | `RetinaTrackerClient` | One per sidecar, shared by every consumer of its events |
| `calibrator` | `Calibrator` | Given `config_mgr` and `apply_service` for its preflight recovery |

`RemoteAccess` and `MenderConnect` are separate on purpose: one records what the owner chose,
the other enforces it, and keeping them apart lets the recording be tested without a systemd
on the other end. `AccessIdentity` gets its config (team domain and this node's application
audience) from node-infra beside the tunnel token. Until that arrives it reports "not
configured" and refuses everything, which is the safe state while it is unwired. See
[remote-access.md](features/remote-access.md).

`calibrator` refers to `apply_service` and `apply_service`'s guard refers to `calibrator`. The
cycle is fine because the guard resolves `calibrator` when it runs, not when it is defined.
`calibrator.on_complete` releases the calibration lock when a run reaches a terminal state.

`read_node_id()` in `services.py` returns the Mender node_id, or `Unknown`. It is also the
node's mDNS host name (owl-mdns-identity derives `ret<node_id>.local` from the same value).
`app.get_node_id()` wraps it with logging and needs the Flask app, so code off the request
path should use `read_node_id()`.

The environment variables `services.py` reads are listed in
[node-files.md](operations/node-files.md#environment-variables).

## Startup sequence

Everything below runs at import time in `app.py`, before Flask serves anything. Each step is
wrapped so a failure is logged (or ignored) rather than stopping the GUI booting: a node that
will not serve its own config page is worse than any of the problems these steps repair.

| Step | What and why |
| --- | --- |
| `secret_key()` | Loads or creates the persisted signing key. See [Session cookies](#session-cookies). |
| `device_state.apply_startup_preferences()` | Re-applies the cloud services (Mender) preference, which an OTA can undo. Skipped in dev mode. |
| `_reapply_shell_agreement()` | Makes `mender-connect.conf` match the owner's recorded shell choice. See below. Skipped in dev mode. |
| `remote_access.publish_marker()` | Writes the support-access inventory marker. See below. Skipped in dev mode. |
| Remove `DATA_DIR/mode.txt` | The node always boots into radar mode. |
| `device_state.release_calibration_lock()` | A calibration run cannot survive a GUI restart, so any lock left behind is stale. |
| `device_state.backfill_setup_wizard_completed(...)` | Writes the setup-completed flag for nodes that finished setup before it existed. |
| Stop and remove `retina-spectrum` | Radar mode at the Docker level too. Only if retina-node is installed. |
| `find_stale_containers()` / `reconcile()` | Repairs a recreate this process was killed in the middle of. See [Stack reconcile](#stack-reconcile). |
| `systemctl stop sdrconnect.service` | Never leave a node serving SDRconnect after a GUI restart. |
| `peers.start()` | Starts the mDNS browse and probe threads. Skipped under pytest. |

### Shell agreement

The owner's remote shell choice lives in `/data`, which survives an OS update. The enforcement
lives in `/etc/mender/mender-connect.conf` on the A/B rootfs, which does not: an update
replaces it with the owl-os template, where Terminal and PortForward are both enabled. Before
`_reapply_shell_agreement` existed nothing re-applied it, so an owner who declined a shell had
it restored by the next update while the marker, the config page and the `remote_shell`
inventory attribute all still said declined. Recorded and enforced state diverged silently, in
the permissive direction.

So the marker in `/data` is authoritative, and startup enforces it. It acts only on a mismatch:
the second execution of the module body finds nothing to do, and a node that already agrees is
not rewritten and its daemon restarted on every boot. If `mender_connect.is_shell_enabled()`
returns `None` (config unreadable), it does nothing. Writing a plausible replacement would mean
inventing transfer limits and a shell user, and could widen access rather than restore it.

### Support access marker

node-infra reads only the inventory marker, and a node that has never been touched has no
state file and so no marker. That was consistent while support access defaulted off. Now that
it defaults on, such a node would keep reporting `remote_access=false` until somebody toggled
something, so startup publishes the marker from `is_enabled()`. Best effort, never worth
failing a boot. See [remote-access.md](features/remote-access.md).

### Setup-completed backfill

retina-telemetry will not register a node without the setup-completed flag. Nodes that
finished setup before the flag existed have none, so this runs on every boot: there is nowhere
to record that it has been done, and it is a no-op once the flag exists. It is the only thing
that parses `user.yml` at import time, so it is guarded; an unparseable `user.yml` must not
stop the GUI booting. See [device-state-and-telemetry.md](features/device-state-and-telemetry.md).

### Spectrum and stale containers

retina-spectrum is only allowed while the wizard location step or the config toggle is active,
so startup stops and removes it. This takes the [restart lock](#the-restart-lock) with the
short opportunistic timeout, because it runs before Flask serves anything and a long wait would
delay the whole GUI. If something else holds the lock it is mid-restart and will stop
retina-spectrum itself, so skipping costs nothing.

Inside the same lock it runs `find_stale_containers()`, and `reconcile()` if any are found.
This is cheap when there is nothing to do (one `docker ps`), so it runs on every boot.

## Blueprints

Route modules are imported at the bottom of `app.py`, after the app exists, because they do
`from app import ...`.

| Blueprint | Module | Doc |
| --- | --- | --- |
| `home` | [routes/home.py](../src/routes/home.py) | [Home page](#home-page) |
| `config` | [routes/config.py](../src/routes/config.py) | [config-editor.md](features/config-editor.md) |
| `mender` | [routes/mender_routes.py](../src/routes/mender_routes.py) | [ota-updates.md](features/ota-updates.md) |
| `setup` | [routes/setup.py](../src/routes/setup.py) | [setup-wizard.md](features/setup-wizard.md) |
| `towers` | [routes/towers.py](../src/routes/towers.py) | [towers.md](features/towers.md) |
| `mode` | [routes/sdr.py](../src/routes/sdr.py) | [sdr-mode.md](features/sdr-mode.md) |
| `network` | [routes/network.py](../src/routes/network.py) | [sdr-mode.md](features/sdr-mode.md) |
| `calibrate` | [routes/calibrate.py](../src/routes/calibrate.py) | [auto-calibrate.md](features/auto-calibrate.md) |
| `tracker`, `tracker_legacy` | [routes/tracker.py](../src/routes/tracker.py) | [tracker.md](features/tracker.md) |
| `fleet` | [routes/fleet.py](../src/routes/fleet.py) | [fleet-and-naming.md](features/fleet-and-naming.md), [Summary page](#summary-page) |
| `remote_access` | [routes/remote_access.py](../src/routes/remote_access.py) | [remote-access.md](features/remote-access.md) |

`inject_globals` (a context processor) adds to every template: `node_id`, the owl-os and
retina-node versions, `is_remote` and `pathway` (which way the request arrived, defaulting to
LAN outside a request), and `fleet_nodes` for the banner. The banner is in `base.html`, so the
node list has to come from here rather than per route. It is an in-memory list copied and
sorted, cheap enough per render.

## Request hooks

Flask runs app-level `before_request` handlers in registration order. The order in `app.py`
is:

1. `CSRFProtect`'s own hook (registered when `CSRFProtect(app)` is constructed).
2. `_gate_the_remote_pathways`.
3. `_keep_the_user_with_the_running_calibration`.

The order of 2 and 3 matters. The calibration hook redirects GETs to `/config`. If it ran
first, an unauthenticated visitor arriving during a calibration would be bounced to a page they
are not allowed to see instead of to the login form.

### Pathway gate

`_gate_the_remote_pathways` classifies the request by its hostname (`classify_host`) and stores
the answer in `g.pathway`:

| Pathway | Hostnames | Gate |
| --- | --- | --- |
| LAN | `owl.local`, `ret<node_id>.local`, a bare IP, and a Mender port-forward (arrives as `localhost`) | None. Being on the network is the credential, and Mender has already authenticated and logged whoever opened a forward. |
| OWNER | `ret<node_id>.<REMOTE_ACCESS_DOMAIN>`, over the tunnel | Checked here. Nothing upstream is trusted. |

On the owner pathway:

- If remote access is not enabled, every request gets 404, not 403. The tunnel should not be
  up at all in that state, so the request is a stale DNS record or a guess, and neither is owed
  confirmation that a node answers to this name.
- `/login`, `/static` and `/favicon` are public, so the login page can render.
- A valid Cloudflare Access assertion (`Cf-Access-Jwt-Assertion`) identifies the user. It is
  verified on the node rather than trusted from the header: if the Access application were ever
  deleted or misconfigured, this is the only thing that would notice.
- With no assertion and no `remote_authed` session: a GET is redirected to the login form only
  if a password exists, and otherwise gets `remote_denied.html` with 403. It used to redirect
  unconditionally, which made every verification failure a dead end: no password can be set
  from the support pathway, so the form could only ever refuse, and the person was shown a
  login box instead of the reason. A POST gets a bare 403, so `fetch()` callers get a status
  they can act on rather than an HTML page parsed as JSON. In practice a stale page's POST is
  often refused with 400 by CSRFProtect first, because its token is stale too.
- Some paths (`requires_presence`) are refused with 403 even when authenticated, including to
  support engineers. If an Access session could add an SSH key, support access would become a
  route to shell access and the two owner agreements would no longer be independent.

The full remote access design is in [remote-access.md](features/remote-access.md).

### Calibration hold

While Auto-Calibrate is running, `_keep_the_user_with_the_running_calibration` redirects every
GET to `/config`. A run owns the SDR for up to 15 minutes and blocks every config change, but
it is only visible in the modal on `/config`. Navigating away used to leave it running
invisibly with no way to cancel it short of waiting out the budget or restarting the GUI.

- It keys on `calibrator.is_running()` only, never the lock file. The lock survives a GUI crash
  by design, and a stale one here would lock the user out of the whole interface, including the
  button that clears it. `is_running()` is in memory, so a restart always frees the GUI.
- Only GETs are redirected. POSTs to other routes already refuse with 409 and an explanation.
- It does nothing while the setup wizard is in progress, because the wizard redirects `/config`
  back to itself and the two would bounce forever.
- Allowed prefixes (`_CALIBRATION_ALLOWED_PREFIXES`): `/config` (the page and the status
  endpoints its modal polls), `/calibrate`, `/static`, `/favicon`, and the fleet-scope routes
  `/summary`, `/api/fleet` and `/healthz`. Fleet routes belong to whoever is browsing, who may
  not be the person who started the run. `/healthz` is how every other node decides whether
  this one still exists, so redirecting it would make a calibrating node look unreachable to
  its peers.

## Sessions and CSRF

### Session cookies

`SECRET_KEY` comes from `services.secret_key()`: the `SECRET_KEY` env var if set, otherwise
`DATA_DIR/secret-key`, created with mode 0600 on first run. It used to be `os.urandom(32)` per
process. That was harmless while nothing used the session, but with remote access every GUI
restart and every OTA would sign the owner out mid-session. The key is never logged: anyone who
can read it can forge an owner-pathway session cookie. If `/data` is unwritable the key is used
unpersisted, so sessions do not survive the process but everything else works.

Cookie flags follow the pathway, via `_PathwaySessionInterface`:

| Pathway | Secure | Name |
| --- | --- | --- |
| LAN | No | `session` |
| OWNER (and `SESSION_COOKIE_SECURE` on) | Yes | `__Host-session` |

This used to be one global setting: Secure everywhere, and the `__Host-` name whenever Secure
was on, on the reasoning that the session only mattered on the always-HTTPS remote pathway.
That was badly wrong. A browser silently discards a Secure cookie delivered over plain HTTP,
which is what the LAN is, so the LAN held no session at all. CSRFProtect keeps its token in the
session, so every POST an owner made from their own network was rejected with 400: the config
page, every toggle, the setup wizard. curl hid it completely, because it keeps cookies a
browser refuses.

The `__Host-` prefix is still worth having remotely: every node is a sibling under one
registrable domain, and without it one node could set a domain-wide cookie that shadows
another's. The prefix requires Secure, so the two have to move together. If the pathway cannot
be determined, the interface falls back to the LAN answer, because a node that cannot hold a
session on the pathway owners actually use is the failure this class exists to fix.

`SESSION_COOKIE_SECURE` defaults on, and off in dev mode so `http://localhost` can hold a
session. On, it only permits Secure cookies on the owner pathway rather than forcing them
everywhere. The cookie is also `HttpOnly` and `SameSite=Lax`.

`REMOTE_ACCESS_DOMAIN` is deliberately not the main product domain. A page served from a node
can set a cookie scoped to its parent domain, which the browser would then send to every host
on that domain, including the ingest API. Nodes run in owners' homes on hardware they control,
so that separation matters.

### CSRF tokens

`CSRFProtect` covers every POST. Pages read the token from the `csrf-token` meta tag in
`base.html` and send it as `X-CSRFToken`; plain forms use a hidden `csrf_token` field.

`WTF_CSRF_TIME_LIMIT` is `None`. Flask-WTF's default expires a token an hour after it is
issued, and the setup wizard reads its token once at page load and reuses it for the whole run
(see `postJSON` in `setup.js`). A real run often passes the hour: reading the agreements, an OS
update, around 600 MB of packages, then calibration. Every POST past that point returned 400:
consent, tower selection and `/set-up/complete` failed, and `/set-up/save-step` silently stopped
recording progress, so even a reload resumed at the wrong step. Dropping the wall clock does
not weaken the token: it is still signed with `SECRET_KEY` and bound to the session cookie, so
it expires when the session does.

`handle_csrf_error` answers a rejected token in the caller's format. Flask-WTF's default is an
HTML page, which every `fetch()` in the GUI hands to `r.json()`, so the owner saw
`Failed to save: Unexpected token '<'` on top of a step that looked fine. Now a JSON caller gets
`{"session_expired": true, "error": "..."}` and can tell the user to reload. A browser
navigation (`Accept: text/html`, not JSON) still gets a plain-text 400. With a persisted key
and no time limit, a token is only rejected if the GUI restarted without its persisted key or
the browser dropped the session cookie, and a reload fixes both.

## The apply service

`ApplyService` runs config apply (`run_config_merger_and_restart` in
[routes/sdr.py](../src/routes/sdr.py)) on a background thread and coalesces repeat requests.
Every route that restarts the stack for a config change goes through `apply_service.request()`:
`/config/apply`, `/towers/select`, `/calibrate/apply`, and the home page's Restart services
button (which posts to `/config/apply`). Progress is read from `/config/apply/status`
(`get_status()`).

### Why it is asynchronous

A whole apply measures about 45 s on real hardware, two thirds of it the SDRplay settle window,
a fixed sleep with nothing to show. When it ran synchronously inside the POST, the user saw 45
seconds of a spinner that could not distinguish working from hung, and one success or failure
at the end. Users reasonably concluded it had stalled and clicked Apply again, which started a
second `docker compose` against the same project. That produced container name conflicts
("The container name /tar1090 is already in use"), and the contention pushed the compose step
past its own timeout, producing "Command timed out", which re-enabled the button and invited
another click.

So:

- The work runs on a daemon thread and the route returns immediately. No HTTP timeout, proxy
  timeout or closed tab can interrupt a restart half-way.
- A request while one is running does not start a second run. It sets a re-run flag and
  `queued: true`, and the worker runs exactly one more pass when the current one succeeds.
  config-merger reads `user.yml` when it runs rather than being handed a snapshot, so that one
  extra pass picks up every change saved in the meantime, however many requests arrived. If the
  current pass fails, the queued pass is dropped and the failure is reported.

`ApplyService` only stops the config-apply path queuing work against itself. Serialisation
against other callers (mode switches, the cron watchdog, wizard completion) is the
[restart lock](#the-restart-lock)'s job, taken inside `run_config_merger_and_restart`. The
worker waits up to `BACKGROUND_TIMEOUT_SECONDS` for it, because it is not attached to a request
and can queue behind a long operation rather than make the user click again.

### Status and phases

`get_status()` returns `state` (`idle`, `running`, `done`, `failed`), `phase`, `phase_label`,
`settle_remaining` (seconds, only in `settling`), `error`, `queued`, `started_at` and
`finished_at`. Phases, in order, with the labels the UI shows:

| Phase | Label |
| --- | --- |
| `waiting_for_lock` | Waiting for another restart to finish |
| `merging` | Merging configuration |
| `stopping_spectrum` | Releasing the SDR |
| `stopping_radar` | Stopping the radar |
| `restarting_sdr` | Restarting the SDR service |
| `resetting_sdr` | SDR service stuck, forcing it down |
| `settling` | Waiting for the SDR to settle |
| `recreating` | Restarting radar services |
| `repairing` | Cleaning up after an interrupted restart |

The settle countdown exists because a display that sits silent through the longest phase looks
the same as a hang, which is what caused the double clicks in the first place.

In dev mode `request()` returns `done` immediately without doing anything.

### The calibration guard

`request()` calls a guard first (`config_change_guard` in `services.py`) and raises
`ConfigChangeRefused` if Auto-Calibrate is running (`calibrator.is_running()` or a live
`calibrate.lock`). Both signals are needed: `is_running()` is authoritative for this process,
and the lock file also covers a run left by a crashed GUI.

The check lives in the service rather than the routes because routes are what gets forgotten.
`/api/mode` and `/mender/install` each grew their own calibration guard, but `/config/apply`
and `/towers/select`, added later, did not. On a live node, clicking Apply Changes during a run
recreated every container underneath it; every retune then failed and the run carried on to
report an ordinary-looking "no confirmed track". Guarding `request()` covers routes that exist
and routes that do not yet. It raises rather than returning a refusal, so a caller that forgets
to handle it gets a 500 rather than a 202 claiming work was queued.

`request(bypass_guard=True)` has exactly one caller: Auto-Calibrate's preflight recovery
(`Calibrator._run_recovery_apply`), which restarts the stack to unwedge the radio it is about to
search with. There the calibration is the caller and holds the lock itself. No route should
pass it. See [auto-calibrate.md](features/auto-calibrate.md).

`ApplyService` takes an optional `restart_fn` so tests can pass a fake instead of monkeypatching
`routes.sdr`, which conftest's `importlib.reload(app)` would swap out anyway. `None` resolves the
real function lazily, because `routes.sdr` imports `app`, which constructs this class.

## Stack reconcile

Compose recreates a container by renaming the existing one to `<id-prefix>_<name>`, creating
the replacement under the real name, then removing the old one. Interrupt it between the rename
and the remove and the project is left with a container squatting on a name compose needs:

    Error response from daemon: Error when allocating new name: Conflict.
    The container name "/tar1090" is already in use by container "<id>..."

Nothing clears that on its own, so every later apply fails the same way, and it survives a
reboot. It is the difference between one bad restart and a node that can never accept a config
change again.

Two things interrupt a recreate:

- `subprocess.run`'s timeout, which SIGKILLs the compose CLI while the daemon carries on.
- systemd restarting `retina-gui.service`, which kills the whole control group. Compose runs
  as a child of the Flask process, so a crash, a `systemctl restart` or a redeploy during an
  apply all do this.

[stack_reconcile.py](../src/stack_reconcile.py) provides:

- `find_stale_containers()`: names in the project that still carry the 12-hex-character rename
  prefix. Returns an empty list on any error, since it only decides whether to try a repair.
  It filters by compose's own `com.docker.compose.project` label, not by name pattern alone: a
  bare regex over `docker ps -a` would also match other projects' containers, and this removes
  what it finds.
- `reconcile()`: `docker rm -f` on those, then plain `docker compose up -d --remove-orphans`
  (not `--force-recreate`: a repair should create whatever is missing and leave healthy
  containers alone). `bring_up=False` skips the `up` for callers that must not start the radar
  stack (spectrum or SDRconnect mode, where blah2 is deliberately stopped). Returns
  `(removed, error)` and never raises. Callers must already hold the restart lock.

It runs in two places: `_repair` in [routes/sdr.py](../src/routes/sdr.py), after a recreate
times out or fails on a name conflict, and at startup, because after a GUI restart the process
that could have cleaned up is gone.

## The restart lock

Every path that runs `docker compose` against the `retina-node` project must hold
[restart_lock.py](../src/restart_lock.py)'s lock for the whole operation. Without it, callers
collide: a `docker compose down` from the cron watchdog landing inside a GUI apply's
`up -d --force-recreate` leaves containers renamed to `<hash>_<name>` (see
[Stack reconcile](#stack-reconcile)).

It is an `fcntl.flock` on `DATA_DIR/restart.lock`, not a timestamp lock like the ones in
`device_state.py`, for two reasons:

- The kernel releases it when the holder dies, so there is no staleness heuristic to get wrong.
  `device_state`'s locks need timeouts (`INSTALL_LOCK_TIMEOUT` and friends) because a crashed
  holder would otherwise wedge them. A restart is short and frequent enough that guessing a
  staleness window would be worse than the problem.
- `flock(1)` makes the same lock available to shell scripts, which is how the blah2-arm
  watchdog takes it (see [Coupling with blah2-arm](#coupling-with-blah2-arm)).

The file's contents (`pid=<n>`) are for humans reading it during an incident. The lock is held
by the kernel, not the content.

### Rules

- **Not re-entrant.** Exactly one place in a call chain takes it. `flock` is per file
  descriptor, so a nested acquire in the same process opens a second descriptor and blocks
  against itself until it times out.
- `restart_lock()` polls with `LOCK_NB` every `POLL_SECONDS` rather than blocking, so the wait
  is bounded without signals or alarms, which are unsafe on a Flask worker thread. It raises
  `RestartBusy` on timeout.
- The timeout default is resolved inside the function, not as a default argument, so the
  module constant stays adjustable at runtime (tests shorten it).
- `is_locked()` is advisory: its answer can be stale the moment it returns. Use it for display,
  never to decide whether it is safe to proceed.

### Callers and timeouts

| Constant | Seconds | Used by |
| --- | --- | --- |
| `DEFAULT_TIMEOUT_SECONDS` | 90 | Callers on a request thread: `set_mode` (`/api/mode`), and `enforce_radar_mode` (wizard completion, and the Mender install recovery path). Also the default for `run_config_merger_and_restart` when no timeout is passed. A whole restart is about 45 s, so this covers one queued operation plus headroom. |
| `BACKGROUND_TIMEOUT_SECONDS` | 600 | The `ApplyService` worker, which is not attached to a request. |
| `OPPORTUNISTIC_TIMEOUT_SECONDS` | 10 | Fire-and-forget callers: GUI startup, and the wizard's navigate-away beacon (`/api/mode/release-spectrum`). Whoever holds the lock is already doing a restart that subsumes what these wanted (both only stop retina-spectrum, which every restart path does anyway), and blocking startup behind a long restart would be worse than skipping. |

### Coupling with blah2-arm

The watchdog in blah2-arm (`script/blah2_rspduo_restart.bash`, run from cron) restarts the
stack when blah2 stops producing data. It shares three things with retina-gui, and nothing
enforces that they stay in step:

| Shared | retina-gui | blah2-arm watchdog |
| --- | --- | --- |
| Restart lock path | `DATA_DIR/restart.lock` (`LOCK_FILENAME` in `restart_lock.py`) | `RESTART_LOCK=/data/retina-gui/restart.lock`, taken with `flock -n` around its `down`/`up` |
| Calibration lock path | `DATA_DIR/calibrate.lock` (`DeviceState.calibrate_lock_file`) | `CALIBRATE_LOCK=/data/retina-gui/calibrate.lock`; the watchdog skips while it is fresh |
| Calibration lock staleness | `CALIBRATE_LOCK_TIMEOUT` = 20 min in `device_state.py` | `CALIBRATE_LOCK_TIMEOUT_SECONDS=1200` |
| Mode file | `DATA_DIR/mode.txt` (`routes/sdr.py`) | `MODE_FILE=/data/retina-gui/mode.txt`; skips in spectrum or SDRconnect mode |

The watchdog used to guard with `pgrep -f "docker compose"`, which could not work: an apply
spends about 30 s in the SDRplay settle window with no compose process running, so the
watchdog saw an idle system and fired `compose down` into the middle of an apply. The flock
replaced it. The watchdog uses `-n` (do not wait) because a restart already in flight will
likely fix what it detected, and cron comes back in a few minutes anyway.

The calibration lock matters because a stack restart under Auto-Calibrate does not just race:
the run applies gain and LNA settings through blah2's live retune API, which a `down`/`up`
reverts to `config.yml`, and the run would carry on attributing readings to settings no longer
in effect. If either staleness timeout changes, change both, or a crashed calibration would
either silence the watchdog indefinitely or let it restart under a live run. Changing
`DATA_DIR` on a node also silently decouples the two.

## Home page

`/` ([routes/home.py](../src/routes/home.py), [templates/index.html](../templates/index.html))
redirects to `/set-up` while the setup wizard is in progress. Otherwise it shows:

- The receiver name (`location.rx.name`) as the subtitle, and a "Listening on" row with the
  transmitter name. Both use `or ''` rather than a `.get` default, because an unsited node has
  the key with a null value and would otherwise render "None".
- A setup banner if retina-node is not installed.
- A **Restart services** button (only when retina-node is installed). It posts to
  `/config/apply` and polls `/config/apply/status` every second, showing the phase label and
  the settle countdown. The button stays disabled until the run reaches a terminal state, so it
  cannot be clicked into a second, colliding compose run. See [The apply service](#the-apply-service).
- A Telemetry card, absent (not empty) when the telemetry package is not installed. It shows
  the node identifier (`node_ref`) and state, and the `detail` prose from retina-telemetry
  verbatim: mapping its states to our own wording would be a second vocabulary to keep in step
  with a file we do not own. See [device-state-and-telemetry.md](features/device-state-and-telemetry.md).
- Service cards (Passive Radar, Max-Hold, Controller, ADS-B Map, Tracker), or the spectrum
  iframe or SDRconnect panel in those modes (see [sdr-mode.md](features/sdr-mode.md)).

The page is host-agnostic: `owl.local` and the node's own `ret<node_id>.local` produce the same
page. The shared alias is a way in, and the fleet banner moves you between nodes from there.

`?demo=1` fills in a fake version, tower, receiver and telemetry state for design work.

### Service card links

The service cards point at other ports on the node (blah2's web UI, tar1090). A script builds
each link from the hostname the browser actually used, so the link stays on this node.

Over the support tunnel those ports are not reachable: Cloudflare proxies a fixed set of ports
and these are not among them, and an `http://` link on an `https://` page is blocked as mixed
content anyway. The tunnel instead routes a few paths on the same hostname to those services,
so remotely a card with `data-remote-path` becomes a same-origin link. A card with no remote
path is disabled (`.disabled`, which carries `pointer-events: none`) and labelled "Only on this
node's own network" rather than looking clickable and failing.

## Summary page

`/summary` ([routes/fleet.py](../src/routes/fleet.py),
[templates/summary.html](../templates/summary.html)) is fleet scope, not node scope, so it
blanks the node sub-nav: Home and Config would be about whichever node served the page. It has:

- **Nodes on this network**: one card per discovered node. The whole card is the link, like a
  banner tab. The node ID is always on the card because it is what support asks for; with no
  friendly name it is promoted to the title and the line below says "Not yet named". The
  telemetry row is absent until the node has answered a probe, so "not heard from yet" is not
  drawn as "has no telemetry", and the chip appears only when something is wrong. See
  [fleet-and-naming.md](features/fleet-and-naming.md).
- An **Add another node** card, only when this node is the whole fleet. One card in a grid built
  for several reads as a page that half loaded, and one node is the common case.
- **Resources** and **Help**: fixed outbound links rendered by one `link_card` macro, so the
  two sections cannot drift apart. They are separate from the node grid because those cards
  stand for hardware on this network. The outbound arrow means "this leaves for another page",
  so a `mailto` card does not wear one.
- **How your node sees**: the flight-path diagram below.

### Flight simulator

A cut-down version of the primer's closing simulation. The left panel shows a tower, the node,
the wedge of sky the node hears and a flight path; the right panel plots what the node would
record, range (bistatic excess path) across and Doppler up.

It is progressive. The markup alone is a correct still diagram, with the beam and a default
flight path drawn in. [flight-sim.js](../static/flight-sim.js) only adds the ability to fly it,
so the controls ship `hidden` and the script reveals them. With no script, no pointer events, or
a failed fetch, the section is still a picture rather than an empty box. It is a static file
rather than inline so the browser caches it.

How the numbers are made:

- Range is `d1 + d2 - baseline` scaled by `KM` (0.15 km per SVG unit, baseline about 27 km).
- Doppler uses the real bistatic relation, `-(v . (u1 + u2)) / lambda`, with a fixed 213 MHz
  centre frequency and 170 m/s aircraft speed. It is zero when the heading is perpendicular to
  the sum of the two unit legs, which is not the same as perpendicular to the node. That sum
  has magnitude at most 2, which caps Doppler at `2v/lambda`.
- The scale is chosen so the picture cannot reach its own axes: drawn into the furthest corner
  the scene tops out around 51 km and 242 Hz, inside a plot of 60 km and 300 Hz. A track pinned
  to a bound would say the aircraft stopped changing when really the plot ran out. The clamps in
  the plotting code are a backstop, not a working part.
- Leg lengths are floored so a path drawn straight over the tower or the node does not divide by
  zero and make the track vanish.
- Where the aircraft is outside the beam, the track has a gap. Nothing was heard, so nothing is
  drawn; that break is the point of the beam.

Behaviour:

- It does not autoplay on load. A path drawn by the user plays when the pointer is released.
  When reduced motion is requested, the initial hint says to press Play.
- The loop runs at most 30 fps and stops (not just hides) when the tab is hidden or the section
  scrolls out of view.
- Drawn samples closer than 4 units are dropped; a path is a shape, not a recording of the hand.
- The beam can be steered by dragging the ring or with the arrow keys, and narrowed or widened.

The plot axes carry their real limits, because a plot whose bounds are a mystery teaches the
wrong lesson about where a track sits. The link to the full primer shows only when `PRIMER_URL`
in `routes/fleet.py` is set.

## Page shell

[base.html](../templates/base.html) has two navigation rows: the fleet bar (which node, or the
fleet-wide Summary) and a sub-nav (which page of that node). The sub-nav is blanked by pages not
scoped to one node (Summary) and by the setup wizard, where Home and Config are unreachable until
setup finishes. The footer shows the node ID and versions, and a Sign out button only on the
owner pathway; the LAN has no session to end.

[common.css](../static/common.css) holds the shared design-system styles for every page. Its
comments explain individual rules in place.

[eula.html](../templates/eula.html) is a placeholder, linked from the wizard.
