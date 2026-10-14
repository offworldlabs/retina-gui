// Makes the summary page's flight-path diagram interactive.
// See docs/architecture.md#flight-simulator
(function () {
    "use strict";

    var map = document.getElementById("fs-map");
    // Inert anywhere the markup is absent, and on a browser that served the
    // page but could not run this: the section is a still diagram either way.
    if (!map || !window.SVGElement || !("PointerEvent" in window)) return;

    var plot = document.getElementById("fs-plot");
    var beam = document.getElementById("fs-beam"), beamEdge = document.getElementById("fs-beam-edge");
    var pathEl = document.getElementById("fs-path"), planeEl = document.getElementById("fs-plane");
    var trackEl = document.getElementById("fs-track"), live = document.getElementById("fs-live");
    var aimKnob = document.getElementById("fs-aim"), hint = document.getElementById("fs-hint");
    var playBtn = document.getElementById("fs-play"), clearBtn = document.getElementById("fs-clear");
    var rOut = document.getElementById("fs-range"), fOut = document.getElementById("fs-doppler"), wOut = document.getElementById("fs-width");
    var SVG = "http://www.w3.org/2000/svg";

    var T = { x: 34, y: 186 }, N = { x: 212, y: 186 };
    var aim = -Math.PI / 2, half = Math.PI / 3;      // 120° across, pointing up
    var BASE = Math.hypot(T.x - N.x, T.y - N.y);

    // How far the node hears, and how wide the beam can be opened. The sky's
    // viewBox in summary.html is sized from these two, so the whole wedge is
    // in the picture at its widest with sky to spare: change them and it has
    // to follow.
    // See docs/architecture.md#flight-simulator
    var BEAM_R = 200, WIDEST = 1.35, NARROWEST = 0.2;
    // The sky's viewBox, less a margin: where a path may be drawn.
    var SKY = { left: -54, right: 478, top: -54, bottom: 247 };
    // The plot's box, as drawn in summary.html.
    var PLOT = { x: 46, w: 356, mid: 98, h: 84 };

    // The scale is chosen so the picture cannot reach its own axes. Heard at
    // the far end of the beam this scene tops out at 50 km and 242 Hz, inside
    // a plot that goes to 60 km and 300 Hz, so a track curves away from the
    // edge instead of flattening along it. A track pinned to a bound is a lie:
    // it says the aircraft stopped changing when really the plot ran out.
    var KM = 0.125;                      // 1 unit = 0.125 km: baseline ≈ 22 km, beam ≈ 25 km
    var RANGE_MAX = 60;                  // km, the full width of the plot
    var DOPPLER_MAX = 300;               // Hz, top and bottom of the plot

    // Doppler from the real relation rather than a fudge factor, at a fixed
    // typical centre frequency. 170 m/s is an unremarkable airliner.
    var FC = 213e6, LAMBDA = 299792458 / FC, SPEED = 170;

    // A gentle arc past the node, so the piece says something before anyone
    // touches it. Redrawing replaces it.
    var path = (function () {
        var pts = [];
        for (var i = 0; i <= 40; i++) {
            var t = i / 40;
            pts.push({ x: 20 + t * 380, y: 150 - Math.sin(t * Math.PI) * 118 });
        }
        return pts;
    })();

    var lengths = [], total = 0, travelled = 0, playing = false, raf = 0, last = 0;
    var segments = [], current = null, drawing = false;
    var REDUCED = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    function measure() {
        lengths = [0]; total = 0;
        for (var i = 1; i < path.length; i++) {
            total += Math.hypot(path[i].x - path[i - 1].x, path[i].y - path[i - 1].y);
            lengths.push(total);
        }
    }

    // The direction of travel through point k: from the point before it to
    // the point after.
    function tangent(k) {
        var a = path[Math.max(0, k - 1)], b = path[Math.min(path.length - 1, k + 1)];
        var x = b.x - a.x, y = b.y - a.y, h = Math.hypot(x, y) || 1;
        return { x: x / h, y: y / h };
    }

    function at(d) {
        // Position and heading a distance d along the drawn path.
        if (total <= 0) return null;
        d = d % total;
        var i = 1;
        while (i < lengths.length && lengths[i] < d) i++;
        var a = path[i - 1], b = path[i] || path[i - 1];
        var span = (lengths[i] || total) - lengths[i - 1];
        var f = span > 0 ? (d - lengths[i - 1]) / span : 0;
        // The heading turns steadily from one point to the next. Taken from
        // each straight piece instead, it would jump at every point, and
        // Doppler with it: the track came out as a sawtooth.
        var ta = tangent(i - 1), tb = tangent(Math.min(i, path.length - 1));
        var hx = ta.x + (tb.x - ta.x) * f, hy = ta.y + (tb.y - ta.y) * f, h = Math.hypot(hx, hy) || 1;
        return { x: a.x + (b.x - a.x) * f, y: a.y + (b.y - a.y) * f, hx: hx / h, hy: hy / h };
    }

    // Inside the wedge as it is drawn: within its angle and within its reach.
    // The arc is in the picture, so an aircraft beyond it must not be heard.
    function inBeam(p) {
        if (Math.hypot(p.x - N.x, p.y - N.y) > BEAM_R) return false;
        var delta = Math.atan2(p.y - N.y, p.x - N.x) - aim;
        while (delta > Math.PI) delta -= 2 * Math.PI;
        while (delta < -Math.PI) delta += 2 * Math.PI;
        return Math.abs(delta) <= half;
    }

    // The two numbers the node actually measures.
    function reading(p) {
        // Floored, because a path drawn straight over the tower or the node
        // gives a zero-length leg, and 0/0 makes the unit vector NaN and the
        // whole track vanish. Both are visible targets in the middle of the
        // scene, so somebody will fly over one.
        var d1 = Math.max(0.5, Math.hypot(p.x - T.x, p.y - T.y));
        var d2 = Math.max(0.5, Math.hypot(p.x - N.x, p.y - N.y));
        // Doppler follows how fast both legs together are changing, so it is
        // zero when the heading is perpendicular to the sum of the unit legs,
        // which is not the same as perpendicular to the node. That sum has a
        // magnitude of at most 2, which is what caps the plot at 2v/lambda.
        var ux = (p.x - T.x) / d1 + (p.x - N.x) / d2;
        var uy = (p.y - T.y) / d1 + (p.y - N.y) / d2;
        return {
            km: (d1 + d2 - BASE) * KM,
            hz: -(p.hx * ux + p.hy * uy) * SPEED / LAMBDA
        };
    }

    function drawBeam() {
        var a1 = aim - half, a2 = aim + half, R = BEAM_R;
        var p1 = { x: N.x + Math.cos(a1) * R, y: N.y + Math.sin(a1) * R };
        var p2 = { x: N.x + Math.cos(a2) * R, y: N.y + Math.sin(a2) * R };
        var d = "M" + N.x + " " + N.y + " L" + p1.x.toFixed(1) + " " + p1.y.toFixed(1) +
                " A" + R + " " + R + " 0 " + (half > Math.PI / 2 ? 1 : 0) + " 1 " +
                p2.x.toFixed(1) + " " + p2.y.toFixed(1) + " Z";
        beam.setAttribute("d", d);
        beamEdge.setAttribute("d", d);
        aimKnob.setAttribute("transform",
            "translate(" + (N.x + Math.cos(aim) * 70).toFixed(1) + " " + (N.y + Math.sin(aim) * 70).toFixed(1) + ")");
        wOut.textContent = Math.round(half * 2 * 180 / Math.PI) + "°";
    }

    function drawPath() {
        var d = "";
        for (var i = 0; i < path.length; i++) d += (i ? "L" : "M") + path[i].x.toFixed(1) + " " + path[i].y.toFixed(1) + " ";
        pathEl.setAttribute("d", d);
    }

    function resetTrack() {
        while (trackEl.firstChild) trackEl.removeChild(trackEl.firstChild);
        segments = []; current = null; travelled = 0;
    }

    // range → x, hertz → y. The clamps are a backstop against a stray
    // coordinate, not a working part: the scale above keeps every reachable
    // reading well inside the box.
    function place(r) {
        var x = PLOT.x + Math.max(0, Math.min(RANGE_MAX, r.km)) / RANGE_MAX * PLOT.w;
        var hz = Math.max(-DOPPLER_MAX, Math.min(DOPPLER_MAX, r.hz));
        return { x: x, y: PLOT.mid - hz / DOPPLER_MAX * PLOT.h };
    }

    function paint(p) {
        var r = reading(p), heard = inBeam(p), xy = place(r);

        planeEl.setAttribute("transform",
            "translate(" + p.x.toFixed(1) + " " + p.y.toFixed(1) + ") rotate(" +
            (Math.atan2(p.hy, p.hx) * 180 / Math.PI + 90).toFixed(1) + ")");
        document.getElementById("fs-plane-body")
            .setAttribute("fill", heard ? "var(--accent)" : "var(--ink-3)");

        if (heard) {
            if (!current) {
                current = document.createElementNS(SVG, "polyline");
                current.setAttribute("class", "sim-track");
                trackEl.appendChild(current);
                segments.push([]);
            }
            segments[segments.length - 1].push(xy.x.toFixed(1) + "," + xy.y.toFixed(1));
            current.setAttribute("points", segments[segments.length - 1].join(" "));
            live.setAttribute("cx", xy.x); live.setAttribute("cy", xy.y);
            live.setAttribute("opacity", "1");
            rOut.textContent = r.km.toFixed(1) + " km";
            fOut.textContent = (r.hz >= 0 ? "+" : "") + r.hz.toFixed(0) + " Hz";
        } else {
            // A gap in the track is the honest record: nothing was heard, so
            // nothing is drawn. That break is the point of the beam.
            current = null;
            live.setAttribute("opacity", "0");
            rOut.textContent = "—"; fOut.textContent = "not heard";
        }
    }

    // ── The loop, and its fences ───────────────────────────────
    function frame(now) {
        if (!playing) return;
        var dt = Math.min(0.05, (now - last) / 1000);
        // 30fps is plenty for a track that paints over several seconds, and
        // halves the work of the only thing on this page that has any.
        if (dt >= 1 / 30) {
            last = now;
            travelled += dt * 62;
            if (travelled >= total) resetTrack();
            var p = at(travelled);
            if (p) paint(p);
        }
        raf = requestAnimationFrame(frame);
    }

    function play() {
        if (playing || total <= 0) return;
        playing = true; last = performance.now();
        playBtn.textContent = "Pause";
        raf = requestAnimationFrame(frame);
    }

    function pause() {
        playing = false;
        if (raf) cancelAnimationFrame(raf);
        raf = 0;
        playBtn.textContent = "Play";
    }

    playBtn.addEventListener("click", function () { playing ? pause() : play(); });

    // Stopped, not merely invisible: a hidden tab or a scrolled-past section
    // must not keep a loop alive on somebody's phone.
    document.addEventListener("visibilitychange", function () {
        if (document.hidden && playing) pause();
    });
    if (window.IntersectionObserver) {
        new IntersectionObserver(function (entries) {
            if (!entries[0].isIntersecting && playing) pause();
        }, { threshold: 0 }).observe(map);
    }

    // ── Drawing a path ────────────────────────────────────────
    function inside(p) {
        return { x: Math.max(SKY.left, Math.min(SKY.right, p.x)), y: Math.max(SKY.top, Math.min(SKY.bottom, p.y)) };
    }

    function svgPoint(svg, evt) {
        var pt = svg.createSVGPoint();
        pt.x = evt.clientX; pt.y = evt.clientY;
        return pt.matrixTransform(svg.getScreenCTM().inverse());
    }

    map.addEventListener("pointerdown", function (evt) {
        if (evt.target.closest("#fs-aim")) return;
        pause(); resetTrack();
        drawing = true; path = [];
        map.setPointerCapture(evt.pointerId);
        path.push(inside(svgPoint(map, evt)));
        drawPath();
        hint.textContent = "Keep dragging, then let go to fly it.";
    });

    map.addEventListener("pointermove", function (evt) {
        if (!drawing) return;
        var p = inside(svgPoint(map, evt)), lastPt = path[path.length - 1];
        // Thin the samples: a path is a shape, not a recording of the hand.
        if (Math.hypot(p.x - lastPt.x, p.y - lastPt.y) < 4) return;
        path.push(p);
        drawPath();
    });

    map.addEventListener("pointerup", function (evt) {
        if (!drawing) return;
        drawing = false;
        map.releasePointerCapture(evt.pointerId);
        if (path.length < 3) { path = []; drawPath(); hint.textContent = "That was a dot. Drag a line across the sky."; return; }
        measure();
        hint.textContent = "Grab the blue ring to aim the beam. The track breaks where the aircraft leaves it.";
        play();
    });

    // ── The beam knob ─────────────────────────────────────────
    aimKnob.addEventListener("pointerdown", function (evt) {
        aimKnob.setPointerCapture(evt.pointerId);
        evt.stopPropagation();
    });
    aimKnob.addEventListener("pointermove", function (evt) {
        if (!aimKnob.hasPointerCapture(evt.pointerId)) return;
        var p = svgPoint(map, evt);
        aim = Math.atan2(p.y - N.y, p.x - N.x);
        drawBeam(); resetTrack();
    });
    aimKnob.addEventListener("pointerup", function (evt) { aimKnob.releasePointerCapture(evt.pointerId); });
    aimKnob.addEventListener("keydown", function (evt) {
        if (evt.key !== "ArrowLeft" && evt.key !== "ArrowRight") return;
        evt.preventDefault();
        aim += evt.key === "ArrowLeft" ? -0.12 : 0.12;
        drawBeam(); resetTrack();
    });

    document.getElementById("fs-narrow").addEventListener("click", function () {
        half = Math.max(NARROWEST, half - 0.26); drawBeam(); resetTrack();
    });
    document.getElementById("fs-wide").addEventListener("click", function () {
        half = Math.min(WIDEST, half + 0.26); drawBeam(); resetTrack();
    });
    clearBtn.addEventListener("click", function () {
        pause(); resetTrack(); path = []; drawPath();
        hint.textContent = "Drag across the sky to draw a flight path.";
    });

    // ── First paint, standing still ───────────────────────────
    var controls = document.getElementById("fs-controls");
    if (controls) controls.hidden = false;
    // The knob can be grabbed now, so it may say so.
    var aimLabel = document.getElementById("fs-aim-label");
    if (aimLabel) aimLabel.removeAttribute("display");

    measure(); drawBeam(); drawPath();
    var start = at(0);
    if (start) paint(start);
    resetTrack();
    if (start) {
        planeEl.setAttribute("transform",
            "translate(" + start.x.toFixed(1) + " " + start.y.toFixed(1) + ")");
    }
    // Deliberately not autoplaying: a page nobody has touched should not be
    // animating. With reduced motion, the hint points at Play instead.
    if (REDUCED) hint.textContent = "Press Play to fly the path, or drag to draw your own.";
})();
