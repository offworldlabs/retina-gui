# Radar / Spectrum Mode Toggle

A toggle in the Config page (under **Node → Mode**) allows switching the node between **Radar** mode (blah2 passive radar) and **Spectrum** mode (retina-spectrum SDR visualiser) without manual container management.

---

## What was added

| File | Change |
|---|---|
| `spectrum/docker-compose.yml` | Bundled compose file for retina-spectrum, ships with the GUI. References a pre-built image tag via `SPECTRUM_V` env var. |
| `src/routes/mode.py` | New Flask blueprint. `GET /api/mode` returns current mode. `POST /api/mode` stops/starts the appropriate containers and writes `DATA_DIR/mode.txt`. |
| `src/app.py` | Added `RETINA_SPECTRUM_PATH` env var (defaults to `<project_root>/spectrum`). Registered the mode blueprint. |
| `src/routes/home.py` | Reads current mode and passes it to the home template. |
| `templates/config.html` | New **Mode** section with toggle. Syncs from server on page load, POSTs on change with a spinner. |
| `templates/index.html` | Radar mode renders the normal service cards. Spectrum mode renders a full-height iframe pointed at port 3020. |
| `static/common.css` | Spectrum iframe layout styles. |
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

## Running for review

retina-gui and retina-node are independent — retina-gui is packaged by owl-os and runs as its own systemd service; it is not part of the retina-node compose stack. To test the toggle you need both running.

**1. Start retina-gui**

```bash
cd retina-gui/src
pip install -r ../requirements.txt
python app.py
```

Visit `http://localhost:5000`. The GUI works without retina-node installed — docker commands are skipped and the toggle still flips using an in-memory cache.

**2. Start retina-node** (for full end-to-end testing)

retina-node ships its own `docker-compose.yml`. Bring it up as the `retina-node` project so the GUI's docker commands target it correctly:

```bash
cd retina-node
docker compose -p retina-node up -d
```

**3. Verify the toggle**

Open Config → Mode. Switching to Spectrum stops the four blah2 containers and starts retina-spectrum; switching back reverses the process. The home page iframe appears at port 3020 when in Spectrum mode.

**Prerequisite:** retina-spectrum must be published to ghcr.io before the `docker compose up` in step 3 can pull the image (see below).

---

## Deployment prerequisite

`spectrum/docker-compose.yml` references `ghcr.io/offworldlabs/retina-spectrum:${SPECTRUM_V:-v0.1.0}`. Update `SPECTRUM_V` once the image is published to ghcr.io. No changes are required to the retina-node compose stack.

---

## Dev / pre-deployment behaviour

When retina-node is not installed the docker commands are skipped. The toggle still flips and the home page renders the correct content, using an in-memory cache for the mode state.

---

## Validation

The docker command layer was validated end-to-end on a live retina-node stack using a `python:3.12-slim` placeholder container in place of retina-spectrum (the real image targets ARM64 and cannot be built on x86_64). Each subprocess call was executed through Python exactly as `mode.py` issues it, with `cwd` set to the correct project directory. All four commands returned exit code 0, `tar1090` and `adsb2dd` remained untouched throughout, and the full stop → start → stop → restart cycle was confirmed via `docker ps`. The toggle was also validated qualitatively by running the GUI against the live stack and observing the container switching through the browser.
