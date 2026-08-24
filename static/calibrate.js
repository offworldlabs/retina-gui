// Shared Auto-Calibrate driver.
//
// Two entry points run calibration and they must not drift: the Configuration
// page modal (a full run — every candidate tower, dwelling on each until a
// track confirms) and the setup wizard step (current tower, descend then soak
// for overload, no track wait — see calibrator.py's skip_confirmation). What they share is everything except the DOM:
// the status vocabulary, the formatting, the "what did this run actually mean"
// interpretation, and the fetch calls. Rendering stays with each caller, since
// one is a Bootstrap modal and the other is a full-page wizard step.
//
// Deliberately ES5-flavoured (var/function, no arrow functions) to match
// setup.js, which is the more constrained of the two consumers.
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
        restoring: 'Restoring original tuning…'
    };

    var MODE_LABELS = { track: 'Standard', adsb: 'ADS-B verified' };

    // Per-tower outcomes the engine records but the run's summary message
    // cannot express. Without these every failure reads as "probably no
    // aircraft", including the ones that never looked for one.
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

    // A server-pushed Mender deployment installs on its own schedule and
    // cannot be refused from here, so it can replace the containers under a
    // run. Say so rather than let the run die unexplained.
    function updateWarning(status) {
        if (!status.system_update) return '';
        return warnBox('<strong>A system update is installing.</strong> '
            + escapeHtml(status.system_update)
            + '. It restarts the radar, so this calibration will not complete '
            + 'and its results should be ignored. Run it again once the update '
            + 'has finished.');
    }

    // The preflight only reports when it had to intervene. A clean probe is
    // unremarkable; a restart is not — it persisted maximum attenuation to
    // the config, deliberately and without reverting it (see calibrator.py's
    // module docstring), so the user has to be told what was replaced or
    // their gain settings appear to have changed by themselves.
    function preflightNotice(status) {
        var pf = status.preflight;
        if (!pf || !pf.restarted) return '';
        var was = pf.previous
            ? ' Its previous setting was gain reduction A ' + pf.previous.gain_a
              + ' dB / B ' + pf.previous.gain_b + ' dB, LNA state '
              + pf.previous.lna_state + '.'
            : '';
        var lead = pf.recovered
            ? 'The radio had stopped accepting tuning commands and was restarted.'
            : 'The radio had stopped accepting tuning commands.';
        return warnBox('<strong>' + lead + '</strong> It has been set to maximum '
            + 'attenuation (gain reduction 59/59, LNA state 9) and that has '
            + 'been saved to the configuration.' + was);
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
        // "Nothing was watched" is a warning for a run that meant to watch
        // for aircraft, and simply a description for one that never intended
        // to. A soak DID watch — for overload, which is the thing it cared
        // about — so saying this about it would invent a problem.
        var lead = (watched === 0 && !soakOnly)
            ? '<strong>No tower was actually watched, so this is not evidence '
              + 'about aircraft.</strong><br>'
            : '';
        return lead + lines.join('');
    }

    // The tuning a terminal run left available to persist, or null. A
    // confirmed result wins; otherwise the no-track fallback, which exists
    // precisely so a run that confirmed nothing still leaves something worth
    // keeping (see calibrator.py's _apply_top_tower_fallback). Cancelled runs
    // deliberately have neither.
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
        mhz: mhz,
        fmtSeconds: fmtSeconds,
        escapeHtml: escapeHtml,
        warnBox: warnBox,
        updateWarning: updateWarning,
        preflightNotice: preflightNotice,
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
