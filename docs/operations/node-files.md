# Files on a node

Where retina-gui lives on a node, how it is run, and every file and environment variable it
reads or writes there. retina-gui ships inside the owl-os image and runs as a systemd unit, not
a container. Its mutable state lives on the `/data` partition, which survives OS updates; the
code itself is on the A/B rootfs, which every OS update replaces.

## Where it lives

| Path | Role |
| --- | --- |
| `/opt/retina-gui/` | The code (this repository), installed by owl-os. On the rootfs. |
| `/data/retina-gui/` | retina-gui's own mutable state (`DATA_DIR`). Survives OS updates. |
| [systemd/retina-gui.service](../../systemd/retina-gui.service) | The unit that runs the GUI |
| [systemd/sdrconnect.service](../../systemd/sdrconnect.service) | The SDRconnect server unit that SDRconnect mode starts and stops |
| [src/services.py](../../src/services.py) | Every path and environment variable default |

## The systemd units

`retina-gui.service` runs `/usr/bin/python3 /opt/retina-gui/src/app.py` with working directory
`/opt/retina-gui/src`, `Restart=always` and `RestartSec=5`. It listens on port 80 (the `PORT`
default) on all addresses. It loads two optional environment files for the CARTO key (see
[CARTO basemap key](#carto-basemap-key)).

Because the GUI runs `docker compose` and `systemctl` as child processes, restarting the unit
kills them too. A restart during a config apply can leave the stack half-recreated, which the GUI
repairs on its next start (see [architecture.md](../architecture.md#stack-reconcile)).

`sdrconnect.service` runs `/opt/sdrconnect/SDRconnect --server` with `Restart=on-failure`. It is
not enabled at boot by this repo. retina-gui starts it for SDRconnect mode and stops it at every
GUI startup (see [sdr-mode.md](../features/sdr-mode.md)).

## Files under /data/retina-gui

All of these are relative to `DATA_DIR`. Several are read by other repos, so their names and
locations are cross-repo contracts: do not rename them without changing the other side.

| File | Written by | Read by | Purpose |
| --- | --- | --- | --- |
| `secret-key` | `services.secret_key()` | retina-gui | Flask signing key, mode 0600. Anyone who can read it can forge an owner session. See [architecture.md](../architecture.md#session-cookies). |
| `restart.lock` | `restart_lock.py` (`flock`) | retina-gui, **blah2-arm watchdog** | Serialises every `docker compose` run against `retina-node`. Contents (`pid=`) are informational. See [architecture.md](../architecture.md#the-restart-lock). |
| `calibrate.lock` | `DeviceState.acquire_calibration_lock` | retina-gui, **blah2-arm watchdog** | JSON `{"started_at": ...}`; stale after 20 minutes. Cleared at every GUI start. |
| `mode.txt` | `routes/mode.py` | retina-gui, **blah2-arm watchdog** | `radar`, `spectrum` or `sdrconnect`. Deleted at every GUI start, so the node boots into radar. |
| `install.lock` | `DeviceState` | retina-gui | A GUI-initiated install is in progress; stale after 40 minutes. |
| `mender-update.status` | owl-os Mender state scripts | retina-gui | A server-pushed update is in progress. |
| `cloud-services-disabled` | `DeviceState` | retina-gui | Empty flag: the owner turned Mender cloud services off. |
| `setup-wizard.json` | `DeviceState` | retina-gui | Wizard progress, for resuming. |
| `setup-wizard-completed` | `DeviceState` | retina-gui, **retina-telemetry** | Setup finished. retina-telemetry will not register a node without it. |
| `telemetry-consent.json` | `DeviceState` | retina-gui, **retina-telemetry** | Consent records. Required for registration. |
| `telemetry-contact.json` | `DeviceState` | retina-gui, **retina-telemetry** | Owner contact details. |
| `telemetry-claim.json` | `DeviceState` | retina-gui, **retina-telemetry** | The address that owns the node. |
| `telemetry-node-ref` | `TelemetryStatus` | retina-gui | Cached node identifier, so the home page can show it while `status.json` is briefly empty after a restart. |
| `towers-cache.json` | `DeviceState` | retina-gui | Cached tower search results. |
| `authorized_keys` | `SSHKeyManager` | retina-gui, sshd | SSH public keys, mode 0644 so sshd can read it. |
| `node-name` | `NodeName` | retina-gui, **owl-mdns-identity** | The node's friendly name. |
| `remote-access.json` | `RemoteAccess` | retina-gui | Remote access state. |
| `remote-access-enabled` | `RemoteAccess` | **owl-os inventory script** | Marker read into the `remote_access` Mender inventory attribute. |
| `remote-shell-disabled` | `RemoteAccess` | retina-gui, **owl-os inventory script** | Present only when the owner declined a remote shell. |
| `carto.env` | By hand | systemd | This node's own CARTO key. See [CARTO basemap key](#carto-basemap-key). |

The feature docs explain each group: [device-state-and-telemetry.md](../features/device-state-and-telemetry.md),
[remote-access.md](../features/remote-access.md), [fleet-and-naming.md](../features/fleet-and-naming.md),
[towers.md](../features/towers.md), [ota-updates.md](../features/ota-updates.md).

## Other paths

| Path | Access | Purpose |
| --- | --- | --- |
| `/data/retina-node/config/user.yml` | read/write | The owner's config overrides (`USER_CONFIG_PATH`) |
| `/data/retina-node/config/config.yml` | read | The merged config config-merger produces (`MERGED_CONFIG_PATH`) |
| `/data/mender-docker-compose/current/manifests/` | read, `docker compose` cwd | The retina-node compose project (`RETINA_NODE_PATH`). retina-node counts as installed when `docker-compose.yaml` is here. |
| `/data/mender/node_id` | read | The Mender node ID (`NODE_ID_FILE`), also the mDNS host name |
| `/data/mender/mender.conf` | read/write/move | Mender client config, moved aside when cloud services are disabled |
| `/data/mender-cloud-disabled/mender.conf` | read/write | Where `mender.conf` is kept while cloud services are disabled |
| `/etc/mender/mender-connect.conf` | read/write | Remote shell enforcement (`MENDER_CONNECT_CONF`). On the rootfs, so an OS update resets it and startup re-applies the owner's choice. |
| `/data/cloudflared/tunnel-token` | read (size only) | A non-empty file means node-infra has delivered a tunnel token |
| `/data/cloudflared/access.json` | read | Cloudflare Access team domain and audience (`ACCESS_CONFIG_PATH`), delivered by node-infra |
| `/data/retina-telemetry/status.json` | read only | retina-telemetry's status (`TELEMETRY_STATUS_PATH`). That service binds no ports, so this file is the only way its state reaches the GUI. |
| `/data/retina-node/retina-tracker/output/events.jsonl` | read (tail) | retina-tracker's track events (`RETINA_TRACKER_EVENTS_PATH`) |
| `/usr/local/sbin/owl-mdns-identity` | execute | owl-os script that re-advertises the node's mDNS identity |
| `/etc/retina-gui/carto.env` | systemd | The fleet's CARTO key. See below. |

Local services the GUI calls over HTTP:

| URL | Env var | Service |
| --- | --- | --- |
| `http://localhost:3000` | `BLAH2_API_URL` | blah2_api, directly. It runs with `network_mode: host`. This is not the `:8080` blah2_host nginx proxy, which does not forward `/capture/*`. |
| `http://localhost:30101` | `RETINA_TRACKER_CONTROL_URL` | retina-tracker's control surface. Its ingest socket is not used here: blah2_api forwards detections to it directly, and it accepts one connection at a time. |
| `http://localhost:3020` | `RETINA_SPECTRUM_URL` | retina-spectrum, in spectrum mode |

Track events are tailed from the file rather than read over TCP because retina-tracker's
`--tcp` mode is input only (see [tracker.md](../features/tracker.md)).

## Environment variables

Defaults are in [services.py](../../src/services.py) unless noted. In dev mode the data paths
default to `dev_data/` in the repo instead of `/data`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DEV_MODE` | off | `1`, `true` or `yes`: local development, no real device. Skips startup repairs, uses `dev_data/`, relaxes cookie security. |
| `DATA_DIR` | `/data/retina-gui` | retina-gui's state directory. The blah2-arm watchdog hard-codes `/data/retina-gui`, so changing this on a node decouples the two. |
| `USER_CONFIG_PATH` | `/data/retina-node/config/user.yml` | |
| `MERGED_CONFIG_PATH` | `/data/retina-node/config/config.yml` | |
| `RETINA_NODE_PATH` | `/data/mender-docker-compose/current/manifests` | Compose project directory |
| `NODE_ID_FILE` | `/data/mender/node_id` | |
| `TOWER_FINDER_URL` | `https://tower-finder.retina.fm` | |
| `CARTO_API_KEY` | empty | See [CARTO basemap key](#carto-basemap-key) |
| `BLAH2_API_URL` | `http://localhost:3000` | |
| `RETINA_TRACKER_CONTROL_URL` | `http://localhost:30101` | |
| `RETINA_TRACKER_EVENTS_PATH` | `/data/retina-node/retina-tracker/output/events.jsonl` | |
| `RETINA_SPECTRUM_URL` | `http://localhost:3020` | |
| `TELEMETRY_STATUS_PATH` | `/data/retina-telemetry/status.json` | |
| `REMOTE_ACCESS_DOMAIN` | set in `services.py` | The zone the tunnel publishes nodes under. Deliberately separate from the main product domain (see [architecture.md](../architecture.md#session-cookies)). |
| `MENDER_SERVER_URL` | `https://hosted.mender.io` | |
| `MENDER_RELEASE_NAME` | `retina-node` | |
| `MENDER_DEVICE_TYPE` | `pi5-v3-arm64` | |
| `ACCESS_CONFIG_PATH` | `/data/cloudflared/access.json` | |
| `MENDER_CONNECT_CONF` | `/etc/mender/mender-connect.conf` | |
| `SECRET_KEY` | unset | Overrides the persisted `secret-key` file |
| `SESSION_COOKIE_SECURE` | on (off in dev) | Set in `app.py`. Permits Secure cookies on the owner pathway. |
| `PORT` | `80` | Set in `app.py` |
| `FLASK_DEBUG` | `false` | Set in `app.py` |
| `MDNS_PEERS_FIXTURE` | unset | Dev mode only: a fixture file of fake peers (`mdns_peers.py`) |
| `DEV_NODE_VERSION` | `v1.0.0` | Dev mode only: the retina-node version to pretend is installed (`mender.py`) |

## CARTO basemap key

The setup wizard's tower map draws CARTO tiles. CARTO does not refuse an unkeyed request: it
answers `200` with every tile stamped "API KEY REQUIRED". The query parameter is `key`, and any
other name is accepted and ignored, so `?api_key=` looks exactly like having no key at all.

`CARTO_API_KEY` defaults to empty, and an empty value is a working, watermarked map rather than a
broken one.

### Where the key comes from

`retina-gui.service` reads two files, in this order:

| Path | Whose | Survives an OS update? |
| --- | --- | --- |
| `/etc/retina-gui/carto.env` | the fleet's | No, and that is the point |
| `/data/retina-gui/carto.env` | this one node's | Yes |

**The fleet's copy** is written into the OS image by owl-os from a CI secret
(`configuration/retina-gui/`, owl-os#63). `/etc` is on the rootfs, which every A/B update
replaces wholesale, so it reaches existing nodes and rotates with each release. `/data` is the
opposite: it survives updates, so a file the image wrote there would only ever reach a freshly
flashed node.

**One node's own copy** is for a node that needs a different key, or for testing before a
release:

```bash
printf 'CARTO_API_KEY=%s\n' '<key from the CARTO dashboard>' > /data/retina-gui/carto.env
chmod 600 /data/retina-gui/carto.env
systemctl restart retina-gui
```

systemd applies the files in sequence and a later assignment wins, so `/data` is listed second
and a hand-set key is not overwritten by the next OS update.

Both `EnvironmentFile=` lines are prefixed with `-`, which makes them optional. A node with
neither still boots and its tower map comes back watermarked. A missing key must never be the
difference between a GUI that starts and one that does not, so keep the `-`.

For local development, export it instead:

```bash
export CARTO_API_KEY=<key>
```

### Why it is not in the repository

This repository is public, and a key in its history outlives every rotation. retina-server
follows the same rule for its copy of the same key, adding it at deploy time from outside its
repository.

The value is public in the sense that matters for scoping: tile requests come from the browser,
so there is no server in the path that could hold a secret, and anyone who loads the page can
read it. tower-finder bakes the same value into its shipped bundle for the same reason. Only
ever use a tile-scoped key here.
