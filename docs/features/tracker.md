# Tracker page

The Tracker page (`/tracker`, formerly Tracker Preview) plots what a node's radar has detected
and which of those detections retina-tracker has built into tracks. retina-gui holds no tracker
data: the record lives in the retina-tracker sidecar container, which serves it over a loopback
SSE endpoint, and retina-gui proxies that stream to the browser and draws it with Plotly. A
second display, the [wedge view](#wedge-view), draws the node's ADS-B-proven coverage and its
tracks on a map with Leaflet.
Separately, `RetinaTrackerClient` tails the sidecar's track-event file and drives its control
surface for Auto-Calibrate.

## Where it lives

| File | Role |
| --- | --- |
| [src/routes/tracker.py](../../src/routes/tracker.py) | The `/tracker` blueprint (page, SSE proxy, one-shot snapshot, clear, and the coverage proxies) and the `/tracker-preview` redirect. Computes the plot's axis bounds from the merged config. |
| [templates/tracker.html](../../templates/tracker.html) | The page: controls, the Plotly scope, the wedge view's container and reset dialog, the track rail, and the EventSource client. |
| [static/tracker-wedge.js](../../static/tracker-wedge.js) | The wedge view: the Leaflet map, the wedge, trails and arcs, and the replay. |
| [src/retina_tracker_client.py](../../src/retina_tracker_client.py) | `RetinaTrackerClient`: JSONL event tailer plus HTTP control client (`reset`). |
| [src/services.py](../../src/services.py) | `RETINA_TRACKER_CONTROL_URL` and `RETINA_TRACKER_EVENTS_PATH`, and the single shared client instance. |
| [src/calibrator.py](../../src/calibrator.py) | The client's listener and `reset()` caller. See [auto-calibrate.md](auto-calibrate.md). |

## Data flow

```
blah2 -> blah2_api --(network.tracker_forward, TCP)--> retina-tracker sidecar
                                                         |  |  |
             /tracker/events <--- SSE (loopback) --------+  |  |
             (browser page)                                 |  |
                                                            |  |
   RetinaTrackerClient tail <--- events.jsonl (-s flag) ----+  |
   (Auto-Calibrate listener)                                   |
                                                               |
   RetinaTrackerClient.reset() --- HTTP POST /reset ---------->+
   /tracker/clear             --- HTTP POST /history/clear --->+
   /tracker/coverage*         --- HTTP GET  /coverage* ------->+
   /tracker/coverage/reset    --- HTTP POST /coverage/reset -->+
```

Detections travel from blah2 through blah2_api straight to retina-tracker
(blah2_api's `network.tracker_forward`). retina-gui is not on that path. It used to be the
transport between blah2 and the tracker, and sent frames down the sidecar's ingest socket; it
stopped because that socket accepts one connection at a time and blah2_api now owns it. That is
why `services.py` configures no ingest address, only the control URL and the events path.

## The retina-tracker client

`RetinaTrackerClient` has two one-way channels, neither of which carries detections.

**Track events in**, by tailing the JSONL file the sidecar writes (its `-s` flag). The sidecar's
`--tcp` mode is input-only over the socket (retina-tracker's `server.py` `run_tcp_server` never
writes back on the accepted connection), so results come back through the filesystem rather
than the connection that fed it.

**Control out**, over HTTP to the sidecar's loopback control surface. This is what let
retina-gui stop being the transport.

There is one instance, built in `services.py`, and consumers register with `add_listener()`
(`start()` is a back-compat alias). The tail thread is started on the first registration.
Today the only listener is Auto-Calibrate; the Tracker page reads the sidecar's SSE endpoint
instead. Sharing one instance is no longer forced by the single-connection ingest, since
nothing here connects to it: one thread tailing one file is simply enough.

Tailing details (`_tail_loop`):

- It starts at the file's current end, not at 0, so a fresh attach sees only new events rather
  than replaying a run's whole history.
- If the file shrinks, the sidecar has restarted: its `TrackEventWriter` opens the file in `w`
  mode on start, truncating it. The offset resets to 0.
- A trailing partial line (the writer is mid-line) is held back and re-read whole on the next
  poll.
- Lines that are not valid UTF-8 JSON are skipped. Polling is every 0.2 s by default.

## Resetting the tracker

`reset()` POSTs to the sidecar's `/reset`, which clears its `Tracker` state in place
(retina-tracker's `Tracker.reset()`). Auto-Calibrate calls it between candidate towers and at the
start of every dwell, because a confirmed track only means something at the geometry (centre
frequency and transmitter position) it was seen at.

The sidecar holds its lock for the whole reset, so a 200 means the tracker is already clear,
not scheduled to be. The caller relies on that: the next frame it waits on must not be able to
associate into pre-reset state. `reset()` returns True only when the sidecar confirms with
`ok`. The old fire-and-forget reset message down the detection socket could fail silently;
this one cannot, so callers that care whether the tracker really is clear can tell.

## Why the page is proxied

The page talks only to retina-gui (`/tracker/events` and the `/tracker/coverage` routes), never to
the sidecar's port. Proxying
keeps the page behind the same session and Cloudflare Access checks as every other page, and
makes it work over the support tunnel, which routes paths on the GUI's hostname and not
arbitrary ports. It is also nearly free: bytes pass through without being parsed, buffered or
re-serialised.

## The stream proxy

`events()` proxies the sidecar's `/events` byte for byte. The sidecar frames the messages, owns
the cursor for each connection and decides what a snapshot contains. retina-gui only carries
them, so there is no second copy of the record here and no wire-format knowledge to drift out
of step.

Each viewer's connection maps to its own upstream connection. On loopback with one or two
viewers that is cheaper than holding a shared mirror and fanning it out, and each viewer's
window is honoured by the sidecar rather than filtered a second time here.

The upstream request has a 5 s connect timeout and no read timeout: a quiet sky is a silent
stream, and the sidecar's own keepalive is what proves the link is alive. If the sidecar is
unreachable the proxy emits a named `error` event (`{"error": "tracker unreachable: ..."}`).
The page shows that detail in its status line, which is a different condition from the browser
losing the proxy (EventSource's own `onerror`, shown as "connection lost, retrying").

`/tracker/data.json` opens the same stream, returns the first `data:` payload and hangs up. It
is for inspecting the wire format without holding a stream open, and the page does not use it.

### Message format

The page listens for named events rather than `onmessage`:

| Event | Meaning |
| --- | --- |
| `snapshot` | Replace everything held. Sent on connect, and mid-stream when the record is cleared. |
| `delta` | Append: everything added since this connection's last message. |

Each message carries `detections`, keyed by class, and `tracks`, keyed by track id. Both use
parallel columns `t` (ms), `delay`, `doppler` and `snr`, in timestamp order. Tracks also carry
`lat` and `lon`: the ADS-B position blah2 matched to that detection, or `null` where there is no
verified one (retina-tracker's `docs/coverage-wedge.md` says what counts as verified). Only the
wedge view reads them; `appendCols` and `sliceCols` carry them when present, so a sidecar that
does not send them is still drawn. Each track also has `meta`: `adsb_hex`, `is_anomalous`,
`anomaly_types`, `length`, `max_velocity_ms` and `shadow_fraction`. On a delta, a known track's
columns are appended and its `meta` replaced.

## View window

The View control offers 5 min, 15 min, 1 h and 4 h, passed as `?window=` in seconds. The route
clamps it to `MIN_VIEW_WINDOW_S`..`MAX_VIEW_WINDOW_S` (60 s to 4 h) rather than rejecting it, as
it is a display preference, not an assertion. Invalid or non-positive values mean "whatever the
sidecar holds". These bounds mirror the sidecar's own and are copied rather than imported
because they belong to another repo's release; a mismatch clamps to something sane here and the
sidecar clamps again.

The page defaults to 15 min. The sidecar keeps the full four hours either way; the window only
decides how much the node has to build and send when the page opens (measured at 0.28 MB for
15 min against 4.50 MB for 4 h).

Changing the window reconnects rather than filtering locally. Narrowing could be done in the
browser, but widening needs history the browser never received, and one path for both means
there is never a stale-cursor case. The stream re-seeds itself with a snapshot at the new
window.

Between reconnects the browser ages points out of the window itself (`pruneToWindow`), because
the server only sends what was appended. It uses node time: the newest point held is "now", so
this stays correct even if the browser's and node's clocks disagree.

A requested window is only what a viewer gets if the node has that much history. It will not
after a restart, nor once the sidecar's point ceiling starts ageing out the oldest points (which
a wider Doppler span reaches sooner). When the oldest point held covers less than 90% of the
window, the footer says so ("12m of 15m held"), because a short record is otherwise
indistinguishable from a quiet sky.

## Axis bounds

`_axis_bounds()` passes the node's ambiguity bounds from its merged blah2 config to the page,
which uses them as the plot's initial axis ranges:

| Axis | Config | Units |
| --- | --- | --- |
| Doppler (y) | `process.ambiguity.dopplerMin` / `dopplerMax` | Hz |
| Bistatic range (x) | `process.ambiguity.delayMin` / `delayMax` | bins, converted to km with `capture.fs`: one bin is `c / fs / 1000` km |

A plot scaled to its own data cannot tell a quiet sky from a narrow one. On a node where nearly
every detection is one interfering tone, an autoscaled Doppler axis collapses to a sliver
around that tone and the picture looks full, and two nodes (or one node an hour apart) are drawn
at different scales and cannot be compared. The ambiguity bounds are the only honest range to
draw over, and they are the node's own numbers.

An axis is `None` (and autoscales) when the config does not state it, when `fs` is missing (for
delay), or when `_drawable()` rejects the pair: both ends must be present and high must exceed
low. A transposed pair would draw the axis backwards and a zero-width one would collapse it,
either way a confident picture of nothing. A guessed range would misrepresent the node as badly
as autoscaling does, only less visibly. A config that cannot be loaded leaves both axes to
autoscale.

The bounds are rendered into the page rather than sent on the stream, because they describe
what the node can see, which is not knowable from what it happened to see. Plotly's
`uirevision` keeps a viewer's own zoom across updates, so the node's range is where the axis
starts rather than somewhere it is dragged back to.

The Doppler bounds are the Full setting of the Doppler view below. By default the page draws a
narrower band inside them.

`SPEED_OF_LIGHT` is mirrored rather than imported for the same reason as the window bounds.

## Doppler view

Every node processes ±1000 Hz of Doppler (retina-node's config-merger enforces it), so that
activity outside the band where ordinary air traffic sits can still be detected and reported.
Drawn at that scale, the traffic people calibrate against fills the middle 30% of the plot. The
Doppler control in the toolbar therefore switches what is drawn between two ranges:

| Setting | Draws | Label |
| --- | --- | --- |
| Narrow (the default) | ±300 Hz, or whatever the gear sets, clipped to the node's span | `±300` |
| Full | The node's own bounds from [Axis bounds](#axis-bounds), or autoscale if the config does not state them | `±1000` |

This is presentation only. Nothing is written to config and nothing changes on the node:
blah2, retina-tracker and the stream carry the full span whichever view is drawn. The buttons
are labelled with the ranges they draw, so they stay truthful on a node with a different span or
after the narrow range is changed.

The gear in the page header opens Tracker settings, a native `<dialog>` (the page does not load
Bootstrap's JavaScript), with one field: the narrow range as a whole number of hertz, from 1 to
half the node's span (or up to 100,000 when the span is unknown). Saving switches the view to
narrow, since setting a range is asking to see it. Reset to ±300 refills the field without
saving.

Narrow is clipped to the node's span so it never draws Doppler the node cannot see. If the span
lies wholly to one side of the narrow band, narrow falls back to the node's bounds.

The plot's `uirevision` includes the view and the narrow range. A viewer's zoom survives stream
updates as before, but switching view or saving a new range redraws at the new range rather than
keeping the old zoom (the delay axis included). Double-click resets to the current view's range.

## Out-of-view alert

Narrowing the plot must not hide activity, so when the narrow view is on, the toolbar shows a
warning pill such as "1 track outside ±300 Hz" with a Show full span button that switches to
Full. Each such track also gets an Outside view chip in the rail.

A track counts when any of its points in the current view window lies beyond the narrow range.
Removed tracks never count. Hidden and filtered-out tracks do, because the alert reports what
the node saw rather than what the rail happens to list. The alert never fires in Full, where
nothing can be outside.

It counts tracks, not raw detections. Widening the processed span from ±300 to ±1000 Hz gives
CFAR about 3.3 times as many cells, and most of the extra detections are false alarms spread
evenly across the band, so an alert on raw detections would be lit permanently. A track needs
repeated detections that line up, which is the evidence worth interrupting someone for.

## What is plotted

The scope plots bistatic range (km) against Doppler (Hz).

**Detections** come in three classes, all reported by retina-tracker. It knows which
detections a confirmed track claimed and which it dropped at the SNR gate, so the page does not
infer any of it. (An earlier version compared timestamps and delays to guess, which could only
approximate an answer the tracker already had.)

| Class | Layer button | Meaning |
| --- | --- | --- |
| `associated` | (All only) | Claimed by a confirmed track. |
| `unassociated` | Unassoc | Considered by the tracker, and no confirmed track claimed it: its miss list. |
| `below_snr` | Below SNR | Dropped at the SNR gate before the tracker considered it. |

The Detections layer control shows All, one class, or Off. Detections are coloured by SNR on a
fixed 0-20 dB blue ramp (clamped at both ends), drawn as a key in the plot's footer.

**Tracks** are drawn as lines with markers over the detections, one colour per track. The
colour comes from a twelve-hue wheel indexed by a hash of the track id, so it is fixed on first
sighting and never recomputed when an older track ages out. Collisions are expected past twelve
live tracks and are tolerated: the rail names every track and hovering a row isolates it, so
colour is a pointing device rather than the thing carrying identity.

The Tracks control turns the overlay off, leaving only the detections: what the node recorded
before anything decided what it meant. The rail still lists the tracks; turning the overlay off
is about the picture, not forgetting they exist.

Hovering a rail row isolates that track: it is drawn on its own trace at full opacity on top,
and everything else fades (detections to 0.12, other tracks to 0.09). The fade values are built
into the trace specs by `render()` rather than applied with a restyle afterwards, because a
restyle is a full redraw of its own and would double the cost of every update.

The legend is off. The rail owns identity, which lets the plot keep its full height and puts the
SNR scale under the plot instead of in a right-hand strip.

## Drawing performance

Plotly redraws the whole scene on every call, so the page is built to keep that cost bounded
however busy the sky is. Every point the node sent is still drawn, except where the untracked
cap below applies.

- **Tracks share traces by hue.** Plotly's cost grows with the number of traces, so one trace
  per track got slower as the sky filled and stopped drawing entirely on a node whose
  interference minted thousands of tracks. With twelve hues, twelve traces carry any number of
  tracks: measured flat at about 300 ms from 133 tracks to 2000, against 398 ms and 934 ms for a
  trace apiece. A `null` between tracks keeps their lines from joining, and the track id rides in
  `customdata` so hover still names the track.
- **Detections are banded by SNR.** Plotly maps a per-point colour array through the colour
  scale on every redraw; at eighty thousand points that was nearly the whole cost of the scope
  (217 ms against 19 ms for the same points in a flat colour). The page precomputes 24 bands
  (`SNR_BANDS`) across 0-20 dB, 0.83 dB a step, finer than an SNR figure from this radar means, so
  the ramp still reads as continuous. Bands are pooled across whichever classes are shown, so
  the scope never carries more than 24 detection traces.
- **"All" is three traces, not a concatenation**, so the page does not rebuild a copy of every
  point on every frame.
- **Untracked detections are capped.** `unassociated` plus `below_snr` are held in a ring of the
  newest `MAX_UNTRACKED` (25,000) points. How many of those a node makes depends on its sky and
  interference, and on a bad node it runs away; the scope's cost is nearly linear in points, so
  an unbounded backdrop stops the page drawing at all. A ring rather than a stride means what is
  shown is always the most recent data. `associated` detections are exempt, because they are the
  evidence a track is built on and dropping them would leave a track drawn over a backdrop that
  no longer contains what produced it. When the cap is active the footer says "untracked capped
  at 25,000", because a backdrop that silently stops going back as far as the window claims looks
  exactly like a sky that went quiet.
- **At most one draw per animation frame** (`scheduleRender`). Messages land about once a second
  and a draw over a wide window can take longer, so without coalescing each message queues its
  own redraw and the page falls permanently behind. Coalescing turns "frozen" into "a frame or
  two late".
- **The status line is updated before the draw**, from what has arrived. Updating it after the
  draw is why a page struggling to draw used to report "connecting" rather than what it was
  struggling with.
- `Plotly.react` (not `newPlot`) with a stable `uirevision` keeps zoom, pan and axis ranges across
  updates.

## The track rail

The rail beside the scope lists every track in the window.

- **Sort:** newest (last seen), longest (duration), strongest (peak SNR), most points.
- **Filter:** All, ADS-B (has `adsb_hex`), Flagged (`is_anomalous`), Unmatched (no `adsb_hex`).
- **Row:** colour swatch, id, duration, point count, peak SNR, age ("live" under 15 s), an
  ADS-B chip with the hex when matched, and an Outside view chip when the narrow Doppler view
  cannot show all of the track (see [Out-of-view alert](#out-of-view-alert)).
- **Flagged tracks get a dot, not a badge.** retina-tracker's anomaly detector is not trusted
  yet, and a solid label would be read as a verdict. The expanded row lists the anomaly types
  marked "(provisional)".
- **Clicking a row** expands a detail panel: first and last seen, length, peak velocity and
  shadow fraction. In the wedge view it also [replays the track](#replaying-a-track).
- **Hide** (eye) removes a track from the scope but keeps its row. **Remove** (bin) drops the row
  too; removals are per browser and can be undone with Restore in the rail footer.

View window, layer, track overlay, sort, filter, Doppler view (`tracker.dopView`), the
narrow range (`tracker.narrowHz`) and the display (`tracker.display`) are remembered in `localStorage` (`tracker.*` keys) and
restored into the controls before connecting. They are per browser: nothing about the page's
presentation is stored on the node.

The page is full width with a viewport-clamped height, since both the plot and the rail want
every pixel; below 900 px the rail stacks under the plot.

## Wedge view

The Display control in the page header switches the panel between the Delay / Doppler scope and
the wedge view: a map of where this node is **proven** to see, built only from ADS-B-verified
detections, with the tracks drawn on it. Nothing in it comes from a declared antenna aim. The
rail, the view window and the Tracks on/off control are shared by both displays; the Doppler,
Detections and Clear buffer controls belong to the scope and are hidden in the wedge view, and
the wedge view adds a "24 h max-hold" chip and Reset wedge. The panel's `data-display`
attribute drives which controls and which body show (`.dd-only`, `.wedge-only`). The scope is
not redrawn while hidden, since a Plotly redraw costs the same whether or not it is seen.

Every shape arrives ready made from retina-tracker, which owns the maths
(retina-tracker's `docs/coverage-wedge.md`). The page draws what it is given:

| Layer | Source | Drawn as |
| --- | --- | --- |
| The wedge | `GET /tracker/coverage`, polled every 30 s | Green lobes, each 5 degree slice drawn edge to edge at its held range |
| Receiver, transmitter, baseline | the same | RX ring, TX triangle, dashed line |
| Labelled tracks | the stream's `lat`/`lon` columns | A trail through the verified positions, broken where they stop for over 15 s; an aircraft glyph with the hex while live (newest point under 15 s old), a small ring at the end otherwise |
| Unlabelled tracks | `GET /tracker/coverage/arcs?window=`, polled every 5 s | The delay ellipse at the newest point: solid inside the wedge, faint and dashed outside it |

A slice holds the second-furthest range that two different aircraft reached in the last 24
hours, so one wrong ADS-B match cannot push the wedge out, and the hold slides rather than
resetting on a schedule. An unlabelled arc is solid where it crosses proven coverage, which is
where the node is known to see, not where the target is known to be: the target can be anywhere
on the arc. Arcs are flat (target altitude is ignored), so a labelled aircraft sits a few
kilometres off its own arc, more at altitude and close in.

The footer has the legend and the wedge's evidence: "Last 24 h", or "Since reset at ..." or
"Since ..." when the record is younger than the hold (judged on the node's clock, `now_ms`),
with the number of aircraft and proven slices.

A notice over the map explains an empty or missing wedge rather than showing a bare map:
before any slice is proven, after a reset (manual, or because the receiver or transmitter
moved), when the node has no receiver and transmitter position, when the sidecar answers 404 or
503 (a retina-tracker without the wedge), and when it is unreachable (502). The proxies relay
the sidecar's status so the page can tell these apart.

The basemap is CARTO's light tiles with the same key handling as the setup wizard
(`withCartoKey` is duplicated in `static/tracker-wedge.js` and must match `static/setup.js`; see
[setup-wizard.md](setup-wizard.md#basemap-key)). The map fits the receiver, transmitter and wedge
(at least 60 km around the receiver) the first time it has a geometry, and keeps the viewer's
pan and zoom after that.

### Replaying a track

In the wedge view, clicking a track's row expands it and replays its travel through the wedge
over a fixed 10 s, whatever its length, so a short pass and a long one are both watchable.
Clicking the row again stops a running replay, or replays a finished one; clicking another
track switches to it. There are no other controls. A panel over the map gives the track, the
replay clock (node time), progress, and elapsed against total.

The page fetches `GET /tracker/coverage/track/<id>` once per replay: at most 240 arcs spread
evenly over the track, about 3 KB each. A labelled track's aircraft moves along its trail, drawn
solid behind it and dashed ahead, with the measured arc at that moment. An unlabelled track's arc
sweeps with an afterglow of four fading earlier arcs. Every other track fades while a replay
runs. The replay draws on its own Leaflet canvas, so a frame repaints only the replay.

Switching to the scope stops the replay. The Delay / Doppler view has no replay cursor: Plotly
redraws the whole scene on every call (see [Drawing performance](#drawing-performance)), which
an animation would do every frame.

### Resetting the wedge

Reset wedge opens a confirm dialog and POSTs `/tracker/coverage/reset`, which calls the
sidecar's `/coverage/reset`. It clears the wedge only: tracking, the history and the tracks on the
page are untouched, which is why it is separate from Clear buffer. Use it after moving or
re-aiming the antenna; otherwise the old lobe would hold for up to a day. The sidecar also
restarts the wedge by itself when the receiver or transmitter position changes. If the sidecar
does not confirm, the dialog says nothing was cleared and stays open.

## Clearing the buffer

Clear buffer POSTs `/tracker/clear`, which calls the sidecar's `/history/clear`. It wipes the
record the page is drawn from without touching the tracker: tracking keeps running, so an
aircraft still overhead reappears within a few events. This lives in the sidecar because the
record does. The page does no local reset: clearing invalidates every cursor on the node, so
the stream's next message is a fresh empty snapshot.

## Legacy URL

The page was called Tracker Preview and nodes have been in the field under that name long
enough for `/tracker-preview` to be bookmarked, so it redirects permanently to `/tracker`
(keeping sub-paths and the query string). The redirect is a 308 rather than a 301 because
`/clear` is a POST and a 301 would let a browser turn it into a GET.
