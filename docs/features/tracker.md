# Tracker page

The Tracker page (`/tracker`, formerly Tracker Preview) plots what a node's radar has detected
and which of those detections retina-tracker has built into tracks. retina-gui holds no tracker
data: the record lives in the retina-tracker sidecar container, which serves it over a loopback
SSE endpoint, and retina-gui proxies that stream to the browser and draws it with Plotly.
Separately, `RetinaTrackerClient` tails the sidecar's track-event file and drives its control
surface for Auto-Calibrate.

## Where it lives

| File | Role |
| --- | --- |
| [src/routes/tracker.py](../../src/routes/tracker.py) | The `/tracker` blueprint (page, SSE proxy, one-shot snapshot, clear) and the `/tracker-preview` redirect. Computes the plot's axis bounds from the merged config. |
| [templates/tracker.html](../../templates/tracker.html) | The page: controls, the Plotly scope, the track rail, and the EventSource client. |
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

The page talks only to retina-gui (`/tracker/events`), never to the sidecar's port. Proxying
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
parallel columns `t` (ms), `delay`, `doppler` and `snr`, in timestamp order. Each track also has
`meta`: `adsb_hex`, `is_anomalous`, `anomaly_types`, `length`, `max_velocity_ms` and
`shadow_fraction`. On a delta, a known track's columns are appended and its `meta` replaced.

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

`SPEED_OF_LIGHT` is mirrored rather than imported for the same reason as the window bounds.

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
- **Row:** colour swatch, id, duration, point count, peak SNR, age ("live" under 15 s), and an
  ADS-B chip with the hex when matched.
- **Flagged tracks get a dot, not a badge.** retina-tracker's anomaly detector is not trusted
  yet, and a solid label would be read as a verdict. The expanded row lists the anomaly types
  marked "(provisional)".
- **Clicking a row** expands a detail panel: first and last seen, length, peak velocity and
  shadow fraction.
- **Hide** (eye) removes a track from the scope but keeps its row. **Remove** (bin) drops the row
  too; removals are per browser and can be undone with Restore in the rail footer.

View window, layer, track overlay, sort and filter are remembered in `localStorage`
(`tracker.*` keys) and restored into the controls before connecting.

The page is full width with a viewport-clamped height, since both the plot and the rail want
every pixel; below 900 px the rail stacks under the plot.

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
