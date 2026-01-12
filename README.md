# retina-gui

Lightweight web GUI for Retina radar nodes.

## Features

- Service links (blah2, tar1090, adsb2dd)
- SSH public key management
- Future: config editing

## Tech Stack

- Flask (Python)
- Bootstrap 5 (CDN)
- systemd service

## Development

See [.plans/001-initial-setup.md](.plans/001-initial-setup.md) for implementation details.

## Deployment

Deployed as part of owl-os image to `/opt/retina-gui/`, runs as systemd service on port 80.
