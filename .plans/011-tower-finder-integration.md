# 011 — Tower Finder Integration

## Goal
Add a "Location" step to the setup wizard that lets operators set their RX location, search for nearby broadcast tower illuminators via the Tower-Finder API, and select one as the TX — writing both to user.yml.

## Context
- Tower-Finder is a FastAPI service hosted at **`https://api.retina.fm`**
  - `GET /api/towers` — ranked tower search by location
  - `GET /api/elevation` — ground elevation lookup
- retina-gui setup wizard currently: Agreements → System → Packages → Done
- Config schema already has `LocationFormConfig` with rx/tx lat/lon/altitude/name fields
- Frontend is Flask + Jinja2 + Bootstrap 5 + vanilla JS (no React)

## Architecture Decisions
- **Proxy via Flask backend** — retina-gui proxies Tower-Finder API calls (avoids CORS, keeps API URL server-side)
- **`TOWER_FINDER_URL` env var** — default `https://api.retina.fm`
- **Vanilla JS + Bootstrap + Leaflet** — matches existing setup wizard style, no React
- **Leaflet map** — loaded from CDN (same tile provider as Tower-Finder: CartoDB light)
- **No frequency matching yet** — spectrum analyser will feed in later
- **Data source auto-detected** — silently from lat/lon bounding boxes (AU/CA/US), no dropdown needed

## New Setup Flow
```
Agreements → System → Packages → Location → Done
                                    │
                                    ├─ 1. Set RX location
                                    │     ├─ "Use My Location" button (browser geolocation)
                                    │     ├─ OR manual lat/lon entry
                                    │     └─ Altitude auto-filled from elevation API
                                    │
                                    ├─ 2. "Find Towers" → calls Tower-Finder API
                                    │     ├─ Loading spinner while querying
                                    │     ├─ Leaflet map with color-coded tower markers
                                    │     ├─ Results table with ranked towers
                                    │     └─ Summary stats (towers found, ideal count, top pick)
                                    │
                                    └─ 3. Select tower → sets TX location
                                          ├─ "Select" button per row
                                          ├─ Confirmation card showing chosen tower
                                          └─ "Save & Continue" writes to user.yml
```

## Tower-Finder API Details

### `GET /api/towers` — what we send
| Parameter   | Type   | Required | Description                          |
|-------------|--------|----------|--------------------------------------|
| `lat`       | float  | yes      | RX latitude (-90 to 90)              |
| `lon`       | float  | yes      | RX longitude (-180 to 180)           |
| `altitude`  | float  | no       | RX altitude in metres (default 0)    |
| `radius_km` | int   | no       | Search radius 0-300 (default 80)     |
| `limit`     | int    | no       | Max results 0-200 (default 100)      |
| `source`    | string | no       | `us`, `au`, `ca`, `auto` (default `auto`) |

### What we get back
```json
{
  "towers": [
    {
      "rank": 1,
      "callsign": "ATN6",
      "name": "ABC Tower Gore Hill",
      "state": "NSW",
      "frequency_mhz": 177.5,
      "band": "VHF",
      "latitude": -33.820079,
      "longitude": 151.185,
      "distance_km": 5.9,
      "bearing_deg": 337.5,
      "bearing_cardinal": "NNW",
      "received_power_dbm": -7.7,
      "distance_class": "Ideal",
      "eirp_dbm": 79.1,
      "altitude_m": 122.5,
      "antenna_height_m": 77.3
    }
  ],
  "query": { "latitude": ..., "longitude": ..., "radius_km": 80, "source": "au" },
  "count": 20
}
```

### `GET /api/elevation` — elevation lookup
- Send: `lat`, `lon`
- Returns: `{ "elevation_m": 45.2 }`

## Source Auto-Detection (from Tower-Finder)
```
Australia:  lat -45 to -10, lon 112 to 155  → "au"
Canada:     lat 42 to 84,   lon -141 to -52 → "ca"
US:         lat 24 to 49,   lon -125 to -66 → "us"
  + Alaska: lat 51 to 72,   lon -180 to -129
  + Hawaii: lat 18 to 23,   lon -161 to -154
```

## Implementation Plan

### Phase 1: Backend — Tower-Finder API proxy routes

- [ ] **1.1** Add `TOWER_FINDER_URL` env var to app.py (default `https://api.retina.fm`)
- [ ] **1.2** Add `GET /towers/search` route — proxies to `GET /api/towers`
  - Forward query params: `lat`, `lon`, `altitude`, `radius_km`, `limit`, `source`
  - Source auto-detected on backend if not provided
  - Timeout: 90s (tower DB queries can be slow)
  - Error handling: return `{ "error": "..." }` on failure
- [ ] **1.3** Add `GET /towers/elevation` route — proxies to `GET /api/elevation`
  - Forward: `lat`, `lon`
  - Returns `{ "elevation_m": float }`
- [ ] **1.4** Add `POST /towers/select` route — saves RX + TX location to user.yml
  - Accepts JSON body:
    ```json
    {
      "rx_latitude": -33.8688, "rx_longitude": 151.2093,
      "rx_altitude": 45.0, "rx_name": "My Location",
      "tx_latitude": -33.8200, "tx_longitude": 151.185,
      "tx_altitude": 122.5, "tx_name": "ATN6 Gore Hill"
    }
    ```
  - Validates via `LocationFormConfig`
  - Converts to nested YAML via `ConfigManager.unflatten_location_from_form()`
  - Saves to user.yml using existing `ConfigManager`

### Phase 2: Frontend — Location step in setup wizard

- [ ] **2.1** Add "Location" step (step 4) to setup.html stepper, renumber Done → step 5
- [ ] **2.2** RX location input section
  - Three fields: Latitude, Longitude, Altitude (m)
  - "Use My Location" button — browser `navigator.geolocation.getCurrentPosition()`
  - Altitude auto-fills from `/towers/elevation` when lat/lon change (debounced)
  - Fields can also be typed manually
- [ ] **2.3** "Find Towers" button + loading state
  - Calls `GET /towers/search?lat=...&lon=...&altitude=...`
  - Shows Bootstrap spinner + "Querying broadcast licence database..."
  - Disabled until lat/lon are filled
- [ ] **2.4** Results display (shown after search completes)
  - **Summary strip**: towers found, ideal range count, bands present, top pick name+distance
  - **Leaflet map**:
    - CDN: leaflet@1.9.4 JS + CSS
    - Tiles: CartoDB light (`https://{s}.basemaps.cartocdn.com/light_all/...`)
    - Blue dot for user location + 80km dashed radius circle
    - Tower markers: colored circles (green=Ideal, yellow=Good, grey=Far, red=Too Close)
    - Popups: callsign, freq, distance, power, suitability
    - Auto fit-bounds to show all results
  - **Results table** (Bootstrap table, scrollable):
    - Columns: #, Callsign, Freq (MHz), Band, Distance (km), Bearing, Rx Power, Suitability, Action
    - Band badges: VHF=purple, UHF=teal, FM=pink
    - Suitability badges: Ideal=green, Good=yellow, Far=grey, Too Close=red
    - "Select" button on each row
- [ ] **2.5** Tower selection
  - Clicking "Select" shows a confirmation card below the table:
    - "Selected: ATN6 — 177.5 MHz VHF — 5.9 km NNW"
    - TX lat/lon/altitude auto-filled (hidden, not editable)
  - "Save & Continue" button enabled only after a tower is selected
- [ ] **2.6** Save & Continue
  - POSTs to `/towers/select` with RX + TX data
  - On success, advances to Done step
  - On error, shows inline error message

### Phase 3: Tests

- [ ] **3.1** Test `GET /towers/search` — mock `requests.get` to Tower-Finder, verify proxy behavior + error handling
- [ ] **3.2** Test `GET /towers/elevation` — mock elevation response
- [ ] **3.3** Test `POST /towers/select` — validates fields, saves to user.yml correctly
- [ ] **3.4** Test setup wizard renders Location step and step count is now 5

## Config Fields Written
```yaml
# user.yml — only overrides written
location:
  rx:
    latitude: -33.8688
    longitude: 151.2093
    altitude: 45.0
    name: "My Location"
  tx:
    latitude: -33.8200
    longitude: 151.1850
    altitude: 122.5
    name: "ATN6 Gore Hill"
```

## Dependencies
- Tower-Finder API at `https://api.retina.fm` (internet required)
- `requests` library for backend HTTP proxy calls (check if already in requirements)
- Leaflet JS/CSS from CDN (no npm install needed)
