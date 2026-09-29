// Shared Auto-Calibrate driver for all three entry points (Configuration
// page Auto-Calibrate and Quick Calibrate, and the setup wizard step), so
// they cannot drift. Everything except DOM rendering lives here.
// See docs/features/auto-calibrate.md#entry-points-and-run-shapes.
//
// Deliberately ES5-flavoured (var/function, no arrow functions) to match setup.js.
window.RetinaCalibrate = (function() {
    'use strict';

    function csrf() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.content : '';
    }

    var PHASE_LABELS = {
        preflight: 'Checking the radio responds…',
        recovering: 'Radio unresponsive, restarting it…',
        descending: 'Maximizing gain, backing off overload…',
        refining: 'Refining gain…',
        dwelling: 'Watching for aircraft…',
        // The skip-confirmation soak: overload watch, no track wait.
        soaking: 'Checking the settings hold…',
        restoring: 'Restoring original tuning…'
    };

    var MODE_LABELS = { track: 'Standard', adsb: 'ADS-B verified' };

    // The quick run's start body, posted by both the setup wizard step and
    // Quick Calibrate. Neither caller should spell it out itself.
    // See docs/features/auto-calibrate.md#skip-confirmation-and-the-soak.
    var QUICK_RUN = { scope: 'current_tower', skip_confirmation: true };

    // Per-tower outcomes the run's summary message cannot express.
    // See docs/features/auto-calibrate.md#status-and-ui.
    var OUTCOME_TEXT = {
        no_confirmed_track: 'watched, but nothing confirmed',
        confirmed_track: 'confirmed a track',
        skipped_no_time: 'never watched (the search ran out of time here)',
        tuning_not_applied: 'never watched (the radio did not accept this tuning)',
        unstable_overload: 'stopped (the signal kept overloading the receiver)',
        tuned: 'tuned and held without overloading (no track was waited for)',
        not_reached: 'not reached'
    };

    function mhz(fc) { return (fc / 1e6).toFixed(1) + ' MHz'; }

    function fmtSeconds(s) {
        var m = Math.floor(s / 60), r = s % 60;
        return m + ':' + (r < 10 ? '0' : '') + r;
    }

    function escapeHtml(s) {
        var d = document.createElement('div');
        d.textContent = s;
        return d.innerHTML;
    }

    function warnBox(html) {
        return '<div style="margin-top:10px;padding:8px 10px;border-radius:6px;'
            + 'background:oklch(0.96 0.04 85);border:1px solid var(--warn,#b7791f);'
            + 'color:var(--warn,#b7791f);font-size:12.5px;">' + html + '</div>';
    }

    // A server-pushed Mender deployment can break a run and cannot be refused.
    // See docs/features/auto-calibrate.md#server-pushed-mender-deployments.
    function updateWarning(status) {
        if (!status.system_update) return '';
        return warnBox('<strong>A system update is installing.</strong> '
            + escapeHtml(status.system_update)
            + '. It restarts the radar, so this calibration will not complete '
            + 'and its results should be ignored. Run it again once the update '
            + 'has finished.');
    }

    function diagnose(history) {
        if (!history.length) return '';
        var watched = history.filter(function(h) {
            return h.outcome === 'no_confirmed_track' || h.outcome === 'confirmed_track';
        }).length;
        var soakOnly = history.every(function(h) {
            return h.outcome === 'tuned';
        });
        var lines = history.map(function(h) {
            var txt = OUTCOME_TEXT[h.outcome] || h.outcome || 'unknown';
            var extra = '';
            if (h.dwell_seconds) extra = ' (' + Math.round(h.dwell_seconds) + 's)';
            if (h.tuning_error) extra += ' - ' + escapeHtml(h.tuning_error);
            return '<div>' + escapeHtml(h.tower_name || 'Tower') + ': ' + txt + extra + '</div>';
        });
        // Not said of a soak-only run: a soak did watch, for overload.
        var lead = (watched === 0 && !soakOnly)
            ? '<strong>No tower was actually watched, so this is not evidence '
              + 'about aircraft.</strong><br>'
            : '';
        return lead + lines.join('');
    }

    // What a quick run's soak proved, in one sentence, or '' if it never
    // soaked. A quick run only tries the current tower, so history[0].
    function soakSummary(status) {
        var entry = (status.history || [])[0] || {};
        if (!entry.soak_seconds) return '';
        var seconds = Math.round(entry.soak_seconds);
        var backoffs = (entry.dwell_backoffs || []).length;
        return backoffs === 0
            ? 'Held cleanly for ' + seconds + 's with no sign of overload.'
            : 'Backed off ' + backoffs + ' time(s) during a ' + seconds
              + 's check, and settled here.';
    }

    // The tuning a terminal run left available to persist, or null: the
    // confirmed result, else the no-track fallback. Cancelled runs have neither.
    function tuningOf(status) {
        if (status.state === 'done' && status.result) return status.result;
        return status.fallback || null;
    }

    function isTerminal(status) {
        return status.state !== 'running' && status.state !== 'idle';
    }

    function post(url, body) {
        var opts = { method: 'POST', headers: { 'X-CSRFToken': csrf() } };
        if (body) {
            opts.headers['Content-Type'] = 'application/json';
            opts.body = JSON.stringify(body);
        }
        return fetch(url, opts).then(function(r) { return r.json(); });
    }

    function getStatus() {
        return fetch('/calibrate/status').then(function(r) { return r.json(); });
    }

    // Poll /calibrate/status until the run leaves 'running', calling onStatus
    // for every reading including the terminal one. Returns a stop function —
    // callers that can be dismissed (modal close, wizard step change) must
    // call it or the timer outlives the view.
    function pollStatus(onStatus, intervalMs) {
        var timer = null;
        var stopped = false;
        var every = intervalMs || 2000;
        function tick() {
            getStatus().then(function(status) {
                if (stopped) return;
                onStatus(status);
                if (status.state === 'running') timer = setTimeout(tick, every);
            }).catch(function() {
                // Transient: the GUI blinks out while the stack restarts
                // under us. Keep watching rather than declaring the run over.
                if (!stopped) timer = setTimeout(tick, every);
            });
        }
        tick();
        return function stop() {
            stopped = true;
            if (timer) { clearTimeout(timer); timer = null; }
        };
    }

    // Poll the shared config-apply queue to completion. Resolves with the
    // terminal status; rejects only if it ends in 'failed'.
    function pollApply(onProgress) {
        return new Promise(function(resolve, reject) {
            var misses = 0;
            function tick() {
                fetch('/config/apply/status')
                    .then(function(r) { return r.json(); })
                    .then(function(s) {
                        misses = 0;
                        if (s.state === 'running') {
                            if (onProgress) onProgress(s);
                            setTimeout(tick, 1000);
                        } else if (s.state === 'failed') {
                            reject(new Error(s.error || 'Apply failed'));
                        } else {
                            resolve(s);
                        }
                    })
                    .catch(function() {
                        // A miss or two is the stack restarting; a run of
                        // them is a real problem worth surfacing rather than
                        // polling forever behind a spinner.
                        if (++misses >= 5) {
                            reject(new Error('Progress could not be read'));
                            return;
                        }
                        setTimeout(tick, 2000);
                    });
            }
            tick();
        });
    }

    return {
        PHASE_LABELS: PHASE_LABELS,
        MODE_LABELS: MODE_LABELS,
        OUTCOME_TEXT: OUTCOME_TEXT,
        QUICK_RUN: QUICK_RUN,
        soakSummary: soakSummary,
        mhz: mhz,
        fmtSeconds: fmtSeconds,
        escapeHtml: escapeHtml,
        warnBox: warnBox,
        updateWarning: updateWarning,
        diagnose: diagnose,
        tuningOf: tuningOf,
        isTerminal: isTerminal,
        getStatus: getStatus,
        pollStatus: pollStatus,
        pollApply: pollApply,
        start: function(body) { return post('/calibrate/start', body); },
        cancel: function() { return post('/calibrate/cancel'); },
        apply: function() { return post('/calibrate/apply'); }
    };
})();
