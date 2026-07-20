# retina-gui

Python-based web GUI baked into owl-os and deployed to every Retina node. Served locally at `owl.local` and `retina.local` on port 80.

## Features

- **Quick links** to deployed services (Passive Radar / blah2, ADS-B Map / tar1090)
- **Radar config editor** — schema-driven UI for updating node configuration
- **Onboarding wizard** — guided setup flow covering OS updates, radar stack install, location, and tower selection
- **Tracker Preview** — live delay/Doppler plot of blah2's detections and confirmed tracks (via [retina-tracker](https://github.com/offworldlabs/retina-tracker)), for verifying tracking against real data
- **SSH key management** — add and remove public keys for local access
- **Cloud services toggle** — enable or disable Mender OTA updates and remote access

## Tech Stack

- Flask (Python), Jinja2 templates
- Bootstrap 5 (CDN), vanilla JS — no build step
- Pydantic for config schema and form generation
- systemd service

## Deployment

Deployed as part of owl-os to `/opt/retina-gui/`. Runs as a systemd service on port 80. Mutable runtime state lives separately under `/data/retina-gui/`.

## Development

```bash
cd src
pip install -r requirements.txt
python app.py
```

Visit `http://localhost:5000`. Use `?demo=1` to run the wizard in demo mode without a real device.

## Testing

```bash
pip install pytest
pytest tests/
```

Tests cover routes, device state, install flow, config schema, form generation, Mender client, SSH key validation, tower search, and the tracker-preview capture service.
