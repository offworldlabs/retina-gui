# Passive Radar

The Passive Radar page (`/radar`) draws blah2's delay-Doppler map with its detections and ADS-B
truth over it. A toggle switches it between two views of the same display fed by different data:
Live shows the latest map, and Max-Hold shows the per-cell maximum over the last 20 maps, which
blah2_api computes.

retina-gui holds no radar data. blah2 produces it, blah2_api serves it, and retina-gui proxies it
to the browser and draws it with Plotly.

## Where it lives

| File | Role |
| --- | --- |
| [src/routes/radar.py](../../src/routes/radar.py) | The `/radar` blueprint: the page, the old Max-Hold address and the data proxy. |
| [templates/radar.html](../../templates/radar.html) | The page, in the frame it shares with the Tracker ([Scope pages](../architecture.md#scope-pages)). |
| [static/radar-map.js](../../static/radar-map.js) | The polling loop, the view toggle, the status line and the plot. |
| [static/vendor/plotly-2.20.0.min.js](../../static/vendor/plotly-2.20.0.min.js) | Plotly, unmodified. See [Plotly](#plotly). |
| [src/blah2_client.py](../../src/blah2_client.py) | `Blah2Client.get_raw`, which the proxy goes through. |
| [src/plot_axes.py](../../src/plot_axes.py) | The node's extent, shared with the Tracker. See [Axes](#axes). |

## Data flow

```
blah2 --(TCP)--> blah2_api --(HTTP, localhost:3000)--> /radar/data/* --> browser
```

Once a second the page fetches `/radar/data/timestamp`. When the timestamp has changed it fetches
the detections, the ADS-B truth (if `truth.adsb.enabled` is on) and the current view's map
(`/radar/data/map` for Live, `/radar/data/maxhold` for Max-Hold), and redraws when the map arrives.

## The page

The page uses the frame it shares with the Tracker (see [Scope pages](../architecture.md#scope-pages)):
the heading with the View toggle and the settings gear on the right, then one panel holding the
toolbar, the plot and a footer. The toolbar has the status line, the
[out-of-view alert](#out-of-view-alert), the [Doppler and Delay toggles](#doppler-and-delay-views)
and Reset View, which returns to the toggles' ranges. The footer has a key for the two marker
kinds and the extent of the map being drawn, such as `±1000 Hz · -1.5 to 60.0 km`. The plot's
height follows the Tracker's: 72% of the viewport, clamped between 520 and 980 px.

## Axes

The full extent of each axis is the node's own: its `process.ambiguity` Doppler bounds, and its
delay bounds converted to kilometres. The [Doppler and Delay toggles](#doppler-and-delay-views)
draw either that or a narrow range within it. They come from the same `axis_bounds()` the Tracker uses (see
[Axis bounds](tracker.md#axis-bounds)), and the route renders them into the page as
`window.RADAR_AXES`, so the empty plot already has the right axes while it waits for the first map.

- **They do not move with the data.** They change only when a viewer changes a toggle, or a map
  of a different shape (rows or columns) rebuilds the plot. Every frame of the same shape is drawn
  on identical axes, and Live and Max-Hold share them.
- **Full never cuts off the map.** If blah2 is running a wider extent than the config said when
  the page loaded (the config was applied since), the full extent widens to take in the whole map,
  and the Full buttons' labels follow. A narrower map leaves blank margins until the page is
  reloaded.
- **An axis the config does not state follows the map,** from its first cell to its last, as
  blah2's own page did. So does a config that cannot be read.
- **Reset View and a double-click return to the toggles' ranges.** Both call `showViews()`, which
  relays them to Plotly. Plotly's own double-click is off (`doubleClick: false`) because it returns
  to the ranges the plot was built with, which after a toggle is the wrong view; the page listens
  for `plotly_doubleclick` instead, which Plotly still emits. blah2's page simulated Plotly's
  double-click, which autoscaled when not zoomed: half a cell wider, and out to any ADS-B marker
  beyond the map (124 km and +1044 Hz on one node).

The plot keeps its width from the first draw. Before the first map it holds one transparent cell
whose only job is to draw the colour bar; without it the plot narrowed by the colour bar's width
when the first map landed.

## Doppler and delay views

The toolbar's Doppler and Delay toggles are the Tracker's (see
[Doppler view](tracker.md#doppler-view) and [Delay view](tracker.md#delay-view)), with the same
defaults, limits, dialog and wording, so the two pages work the same way:

| Toggle | Narrow | Full | Opens on |
| --- | --- | --- | --- |
| Doppler | ±300 Hz, set with the gear | The node's span, such as ±1000 | Narrow, where ordinary air traffic sits |
| Delay | Out to 30 km, set with the gear | The node's furthest range, such as 60 km | Full |

- **Presentation only.** blah2 processes its full span whichever is drawn, and nothing is written
  to the node. Both views, Live and Max-Hold, draw the same ranges.
- **The buttons name their ranges,** such as `±300 / ±1000` and `30 km / 60 km`, so they stay
  truthful on a node with a different span and after the narrow range is changed.
- **Narrow is clipped to the full extent,** so it never draws Doppler or range the node cannot see.
  The narrow delay range starts where the node starts, slightly below zero, so narrowing never
  cuts off the nearest detections.
- **Saved in this browser,** under `radar.dopView`, `radar.delView`, `radar.narrowHz` and
  `radar.narrowKm`. They are kept apart from the Tracker's `tracker.*` settings, as the Tracker
  keeps its own, so changing one page never changes the other. A saved value the node's span
  cannot draw falls back to the default.
- **The gear opens Radar settings,** a native `<dialog>` with the two narrow ranges. The Doppler
  field takes a whole number of hertz from 1 to half the full span; the delay field a whole number
  of kilometres from 1 to the furthest range, rounded up. Saving checks both and saves neither
  unless both are valid. A field whose value changed switches its toggle to Narrow, since setting
  a range is asking to see it. Reset to defaults refills ±300 Hz and 30 km (or the furthest range,
  if that is nearer) without saving.

## Out-of-view alert

A narrow view leaves part of the map off the plot, so the toolbar says when detections are there:
`8 detections outside ±300 Hz in 10s`, with a Show full span button that sets both toggles to Full.
It names only the ranges the detections fall outside, and is hidden when there are none or both
toggles are on Full.

It counts the detections of the last 10 seconds (`OOV_WINDOW_MS`), not of one frame. A frame
carries only a few detections, so a per-frame count switched on and off every second. The Tracker
counts tracks over its view window instead, since it has them; this page has only detections.
ADS-B aircraft are not counted.

## Colour scale

The heatmap's colour scale is fixed at 0-20 dB (`SNR_RANGE`), the same range the Tracker colours
its detections on. The colours are blah2's Viridis.

blah2's page stretched the scale to each frame's loudest cell (`zmax = max(13, maxPower)`). That
had three effects:

- The whole map was recoloured every frame as the loudest cell changed: the "SNR jitter".
- The colour bar's tick labels changed width with the scale, which moved the plot's x-axis by
  8 px from frame to frame: the "shifting x-axis". Measured on a live node with an injected target.
- A quiet sky was drawn at the 13 dB floor, its most sensitive scale, so it looked busiest when it
  was emptiest.

With a fixed scale a colour means the same SNR in every frame, in both views and on both pages.
Cells above 20 dB draw at the top colour. The values are blah2's, which are half true dB (its map
uses `10·log10` of an amplitude); the colour bar is labelled as blah2 labels them.

## The status line

The status line says whether the picture is current, so a stopped radar or an expired session
never passes for a quiet sky. It is redrawn every second from two facts: when the last new map was
drawn, and whether the last timestamp request failed.

| Shown | Dot | When |
| --- | --- | --- |
| `connecting…` | amber | No map drawn yet. |
| `live · updated 1s ago` | green | A new map was drawn in the last 5 s (`STALE_MS`). |
| `no new frame for 12s` | amber | blah2_api answers, but the timestamp has not moved for 5 s: blah2 is not producing maps. |
| `cannot reach the radar · retrying` | red | The timestamp request failed (a 502 from the proxy, or the network). |
| `signed out · reload the page to sign in` | red | The request was redirected to the login page, or refused with 401 or 403: the session has expired over the tunnel. |

The plot is not cleared in any of these: the last picture stays, and the status line says how old
it is.

## The view toggle

The toolbar's View control switches between Live and Max-Hold. Both views draw on the same plot
with the same detections and ADS-B dots; only the heatmap's source changes.

- **The URL carries the view.** `?view=maxhold` opens Max-Hold, and anything else, or nothing,
  opens Live. The route checks the value against `VIEWS` and renders the pressed button, so the
  page opens on the right view without a flash. Switching updates the address with
  `history.replaceState`, so a reload or a bookmark keeps the view, and Back leaves the page
  rather than stepping through toggles.
- **Home has one card.** Passive Radar opens `/radar`, which starts on Live. There was a Max-Hold
  card while Max-Hold was its own page; the toggle replaced it, and the tutorial's Max-Hold step now
  points at the Passive Radar card.
- **The old address redirects.** Max-Hold was its own page at `/radar/maxhold` before the toggle,
  so that path redirects to `/radar?view=maxhold`.
- **Switching draws now.** The new view's map is fetched straight away rather than at the next
  frame, which on a quiet radar could be a second away.
- **Switching keeps the zoom.** The two maps share axes, so the plot takes its update path and the
  viewer's zoom survives. That is what makes comparing the views at one spot useful. A map of a
  different shape still rebuilds the plot and returns to the fixed [axes](#axes).
- **A late reply is dropped.** A map requested before a switch but arriving after it would
  overwrite the new view, so `fetchMap` draws a reply only if its view is still the current one.
- **The view is not remembered elsewhere.** It lives in the URL only, not in `localStorage`, so the
  Home card always opens Live.

## What was ported

This page replaces the Passive Radar and Max-Hold cards' links to blah2's own web UI on port
49152, and the two cards are now one. `static/radar-map.js` is a port of blah2-arm's `web/html/js/plot_map.js`, and it was kept
close to the original on purpose, so that moving the display changed where it lives and not what
it shows. The markers and the polling are still the same. A change to one should be judged as a
change to the display, not as tidying.

What differs from the original:

- jQuery is replaced by `fetch`, since no other page here loads it.
- `common.js`'s `is_localhost` switch is gone. Everything is same-origin now.
- The page sits inside the app's navigation rather than blah2's navbar, and has no footer, so
  the plot keeps the whole height.
- Live and Max-Hold are one page with a toggle, not two pages. See
  [The view toggle](#the-view-toggle).
- Reset View sits in the toolbar rather than floating over the corner of the plot.
- The page has a status line and a footer key, in the frame it shares with the Tracker.
- The unused ADS-B fetch at startup is gone. Its result was never read.
- The axes are fixed at the node's extent and Reset View returns to them. See [Axes](#axes).
- Doppler and Delay toggles narrow the plot, as on the Tracker. See
  [Doppler and delay views](#doppler-and-delay-views).
- The colour scale is fixed at 0-20 dB. See [Colour scale](#colour-scale).

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

The page loads a bundled Plotly rather than the CDN copy the Tracker page uses, because the
display has to work on a node with no internet. It always has, because blah2 bundles Plotly too.
Nothing on the page depends on this version's internals.

The file is byte-identical to the one in blah2-arm's `web/html/lib/`.

## SDR modes

blah2 does not run in spectrum or SDRconnect mode, so the page redirects to Home there rather
than opening an empty plot that looks like a quiet sky. The Home page hides their cards in those
modes.

## During a calibration run

`/radar` is on `_CALIBRATION_ALLOWED_PREFIXES`. The display has always been watchable during a run,
and without this an open page's data requests would be redirected to `/config` and it would freeze
with no explanation. See [Calibration hold](../architecture.md#calibration-hold).

## Known limits

- **Cost.** A map at ±1000 Hz is about 2.2 MB, so one viewer moves that much a second through this
  app. blah2's page fetched it straight from blah2_api.
- **The picture is never cleared.** A stopped radar or an expired session leaves the last picture
  on screen. The [status line](#the-status-line) says so, but nothing on the plot itself does.
