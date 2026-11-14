// The Narrow/Full Doppler and delay views, the gear's two narrow ranges and
// the km/mi toggle, shared by the Tracker and the Passive Radar pages.
//
// Presentation only: the node processes its whole span whichever view is
// drawn. Each page keeps its settings under its own prefix (tracker.* or
// radar.*) and owns its drawing; this file owns the ranges, the toggles and
// the dialog's narrow fields. Delay stays in km and only what is shown
// converts (static/units.js). See docs/features/tracker.md#shared-controls
(function () {
    'use strict';

    // Narrow is where ordinary air traffic sits, so it is the Doppler default.
    var DEFAULT_NARROW_HZ = 300;
    var MAX_NARROW_HZ = 100000;    // a sanity cap, used only when the span is unknown
    // Full is the delay default, so nothing is hidden until a viewer asks.
    var DEFAULT_NARROW_KM = 30;
    var MAX_NARROW_KM = 1000;      // a sanity cap, used only when the range is unknown

    // A page's own view settings, kept in this browser under `prefix.`.
    function storage(prefix) {
        return {
            remember: function (key, value) {
                try { localStorage.setItem(prefix + '.' + key, String(value)); } catch (e) { /* private mode */ }
            },
            recall: function (key, fallback) {
                try {
                    var v = localStorage.getItem(prefix + '.' + key);
                    return v === null ? fallback : v;
                } catch (e) { return fallback; }
            }
        };
    }

    // A segmented control: a click presses that button in its group, then
    // calls apply with its value.
    function wireSeg(attr, apply) {
        document.querySelectorAll('.seg [data-' + attr + ']').forEach(function (b) {
            b.addEventListener('click', function () {
                b.parentNode.querySelectorAll('[data-' + attr + ']').forEach(function (o) {
                    o.setAttribute('aria-pressed', String(o === b));
                });
                apply(b.getAttribute('data-' + attr));
            });
        });
    }

    function pressSeg(attr, value) {
        document.querySelectorAll('.seg [data-' + attr + ']').forEach(function (b) {
            b.setAttribute('aria-pressed', String(b.getAttribute('data-' + attr) === String(value)));
        });
    }

    // Rounded, because the radar page widens its span to a map's own extent.
    function fmtHzRange(r) {
        var lo = Math.round(r[0]), hi = Math.round(r[1]);
        return lo === -hi ? '±' + hi : lo + '…' + hi;
    }

    function showFieldError(input, err, text) {
        err.textContent = text;
        err.hidden = !text;
        input.classList.toggle('is-invalid', !!text);
    }

    // Wires one page's views and returns what its drawing code reads.
    //   prefix            'tracker' or 'radar', for the saved settings
    //   state             the page's state object; gains dopView, narrowHz,
    //                     delView and narrowKm
    //   extent()          { x: [lo, hi] km or null, y: [lo, hi] Hz or null },
    //                     read on every call, as the radar's widens
    //   onViewChange()    redraw after a toggle or a save
    //   onUnitsChange(U)  redraw after the units toggle, with the new conversions
    //   labelExtra()      optional: more labels to refresh with the range buttons
    //   dialog            optional hooks for the page's own fields in the gear:
    //                     open(), reset(), read() -> values or null, save(values)
    function create(opts) {
        var state = opts.state;
        var saved = storage(opts.prefix);
        var dialog = opts.dialog || {};
        var U = DisplayUnits.current();

        // ── Doppler ──────────────────────────────────────────────────────

        // Half the full span, the widest narrow range worth having.
        function spanHalfWidth() {
            var y = opts.extent().y;
            return y ? Math.floor(Math.max(Math.abs(y[0]), Math.abs(y[1]))) : null;
        }

        function validNarrow(n) {
            var cap = spanHalfWidth() || MAX_NARROW_HZ;
            return Number.isInteger(n) && n >= 1 && n <= cap ? n : null;
        }

        state.dopView = saved.recall('dopView', 'narrow') === 'full' ? 'full' : 'narrow';
        state.narrowHz = validNarrow(parseInt(saved.recall('narrowHz', DEFAULT_NARROW_HZ), 10)) || DEFAULT_NARROW_HZ;

        // Clipped to the full span, so narrow never draws Doppler the node cannot
        // see. A span lying wholly to one side of the band leaves nothing to
        // narrow to.
        function narrowRange() {
            var y = opts.extent().y, lo = -state.narrowHz, hi = state.narrowHz;
            if (y) {
                lo = Math.max(lo, y[0]);
                hi = Math.min(hi, y[1]);
                if (lo >= hi) return y.slice();
            }
            return [lo, hi];
        }

        // null autoscales, as the full view does while the span is unknown.
        function dopplerRange() {
            return state.dopView === 'narrow' ? narrowRange() : opts.extent().y;
        }

        // ── Delay ────────────────────────────────────────────────────────

        // The furthest processed range, the widest narrow range worth having, in km.
        function delayReach() {
            var x = opts.extent().x;
            return x ? x[1] : null;
        }

        // The narrow range is kept in km, so a browser that switches units keeps
        // the same range. Accepted up to the reach rounded up (the previous
        // build's cap, so its saved values still read), then clipped to the reach.
        function validNarrowKm(km) {
            var reach = delayReach(), cap = reach ? Math.ceil(reach) : MAX_NARROW_KM;
            if (!(Number.isFinite(km) && km > 0 && km <= cap)) return null;
            return reach ? Math.min(km, reach) : km;
        }

        // What the gear accepts: a whole number of the display unit, 1 to the
        // reach rounded up in that unit.
        function reachDist() {
            return Math.ceil(U.distOf(delayReach() || MAX_NARROW_KM));
        }
        function validNarrowDist(n) {
            return Number.isInteger(n) && n >= 1 && n <= reachDist() ? n : null;
        }

        // Clipped to the reach, or a node processing less than 30 km (the drone
        // profile's delayMax) would open the gear on a value it refuses.
        function defaultNarrowKm() {
            var reach = delayReach();
            return reach ? Math.min(DEFAULT_NARROW_KM, reach) : DEFAULT_NARROW_KM;
        }

        state.delView = saved.recall('delView', 'full') === 'narrow' ? 'narrow' : 'full';
        state.narrowKm = validNarrowKm(parseFloat(saved.recall('narrowKm', defaultNarrowKm()))) || defaultNarrowKm();

        // Starts where the node starts (a little below zero, past the direct
        // signal), clipped so narrow never draws range the node cannot see.
        function narrowDelayRange() {
            var x = opts.extent().x, lo = x ? x[0] : 0, hi = state.narrowKm;
            if (x) {
                hi = Math.min(hi, x[1]);
                if (lo >= hi) return x.slice();
            }
            return [lo, hi];
        }

        function delayRange() {
            return state.delView === 'narrow' ? narrowDelayRange() : opts.extent().x;
        }

        // The delay range in the display unit, for the x axis.
        function displayDelayRange() {
            var r = delayRange();
            return r ? [U.distOf(r[0]), U.distOf(r[1])] : null;
        }

        // ── Toggles ──────────────────────────────────────────────────────

        function setDopView(v) {
            state.dopView = v;
            saved.remember('dopView', v);
            pressSeg('dop', v);
            opts.onViewChange();
        }

        function setDelView(v) {
            state.delView = v;
            saved.remember('delView', v);
            pressSeg('del', v);
            opts.onViewChange();
        }

        // From the pressed value, not the store: a blocked localStorage
        // (private mode) must still switch for this load.
        function setUnits(v) {
            DisplayUnits.set(v);
            U = DisplayUnits.current(v);
            pressSeg('units', U.name);
            labelRangeButtons();
            opts.onUnitsChange(U);
        }

        // The buttons name the ranges they draw, so the toggle reads "±300 / ±1000"
        // on a node at ±1000 and stays truthful after the gear changes narrow.
        function labelRangeButtons() {
            var ext = opts.extent();
            document.getElementById('dopNarrowBtn').textContent = fmtHzRange(narrowRange());
            document.getElementById('dopFullBtn').textContent = ext.y ? fmtHzRange(ext.y) : 'Full';
            document.getElementById('delNarrowBtn').textContent = U.fmtDist(narrowDelayRange()[1]);
            document.getElementById('delFullBtn').textContent = ext.x ? U.fmtDist(ext.x[1]) : 'Full';
            document.getElementById('narrowKmUnit').textContent = U.dist;
            if (opts.labelExtra) opts.labelExtra();
        }

        wireSeg('dop', setDopView);
        wireSeg('del', setDelView);
        wireSeg('units', setUnits);
        pressSeg('dop', state.dopView);
        pressSeg('del', state.delView);
        pressSeg('units', U.name);

        // ── Settings dialog ──────────────────────────────────────────────

        var settingsDlg = document.getElementById('settingsDlg');
        var narrowInput = document.getElementById('narrowHzInput');
        var narrowErr = document.getElementById('narrowHzErr');
        var narrowKmInput = document.getElementById('narrowKmInput');
        var narrowKmErr = document.getElementById('narrowKmErr');

        function showNarrowError(text) { showFieldError(narrowInput, narrowErr, text); }
        function showNarrowKmError(text) { showFieldError(narrowKmInput, narrowKmErr, text); }

        document.getElementById('settingsBtn').addEventListener('click', function () {
            var ext = opts.extent();
            narrowInput.value = state.narrowHz;
            var half = spanHalfWidth();
            if (half) narrowInput.max = half;
            document.getElementById('fullSpanText').textContent =
                ext.y ? fmtHzRange(ext.y) + ' Hz' : 'its full span';
            narrowKmInput.value = Math.round(U.distOf(state.narrowKm));
            narrowKmInput.max = reachDist();
            document.getElementById('fullDelayText').textContent =
                ext.x ? 'out to ' + U.fmtDist(ext.x[1]) : 'its full range';
            showNarrowError('');
            showNarrowKmError('');
            if (dialog.open) dialog.open();
            settingsDlg.showModal();
            narrowInput.select();
        });

        document.getElementById('narrowResetBtn').addEventListener('click', function () {
            narrowInput.value = DEFAULT_NARROW_HZ;
            narrowKmInput.value = Math.round(U.distOf(defaultNarrowKm()));
            showNarrowError('');
            showNarrowKmError('');
            if (dialog.reset) dialog.reset();
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
            var extra = dialog.read ? dialog.read() : {};
            // Nothing is saved unless every field is valid.
            if (!hzOk || !kmOk || !extra) return;

            // Typed in the display unit, kept in km. Unchanged if it rounds back
            // to what is stored, so reopening and saving does not re-narrow.
            var km = Math.round(U.distOf(state.narrowKm)) === dist ? state.narrowKm : U.kmOf(dist);
            // The gear's cap is the reach rounded up in the display unit, which
            // can lie past the reach; a km stored past it would not read back.
            if (reach && km > reach) km = reach;
            var hzChanged = n !== state.narrowHz, kmChanged = km !== state.narrowKm;
            state.narrowHz = n;
            state.narrowKm = km;
            saved.remember('narrowHz', n);
            saved.remember('narrowKm', km);
            if (dialog.save) dialog.save(extra);
            labelRangeButtons();
            settingsDlg.close();
            // Saving a new narrow range is asking to see it. Only the axis whose
            // range changed switches, so editing one never narrows the other.
            if (hzChanged) setDopView('narrow');
            if (kmChanged) setDelView('narrow');
            opts.onViewChange();
        });

        return {
            units: function () { return U; },
            spanHalfWidth: spanHalfWidth,
            narrowRange: narrowRange,
            dopplerRange: dopplerRange,
            narrowDelayRange: narrowDelayRange,
            displayDelayRange: displayDelayRange,
            labelRangeButtons: labelRangeButtons,
            setDopView: setDopView,
            setDelView: setDelView
        };
    }

    window.ScopeControls = {
        MAX_NARROW_HZ: MAX_NARROW_HZ,
        storage: storage,
        wireSeg: wireSeg,
        pressSeg: pressSeg,
        fmtHzRange: fmtHzRange,
        showFieldError: showFieldError,
        create: create
    };
})();
