# Passive Radar and Max-Hold

The Passive Radar page (`/radar`) and the Max-Hold page (`/radar/maxhold`) draw blah2's
delay-Doppler map with its detections and ADS-B truth over it. They are the same display fed by
different data: Passive Radar shows the latest map, and Max-Hold shows the per-cell maximum over
the last 20 maps, which blah2_api computes.

retina-gui holds no radar data. blah2 produces it, blah2_api serves it, and retina-gui proxies it
to the browser and draws it with Plotly.

## Where it lives

| File | Role |
| --- | --- |
| [src/routes/radar.py](../../src/routes/radar.py) | The `/radar` blueprint: the two pages and the data proxy. |
| [templates/radar.html](../../templates/radar.html) | The page, shared by both displays. |
| [static/radar-map.js](../../static/radar-map.js) | The polling loop and the plot. |
| [static/vendor/plotly-2.20.0.min.js](../../static/vendor/plotly-2.20.0.min.js) | Plotly, unmodified. See [Plotly](#plotly). |
| [src/blah2_client.py](../../src/blah2_client.py) | `Blah2Client.get_raw`, which the proxy goes through. |

## Data flow

```
blah2 --(TCP)--> blah2_api --(HTTP, localhost:3000)--> /radar/data/* --> browser
```

Once a second the page fetches `/radar/data/timestamp`. When the timestamp has changed it fetches
the detections, the ADS-B truth (if `truth.adsb.enabled` is on) and the map, and redraws when the
map arrives.

## What was ported

These pages replace the Passive Radar and Max-Hold cards' links to blah2's own web UI on port
49152. `static/radar-map.js` is a port of blah2-arm's `web/html/js/plot_map.js`, and it was kept
close to the original on purpose, so that moving the display changed where it lives and not what
it shows. Axes, colour scale, markers, the Reset View button and the polling are all the same.
A change to one should be judged as a change to the display, not as tidying.

What differs from the original:

- jQuery is replaced by `fetch`, since no other page here loads it.
- `common.js`'s `is_localhost` switch is gone. Everything is same-origin now.
- The page sits inside the app's navigation rather than blah2's navbar, and has no footer, so
  the plot keeps the whole height.
- The unused ADS-B fetch at startup is gone. Its result was never read.

blah2's original pages are still served on port 49152, and the Controller card still reaches them.

## The data proxy

`/radar/data/<source>` relays one blah2_api reply, status included. Only the sources in `SOURCES`
exist, so the proxy cannot be used to reach `/capture` or anything else on blah2_api. The ADS-B
source forwards `detection_ts`, and nothing else forwards a parameter.

The page talks only to retina-gui, which is what makes it work over the support tunnel and puts
it behind the same gate as every other page. See
[Why the page is proxied](tracker.md#why-the-page-is-proxied).

The content type is set by `SOURCES` rather than relayed. Older blah2_api labels its JSON
`text/html`, and relaying that would serve ADS-B flight names as a page from this origin. Replies
are marked `no-store`, as blah2_api marks its own.

`/api/adsb2dd` answers 400 when ADS-B truth is off. That is relayed as it is, because it is not an
outage. When blah2_api cannot be reached the route answers 502 and logs one warning per outage,
since an open page asks several times a second. The page treats any failure as blah2's page did:
it keeps the last picture and tries again on the next tick.

### Choosing paths on this app

The support tunnel sends `/api/...` (a fixed list), `/capture`, `/stash`, `/display`, `/controller`,
`/lib` and `/js` to blah2 before anything reaches retina-gui (node-infra's `tunnel_sync.py`). A
route of ours under those prefixes would work on the LAN and break over the tunnel, which is why
the data lives under `/radar/data`.

## Plotly

The pages load a bundled Plotly rather than the CDN copy the Tracker page uses, for two reasons:

- The display has to work on a node with no internet. It always has, because blah2 bundles Plotly
  too.
- Reset View simulates Plotly's internal double-click handling, which is specific to this version.

The file is byte-identical to the one in blah2-arm's `web/html/lib/`.

## SDR modes

blah2 does not run in spectrum or SDRconnect mode, so both pages redirect to Home there rather
than opening an empty plot that looks like a quiet sky. The Home page hides their cards in those
modes.

## During a calibration run

`/radar` is on `_CALIBRATION_ALLOWED_PREFIXES`. The display has always been watchable during a run,
and without this an open page's data requests would be redirected to `/config` and it would freeze
with no explanation. See [Calibration hold](../architecture.md#calibration-hold).

## Known limits

- **Cost.** A map at ±1000 Hz is about 2.2 MB, so one viewer moves that much a second through this
  app. blah2's page fetched it straight from blah2_api.
- **Failures are silent.** The page shows no connection state, as blah2's did not. A stopped radar,
  or an expired session over the tunnel, leaves the last picture on screen.
