# retina-gui

Python-based web GUI baked into owl-os and deployed to every Retina node. Served on port 80 at the node's own `ret<node_id>.local`, and at the shared `owl.local`, which every node on the network answers.

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

### The CARTO basemap key lives on the node, not in this repo

The setup wizard's tower map draws CARTO tiles, and CARTO does not refuse an
unkeyed request: it answers `200` with every tile stamped "API KEY REQUIRED".
The parameter is `key`, and any other name is accepted and ignored, so
`?api_key=` looks exactly like having no key at all.

`systemd/retina-gui.service` reads two files, in this order:

| Path | Whose | Survives an OS update? |
| --- | --- | --- |
| `/etc/retina-gui/carto.env` | the fleet's | No, and that is the point |
| `/data/retina-gui/carto.env` | this one node's | Yes |

**The fleet's copy** is written into the OS image by owl-os from a CI secret
(`configuration/retina-gui/`, owl-os#63). `/etc` is on the rootfs, which every
A/B update replaces wholesale, so it reaches existing nodes and rotates with
each release. `/data` is the opposite: it survives updates, which means a file
the image writes there would only ever reach a freshly flashed node.

**One node's own copy** is for a node that needs a different key, or for
testing before a release:

```bash
printf 'CARTO_API_KEY=%s\n' '<key from the CARTO dashboard>' > /data/retina-gui/carto.env
chmod 600 /data/retina-gui/carto.env
systemctl restart retina-gui
```

systemd applies these in sequence and a later assignment wins, so `/data` is
listed second and a hand-set key is not overwritten by the next OS update.

Both `EnvironmentFile=` lines are prefixed with `-`, so a node with neither
still boots; its tower map just comes back watermarked. A missing key must
never be the difference between a GUI that starts and one that does not.

Deliberately not committed, and `CARTO_API_KEY` defaults to empty in
`src/services.py`. This repository is public and a key in its history outlives
every rotation. That is the same reason retina-server keeps its copy in
`/root/.secrets/carto.env` on the droplet and appends it at deploy time rather
than committing it, and the key itself is the same one.

The value is public in the sense that matters for scoping: tile requests are
issued by the browser, so no server sits in the path that could hold a secret,
and anyone who loads the page can read it. Only ever put a tile-scoped key
here.

For local development, export it instead:

```bash
export CARTO_API_KEY=<key>
```

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
