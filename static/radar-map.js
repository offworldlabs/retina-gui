// The Passive Radar page: blah2's delay-Doppler map, live or max-hold, with its
// detections and ADS-B truth drawn over it. A port of blah2's plot_map.js, kept
// close to it on purpose. See docs/features/radar.md#what-was-ported
(function () {
    'use strict';

    // Which map each view draws. See docs/features/radar.md#the-view-toggle
    var MAPS = { live: '/radar/data/map', maxhold: '/radar/data/maxhold' };
    var view = MAPS[window.RADAR_VIEW] ? window.RADAR_VIEW : 'live';
    var urlMap = MAPS[view];
    var urlTimestamp = '/radar/data/timestamp';
    var urlDetection = '/radar/data/detection';
    var urlAdsbLink = '/radar/data/adsb';
    var urlConfig = '/radar/data/config';

    var timestamp = -1;
    var shape = '';            // the drawn map's rows x columns; a new shape rebuilds the plot

    // The node's own extent, the same bounds the Tracker draws, so the axes do not
    // move with the data. null on an axis the config does not state.
    // See docs/features/radar.md#axes
    var AXES = window.RADAR_AXES || {};
    // Fixed, and the Tracker's detection scale, so a colour means the same SNR in
    // every frame, in both views and on both pages. See docs/features/radar.md#colour-scale
    var SNR_RANGE = [0, 20];
    // The full extent: the node's, widened to take in the whole map once one
    // arrives (see cover()). null on an axis nothing has stated yet.
    var full = { x: AXES.delay || null, y: AXES.doppler || null };

    // ── Doppler and delay views, and units ───────────────────────────────
    // The Tracker's Narrow/Full toggles, gear and km/mi toggle, from
    // static/scope-controls.js, so the two pages work the same way. Saved in
    // this browser under radar.*. Presentation only: blah2 processes its full
    // span whichever is drawn. See docs/features/radar.md#doppler-and-delay-views
    var state = {};
    var views = ScopeControls.create({
        prefix: 'radar',
        state: state,
        extent: function () { return full; },
        onViewChange: function () { showViews(); renderOutsideAlert(); },
        onUnitsChange: function (u) {
            U = u;
            layout.xaxis.title.text = 'Bistatic Range (' + U.dist + ')';
            shape = null;
            if (lastMap) drawMap(lastMap);
        }
    });
    // km or mi for this browser (static/units.js). The maps, `full` and the
    // narrow range stay in km; only what is drawn and written converts.
    var U = views.units();
    var narrowRange = views.narrowRange, dopplerRange = views.dopplerRange;
    var narrowDelayRange = views.narrowDelayRange, displayDelayRange = views.displayDelayRange;
    var labelRangeButtons = views.labelRangeButtons;
    var setDopView = views.setDopView, setDelView = views.setDelView;
    var fmtHzRange = ScopeControls.fmtHzRange;

    // A failed request leaves the plot as it was and the next tick tries again.
    // Only the request is caught: an error while drawing still reaches the console.
    // A redirect to the login page means the session has expired.
    function get(url, read, done, failed) {
        fetch(url).then(function (r) {
            if (r.redirected && new URL(r.url).pathname.indexOf('/login') === 0) throw 'signedout';
            if (r.status === 401 || r.status === 403) throw 'signedout';
            if (!r.ok) throw 'unreachable';
            return read(r);
        }).then(done, function (why) { if (failed) failed(why === 'signedout' ? why : 'unreachable'); });
    }
    function getText(url, done, failed) { get(url, function (r) { return r.text(); }, done, failed); }
    function getJson(url, done) { get(url, function (r) { return r.json(); }, done); }

    // ── Status line ──────────────────────────────────────────────────────
    // Says whether the picture is current, so a stopped radar or an expired
    // session never passes for a quiet sky. See docs/features/radar.md#the-status-line
    var STALE_MS = 5000;
    var statusEl = document.getElementById('radarStatus');
    var lastFrameAt = 0;       // browser time the last new map was drawn
    var failure = null;        // why the last timestamp request failed, or null

    function ago(ms) {
        var s = Math.round(ms / 1000);
        return s < 90 ? s + 's' : Math.round(s / 60) + 'm';
    }

    function renderStatus() {
        var text, cls;
        var since = Date.now() - lastFrameAt;
        if (failure === 'signedout') {
            text = 'signed out &middot; reload the page to sign in'; cls = 'lost';
        } else if (failure) {
            text = 'cannot reach the radar &middot; retrying'; cls = 'lost';
        } else if (!lastFrameAt) {
            text = 'connecting&hellip;'; cls = 'stale';
        } else if (since > STALE_MS) {
            text = 'no new frame for ' + ago(since); cls = 'stale';
        } else {
            text = 'live &middot; updated ' + ago(since) + ' ago'; cls = '';
        }
        statusEl.className = 'scope-status' + (cls ? ' ' + cls : '');
        statusEl.innerHTML = '<span class="dot"></span>' + text;
    }

    // The extent of the map being drawn, so a narrow or wide span is visible.
    var extentEl = document.getElementById('radarExtent');
    function renderExtent(data) {
        var d0 = data.doppler[0], d1 = data.doppler[data.doppler.length - 1];
        var k0 = data.delay[0], k1 = data.delay[data.delay.length - 1];
        var dop = d0 === -d1 ? '\u00b1' + d1 + ' Hz' : d0 + ' to ' + d1 + ' Hz';
        extentEl.textContent = dop + ' \u00b7 ' + U.distOf(k0).toFixed(1) + ' to ' + U.distOf(k1).toFixed(1) + ' ' + U.dist;
    }

    // get truth flag
    var isTruth = false;
    getJson(urlConfig, function (data_config) {
        if (data_config.truth.adsb.enabled === true) {
            isTruth = true;
        }
    });

    // setup plotly
    var layout = {
        autosize: true,
        margin: {
            l: 50,
            r: 50,
            b: 50,
            t: 10,
            pad: 0
        },
        hoverlabel: {
            namelength: 0
        },
        plot_bgcolor: "#ffffff",
        paper_bgcolor: "#ffffff",
        font: {
            family: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
            color: "#1a1a18"
        },
        annotations: [],
        displayModeBar: false,
        xaxis: {
            title: {
                text: 'Bistatic Range (' + U.dist + ')',
                font: {
                    size: 16
                }
            },
            ticks: '',
            side: 'bottom'
        },
        yaxis: {
            title: {
                text: 'Bistatic Doppler (Hz)',
                font: {
                    size: 16
                }
            },
            ticks: '',
            ticksuffix: ' ',
            autosize: false,
            categoryorder: "total descending"
        },
        showlegend: false
    };
    // Plotly's own double-click goes back to the ranges the plot was built
    // with, not the view chosen since, so the page handles it (bindPlot).
    var config = {
        responsive: true,
        displayModeBar: false,
        doubleClick: false
    };

    // The chosen views' ranges go into the layout before a plot is built.
    function layoutRanges() {
        var x = displayDelayRange(), y = dopplerRange();
        layout.xaxis.range = x ? x.slice() : undefined;
        layout.xaxis.autorange = !x;
        layout.yaxis.range = y ? y.slice() : undefined;
        layout.yaxis.autorange = !y;
    }

    // Draw the chosen views' ranges: on a toggle, Reset View and a double-click.
    function showViews() {
        var x = displayDelayRange(), y = dopplerRange(), update = {};
        if (x) update['xaxis.range'] = x.slice(); else update['xaxis.autorange'] = true;
        if (y) update['yaxis.range'] = y.slice(); else update['yaxis.autorange'] = true;
        Plotly.relayout('data', update);
    }

    function bindPlot() {
        var gd = document.getElementById('data');
        gd.removeAllListeners('plotly_doubleclick');
        gd.on('plotly_doubleclick', showViews);
    }

    var detection = [];
    var adsb = {};

    var colorbar = { title: { text: 'SNR (dB)', side: 'right' } };

    // Empty axes at the chosen views until the first map arrives. The one
    // transparent cell is only there to draw the colour bar, so the plot does
    // not narrow when the first map lands.
    layoutRanges();
    Plotly.newPlot('data', [{
        type: 'heatmap', z: [[0]], x: [0], y: [0], opacity: 0, hoverinfo: 'skip',
        colorscale: 'Viridis', colorbar: colorbar,
        zauto: false, zmin: SNR_RANGE[0], zmax: SNR_RANGE[1]
    }], layout, config).then(bindPlot);

    // Back to the chosen views' ranges.
    document.getElementById('resetViewBtn').addEventListener('click', showViews);

    // ── Out-of-view alert ────────────────────────────────────────────────
    // Detections from the last 10 s that a narrow view leaves off the plot, so
    // narrowing never passes for an empty sky. Counted over a window, not one
    // frame, so the alert does not flicker with each frame's few detections.
    // See docs/features/radar.md#out-of-view-alert
    var OOV_WINDOW_MS = 10000;
    var recent = [];               // one entry per new set of detections
    var oovAlert = document.getElementById('oovAlert');
    var oovText = document.getElementById('oovText');

    function noteDetections(d) {
        recent.push({ at: Date.now(), delay: d.delay || [], doppler: d.doppler || [] });
    }

    function renderOutsideAlert() {
        var cut = Date.now() - OOV_WINDOW_MS;
        while (recent.length && recent[0].at < cut) recent.shift();
        var dop = state.dopView === 'narrow' ? narrowRange() : null;
        var del = state.delView === 'narrow' ? narrowDelayRange() : null;
        var n = 0, nDop = 0, nDel = 0;
        if (dop || del) {
            recent.forEach(function (f) {
                for (var i = 0; i < f.delay.length; i++) {
                    var d = !!dop && (f.doppler[i] < dop[0] || f.doppler[i] > dop[1]);
                    var k = !!del && (f.delay[i] < del[0] || f.delay[i] > del[1]);
                    if (d || k) n++;
                    if (d) nDop++;
                    if (k) nDel++;
                }
            });
        }
        oovAlert.hidden = n === 0;
        if (n) {
            var named = [];
            if (nDop) named.push(fmtHzRange(dop) + ' Hz');
            if (nDel) named.push(U.fmtDist(del[1]));
            oovText.textContent = n + (n === 1 ? ' detection' : ' detections') + ' outside '
                + named.join(' / ') + ' in 10s';
        }
    }

    // ── Toggles ──────────────────────────────────────────────────────────
    // The Doppler, delay and units toggles and the gear are wired by
    // ScopeControls.create above; a units press rebuilds the plot from the
    // last map (shape reset) so the heatmap's x values and the axis title are
    // redrawn in the new unit.
    var lastMap = null;

    document.getElementById('oovShowBtn').addEventListener('click', function () {
        setDopView('full');
        setDelView('full');
    });

    labelRangeButtons();

    // A reply for the view just left is dropped, or it would overwrite the new one.
    function fetchMap() {
        var asked = urlMap;
        getJson(asked, function (data) {
            if (asked === urlMap) drawMap(data);
        });
    }

    // Switching draws the other map now rather than at the next frame, and keeps
    // the zoom, so the two can be compared at the same spot.
    function setView(next) {
        if (!MAPS[next] || next === view) return;
        view = next;
        urlMap = MAPS[view];
        document.querySelectorAll('[data-view]').forEach(function (b) {
            b.setAttribute('aria-pressed', String(b.getAttribute('data-view') === view));
        });
        history.replaceState(null, '', view === 'live' ? '/radar' : '/radar?view=' + view);
        fetchMap();
    }
    document.querySelectorAll('[data-view]').forEach(function (b) {
        b.addEventListener('click', function () { setView(b.getAttribute('data-view')); });
    });

    // A fixed range, or the map's own extent where there is none; never less than the map.
    function cover(fixed, values) {
        var lo = values[0], hi = values[values.length - 1];
        return fixed ? [Math.min(fixed[0], lo), Math.max(fixed[1], hi)] : [lo, hi];
    }

    function drawMap(data) {
        lastMap = data;
        lastFrameAt = Date.now();
        renderExtent(data);
        // case draw new plot
        var drawn = data.nRows + 'x' + data.nCols;
        if (drawn !== shape) {
            shape = drawn;

            // The node's extent, widened to take in the whole map if blah2 is
            // running a wider one than the config said when the page loaded.
            full.x = cover(AXES.delay, data.delay);
            full.y = cover(AXES.doppler, data.doppler);
            labelRangeButtons();
            layoutRanges();

            var trace1 = {
                z: data.data,
                x: data.delay.map(U.distOf),
                y: data.doppler,
                colorscale: 'Viridis',
                colorbar: colorbar,
                zauto: false,
                zmin: SNR_RANGE[0],
                zmax: SNR_RANGE[1],
                type: 'heatmap'
            };
            var trace2 = {
                x: (detection.delay || []).map(U.distOf),
                y: detection.doppler,
                mode: 'markers',
                type: 'scatter',
                marker: {
                    size: 16,
                    opacity: 0.9,
                    color: '#FFFFFF',
                    line: {
                        color: '#000000',
                        width: 1.5
                    }
                }
            };
            var trace3 = {
                x: (adsb.delay || []).map(U.distOf),
                y: adsb.doppler,
                mode: 'markers',
                type: 'scatter',
                marker: {
                    size: 16,
                    opacity: 0.9,
                    color: '#D55E00',
                    line: {
                        color: '#000000',
                        width: 1.5
                    }
                }
            };

            var data_trace = [trace1, trace2, trace3];
            Plotly.newPlot('data', data_trace, layout, config).then(bindPlot);
        }
        // case update plot
        else {
            var trace_update = {
                x: [data.delay.map(U.distOf), (detection.delay || []).map(U.distOf), (adsb.delay || []).map(U.distOf)],
                y: [data.doppler, detection.doppler, adsb.doppler],
                z: [data.data, [], []],
                text: [[], [], adsb.flight]
            };
            Plotly.update('data', trace_update);
        }
    }

    // callback function
    window.setInterval(function () {
        renderStatus();
        renderOutsideAlert();

        // check if timestamp is updated
        getText(urlTimestamp, function (data) {
            failure = null;
            if (timestamp != data) {
                timestamp = data;

                // get detection data (no detection lag)
                getJson(urlDetection, function (data_detection) {
                    detection = data_detection;
                    noteDetections(data_detection);
                });

                // get ADS-B data if enabled in config, pass detection timestamp for synchronization
                if (isTruth) {
                    getJson(urlAdsbLink + '?detection_ts=' + encodeURIComponent(timestamp), function (data_adsb) {
                        adsb['delay'] = [];
                        adsb['doppler'] = [];
                        adsb['flight'] = [];
                        for (const aircraft in data_adsb) {
                            if ('doppler' in data_adsb[aircraft]) {
                                adsb['delay'].push(data_adsb[aircraft]['delay']);
                                adsb['doppler'].push(data_adsb[aircraft]['doppler']);
                                adsb['flight'].push(data_adsb[aircraft]['flight']);
                            }
                        }
                    });
                }

                // get new map data
                fetchMap();
            }
        }, function (why) { failure = why; renderStatus(); });
    }, 1000);
})();
