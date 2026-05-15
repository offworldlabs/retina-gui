# Radar / Spectrum Mode Toggle

A nav-bar toggle allows switching the node between **Radar** mode (blah2 passive radar) and **Spectrum** mode (retina-spectrum SDR visualiser) without manual container management.

---

## What was added

| File | Change |
|---|---|
| `spectrum/docker-compose.yml` | Bundled compose file for retina-spectrum, ships with the GUI. References a pre-built image tag via `SPECTRUM_V` env var. |
| `src/routes/mode.py` | New Flask blueprint. `GET /api/mode` returns current mode. `POST /api/mode` stops/starts the appropriate containers and writes `DATA_DIR/mode.txt`. |
| `src/app.py` | Added `RETINA_SPECTRUM_PATH` env var (defaults to `<project_root>/spectrum`). Registered the mode blueprint. |
| `src/routes/home.py` | Reads current mode and passes it to the home template. |
| `templates/base.html` | Nav toggle syncs from server on page load, POSTs on change with a spinner, reloads page on success. |
| `templates/index.html` | Radar mode renders the normal service cards. Spectrum mode renders a full-height iframe pointed at port 3020. |
| `static/common.css` | Spinner animation and spectrum iframe layout styles. |
| `tests/test_mode.py` | 17 tests covering the API endpoints, docker command sequencing, failure rollback, mode persistence, and home page rendering. |

---

## How it works

**Switching to Spectrum:**
1. `docker compose -p retina-node stop blah2 blah2_api blah2_web blah2_host` — stops the four SDR-consuming containers. `tar1090` and `adsb2dd` continue running.
2. `docker compose -p retina-spectrum up -d` — starts retina-spectrum from `RETINA_SPECTRUM_PATH`. The container kills any stale `sdrplay_apiService`, waits 2 seconds, then serves its UI on port 3020.
3. Mode written to `DATA_DIR/mode.txt`.

**Switching to Radar:**
1. `docker compose -p retina-spectrum down` — stops retina-spectrum.
2. `docker compose -p retina-node start blah2 blah2_api blah2_web blah2_host` — restarts the blah2 cluster from existing container state (no config-merger, no `--force-recreate`).
3. Mode written to `DATA_DIR/mode.txt`.

If either docker command fails the mode file is not updated and the toggle reverts to its previous position.

---

## Deployment prerequisite

`spectrum/docker-compose.yml` references `ghcr.io/offworldlabs/retina-spectrum:${SPECTRUM_V:-v0.1.0}`. Update `SPECTRUM_V` once the image is published to ghcr.io. No changes are required to the retina-node compose stack.

---

## Dev / pre-deployment behaviour

When retina-node is not installed the docker commands are skipped. The toggle still flips and the home page renders the correct content, using an in-memory cache for the mode state.
