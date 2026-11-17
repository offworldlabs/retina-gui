// The Tracker page's Stored Detections display: the tracks retina-tracker kept
// for going beyond its Doppler limit, listed from its store and drawn one at a
// time with everything the node detected around them. The sidecar owns the
// store; this only reads it. See docs/features/tracker.md#stored-detections
(function () {
    'use strict';

    // One page of the list. retina-tracker serves 50 by default and at most 500.
    var PAGE = 50;
    var RETRY_OPENING_MS = 5000;
    var RETRY_LOST_MS = 10000;
    // Whole records kept in the browser, so stepping between rows does not
    // refetch. One can be a few hundred kB of JSON.
    var CACHE_RECORDS = 6;
    var SPEED_OF_LIGHT = 299792458;

    var LIMIT_LINE = { color: 'rgba(161,98,7,0.85)', width: 1.2, dash: 'dash' };
    var RING = '#b45309';
    var OTHER_TRACK = 'rgba(107,114,128,0.6)';

    function esc(s) {
        return String(s).replace(/[&<>"]/g, function (c) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
        });
    }

    // The node's epoch milliseconds, shown in the viewer's own time zone.
    function stamp(ms) {
        var d = new Date(ms);
        return d.toLocaleDateString([], { day: 'numeric', month: 'short' }) + ' '
            + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
    }
    function utc(ms) { return new Date(ms).toISOString().replace('T', ' ').slice(0, 19) + ' UTC'; }
    function signedHz(hz) { return (hz > 0 ? '+' : '') + Number(hz).toFixed(1) + ' Hz'; }
    function size(bytes) {
        if (typeof bytes !== 'number') return '?';
        if (bytes < 100 * 1024) return Math.max(1, Math.round(bytes / 1024)) + ' kB';
        return (bytes / (1024 * 1024)).toFixed(1).replace(/\.0$/, '') + ' MB';
    }

    // fetch, always resolving to {status, body}. 0 means retina-gui itself did
    // not answer.
    function getJson(url) {
        return fetch(url, { headers: { Accept: 'application/json' } })
            .then(function (r) {
                return r.json().catch(function () { return {}; })
                    .then(function (body) { return { status: r.status, body: body || {} }; });
            })
            .catch(function () { return { status: 0, body: { error: 'the node did not answer' } }; });
    }

    function cols(c) { return c && Array.isArray(c.t) ? c : { t: [], delay: [], doppler: [], snr: [] }; }

    function create(opts) {
        var el = opts.el;
        var shown = false;
        var gen = 0;                 // bumped by every list reload; older answers are dropped
        var retryTimer = null;

        var list = { state: 'idle', error: '', rows: [], more: false, limitHz: 300 };
        var older = { busy: false, error: '' };
        var stats = null;
        var filter = 'all';

        var selected = null;         // record id, or null
        var detail = { state: 'none', error: '', record: null };
        var cache = {}, cacheOrder = [];
        var drawnUnits = null;       // what the plots were last drawn in

        // ── loading ─────────────────────────────────────────────────────

        function retry(ms) {
            clearTimeout(retryTimer);
            retryTimer = setTimeout(function () { if (shown) reload(); }, ms);
        }

        // What a refused list means. 404 is a tracker with no /outside at all.
        function classify(res) {
            var err = (res.body && res.body.error) || '';
            list.error = err;
            if (res.status === 404) {
                list.state = 'old';
            } else if (res.status === 503 && /opening/.test(err)) {
                list.state = 'opening';
                retry(RETRY_OPENING_MS);
            } else if (res.status === 503) {
                list.state = 'off';
            } else {
                list.state = 'lost';
                if (!err) list.error = 'HTTP ' + res.status;
                retry(RETRY_LOST_MS);
            }
        }

        function reload() {
            clearTimeout(retryTimer);
            var g = ++gen;
            if (list.state !== 'ok') list.state = 'loading';
            older = { busy: false, error: '' };
            paint();
            getJson('/tracker/outside?limit=' + PAGE).then(function (res) {
                if (g !== gen) return;
                if (res.status === 200 && Array.isArray(res.body.records)) {
                    list.state = 'ok';
                    list.error = '';
                    list.rows = res.body.records;
                    list.more = res.body.records.length === PAGE;
                    if (typeof res.body.limit_hz === 'number') list.limitHz = res.body.limit_hz;
                    if (selected === null && list.rows.length) select(list.rows[0].id);
                } else {
                    classify(res);
                }
                paint();
            });
            getJson('/tracker/outside/status').then(function (res) {
                if (g !== gen) return;
                stats = res.status === 200 ? res.body.outside : null;
                paintStatus();
                paintFoot();
            });
        }

        function loadOlder() {
            if (older.busy || !list.rows.length) return;
            var g = gen;
            older.busy = true;
            paintFoot();
            var last = list.rows[list.rows.length - 1].id;
            getJson('/tracker/outside?limit=' + PAGE + '&before=' + last).then(function (res) {
                if (g !== gen) return;
                older.busy = false;
                if (res.status === 200 && Array.isArray(res.body.records)) {
                    older.error = '';
                    list.rows = list.rows.concat(res.body.records);
                    list.more = res.body.records.length === PAGE;
                } else {
                    older.error = (res.body && res.body.error) || 'HTTP ' + res.status;
                }
                paintTable();
                paintFoot();
            });
        }

        function keep(id, record) {
            if (!cache[id]) cacheOrder.push(id);
            cache[id] = record;
            while (cacheOrder.length > CACHE_RECORDS) delete cache[cacheOrder.shift()];
        }

        function select(id) {
            selected = id;
            if (opts.onSelect) opts.onSelect(id);
            paintTable();
            if (cache[id]) {
                detail = { state: 'ok', error: '', record: cache[id] };
                paintDetail();
                return;
            }
            detail = { state: 'loading', error: '', record: null };
            paintDetail();
            getJson('/tracker/outside/' + id).then(function (res) {
                if (selected !== id) return;
                if (res.status === 200 && res.body && res.body.track) {
                    keep(id, res.body);
                    detail = { state: 'ok', error: '', record: res.body };
                } else if (res.status === 404) {
                    detail = { state: 'gone', error: '', record: null };
                } else {
                    detail = { state: 'error', error: (res.body && res.body.error) || 'HTTP ' + res.status, record: null };
                }
                paintDetail();
            });
        }

        // ── painting ────────────────────────────────────────────────────

        function paint() {
            paintStatus();
            paintTable();
            paintFoot();
            paintDetail();
        }

        function setStatus(text, cls) {
            el.status.className = 'scope-status stored-only' + (cls ? ' ' + cls : '');
            el.status.innerHTML = '<span class="dot"></span>' + text;
        }

        function paintStatus() {
            var s = list.state;
            if (s === 'old') return setStatus('not available on this node', 'stale');
            if (s === 'opening') return setStatus('opening the store&hellip;', 'stale');
            if (s === 'off') return setStatus('store off', 'lost');
            if (s === 'lost') return setStatus('tracker unreachable', 'lost');
            if (s !== 'ok') return setStatus('loading&hellip;', 'stale');
            var parts = [];
            if (stats && typeof stats.records === 'number') {
                parts.push(stats.records.toLocaleString() + ' stored');
                parts.push(size(stats.used_bytes) + ' of ' + size(stats.max_bytes));
            } else {
                parts.push(list.rows.length + (list.more ? '+' : '') + ' stored');
            }
            if (list.rows.length) parts.push('newest ' + esc(stamp(list.rows[0].birth_ms)));
            setStatus(parts.join(' &middot; '), '');
        }

        function message(main, sub, reason) {
            return '<p class="sd-msg-main">' + main + '</p>'
                + (reason ? '<span class="sd-reason">' + esc(reason) + '</span>' : '')
                + (sub ? '<p>' + sub + '</p>' : '');
        }

        function listMessage() {
            var lim = '&plusmn;' + list.limitHz + ' Hz';
            switch (list.state) {
            case 'loading':
            case 'idle':
                return message('Loading stored detections&hellip;');
            case 'old':
                return message("This node's tracker doesn't keep Stored Detections yet.",
                    'They arrive with a later update. Live tracking on the other displays is unaffected.');
            case 'opening':
                return message('Opening the store.',
                    'This takes a few seconds after the tracker restarts. The page tries again on its own.');
            case 'off':
                return message('Stored Detections is off on this node.',
                    'Live tracking is unaffected.', list.error);
            case 'lost':
                return message("Can't reach the tracker.",
                    'It may be restarting. The page tries again on its own.', list.error);
            }
            if (!list.rows.length) {
                return message('Nothing stored yet.',
                    'A track appears here about a minute after it ends, once it has gone beyond ' + lim
                    + ' of Doppler. On lower-frequency towers that can be days apart.');
            }
            return '';
        }

        function visibleRows() {
            return list.rows.filter(function (r) {
                if (filter === 'adsb') return !!r.adsb_hex;
                if (filter === 'none') return !r.adsb_hex;
                return true;
            });
        }

        function paintTable() {
            el.limitHead.textContent = 'Beyond ±' + list.limitHz + ' Hz';
            el.limitText.textContent = '±' + list.limitHz + ' Hz';
            var msg = listMessage();
            el.msg.innerHTML = msg;
            el.msg.hidden = !msg;
            el.table.hidden = !!msg;
            el.filters.hidden = !!msg;
            if (msg) { el.rows.innerHTML = ''; return; }

            var rows = visibleRows();
            el.rows.innerHTML = rows.map(function (r) {
                var dur = Math.max(0, (r.last_ms - r.birth_ms) / 1000);
                var col = opts.colorFor(r.track_id);
                return '<tr class="sd-row' + (r.id === selected ? ' sel' : '') + '" data-id="' + r.id + '">'
                    + '<td class="t"><button type="button" class="sd-rowbtn" data-id="' + r.id + '"'
                    + ' title="' + esc(utc(r.birth_ms)) + '" aria-label="Open the record from ' + esc(stamp(r.birth_ms)) + '">'
                    + '<span class="sd-sw" style="background:' + col + '"></span>' + esc(stamp(r.birth_ms)) + '</button></td>'
                    + '<td class="n">' + esc(opts.fmtDur(dur)) + '</td>'
                    + '<td class="n ' + (r.peak_doppler_hz > 0 ? 'pos' : 'neg') + '">' + esc(signedHz(r.peak_doppler_hz)) + '</td>'
                    + '<td class="n">' + r.n_outside + '</td>'
                    + '<td class="n">' + r.n_points
                    + (r.complete ? '' : ' <span class="chip part" title="The history no longer held every point of this track when it was stored">partial</span>')
                    + '</td>'
                    + '<td class="n">' + Number(r.n_neighbours).toLocaleString() + '</td>'
                    + '<td>' + (r.adsb_hex ? '<span class="chip ok">' + esc(r.adsb_hex) + '</span>' : '<span class="sd-none">none</span>') + '</td>'
                    + '<td>' + esc(r.track_id) + '</td>'
                    + '<td class="n">#' + r.id + '</td>'
                    + '</tr>';
            }).join('');
            if (!rows.length) {
                var what = filter === 'adsb' ? 'with ADS-B' : 'without ADS-B';
                el.rows.innerHTML = '<tr><td colspan="9" class="sd-empty">No records ' + what + ' in the '
                    + list.rows.length + ' loaded.</td></tr>';
            }
        }

        function paintFoot() {
            var ok = list.state === 'ok' && list.rows.length;
            el.foot.hidden = !ok;
            if (!ok) return;
            var total = stats && typeof stats.records === 'number' ? stats.records : null;
            el.count.textContent = list.rows.length + (total !== null ? ' of ' + total.toLocaleString() : '') + ' loaded'
                + (older.error ? ' · older records did not load: ' + older.error : '');
            el.older.hidden = !list.more;
            el.older.disabled = older.busy;
            el.older.textContent = older.busy ? 'Loading…' : 'Load older';
        }

        function rowFor(id) {
            for (var i = 0; i < list.rows.length; i++) if (list.rows[i].id === id) return list.rows[i];
            return null;
        }

        function paintDetail() {
            var showIt = selected !== null && list.state === 'ok';
            el.detail.hidden = !showIt;
            if (!showIt) return;
            var ok = detail.state === 'ok';
            el.body.hidden = !ok;
            el.detailMsg.hidden = ok;
            if (!ok) {
                var row = rowFor(selected);
                if (detail.state === 'loading') {
                    el.detailMsg.innerHTML = message('Loading ' + (row ? esc(stamp(row.birth_ms)) + ' (' + size(row.bytes) + ')' : 'record #' + selected) + '&hellip;');
                } else if (detail.state === 'gone') {
                    el.detailMsg.innerHTML = message('Record #' + selected + ' is no longer held.',
                        'The oldest records roll off when the store is full.')
                        + (list.rows.length ? '<button type="button" class="ds-btn ghost sm" data-newest="1">Show the newest</button>' : '');
                } else {
                    el.detailMsg.innerHTML = message("This record can't be read.", 'The others are unaffected.', detail.error);
                }
                el.head.innerHTML = '';
                return;
            }
            var r = detail.record;
            el.head.innerHTML = '<span class="sd-sw lg" style="background:' + opts.colorFor(r.track_id) + '"></span>'
                + '<h2 title="' + esc(utc(r.birth_ms)) + '">' + esc(stamp(r.birth_ms)) + '</h2>'
                + '<span class="sd-sub">record #' + r.id + ' &middot; track ' + esc(r.track_id) + '</span>'
                + (r.adsb_hex ? '<span class="chip ok">ADS-B ' + esc(r.adsb_hex) + '</span>' : '')
                + (r.complete ? '' : '<span class="chip part" title="The history no longer held every point of this track when it was stored">partial</span>');
            el.keyTrack.style.background = opts.colorFor(r.track_id);
            el.keyRing.style.background = opts.colorFor(r.track_id);
            paintFacts(r);
            draw();
        }

        function paintFacts(r) {
            var U = opts.units();
            var dur = Math.max(0, (r.last_ms - r.birth_ms) / 1000);
            var freq = r.radar && r.radar.center_frequency;
            var nb = r.neighbours || {};
            var others = Object.keys(nb.tracks || {}).length;
            var lim = r.limit_hz || list.limitHz;
            var facts = [
                ['Lasted', esc(opts.fmtDur(dur)), (r.n_detections || r.n_points) + ' detections over ' + (r.n_frames || '?') + ' frames'],
                ['Peak Doppler', esc(signedHz(r.peak_doppler_hz)),
                    freq ? U.fmtSpeedMps(Math.abs(r.peak_doppler_hz) * SPEED_OF_LIGHT / freq) + ' range rate at '
                        + (freq / 1e6).toFixed(1).replace(/\.0$/, '') + ' MHz' : ''],
                ['Beyond ±' + lim + ' Hz', r.n_outside + ' detections', ''],
                ['Mean SNR', typeof r.mean_snr === 'number' ? r.mean_snr.toFixed(1) + ' dB' : 'not recorded', ''],
                ['Max velocity', typeof r.max_velocity_ms === 'number' ? U.fmtSpeedMps(r.max_velocity_ms) : 'not recorded', "the tracker's estimate"],
                ['ADS-B', r.adsb_hex ? esc(r.adsb_hex) : 'none', ''],
                ['Around it', Number(nb.total || 0).toLocaleString() + ' detections',
                    others + (others === 1 ? ' other track' : ' other tracks')
                    + (nb.stride > 1 ? ', every ' + nb.stride + 'th kept' : '')]
            ];
            el.facts.innerHTML = facts.map(function (f) {
                return '<div><dt>' + f[0] + '</dt><dd>' + f[1] + (f[2] ? '<small>' + f[2] + '</small>' : '') + '</dd></div>';
            }).join('');
        }

        // The open record's plot. Detections pool into one trace per SNR band
        // and the other tracks into one, as on the live scope.
        function draw() {
            if (!shown || detail.state !== 'ok') return;
            var r = detail.record, U = opts.units();
            drawnUnits = U.name;
            var lim = r.limit_hz || list.limitHz;
            var nb = r.neighbours || {};
            var colour = opts.colorFor(r.track_id);
            var traces = [];

            var bands = {};
            Object.keys(nb.detections || {}).forEach(function (cls) {
                var c = cols(nb.detections[cls]);
                for (var i = 0; i < c.t.length; i++) {
                    var b = opts.bandOf(c.snr[i]);
                    var bin = bands[b] || (bands[b] = { x: [], y: [], snr: [] });
                    bin.x.push(U.distOf(c.delay[i]));
                    bin.y.push(c.doppler[i]);
                    bin.snr.push(c.snr[i]);
                }
            });
            Object.keys(bands).forEach(function (b) {
                var bin = bands[b];
                traces.push({ type: 'scattergl', mode: 'markers', x: bin.x, y: bin.y,
                    marker: { color: opts.bandColour[b], size: 4, opacity: 0.55 }, customdata: bin.snr,
                    hovertemplate: 'Range: %{x:.2f} ' + U.dist + '<br>Doppler: %{y:.2f} Hz<br>SNR: %{customdata:.1f} dB<extra></extra>',
                    showlegend: false });
            });

            var others = { x: [], y: [], cd: [] };
            Object.keys(nb.tracks || {}).forEach(function (id) {
                var c = cols(nb.tracks[id]);
                if (others.x.length) { others.x.push(null); others.y.push(null); others.cd.push(null); }
                for (var i = 0; i < c.t.length; i++) {
                    others.x.push(U.distOf(c.delay[i])); others.y.push(c.doppler[i]); others.cd.push(id);
                }
            });
            if (others.x.length) {
                traces.push({ type: 'scattergl', mode: 'lines+markers', x: others.x, y: others.y,
                    line: { color: 'rgba(0,0,0,0.18)', width: 1 }, marker: { color: OTHER_TRACK, size: 5 },
                    customdata: others.cd, hovertemplate: '%{customdata}<br>Range: %{x:.2f} ' + U.dist + '<br>Doppler: %{y:.2f} Hz<extra></extra>',
                    showlegend: false });
            }

            var tr = cols(r.track);
            var path = { x: [], y: [] };
            var inside = { x: [], y: [], snr: [] }, beyond = { x: [], y: [], snr: [] };
            for (var i = 0; i < tr.t.length; i++) {
                var x = U.distOf(tr.delay[i]), y = tr.doppler[i];
                path.x.push(x); path.y.push(y);
                var into = Math.abs(y) > lim ? beyond : inside;
                into.x.push(x); into.y.push(y); into.snr.push(tr.snr[i]);
            }
            traces.push({ type: 'scattergl', mode: 'lines', x: path.x, y: path.y,
                line: { color: 'rgba(0,0,0,0.4)', width: 1.2 }, hoverinfo: 'skip', showlegend: false });
            [[inside, { color: colour, size: 7, line: { color: '#ffffff', width: 0.6 } }],
             [beyond, { color: colour, size: 8, line: { color: RING, width: 1.6 } }]].forEach(function (p) {
                if (!p[0].x.length) return;
                traces.push({ type: 'scattergl', mode: 'markers', x: p[0].x, y: p[0].y, marker: p[1], customdata: p[0].snr,
                    hovertemplate: esc(r.track_id) + '<br>Range: %{x:.2f} ' + U.dist
                        + '<br>Doppler: %{y:.2f} Hz<br>SNR: %{customdata:.1f} dB<extra></extra>',
                    showlegend: false });
            });

            var dop = fitted(opts.extent().y, tr.doppler, 1), del = fitted(opts.extent().x, tr.delay, 1);
            if (del) del = [U.distOf(del[0]), U.distOf(del[1])];
            Plotly.react(el.plot, traces, {
                uirevision: 'stored:' + r.id + ':' + (dop || []).join(',') + ':' + (del || []).join(',') + ':' + U.name,
                showlegend: false, margin: { t: 10, r: 14, b: 44, l: 58 },
                xaxis: axis('Bistatic range (' + U.dist + ')', del),
                yaxis: axis('Doppler (Hz)', dop),
                shapes: [lim, -lim].map(function (y) {
                    return { type: 'line', xref: 'paper', x0: 0, x1: 1, yref: 'y', y0: y, y1: y, line: LIMIT_LINE, layer: 'below' };
                })
            }, { displayModeBar: false, responsive: true });
        }

        // The node's span for this axis, widened if the record's own track
        // reaches past it (a record kept from before a config change). Null,
        // which autoscales, when the node's span is unknown.
        function fitted(span, values, pad) {
            if (!span) return null;
            var lo = span[0], hi = span[1];
            for (var i = 0; i < values.length; i++) {
                if (values[i] < lo) lo = values[i] - pad;
                if (values[i] > hi) hi = values[i] + pad;
            }
            return [lo, hi];
        }

        function axis(title, range) {
            var spec = { title: { text: title } };
            if (range) spec.range = range.slice();
            return spec;
        }

        // ── wiring ──────────────────────────────────────────────────────

        el.rows.addEventListener('click', function (e) {
            var row = e.target.closest('tr[data-id]');
            if (!row) return;
            var id = parseInt(row.getAttribute('data-id'), 10);
            if (id !== selected) select(id);
        });
        el.older.addEventListener('click', loadOlder);
        el.detailMsg.addEventListener('click', function (e) {
            if (e.target.closest('[data-newest]') && list.rows.length) select(list.rows[0].id);
        });
        el.filterBtns.forEach(function (b) {
            b.addEventListener('click', function () {
                filter = b.getAttribute('data-sfilter');
                el.filterBtns.forEach(function (o) { o.setAttribute('aria-pressed', String(o === b)); });
                paintTable();
            });
        });

        return {
            // Every time the display is chosen the list reloads, so records
            // stored since it was last open appear.
            show: function () {
                shown = true;
                reload();
                if (detail.state === 'ok') draw();
            },
            hide: function () {
                shown = false;
                clearTimeout(retryTimer);
            },
            // A record named by the page's URL, before the list has loaded.
            open: function (id) { select(id); },
            selected: function () { return selected; },
            // Called on every page render, which the live stream triggers a
            // few times a second; only a change of units redraws.
            render: function () {
                if (!shown || detail.state !== 'ok' || opts.units().name === drawnUnits) return;
                paintFacts(detail.record);
                draw();
            }
        };
    }

    window.TrackerStored = { create: create };
})();
