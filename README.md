# retina-gui

Python-based web GUI baked into owl-os and deployed to every Retina node. Served on port 80 at the node's own `ret<node_id>.local`, and at the shared `owl.local`, which every node on the network answers.

## Features

- **Quick links** to deployed services (Passive Radar / blah2, ADS-B Map / tar1090)
- **Radar config editor**: schema-driven UI for updating node configuration
- **Onboarding wizard**: guided setup flow covering OS updates, radar stack install, location, and tower selection
- **Tracker Preview**: live delay/Doppler plot of blah2's detections and confirmed tracks (via [retina-tracker](https://github.com/offworldlabs/retina-tracker)), for verifying tracking against real data
- **SSH key management**: add and remove public keys for local access
- **Cloud services toggle**: enable or disable Mender OTA updates and remote access

## Tech Stack

- Flask (Python), Jinja2 templates
- Bootstrap 5 (CDN), vanilla JS, no build step
- Pydantic for config schema and form generation
- systemd service

## Deployment

Deployed as part of owl-os to `/opt/retina-gui/`. Runs as a systemd service on port 80. Mutable runtime state lives separately under `/data/retina-gui/`.

The wizard's tower map needs a CARTO basemap key, which is deliberately not in this public
repository. It is supplied on the node through an optional environment file; see
[docs/operations/node-files.md](docs/operations/node-files.md#carto-basemap-key).

## Development

```bash
pip install -r requirements.txt
cd src
DEV_MODE=1 PORT=5000 python app.py
```

Visit `http://localhost:5000`. `DEV_MODE=1` keeps state in `dev_data/` instead of `/data` and skips the on-device startup steps. Use `?demo=1` to run the wizard in demo mode without a real device.

## Testing

```bash
pip install pytest
pytest tests/
```

Tests cover routes, device state, install flow, config schema, form generation, Mender client, SSH key validation, tower search, and the tracker-preview capture service.

## Documentation

Developer documentation lives in [docs/](docs/README.md). Start with
[docs/architecture.md](docs/architecture.md) for how the app is put together, then the feature
doc for the area you are changing. Files, paths and environment variables on a node are in
[docs/operations/node-files.md](docs/operations/node-files.md).
