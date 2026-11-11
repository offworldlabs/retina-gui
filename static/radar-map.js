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

    // ── Doppler and delay views ──────────────────────────────────────────
    // The Tracker's Narrow/Full toggles, with its defaults, limits and wording.
    // Presentation only: blah2 processes its full span whichever is drawn.
    // Saved in this browser under radar.*. See docs/features/radar.md#doppler-and-delay-views

    function remember(key, value) {
        try { localStorage.setItem('radar.' + key, String(value)); } catch (e) { /* private mode */ }
    }
    function recall(key, fallback) {
        try {
            var v = localStorage.getItem('radar.' + key);
            return v === null ? fallback : v;
        } catch (e) { return fallback; }
    }

    var state = {};

    // Narrow is where ordinary air traffic sits, so it is the default.
    var DEFAULT_NARROW_HZ = 300;
    var MAX_NARROW_HZ = 100000;    // a sanity cap, used only when the span is unknown

    // Half the full span, the widest narrow range worth having.
    function spanHalfWidth() {
        return full.y ? Math.floor(Math.max(Math.abs(full.y[0]), Math.abs(full.y[1]))) : null;
    }

    function validNarrow(n) {
        var cap = spanHalfWidth() || MAX_NARROW_HZ;
        return Number.isInteger(n) && n >= 1 && n <= cap ? n : null;
    }

    state.dopView = recall('dopView', 'narrow') === 'full' ? 'full' : 'narrow';
    state.narrowHz = validNarrow(parseInt(recall('narrowHz', DEFAULT_NARROW_HZ), 10)) || DEFAULT_NARROW_HZ;

    // Clipped to the full span, so narrow never draws Doppler the node cannot see.
    // A span lying wholly to one side of the band leaves nothing to narrow to.
    function narrowRange() {
        var lo = -state.narrowHz, hi = state.narrowHz;
        if (full.y) {
            lo = Math.max(lo, full.y[0]);
            hi = Math.min(hi, full.y[1]);
            if (lo >= hi) return full.y.slice();
        }
        return [lo, hi];
    }

    // null autoscales, as the full view does until a span is known.
    function dopplerRange() {
        return state.dopView === 'narrow' ? narrowRange() : full.y;
    }

    function fmtHzRange(r) {
        var lo = Math.round(r[0]), hi = Math.round(r[1]);
        return lo === -hi ? '\u00b1' + hi : lo + '\u2026' + hi;
    }

    // Full is the default on this axis, so nothing is hidden until a viewer asks.
    var DEFAULT_NARROW_KM = 30;
    var MAX_NARROW_KM = 1000;      // a sanity cap, used only when the range is unknown

    // ── Units ────────────────────────────────────────────────────────────
    // km or mi for this browser, from static/units.js, shared with the
    // Tracker. The maps, `full` and the narrow range stay in km; only what
    // is drawn and written converts. See docs/features/tracker.md#units
    var U = DisplayUnits.current();

    // The furthest processed range, the widest narrow range worth having, in km.
    function delayReach() {
        return full.x ? full.x[1] : null;
    }

    // The narrow range is kept in km, so a browser that switches units keeps
    // the same range. Any positive km inside the reach is valid.
    function validNarrowKm(km) {
        var cap = delayReach() || MAX_NARROW_KM;
        return Number.isFinite(km) && km > 0 && km <= cap ? km : null;
    }

    // What the gear accepts: a whole number of the display unit, 1 to the
    // reach rounded up in that unit.
    function reachDist() {
        return Math.ceil(U.distOf(delayReach() || MAX_NARROW_KM));
    }
    function validNarrowDist(n) {
        return Number.isInteger(n) && n >= 1 && n <= reachDist() ? n : null;
    }

    // Clipped to the reach, or a node processing less than 30 km would open the
    // gear on a value it refuses.
    function defaultNarrowKm() {
        var reach = delayReach();
        return reach ? Math.min(DEFAULT_NARROW_KM, reach) : DEFAULT_NARROW_KM;
    }

    state.delView = recall('delView', 'full') === 'narrow' ? 'narrow' : 'full';
    state.narrowKm = validNarrowKm(parseFloat(recall('narrowKm', defaultNarrowKm()))) || defaultNarrowKm();

    // Starts where the node starts (a little below zero, past the direct
    // signal), clipped so narrow never draws range the node cannot see.
    function narrowDelayRange() {
        var lo = full.x ? full.x[0] : 0, hi = state.narrowKm;
        if (full.x) {
            hi = Math.min(hi, full.x[1]);
            if (lo >= hi) return full.x.slice();
        }
        return [lo, hi];
    }

    function delayRange() {
        return state.delView === 'narrow' ? narrowDelayRange() : full.x;
    }

    // The delay range in the display unit, for the x axis.
    function displayDelayRange() {
        var r = delayRange();
        return r ? [U.distOf(r[0]), U.distOf(r[1])] : null;
    }

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

    // ── Toggles and settings ─────────────────────────────────────────────

    function wireSeg(attr, apply) {
        document.querySelectorAll('.seg [data-' + attr + ']').forEach(function (b) {
            b.addEventListener('click', function () { apply(b.getAttribute('data-' + attr)); });
        });
    }

    function pressSeg(attr, value) {
        document.querySelectorAll('.seg [data-' + attr + ']').forEach(function (b) {
            b.setAttribute('aria-pressed', String(b.getAttribute('data-' + attr) === value));
        });
    }

    function setDopView(v) {
        state.dopView = v;
        remember('dopView', v);
        pressSeg('dop', v);
        showViews();
        renderOutsideAlert();
    }

    function setDelView(v) {
        state.delView = v;
        remember('delView', v);
        pressSeg('del', v);
        showViews();
        renderOutsideAlert();
    }

    wireSeg('dop', setDopView);
    wireSeg('del', setDelView);

    // Units: a presentation choice for this browser, shared with the Tracker.
    // The next map rebuilds the plot (shape reset) so the heatmap's x values
    // and the axis title are redrawn in the new unit.
    var lastMap = null;
    function setUnits(v) {
        DisplayUnits.set(v);
        // From the pressed value, not the store: a blocked localStorage
        // (private mode) must still switch for this load.
        U = DisplayUnits.current(v);
        pressSeg('units', U.name);
        layout.xaxis.title.text = 'Bistatic Range (' + U.dist + ')';
        shape = null;
        labelRangeButtons();
        if (lastMap) drawMap(lastMap);
    }
    wireSeg('units', setUnits);
    pressSeg('units', U.name);
    pressSeg('dop', state.dopView);
    pressSeg('del', state.delView);

    document.getElementById('oovShowBtn').addEventListener('click', function () {
        setDopView('full');
        setDelView('full');
    });

    // The buttons name the ranges they draw, so the toggle reads "±300 / ±1000"
    // on a node at ±1000 and stays truthful after the gear changes narrow.
    function labelRangeButtons() {
        document.getElementById('dopNarrowBtn').textContent = fmtHzRange(narrowRange());
        document.getElementById('dopFullBtn').textContent = full.y ? fmtHzRange(full.y) : 'Full';
        document.getElementById('delNarrowBtn').textContent = U.fmtDist(narrowDelayRange()[1]);
        document.getElementById('delFullBtn').textContent = full.x ? U.fmtDist(full.x[1]) : 'Full';
        document.getElementById('narrowKmUnit').textContent = U.dist;
    }
    labelRangeButtons();

    var settingsDlg = document.getElementById('settingsDlg');
    var narrowInput = document.getElementById('narrowHzInput');
    var narrowErr = document.getElementById('narrowHzErr');
    var narrowKmInput = document.getElementById('narrowKmInput');
    var narrowKmErr = document.getElementById('narrowKmErr');

    function showFieldError(input, err, text) {
        err.textContent = text;
        err.hidden = !text;
        input.classList.toggle('is-invalid', !!text);
    }
    function showNarrowError(text) { showFieldError(narrowInput, narrowErr, text); }
    function showNarrowKmError(text) { showFieldError(narrowKmInput, narrowKmErr, text); }

    document.getElementById('settingsBtn').addEventListener('click', function () {
        narrowInput.value = state.narrowHz;
        var half = spanHalfWidth();
        if (half) narrowInput.max = half;
        document.getElementById('fullSpanText').textContent =
            full.y ? fmtHzRange(full.y) + ' Hz' : 'its full span';
        narrowKmInput.value = Math.round(U.distOf(state.narrowKm));
        narrowKmInput.max = reachDist();
        document.getElementById('fullDelayText').textContent =
            full.x ? 'out to ' + U.fmtDist(full.x[1]) : 'its full range';
        showNarrowError('');
        showNarrowKmError('');
        settingsDlg.showModal();
        narrowInput.select();
    });

    document.getElementById('narrowResetBtn').addEventListener('click', function () {
        narrowInput.value = DEFAULT_NARROW_HZ;
        narrowKmInput.value = Math.round(U.distOf(defaultNarrowKm()));
        showNarrowError('');
        showNarrowKmError('');
    });

    document.getElementById('settingsCancel').addEventListener('click', function () { settingsDlg.close(); });

    // A click on the backdrop lands on the dialog element itself.
    settingsDlg.addEventListener('click', function (e) { if (e.target === settingsDlg) settingsDlg.close(); });

    document.getElementById('settingsForm').addEventListener('submit', function (e) {
        e.preventDefault();
        var n = Number(narrowInput.value);
        var dist = Number(narrowKmInput.value);
        var hzOk = validNarrow(n) !== null, kmOk = validNarrowDist(dist) !== null;
        var half = spanHalfWidth(), reach = delayReach();
        showNarrowError(hzOk ? '' : half
            ? 'Enter a whole number from 1 to ' + half + ', the most the node processes.'
            : 'Enter a whole number of Hz, 1 or more.');
        showNarrowKmError(kmOk ? '' : reach
            ? 'Enter a whole number from 1 to ' + reachDist() + ', the most the node processes.'
            : 'Enter a whole number of ' + U.dist + ', from 1 to ' + reachDist() + '.');
        // Nothing is saved unless every field is valid.
        if (!hzOk || !kmOk) return;

        // Typed in the display unit, kept in km. Unchanged if it rounds back
        // to what is stored, so reopening and saving does not re-narrow.
        var km = Math.round(U.distOf(state.narrowKm)) === dist ? state.narrowKm : U.kmOf(dist);
        var hzChanged = n !== state.narrowHz, kmChanged = km !== state.narrowKm;
        state.narrowHz = n;
        state.narrowKm = km;
        remember('narrowHz', n);
        remember('narrowKm', km);
        labelRangeButtons();
        settingsDlg.close();
        // Saving a new narrow range is asking to see it. Only the axis whose
        // range changed switches, so editing one never narrows the other.
        if (hzChanged) setDopView('narrow');
        if (kmChanged) setDelView('narrow');
        showViews();
        renderOutsideAlert();
    });

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
