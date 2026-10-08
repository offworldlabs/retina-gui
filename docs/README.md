# retina-gui documentation

Developer documentation for retina-gui: how each feature works, why it is built the way it is,
and what it touches on the node. Start with [architecture.md](architecture.md) for the overall
shape of the app, then the feature doc for the area you are changing.

**Keeping these docs current is part of every change.** See [CLAUDE.md](../CLAUDE.md).

## Contents

| Doc | Covers |
| --- | --- |
| [architecture.md](architecture.md) | App startup, blueprints, sessions and CSRF, service status, the apply service, stack reconcile, the restart lock and its couplings to other repos, home and summary pages |
| [features/auto-calibrate.md](features/auto-calibrate.md) | Auto-Calibrate: the tower, gain and LNA search, overload handling, wedge recovery, the wizard path |
| [features/setup-wizard.md](features/setup-wizard.md) | The onboarding wizard, step by step |
| [features/device-state-and-telemetry.md](features/device-state-and-telemetry.md) | What the node persists about itself, telemetry consent and claim, the telemetry status document |
| [features/remote-access.md](features/remote-access.md) | LAN vs tunnel access, identity and login, Mender Connect, SSH keys |
| [features/fleet-and-naming.md](features/fleet-and-naming.md) | mDNS peer discovery, the fleet bar, node names |
| [features/config-editor.md](features/config-editor.md) | The schema-driven radar config editor |
| [features/ota-updates.md](features/ota-updates.md) | Mender OS and stack updates |
| [features/sdr-mode.md](features/sdr-mode.md) | Radar vs spectrum mode, restarting the SDRplay service |
| [features/towers.md](features/towers.md) | Tower finder integration and tower presets |
| [features/tracker.md](features/tracker.md) | The Tracker Preview page |
| [operations/node-files.md](operations/node-files.md) | Where retina-gui lives on a node, the files it reads and writes, environment variables, the CARTO key |
| [history/](history/README.md) | The original pre-implementation plans, kept as an archive |

## Source file map

Use this to find the doc to update when you change a file.

| Source | Doc |
| --- | --- |
| `src/app.py`, `src/services.py`, `src/apply_service.py`, `src/stack_reconcile.py`, `src/restart_lock.py`, `src/routes/home.py` | [architecture.md](architecture.md) |
| `templates/base.html`, `templates/index.html`, `templates/summary.html`, `templates/eula.html`, `static/flight-sim.js`, `static/common.css` | [architecture.md](architecture.md) |
| `src/calibrator.py`, `src/routes/calibrate.py`, `src/blah2_client.py`, `static/calibrate.js`, `templates/setup/_calibrate.html` | [features/auto-calibrate.md](features/auto-calibrate.md) |
| `src/routes/setup.py`, `static/setup.js`, `templates/setup.html`, `templates/setup/*` | [features/setup-wizard.md](features/setup-wizard.md) |
| `src/device_state.py`, `src/telemetry_status.py` | [features/device-state-and-telemetry.md](features/device-state-and-telemetry.md) |
| `src/remote_access.py`, `src/access_identity.py`, `src/mender_connect.py`, `src/ssh_keys.py`, `src/routes/remote_access.py`, `templates/login.html`, `templates/remote_denied.html` | [features/remote-access.md](features/remote-access.md) |
| `src/mdns_peers.py`, `src/routes/fleet.py`, `src/node_name.py`, `templates/_fleet_bar.html` | [features/fleet-and-naming.md](features/fleet-and-naming.md) |
| `src/config_schema.py`, `src/config_manager.py`, `src/form_utils.py`, `src/routes/config.py`, `templates/config.html` | [features/config-editor.md](features/config-editor.md) |
| `src/mender.py`, `src/routes/mender_routes.py` | [features/ota-updates.md](features/ota-updates.md) |
| `src/routes/sdr.py` | [features/sdr-mode.md](features/sdr-mode.md) |
| `src/network_manager.py`, `src/routes/network.py` | No doc yet: the prose is short and stays in code. Write `features/network.md` if it grows. |
| `src/routes/towers.py` | [features/towers.md](features/towers.md) |
| `src/routes/tracker.py`, `src/retina_tracker_client.py`, `templates/tracker.html` | [features/tracker.md](features/tracker.md) |
| `systemd/*.service`, anything under `/data/retina-gui` | [operations/node-files.md](operations/node-files.md) |

## Conventions

- Docs name code by file and function, never by line number.
- Code points into docs with `See docs/<path>.md#<heading-anchor>`. Renaming a heading means
  updating those pointers.
- The repo is public: no real node names, IPs, sites, people or credentials. See [CLAUDE.md](../CLAUDE.md).
