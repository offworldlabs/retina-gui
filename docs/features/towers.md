# Tower finder

A passive radar node needs a broadcast transmitter to illuminate its sky. retina-gui does not
know where the towers are: it proxies the external tower-finder service, which ranks nearby
towers for a receiver position (by measured signal match when the node supplies an RF scan, by
geography otherwise). The setup wizard uses these routes to search, geocode an address and
prefill altitude; the best few results are cached on the node and reused as the Tower preset
picker on `/config` and as Auto-Calibrate's alternate-tower list.

## Where it lives

| File | Role |
| --- | --- |
| [src/routes/towers.py](../../src/routes/towers.py) | The `/towers` blueprint: search, geocode, elevation, cache add/remove, the retina-spectrum SSE proxy, and `select` (save RX + TX location and queue an apply). |
| [src/device_state.py](../../src/device_state.py) | `save_towers_cache`, `add_tower_to_cache`, `remove_tower_from_cache`, `get_towers_cache`: the `towers-cache.json` file under the data dir. |
| [src/services.py](../../src/services.py) | `TOWER_FINDER_URL` and `RETINA_SPECTRUM_URL`, both overridable by environment variable. |
| [src/config_schema.py](../../src/config_schema.py) | `TX_NAME_MAX_LENGTH` and `LocationFormConfig`, which validate what `select` saves. |
| [static/setup.js](../../static/setup.js) | The wizard's location step: RF scan, address lookup, elevation prefill, search, map/table, and the final `select`. |
| [templates/config.html](../../templates/config.html) | The Tower preset picker and the manage list (add/remove). |
| [src/routes/calibrate.py](../../src/routes/calibrate.py) | Reads the same cache as Auto-Calibrate's alternate towers. See [auto-calibrate.md](auto-calibrate.md). |

## Routes

| Route | Method | Upstream | Notes |
| --- | --- | --- | --- |
| `/towers/search` | POST | tower-finder `/api/towers` | Caches the best results. 90 s timeout. |
| `/towers/geocode` | POST | tower-finder `/api/geocode` | Address to coordinates. Passes 404 and 503 through. |
| `/towers/elevation` | GET | tower-finder `/api/elevation` | Advisory altitude prefill. |
| `/towers/cache/add` | POST | none | Manual tower into the cache. |
| `/towers/cache/remove` | POST | none | Remove a cached tower by index. |
| `/towers/spectrum/events` | GET (SSE) | retina-spectrum `/api/events` | Byte-for-byte stream proxy for the wizard's RF scan. |
| `/towers/select` | POST | none | Save location and centre frequency, then queue an apply. |

## Tower search

`search()` takes `lat` and `lon` (both required) plus optional `radius_km`, `limit` and `source`.

- **With `measurements`** (the wizard's RF scan results, collected over `/towers/spectrum/events`)
  it POSTs to the tower-finder so towers can be ranked by signal match rather than geography
  alone.
- **Without** it GETs the same endpoint, and additionally forwards `altitude` and `frequencies`
  as query parameters.

The response is returned to the browser unchanged, so the wizard's map and table can show
every tower the service found. Separately, the towers are screened (see
[Screening cached towers](#screening-cached-towers)) and the first `MAX_CACHED_TOWERS` (5) are
written to the cache. The service already ranks best-first, and the cache only backs a dropdown
and a manage list, which stay usable only when short. A failed cache write is logged and does
not fail the search.

Failures collapse to one message per kind: a timeout is 504, any other request failure is 502,
and anything unexpected is 500.

## The tower cache

`towers-cache.json` holds `lat`, `lon`, `cached_at` and a `towers` list. It has no expiry:
tower frequencies and positions essentially never change, and re-running the wizard's tower
step overwrites it with a fresh search.

Its consumers:

- The Tower preset picker and manage list on `/config` (and the setup page, which passes it to
  its template).
- Auto-Calibrate's alternate-tower list. The wizard's search can be RF-informed, which ranks
  better than a plain geography lookup, and reusing it avoids a second live tower-finder call
  at calibration time. Auto-Calibrate falls back to a geography-only lookup when the cache is
  empty.

`cache_add()` appends a manually entered tower (source `manual`, with `callsign` and `name` both
set to the typed name), creating the cache if the wizard's search never ran.
`cache_remove()` deletes by list position and returns 404 when the index is out of range.
Both return the updated list so the page can re-render without a reload. They write the cache
file immediately and are independent of the staged `/config` form (the same "takes effect
immediately" model as SSH keys).

`cache_add()` refuses a name longer than `TX_NAME_MAX_LENGTH` itself, as well as through
`LocationFormConfig`, so the error lands on the field the owner is typing into rather than
later, when the tower is picked and the whole location block fails to validate.

## Screening cached towers

`_cacheable_towers()` is the search-path equivalent of the checks in `cache_add()`.

Picking a preset on `/config` assigns straight into the location inputs with `el.value = ...`.
`maxlength` does not constrain a programmatic assignment, and the validity flag it would
otherwise raise is ignored because the form is `novalidate`. An over-long name or out-of-range
coordinate would therefore land in the form intact, and the save would then fail on a field
the owner never touched. Before this screen existed the search path cached whatever the
service returned.

The screen:

| Problem | Action |
| --- | --- |
| Entry is not an object | Dropped |
| `latitude`/`longitude` missing or not numeric | Dropped |
| Latitude outside -90..90 or longitude outside -180..180 | Dropped |
| `callsign` or `name` longer than `TX_NAME_MAX_LENGTH` | Trimmed to the limit, kept |

Names are trimmed rather than dropped: `TX_NAME_MAX_LENGTH` is retina-telemetry's `tx_callsign`
limit, not a reason to lose an otherwise usable tower. Both keys are trimmed because the picker
falls back from callsign to name, and a facility name runs long far more readily than a
callsign does. A tower whose coordinates cannot be a position is useless to both the picker and
Auto-Calibrate, so it is dropped. The number dropped is logged as a warning.

## Tower presets on /config

The preset dropdown fills the transmitter fields (`location.tx_name`, `tx_latitude`,
`tx_longitude`, `tx_altitude`) and `capture.fc` from the chosen tower, using callsign, then
name, then "Tower" for the name. These are staged form edits: they take effect when the owner
saves and applies the config form. The manage list's Remove buttons and the Add Tower modal
call the cache routes above.

## Address lookup

`geocode()` resolves a typed address to coordinates through the tower-finder service.

The node GUI is served over plain HTTP on a LAN, so the browser's Geolocation API is unavailable
to it and the wizard's "Use my location" button stays commented out. Geocoding runs server-side,
so that constraint does not apply: typing an address is the one way an owner can fill the
coordinates without reading them off a map.

Unlike `search()`, the upstream's two failure codes are passed through rather than collapsed:

| Upstream | Meaning | What the owner should do |
| --- | --- | --- |
| 404 | Neither geocoder knew the address | Check the spelling |
| 503 | A geocoder could not be reached | Retry the same query |

The error text is the upstream's own `detail` sentence when it is a plain string
(`_upstream_detail()`). FastAPI puts a list of field errors in `detail` for a validation
failure, and that is not something to show an owner, so any non-string falls back to our own
sentence.

The query is limited to `GEOCODE_QUERY_MAX_LENGTH` (200), the upstream's own `AddressQuery`
bound, so an over-long address is refused here with a sentence instead of upstream with a 422.

The query text is kept out of every log line, as it is upstream. An address typed into this box
is the most personal thing these routes handle, and the outcome alone is what an operator needs.

## Elevation prefill

`elevation()` returns ground elevation at a point, to prefill the wizard's altitude box. It is
advisory: nothing gates on altitude, and the wizard leaves the box blank on failure rather than
reporting an error, so the route answers failures with a plain 502 the caller swallows. Failures
are logged at info rather than warning because a missing prefill is nothing an owner or
operator needs to act on.

It exists because nothing else filled the box, which left `rx_altitude` at 0 in the radar config
for every owner who did not type a figure. It is a GET to match the upstream, which also keeps
it clear of CSRF.

## Saving a tower selection

`select()` is how the wizard commits a location. It:

1. Builds a flat location from the request (`rx_name` is always the node ID, `tx_name` is the
   tower's callsign) and validates it with `LocationFormConfig`.
2. Refuses a location that is not fully sited (`is_located`). The model permits a wholly empty
   location because a node legitimately has none, but this endpoint is the one that sets one,
   so an empty POST must not silently unsite a configured node.
3. Sets `capture.fc` from `frequency_mhz` (MHz to Hz) when given.
4. Writes `user.yml` synchronously. The write must be visible to anything that reads
   `user.yml` the moment the request returns.
5. Returns `applied: false` when retina-node is not installed, and 409 when an update is in
   progress.
6. Hands the slow part (config-merger plus a stack restart, about 45 s) to the shared apply
   queue in [apply_service.py](../../src/apply_service.py) and returns 202. The browser polls
   `/config/apply/status`. The queue always merges whatever is in `user.yml` when it runs, so it
   necessarily picks up the write from step 4. Queuing is what stops the request blocking a
   browser for minutes when it lands behind another restart.

A calibration in flight is refused inside `ApplyService.request()` (`ConfigChangeRefused`,
returned as 409), which is why `select()` does not check for one itself. In spectrum mode the
apply only runs config-merger: blah2 is intentionally stopped and must not be restarted until
the owner switches back to radar mode.

## Timeouts and limits

| Constant | Value | Why |
| --- | --- | --- |
| Search timeout | 90 s | A measurement-ranked search can be slow. |
| `GEOCODE_TIMEOUT_S` | 30 s | The upstream allows 10 s per provider across two providers, plus a 1 s throttle on Nominatim. 30 s covers a slow two-provider miss without holding a browser for the 90 s a search is allowed. |
| `ELEVATION_TIMEOUT_S` | 15 s | One cached upstream lookup, and nothing waits on the result. |
| `GEOCODE_QUERY_MAX_LENGTH` | 200 | Mirrors the upstream's `AddressQuery` bound. |
| `MAX_CACHED_TOWERS` | 5 | Keeps the preset dropdown and manage list usable. |
| Spectrum SSE | 5 s connect, no read timeout | The scan stream is long-lived. On failure the proxy emits one `{"type":"error"}` message. |
