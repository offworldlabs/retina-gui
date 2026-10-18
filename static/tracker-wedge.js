// The Tracker page's wedge view: the node's ADS-B-proven coverage on a map,
// its tracks over it, and a replay of one track through it. Every shape
// (wedge lobes, arcs) arrives ready made from retina-tracker; this only draws.
// See docs/features/tracker.md#wedge-view
(function () {
    'use strict';

    // Must match withCartoKey in static/setup.js: a wrong parameter name fails
    // silently with a watermarked tile. See docs/features/setup-wizard.md#basemap-key
    var CARTO_HOST = 'basemaps.cartocdn.com';

    function withCartoKey(url, key) {
        if (!key || url.indexOf(CARTO_HOST) === -1) return url;
        return url + (url.indexOf('?') === -1 ? '?' : '&') + 'key=' + encodeURIComponent(key);
    }

    var COVERAGE_POLL_MS = 30000;
    var ARCS_POLL_MS = 5000;
    var REPLAY_MS = 10000;
    var LIVE_MS = 15000;
    var TRAIL_GAP_MS = 15000;
    var GLOW = [0.45, 0.3, 0.18, 0.1];
    var GLOW_STEP = 5;
    var DIM_REPLAY = 0.16;
    var DIM_HOVER = 0.12;
    var DEFAULT_VIEW_M = 120000;
    var WEDGE_EDGE = '#15803d';
    var WEDGE_FILL = '#16a34a';
    var SITE_INK = '#22252c';
    var SPAN_INK = '#6f6c65';

    function esc(s) {
        return String(s).replace(/[&<>"]/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
        });
    }

    function hhmm(ms) { return new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }); }

    // For pointing the aircraft glyph only; nothing is measured from it.
    function headingDeg(a, b) {
        var p1 = a[0] * Math.PI / 180, p2 = b[0] * Math.PI / 180;
        var dl = (b[1] - a[1]) * Math.PI / 180;
        var x = Math.sin(dl) * Math.cos(p2);
        var y = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl);
        return (Math.atan2(x, y) * 180 / Math.PI + 360) % 360;
    }

    // A track's verified positions as [{t, ll}], in time order.
    function positionsOf(trk) {
        var out = [];
        if (!trk || !trk.lat) return out;
        for (var i = 0; i < trk.t.length; i++) {
            if (trk.lat[i] != null && trk.lon[i] != null) out.push({ t: trk.t[i], ll: [trk.lat[i], trk.lon[i]] });
        }
        return out;
    }

    // Split where positions stop for longer than a coast, so a trail never
    // draws a straight line across a stretch nobody verified.
    function segmentsOf(points) {
        var segs = [], cur = null;
        for (var i = 0; i < points.length; i++) {
            if (!cur || points[i].t - points[i - 1].t > TRAIL_GAP_MS) { cur = []; segs.push(cur); }
            cur.push(points[i].ll);
        }
        return segs;
    }

    function lastAtOrBefore(items, t) {
        var lo = 0, hi = items.length;
        while (lo < hi) {
            var mid = (lo + hi) >> 1;
            if (items[mid].t <= t) lo = mid + 1; else hi = mid;
        }
        return lo - 1;
    }

    function getJson(url, init) {
        return fetch(url, init).then(function (r) {
            return r.json().catch(function () { return {}; }).then(function (body) {
                return { status: r.status, body: body };
            });
        });
    }

    function create(opts) {
        var map = null;
        var layers = {};
        var replayRenderer = null;
        var coverage = null;
        var coverageError = null;
        var latestArcs = {};
        var timers = [];
        var visible = false;
        var fittedTo = null;
        var replay = null;
        var rows = [];
        var view = { hidden: {}, hover: null, tracksOn: true };

        function ensureMap() {
            if (map) return;
            map = L.map(opts.mapEl, { preferCanvas: true });
            L.tileLayer(withCartoKey('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', opts.cartoKey), {
                attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
                maxZoom: 19
            }).addTo(map);
            // Its own canvas, so a replay frame repaints only the replay.
            replayRenderer = L.canvas();
            ['wedge', 'sites', 'tracks', 'replay'].forEach(function (k) { layers[k] = L.layerGroup().addTo(map); });
            map.setView([0, 0], 2);
        }

        // ── Coverage ────────────────────────────────────────────────────

        function refreshCoverage() {
            return getJson('/tracker/coverage').then(function (res) {
                coverage = res.status === 200 ? res.body : null;
                coverageError = res.status === 200 ? null : (res.status === 502 ? 'unreachable' : 'unsupported');
            }).catch(function () {
                coverage = null;
                coverageError = 'unreachable';
            }).then(drawCoverage);
        }

        function siteMarker(pos, label) {
            var icon = L.divIcon({
                className: 'wedge-plane', iconSize: [18, 18], iconAnchor: [9, 9],
                html: label === 'RX'
                    ? '<svg width="18" height="18" viewBox="0 0 18 18"><circle cx="9" cy="9" r="7.5" fill="none" stroke="' + SITE_INK + '" stroke-width="1.5"/><circle cx="9" cy="9" r="3.5" fill="' + SITE_INK + '"/></svg><span style="color:' + SITE_INK + '">RX</span>'
                    : '<svg width="18" height="18" viewBox="0 0 18 18"><path d="M9,2 L15,16 L3,16 Z" fill="#fff" stroke="' + SITE_INK + '" stroke-width="1.5" stroke-linejoin="round"/></svg><span style="color:' + SITE_INK + '">TX</span>'
            });
            return L.marker([pos.lat, pos.lon], { icon: icon, interactive: false, keyboard: false });
        }

        function drawCoverage() {
            if (!map) return;
            layers.wedge.clearLayers();
            layers.sites.clearLayers();
            var g = coverage && coverage.enabled ? coverage.geometry : null;
            if (g) {
                if ((coverage.span_ring || []).length) {
                    L.polyline(coverage.span_ring, { color: SPAN_INK, weight: 1.2, opacity: 0.7, dashArray: '6 5', interactive: false })
                        .addTo(layers.wedge);
                }
                (coverage.lobes || []).forEach(function (ring) {
                    L.polygon(ring, { color: WEDGE_EDGE, weight: 1.4, fillColor: WEDGE_FILL, fillOpacity: 0.12, interactive: false })
                        .addTo(layers.wedge);
                });
                L.polyline([[g.rx.lat, g.rx.lon], [g.tx.lat, g.tx.lon]],
                           { color: '#9a978f', weight: 1, dashArray: '2 4', interactive: false }).addTo(layers.sites);
                siteMarker(g.rx, 'RX').addTo(layers.sites);
                siteMarker(g.tx, 'TX').addTo(layers.sites);
                fit(g);
            }
            renderNotice();
            renderStat();
        }

        // Fit to the lobes once there are any, else a default box around the
        // receiver; refit only on that change, so the viewer's own pan and
        // zoom are kept. See docs/features/tracker.md#wedge-view
        function fit(g) {
            var lobes = coverage.lobes || [];
            var target = lobes.length ? 'lobes' : 'default';
            if (fittedTo === target || fittedTo === 'lobes') return;
            var bounds = L.latLngBounds([[g.rx.lat, g.rx.lon], [g.tx.lat, g.tx.lon]]);
            if (lobes.length) {
                lobes.forEach(function (ring) { bounds.extend(L.latLngBounds(ring)); });
            } else {
                bounds.extend(L.latLng(g.rx.lat, g.rx.lon).toBounds(DEFAULT_VIEW_M));
            }
            map.fitBounds(bounds, { padding: [24, 24] });
            fittedTo = target;
        }

        function renderNotice() {
            var text = '';
            if (coverageError === 'unreachable') {
                text = 'Coverage is unavailable: the tracker is unreachable.';
            } else if (coverageError) {
                text = 'This node’s tracker does not serve a coverage wedge yet. It needs a newer retina-tracker.';
            } else if (coverage && !coverage.enabled) {
                text = 'No wedge: the receiver and transmitter positions are not set. Choose a tower in Setup.';
            } else if (coverage && !coverage.proven_bins) {
                var rule = ' Each ' + coverage.bin_deg + '° slice is drawn once two aircraft have reached it.'
                         + ' Until then every arc is drawn faint.';
                if (coverage.reset_ms && coverage.reset_reason === 'geometry') {
                    text = 'The wedge restarted at ' + hhmm(coverage.reset_ms) + ' because the receiver or transmitter moved.'
                         + ' It rebuilds as ADS-B-verified aircraft pass through.' + rule;
                } else if (coverage.reset_ms) {
                    text = 'Wedge reset at ' + hhmm(coverage.reset_ms) + '. It rebuilds as ADS-B-verified aircraft pass through.' + rule;
                } else {
                    text = 'No proven coverage yet. The wedge builds as ADS-B-verified aircraft pass through.' + rule;
                }
            }
            opts.noticeEl.textContent = text;
            opts.noticeEl.hidden = !text;
        }

        function renderStat() {
            if (!coverage || !coverage.enabled) {
                opts.statEl.textContent = '';
                return;
            }
            var full = coverage.since_ms <= coverage.now_ms - coverage.hold_s * 1000 + 1000;
            var label = full ? 'Last ' + Math.round(coverage.hold_s / 3600) + ' h'
                      : (coverage.reset_ms && coverage.reset_ms >= coverage.since_ms ? 'Since reset at ' : 'Since ')
                        + hhmm(coverage.since_ms);
            opts.statEl.textContent = label + ' · ' + coverage.aircraft + ' aircraft · '
                + coverage.proven_bins + ' of ' + coverage.held_km.length + ' slices proven'
                + (coverage.limited_bins ? ' \u00b7 ' + coverage.limited_bins + ' at the processed range limit' : '');
        }

        // ── Tracks ──────────────────────────────────────────────────────

        function refreshArcs() {
            return getJson('/tracker/coverage/arcs?window=' + opts.windowS()).then(function (res) {
                latestArcs = {};
                if (res.status === 200) {
                    (res.body.arcs || []).forEach(function (a) { latestArcs[a.id] = a; });
                }
                drawTracks();
            }).catch(function () { /* the next poll retries */ });
        }

        function opacityFor(id) {
            if (replay) return replay.id === id ? 1 : DIM_REPLAY;
            if (view.hover && view.hover !== id) return DIM_HOVER;
            return 1;
        }

        function drawArc(arc, color, op, group, renderer) {
            if (!arc) return;
            arc.faint.forEach(function (line) {
                L.polyline(line, { color: color, weight: 1.3, opacity: 0.4 * op, dashArray: '3 4', interactive: false, renderer: renderer })
                    .addTo(group);
            });
            arc.solid.forEach(function (line) {
                L.polyline(line, { color: color, weight: 2.4, opacity: 0.95 * op, interactive: false, renderer: renderer })
                    .addTo(group);
            });
        }

        function planeMarker(ll, heading, color, label) {
            var icon = L.divIcon({
                className: 'wedge-plane', iconSize: [16, 16], iconAnchor: [8, 8],
                html: '<svg width="16" height="16" viewBox="-8 -8 16 16"><path d="M0,-7 L4.5,6 L0,3 L-4.5,6 Z" transform="rotate('
                    + heading.toFixed(0) + ')" fill="' + color + '" stroke="#fff" stroke-width="1.2" stroke-linejoin="round"/></svg>'
                    + (label ? '<span style="color:' + color + '">' + esc(label) + '</span>' : '')
            });
            return L.marker(ll, { icon: icon, interactive: false, keyboard: false });
        }

        function drawTrail(r, color, op) {
            var points = positionsOf(opts.getTrack(r.id));
            if (!points.length) return;
            segmentsOf(points).forEach(function (seg) {
                L.polyline(seg, { color: color, weight: 2.4, opacity: 0.95 * op, interactive: false }).addTo(layers.tracks);
            });
            var last = points[points.length - 1];
            if (opts.nowMs() - last.t < LIVE_MS && points.length > 1) {
                var prev = points[points.length - 2];
                planeMarker(last.ll, headingDeg(prev.ll, last.ll), color, r.meta.adsb_hex)
                    .setOpacity(op).addTo(layers.tracks);
            } else {
                L.circleMarker(last.ll, { radius: 3.2, color: color, weight: 1.6, fillColor: '#fff', fillOpacity: op, opacity: op, interactive: false })
                    .addTo(layers.tracks);
            }
        }

        function drawTracks() {
            if (!map) return;
            layers.tracks.clearLayers();
            if (!view.tracksOn) return;
            rows.forEach(function (r) {
                if (view.hidden[r.id] || (replay && replay.id === r.id)) return;
                var color = opts.colorFor(r.id), op = opacityFor(r.id);
                if (r.meta.adsb_hex) drawTrail(r, color, op);
                else drawArc(latestArcs[r.id] && latestArcs[r.id].arc, color, op, layers.tracks);
            });
        }

        // ── Replay ──────────────────────────────────────────────────────

        function replayTrack(id) {
            stopReplay();
            var trk = opts.getTrack(id);
            if (!map || !trk || !trk.t.length) return;
            var points = positionsOf(trk);
            replay = {
                id: id,
                color: opts.colorFor(id),
                labelled: !!(trk.meta && trk.meta.adsb_hex),
                hex: trk.meta ? trk.meta.adsb_hex : null,
                t0: trk.t[0],
                t1: trk.t[trk.t.length - 1],
                points: points,
                arcs: [],
                start: null,
                done: false,
                raf: null,
                failed: false
            };
            var mine = replay;
            drawTracks();
            showHud(0, mine.t0);
            getJson('/tracker/coverage/track/' + encodeURIComponent(id)).then(function (res) {
                if (replay !== mine) return;
                mine.arcs = res.status === 200 ? (res.body.arcs || []).filter(function (a) { return a.arc; }) : [];
                mine.failed = res.status !== 200;
            }).catch(function () {
                mine.failed = true;
            }).then(function () {
                if (replay !== mine) return;
                mine.start = performance.now();
                tick();
            });
        }

        function tick() {
            if (!replay) return;
            var p = Math.min(1, (performance.now() - replay.start) / REPLAY_MS);
            var t = replay.t0 + p * (replay.t1 - replay.t0);
            drawFrame(t);
            if (p < 1) {
                replay.raf = requestAnimationFrame(tick);
            } else {
                replay.done = true;
            }
            showHud(p, t);
        }

        function drawFrame(t) {
            layers.replay.clearLayers();
            if (!view.tracksOn) return;
            var r = replay, renderer = replayRenderer;
            var i = lastAtOrBefore(r.arcs, t);
            if (!r.labelled) {
                for (var g = GLOW.length; g >= 1; g--) {
                    var k = i - g * GLOW_STEP;
                    if (k >= 0) drawArc(r.arcs[k].arc, r.color, GLOW[g - 1], layers.replay, renderer);
                }
            }
            if (i >= 0) drawArc(r.arcs[i].arc, r.color, 1, layers.replay, renderer);
            if (!r.labelled || !r.points.length) return;

            var j = lastAtOrBefore(r.points, t);
            var done = r.points.slice(0, Math.max(j + 1, 0));
            var rest = r.points.slice(Math.max(j, 0));
            segmentsOf(rest).forEach(function (seg) {
                L.polyline(seg, { color: r.color, weight: 1.6, opacity: 0.35, dashArray: '2 3', interactive: false, renderer: renderer })
                    .addTo(layers.replay);
            });
            segmentsOf(done).forEach(function (seg) {
                L.polyline(seg, { color: r.color, weight: 2.4, opacity: 0.95, interactive: false, renderer: renderer })
                    .addTo(layers.replay);
            });
            if (j < 0) return;
            var a = r.points[j], b = r.points[Math.min(j + 1, r.points.length - 1)];
            var f = b.t > a.t ? Math.min(1, (t - a.t) / (b.t - a.t)) : 0;
            var ll = [a.ll[0] + (b.ll[0] - a.ll[0]) * f, a.ll[1] + (b.ll[1] - a.ll[1]) * f];
            var from = j > 0 ? r.points[j - 1].ll : a.ll;
            var heading = b !== a ? headingDeg(a.ll, b.ll) : headingDeg(from, a.ll);
            planeMarker(ll, heading, r.color, r.hex).addTo(layers.replay);
        }

        function showHud(p, t) {
            var r = replay;
            opts.hud.el.hidden = !r;
            if (!r) return;
            opts.hud.dot.style.background = r.color;
            opts.hud.bar.style.background = r.color;
            opts.hud.bar.style.width = (p * 100).toFixed(1) + '%';
            opts.hud.title.textContent = (r.done ? 'Replay finished · ' : 'Replaying ') + r.id;
            opts.hud.clock.textContent = new Date(t).toLocaleTimeString();
            var sub = opts.fmtDur((t - r.t0) / 1000) + ' of ' + opts.fmtDur((r.t1 - r.t0) / 1000)
                    + (r.done ? ' · click the track again to replay' : ' · click the track again to stop');
            if (r.failed && !r.labelled) sub = 'No arcs could be fetched for this track.';
            opts.hud.sub.textContent = sub;
        }

        function stopReplay() {
            if (!replay) return;
            if (replay.raf) cancelAnimationFrame(replay.raf);
            replay = null;
            if (map) layers.replay.clearLayers();
            showHud(0, 0);
            drawTracks();
        }

        // ── Lifecycle ───────────────────────────────────────────────────

        function show() {
            visible = true;
            ensureMap();
            map.invalidateSize();
            refreshCoverage();
            refreshArcs();
            timers.forEach(clearInterval);
            timers = [setInterval(refreshCoverage, COVERAGE_POLL_MS), setInterval(refreshArcs, ARCS_POLL_MS)];
        }

        function hide() {
            visible = false;
            timers.forEach(clearInterval);
            timers = [];
            stopReplay();
        }

        return {
            show: show,
            hide: hide,
            render: function (nextRows, nextView) {
                rows = nextRows;
                view = nextView;
                if (visible) drawTracks();
            },
            replay: replayTrack,
            stopReplay: stopReplay,
            isPlaying: function (id) { return !!replay && replay.id === id && !replay.done; },
            refresh: function () {
                if (!visible) return;
                refreshCoverage();
                refreshArcs();
            }
        };
    }

    window.TrackerWedge = { create: create };
})();
