# OTA updates (Mender)

A node carries two independently versioned things: the owl-os root filesystem and the retina-node Docker Compose stack. Both are delivered as Mender artifacts. retina-gui installs the retina-node stack itself (device-initiated, standalone `mender-update install`) during the setup wizard, and only *observes* owl-os updates, which the Mender daemon applies in managed mode from server-side deployments. After the wizard has completed once, both kinds of update are the server's job and the GUI stops offering them.

## Where it lives

| File | Role |
| --- | --- |
| [mender.py](../../src/mender.py) | `MenderClient` (JWT, installed versions, artifact listing, standalone install), GitHub release lookups, version parsing, dev-mode simulation |
| [routes/mender_routes.py](../../src/routes/mender_routes.py) | `/mender/check`, `/mender/install`, `/mender/check-os`, `/mender/cloud-services` |
| [device_state.py](../../src/device_state.py) | `install.lock`, `mender-update.status`, cloud-services flag, `ensure_cloud_services_enabled` |
| [routes/mode.py](../../src/routes/mode.py) | `enforce_radar_mode` and `_write_mode`, used to recover a failed install |
| [static/setup.js](../../static/setup.js) | Wizard System and Packages steps: polling and success/failure display |

## Two kinds of update

| | retina-node stack | owl-os |
| --- | --- | --- |
| Mender provides key | `data-docker.mender-docker-compose.retina-node.version` | `rootfs-image.owl-os-pi5.version` |
| Who starts it | The GUI (`POST /mender/install`) during the wizard | A server-side Mender deployment |
| How it runs | `mender-update install <url>` then `mender-update commit` (standalone, no reboot) | The `mender-updated` daemon in managed mode |
| Progress source | `install.lock` in the data dir, with a `stage` field | `mender-update.status`, JSON written by owl-os state scripts |
| Status route | `/mender/check` | `/mender/check-os` |

## Installed versions

`MenderClient.get_versions` returns `(owl_os_version, retina_node_version)`.

1. It runs `mender-update show-provides` and reads the two provides keys above. The retina-node value has its `retina-node-` prefix stripped.
2. If the retina-node key is missing, or `mender-update` fails or is absent, it falls back to `get_retina_node_version_from_docker`, which returns the tag of the first running image whose name contains `/blah2:`. This covers the window where `install_from_url` has succeeded but Mender has not yet committed the provides.

On a freshly bootstrapped node only the owl-os version exists. The retina-node version appears after the first stack install.

## Version parsing

Only stable releases are ever offered or compared. Pre-releases are excluded on purpose: a node offered an `-rc` or `-dev` build as "latest" would be updated onto an untested build.

| Function | Accepts | Rejects |
| --- | --- | --- |
| `parse_version` | `retina-node-vX.Y.Z` and `retina-node-vX.Y.Z.W` | rc, dev, beta, anything else |
| `parse_os_version` | `os-vX.Y.Z`, `vX.Y.Z`, `X.Y.Z` | rc, dev, anything else |

GitHub release tags are fed to `parse_version` as `retina-node-<tag>`. `get_latest_owl_os_from_github` additionally skips tags that do not start with `os-v`. Both return tuples, so comparison is numeric (`(0, 10, 0) > (0, 9, 0)`).

## GitHub release lookups

GitHub releases are the source of truth for which version exists. Mender is then asked for the artifact carrying that release name.

| Function | Used by | Cache |
| --- | --- | --- |
| `get_all_stable_versions_from_github` | `/mender/check` | 60 s, per repo, errors included |
| `get_latest_stable_from_github` | `/mender/install` when no version is given | none |
| `get_latest_owl_os_from_github` | `/mender/check-os` | 300 s, per repo, errors included |

The caches exist because the wizard polls both check routes every 5 s while its System and Packages steps are open. Neither step can be skipped on a fresh node, so a failed check keeps retrying rather than letting the user through. Without a cache that polling would exhaust GitHub's unauthenticated limit of 60 requests per hour. Errors are cached too, so a GitHub outage does not turn into one request per poll.

`get_all_stable_versions_from_github` reports `size_bytes` for each version: the size of the `.mender` asset, else the largest asset, else `None`.

## Install flow

`POST /mender/install` (optional body `{"version": "v0.3.11"}`, default is the latest stable release):

1. Refuse with 409 if Auto-Calibrate is running. `calibrator.is_running()` is checked directly, not only through the calibration lock file, because an ADS-B calibration run has no time limit and can outlive that lock file's 20-minute staleness window.
2. `device_state.ensure_cloud_services_enabled`: if cloud services were disabled, restore `mender.conf`, enable and start the Mender services, and wait up to about 60 s for a JWT.
3. Record whether a stack was already running (`already_installed`).
4. Refuse with 409 if `device_state.can_start_install()` says an update or calibration is in progress.
5. Resolve the version, take `install.lock` for `retina-node-<version>`, list the Mender artifacts for that release name and device type, and get a signed download URL for the first one. Any failure here releases the lock.
6. Return `{"success": true}` and do the rest on a background thread:
   1. `mender-update rollback` (best effort) to clear any half-finished previous install.
   2. `MenderClient.install_from_url`: `mender-update install`, then `mender-update commit`. Ten-minute timeout.
   3. On success, set the lock's stage to `starting` and wait up to 120 s for a blah2 container to be running, then persist mode `radar`.
   4. On failure, crash, or no containers within 120 s, run the recovery step below.
   5. Always release `install.lock`.

### The Update Module owns the stack swap

The GUI does not stop the running stack itself. owl-os's fork of the docker-compose Update Module loads the new images first, stops the stack only for the short swap (about 11 s), and holds the stack restart lock throughout, which keeps the SDR watchdog from restarting blah2 mid-swap.

An earlier version ran `docker compose down` before installing and switched the mode to spectrum to silence the watchdog. That made every GUI install a full radar outage for the whole image load. retina-gui ships inside the owl-os image, so it never runs alongside the unforked Update Module.

### Recovery after a failed install

`_run_install._recover` runs when the artifact failed to apply or the containers did not come up. The Update Module's rollback normally restores the previous stack already. As a backstop, if a stack was running before the install, recovery calls `enforce_radar_mode` (idempotent, takes the restart lock) so the node is not left with no radar containers. It always writes mode `radar`.

## Detecting success and failure

### Packages step (retina-node)

The browser polls `/mender/check` every 5 s after `POST /mender/install` succeeds. While `installing` is true it shows the stage (`downloading`, `starting`, else "Installing..."). Once `installing` is false it compares `current_version` with the version it asked for:

| Result | What the user sees |
| --- | --- |
| `current_version` equals the requested version | Success, the wizard advances |
| A different `current_version` | "Update failed. Your previous version ... has been restored and is running." with Try again and Continue without updating |
| No `current_version` | "Install may have failed. Try again." |

`current_version` comes from `get_versions`, so a working previous stack is detected through either Mender provides or the Docker fallback. The last row does not distinguish "Mender install failed" from "install succeeded but the containers never started".

If the page is reloaded mid-install, `/mender/check` reports `installing` with `version` set to the lock's release name, and the page recovers the target version from it by stripping `retina-node-`.

### `/mender/check` responses

| Situation | Response |
| --- | --- |
| Any update in progress | `installing: true`, `stage`, `version`, `started_at`, `reason` |
| A stack is installed and the wizard has completed once | `installing: false`, `current_version` only. Updates are the server's job from here |
| Otherwise (nothing installed, or first wizard run on a node that shipped with a stack) | `latest_version`, `latest_size_bytes`, `current_version` |
| GitHub lookup failed | `{"error": ...}` |

The `stage` comes from `mender-update.status` if present, else the lock's `stage`, else `downloading`. `version` is the lock's release name, or the placeholder `system update` for a server-pushed update (the page does not display it).

### System step (owl-os)

The browser polls `/mender/check-os` every 5 s. `update_available` is true when the latest stable `os-v*` tag is newer than the installed version, or when the installed version is missing or does not parse. While an update is available but not yet running (the server is still creating the deployment) the page shows "Preparing system update..." with no way to skip, because the Packages step is not safe to enter until the OS update has started or turned out to be unnecessary. When `installing` becomes true it shows the Mender state (`downloading`, `installing`, `rebooting`). Once the device has rebooted onto the new image, `update_available` is false and the step completes.

On a wizard re-run the step only displays the current version: OS updates are managed remotely after onboarding.

## Update-in-progress guards

`device_state.is_any_update_in_progress()` is true while `install.lock` is fresh or `mender-update.status` reports an active update (it is auto-cleared after 2 h, as crash recovery). Other features refuse to start while it is true, because an install replaces the containers and manifests they would touch and `mender-update`'s own docker commands are outside the restart lock. See `set_mode` in [sdr-mode.md](sdr-mode.md).

## Cloud services

`/mender/cloud-services` reads and toggles the Mender services. Disabling moves `mender.conf` aside to a backup and stops the services. Toggling is refused while any update is in progress. An install re-enables cloud services automatically (step 2 above).

## Dev mode

With `DEV_MODE`, no Mender or Docker commands run.

- `DEV_VERSIONS` in `mender.py` is the simulated release list, newest first.
- The simulated installed stack version comes from `dev_node_version.txt` in the dev data dir, else the `DEV_NODE_VERSION` environment variable (default `v1.0.0`, which simulates a re-run with a stack already installed). Set it empty to simulate a fresh install.
- A simulated install sleeps 8 s, then writes the new version.
- owl-os is always reported as `2.4.1-dev` with no update available.
