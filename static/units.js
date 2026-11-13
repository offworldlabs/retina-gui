// The viewer's choice of kilometres or miles, shared by the Tracker and the
// Passive Radar pages.
//
// A presentation choice kept in this browser under one key, so switching on
// either page switches both. Nothing on the node knows about it: the stream,
// the radar's maps and every range a page reasons with stay in kilometres,
// and only what is drawn and written converts. Each page owns its toggle;
// this file owns the conversions, so there is one copy of the arithmetic.
// See docs/features/tracker.md#units
(function () {
    var KEY = 'display.units';
    var KM_PER_MI = 1.609344;
    var MPS_PER_MPH = 0.44704;
    var MPS_PER_KMH = 1 / 3.6;

    // 'metric' unless this browser has chosen 'imperial'. A blocked or empty
    // localStorage (private mode) reads as metric.
    function get() {
        try { return localStorage.getItem(KEY) === 'imperial' ? 'imperial' : 'metric'; }
        catch (e) { return 'metric'; }
    }

    function set(name) {
        try { localStorage.setItem(KEY, name === 'imperial' ? 'imperial' : 'metric'); }
        catch (e) { /* private mode: the page still switches for this load */ }
    }

    // The conversions for the chosen units: the stored choice, or `name` when
    // a page passes the value just pressed. Pages hold the result and refresh
    // it when the toggle is pressed.
    function current(name) {
        var imperial = (name || get()) === 'imperial';
        var kmPerDist = imperial ? KM_PER_MI : 1;
        var mpsPerSpeed = imperial ? MPS_PER_MPH : MPS_PER_KMH;
        function distOf(km) { return km / kmPerDist; }
        return {
            name: imperial ? 'imperial' : 'metric',
            imperial: imperial,
            dist: imperial ? 'mi' : 'km',
            speed: imperial ? 'mph' : 'km/h',
            distOf: distOf,
            kmOf: function (dist) { return dist * kmPerDist; },
            // A whole number of the unit, for button labels and alerts.
            fmtDist: function (km) { return Math.round(distOf(km)) + (imperial ? ' mi' : ' km'); },
            // A speed in m/s, as the stream reports it, as a whole number of km/h or mph.
            fmtSpeedMps: function (mps) { return Math.round(mps / mpsPerSpeed) + (imperial ? ' mph' : ' km/h'); }
        };
    }

    window.DisplayUnits = { KEY: KEY, get: get, set: set, current: current };
})();
