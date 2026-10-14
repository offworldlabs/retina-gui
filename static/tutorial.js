// The guided tutorial: dims the page, lights up what the current step is
// about, and points at it from a card with Previous, Next and Skip tutorial.
// The steps, their order and their wording come from the server
// (window.OWL_TUTORIAL, built by src/tutorial.py); this file only draws them.
// See docs/features/tutorial.md
(function () {
    'use strict';

    var data = window.OWL_TUTORIAL;
    if (!data || !data.steps || !data.steps.length) return;

    var SVG_NS = 'http://www.w3.org/2000/svg';
    var PAD = 6;          // breathing room around a lit region
    var RADIUS = 10;      // matches --r-md, so a lit card keeps its corners
    var EDGE = 12;        // the card's minimum distance from the viewport edge
    var GAP = 44;         // the card's distance from the region it describes
    var MIN_ARROW = 24;   // shorter than this and an arrow is only clutter
    var HEAD = 11;        // arrowhead length
    var MARGIN_GAP = 30;  // between a card in the margin and the page's column
    var MIN_CARD = 220;   // narrower than this, a card in the margin is unreadable
    var MAX_CARD = 324;   // the card's width when it has the room (as tutorial.css)
    var MIN_WIDE = 460;   // the least a picture card shrinks to before it overlaps instead

    var steps = data.steps;
    var current = data.current;
    var page = steps[current].page;
    var closed = false;
    var litNow = [];      // the regions drawn by the last layout, for hit-testing

    // ── DOM ─────────────────────────────────────────────────────

    function el(tag, className, text) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        if (text) node.textContent = text;
        return node;
    }

    function svgEl(tag, attrs) {
        var node = document.createElementNS(SVG_NS, tag);
        for (var key in attrs) node.setAttribute(key, attrs[key]);
        return node;
    }

    var root = el('div', 'tut');
    root.setAttribute('role', 'dialog');
    root.setAttribute('aria-modal', 'true');
    root.setAttribute('aria-labelledby', 'tutTitle');
    root.setAttribute('aria-describedby', 'tutBody');

    var svg = svgEl('svg', { 'class': 'tut-svg', 'aria-hidden': 'true' });
    var defs = svgEl('defs', {});
    var mask = svgEl('mask', { id: 'tutMask', maskUnits: 'userSpaceOnUse' });
    defs.appendChild(mask);
    svg.appendChild(defs);
    var dim = svgEl('rect', { 'class': 'tut-dim', x: 0, y: 0, mask: 'url(#tutMask)' });
    svg.appendChild(dim);
    var rings = svgEl('g', {});
    svg.appendChild(rings);
    var arrows = svgEl('g', { 'class': 'tut-arrows' });
    svg.appendChild(arrows);
    root.appendChild(svg);

    var card = el('div', 'tut-card');
    card.tabIndex = -1;
    var meta = el('div', 'tut-meta');
    var count = el('span');
    var bar = el('div', 'tut-bar');
    var barFill = el('div', 'tut-bar-fill');
    bar.appendChild(barFill);
    meta.appendChild(count);
    meta.appendChild(bar);

    var title = el('h2', 'tut-title');
    title.id = 'tutTitle';
    var body = el('p', 'tut-body');
    body.id = 'tutBody';

    var figure = el('figure', 'tut-figure');
    var frame = el('div', 'tut-frame');
    var image = el('img');
    // Drawn over the image in the image's own pixels, so a mark stays on its
    // dot whatever size the image is shown at.
    var marks = svgEl('svg', { 'class': 'tut-marks', 'aria-hidden': 'true', preserveAspectRatio: 'none' });
    var caption = el('figcaption', '', 'Example view');
    frame.appendChild(image);
    frame.appendChild(marks);
    figure.appendChild(frame);
    figure.appendChild(caption);

    var link = el('a', 'tut-link');
    link.target = '_blank';
    link.rel = 'noopener';
    var note = el('p', 'tut-note');

    var actions = el('div', 'tut-actions');
    var skipBtn = el('button', 'tut-skip', 'Skip tutorial');
    var prevBtn = el('button', 'ds-btn ghost', 'Previous');
    var nextBtn = el('button', 'ds-btn primary', 'Next');
    skipBtn.type = prevBtn.type = nextBtn.type = 'button';
    // Previous and Next travel together, so a narrow card wraps them onto
    // their own line rather than splitting them.
    var nav = el('div', 'tut-nav');
    nav.appendChild(prevBtn);
    nav.appendChild(nextBtn);
    actions.appendChild(skipBtn);
    actions.appendChild(nav);

    [meta, title, body, figure, link, note, actions].forEach(function (node) {
        card.appendChild(node);
    });
    root.appendChild(card);

    // ── Geometry ────────────────────────────────────────────────

    // The size of what the overlay covers. Not viewWidth(), which counts
    // the scrollbar: page coordinates do not, so everything drawn would be
    // scaled and drift off its target towards the far edge.
    function viewWidth() { return root.clientWidth; }
    function viewHeight() { return root.clientHeight; }

    function clamp(value, low, high) {
        return Math.max(low, Math.min(high, value));
    }

    // True for anything that stays put while the page scrolls (the banner, the
    // config side nav, the save bar): scrolling to it would achieve nothing.
    function isPinned(node) {
        for (; node && node !== document.body; node = node.parentElement) {
            var position = getComputedStyle(node).position;
            if (position === 'fixed' || position === 'sticky') return true;
        }
        return false;
    }

    // How far a lit region may extend past its elements on each side: PAD,
    // or half the gap to the nearest neighbouring element when that is less,
    // so the highlight never spills onto something it is not about.
    function paddingFor(box, nodes) {
        var gap = { left: Infinity, top: Infinity, right: Infinity, bottom: Infinity };
        nodes.forEach(function (node) {
            var siblings = node.parentElement ? node.parentElement.children : [];
            for (var i = 0; i < siblings.length; i++) {
                if (nodes.indexOf(siblings[i]) !== -1) continue;
                var s = siblings[i].getBoundingClientRect();
                if (s.width < 1 || s.height < 1) continue;
                var level = s.top < box.bottom && s.bottom > box.top;
                var inline = s.left < box.right && s.right > box.left;
                if (level && s.right <= box.left) gap.left = Math.min(gap.left, box.left - s.right);
                if (level && s.left >= box.right) gap.right = Math.min(gap.right, s.left - box.right);
                if (inline && s.bottom <= box.top) gap.top = Math.min(gap.top, box.top - s.bottom);
                if (inline && s.top >= box.bottom) gap.bottom = Math.min(gap.bottom, s.top - box.bottom);
            }
        });
        function pad(space) { return Math.min(PAD, Math.floor(space / 2)); }
        return { left: pad(gap.left), top: pad(gap.top), right: pad(gap.right), bottom: pad(gap.bottom) };
    }

    // One lit region per target: the box around every visible element that
    // carries the target's name. Null when none is on the page.
    function regionFor(target) {
        var all = document.querySelectorAll('[data-tutorial~="' + target.name + '"]');
        var nodes = [];
        var box = null;
        var pinned = true;
        for (var i = 0; i < all.length; i++) {
            var r = all[i].getBoundingClientRect();
            if (r.width < 1 || r.height < 1) continue;
            nodes.push(all[i]);
            if (!box) {
                box = { left: r.left, top: r.top, right: r.right, bottom: r.bottom };
            } else {
                box.left = Math.min(box.left, r.left);
                box.top = Math.min(box.top, r.top);
                box.right = Math.max(box.right, r.right);
                box.bottom = Math.max(box.bottom, r.bottom);
            }
            if (!isPinned(all[i])) pinned = false;
        }
        if (!box) return null;
        var pad = paddingFor(box, nodes);
        return {
            left: box.left - pad.left, top: box.top - pad.top,
            right: box.right + pad.right, bottom: box.bottom + pad.bottom,
            arrow: target.arrow, pinned: pinned
        };
    }

    function regions() {
        var found = [];
        steps[current].targets.forEach(function (target) {
            var region = regionFor(target);
            if (region) found.push(region);
        });
        return found;
    }

    function onScreen(r) {
        return r.right > 0 && r.bottom > 0 && r.left < viewWidth() && r.top < viewHeight();
    }

    function overlap(a, b) {
        var w = Math.min(a.right, b.right) - Math.max(a.left, b.left);
        var h = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
        return w > 0 && h > 0 ? w * h : 0;
    }

    function stickyHeadHeight() {
        var nav = document.querySelector('.page > .nav');
        if (!nav || getComputedStyle(nav).position !== 'sticky') return 0;
        return nav.getBoundingClientRect().height;
    }

    // The empty margins either side of the page's column, and how wide a card
    // standing in one can be. Null when the window is too narrow for a card
    // to fit beside the page. See docs/features/tutorial.md#the-overlay
    function margins() {
        // A step with a picture uses a wide card, which no margin can hold.
        if (steps[current].image) return null;
        var column = document.querySelector('.shell') || document.querySelector('main.main');
        if (!column) return null;
        var box = column.getBoundingClientRect();
        var style = getComputedStyle(column);
        // The column's own padding is empty too. Lit rings reach PAD beyond it.
        var left = box.left + parseFloat(style.paddingLeft) - PAD;
        var right = box.right - parseFloat(style.paddingRight) + PAD;
        var room = Math.min(left, viewWidth() - right) - EDGE - MARGIN_GAP;
        if (room < MIN_CARD) return null;
        return { left: left, right: right, width: Math.min(MAX_CARD, room) };
    }

    // Bring the step's regions into view. Returns true when the page is left
    // gliding there, false when it is already in place.
    // With the card in the margin the
    // region is simply centred. Otherwise room is left for the card: region
    // then card, centred, when both fit; the region at the bottom with the
    // card above it when only the region fits. Either way a region taller
    // than the window is top-aligned under the banner.
    function scrollIntoPlace(smooth) {
        var moving = regions().filter(function (r) { return !r.pinned; });
        if (!moving.length) return false;
        var top = Infinity;
        var bottom = -Infinity;
        moving.forEach(function (r) {
            top = Math.min(top, r.top);
            bottom = Math.max(bottom, r.bottom);
        });
        var roomTop = stickyHeadHeight() + 16;
        var roomBottom = viewHeight() - 16;
        var room = roomBottom - roomTop;
        var height = bottom - top;
        var withCard = margins() ? height : height + GAP + card.offsetHeight;
        var wanted;
        if (withCard <= room) {
            wanted = roomTop + (room - withCard) / 2;
        } else if (height <= room) {
            wanted = roomBottom - height;
        } else {
            wanted = roomTop;
        }
        if (Math.abs(top - wanted) < 2) return false;
        var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
        var glide = smooth && !reduce;
        window.scrollTo({ top: window.scrollY + top - wanted, behavior: glide ? 'smooth' : 'auto' });
        return glide;
    }

    function gapBetween(a, b) {
        var dx = Math.max(0, a.left - b.right, b.left - a.right);
        var dy = Math.max(0, a.top - b.bottom, b.top - a.bottom);
        return Math.sqrt(dx * dx + dy * dy);
    }

    // Put the card where it covers the least of what is lit, as close to the
    // region it describes as that allows. The preferred spots sit beside that
    // region; the grid behind them is what finds a place when a region is as
    // wide or as tall as the window and some overlap cannot be avoided.
    function placeCard(lit) {
        var vw = viewWidth();
        var vh = viewHeight();
        var side = margins();
        var anchor = lit.filter(function (r) { return r.arrow; })[0] || lit[0];
        // Set before measuring: the card's height follows from its width.
        card.style.width = side ? side.width + 'px' : '';
        // A wide card that fits neither above nor below its region has to
        // stand beside it, so it gives up width (and picture size) rather
        // than cover the very thing it is pointing at.
        if (steps[current].image && anchor) {
            var tall = card.offsetHeight + GAP + EDGE;
            var above = anchor.top - stickyHeadHeight() >= tall;
            var below = vh - anchor.bottom >= tall;
            if (!above && !below) {
                var beside = Math.max(anchor.left, vw - anchor.right) - GAP - EDGE;
                if (beside < card.offsetWidth) card.style.width = Math.max(MIN_WIDE, beside) + 'px';
            }
        }
        var cw = card.offsetWidth;
        var ch = card.offsetHeight;
        var maxX = Math.max(EDGE, vw - cw - EDGE);
        var maxY = Math.max(EDGE, vh - ch - EDGE);
        var middleY = anchor ? (clamp(anchor.top, 0, vh) + clamp(anchor.bottom, 0, vh)) / 2 : vh / 2;
        var best = null;

        function consider(x, y, penalty) {
            x = clamp(x, EDGE, maxX);
            y = clamp(y, EDGE, maxY);
            var box = { left: x - 10, top: y - 10, right: x + cw + 10, bottom: y + ch + 10 };
            var cost = penalty;
            lit.forEach(function (r) { cost += overlap(box, r); });
            if (anchor) {
                // Too close leaves no room for the arrow; too far loses the link.
                var gap = gapBetween({ left: x, top: y, right: x + cw, bottom: y + ch }, anchor);
                cost += gap < GAP ? (GAP - gap) * 4 : (gap - GAP) * 0.5;
            }
            if (!best || cost < best.cost) best = { x: x, y: y, cost: cost };
        }

        // In the margin: beside the page, level with the region, on whichever
        // side is nearer to it, so the page itself stays fully in view.
        function considerMargin(x, y, level) {
            var box = { left: x - 10, top: y - 10, right: x + cw + 10, bottom: y + ch + 10 };
            var cost = Math.abs(y - level) * 0.5 +
                Math.max(0, anchor.left - (x + cw), x - anchor.right);
            // A region that is itself in the margin (the Config link) needs
            // the card to stand clear of it, or there is no room for an arrow.
            var gap = gapBetween({ left: x, top: y, right: x + cw, bottom: y + ch }, anchor);
            if (gap < MARGIN_GAP) cost += (MARGIN_GAP - gap) * 4;
            lit.forEach(function (r) { cost += overlap(box, r); });
            if (!best || cost < best.cost) best = { x: x, y: y, cost: cost };
        }

        if (!anchor) {
            consider((vw - cw) / 2, (vh - ch) / 2, 0);
        } else if (side) {
            var minY = Math.min(stickyHeadHeight() + EDGE, maxY);
            // Centred on the part of the region that is on screen, so the
            // arrow runs straight across and the card is not pushed to the
            // bottom of the window by a region that sits low on the page.
            var level = clamp(middleY - ch / 2, minY, maxY);
            [side.left - MARGIN_GAP - cw, side.right + MARGIN_GAP].forEach(function (x) {
                considerMargin(x, level, level);
                for (var y = minY; y <= maxY; y += 12) considerMargin(x, y, level);
                considerMargin(x, maxY, level);
            });
        } else {
            var mid = (anchor.left + anchor.right) / 2 - cw / 2;
            [
                [mid, anchor.bottom + GAP],
                [mid, anchor.top - GAP - ch],
                [anchor.right + GAP, middleY - ch / 2],
                [anchor.left - GAP - cw, middleY - ch / 2],
                [anchor.left, anchor.bottom + GAP],
                [anchor.left, anchor.top - GAP - ch]
            ].forEach(function (spot, order) { consider(spot[0], spot[1], order); });
            for (var gx = EDGE; gx <= maxX; gx += 24) {
                for (var gy = EDGE; gy <= maxY; gy += 24) consider(gx, gy, 40);
            }
            // The far edges, which the grid's stride can step over.
            consider(maxX, maxY, 40);
            consider(EDGE, maxY, 40);
            consider(maxX, EDGE, 40);
        }

        card.style.transform = 'translate(' + Math.round(best.x) + 'px,' + Math.round(best.y) + 'px)';
        return { left: best.x, top: best.y, right: best.x + cw, bottom: best.y + ch };
    }

    function drawArrow(from, region) {
        var vw = viewWidth();
        var vh = viewHeight();
        // Aim at the part of the region that is actually on screen.
        var box = {
            left: clamp(region.left, 0, vw), right: clamp(region.right, 0, vw),
            top: clamp(region.top, 0, vh), bottom: clamp(region.bottom, 0, vh)
        };
        // End at the middle of whichever edge of the region faces the card,
        // never at a corner: beside it when the two are side by side (or
        // further apart sideways than vertically), otherwise above or below.
        var gapX = Math.max(box.left - from.right, from.left - box.right);
        var gapY = Math.max(box.top - from.bottom, from.top - box.bottom);
        if (gapX <= 0 && gapY <= 0) return;    // the card is over the region
        var INSET = 18;                        // keeps the tail off the card's rounded corners
        var startX, startY, endX, endY;
        if (gapY <= 0 || gapX >= gapY) {
            var toRight = from.right <= box.left;
            endX = toRight ? box.left : box.right;
            endY = (box.top + box.bottom) / 2;
            startX = toRight ? from.right : from.left;
            startY = clamp(endY, from.top + INSET, from.bottom - INSET);
        } else {
            var down = from.bottom <= box.top;
            endY = down ? box.top : box.bottom;
            endX = (box.left + box.right) / 2;
            startY = down ? from.bottom : from.top;
            startX = clamp(endX, from.left + INSET, from.right - INSET);
        }
        var dx = endX - startX;
        var dy = endY - startY;
        var length = Math.sqrt(dx * dx + dy * dy);
        if (length < MIN_ARROW) return;

        // A slight bow, so the arrow reads as drawn rather than ruled.
        var ux = dx / length;
        var uy = dy / length;
        var bow = Math.min(18, length * 0.12);
        var ctrlX = (startX + endX) / 2 - uy * bow;
        var ctrlY = (startY + endY) / 2 + ux * bow;

        // The head follows the curve's final direction.
        var hx = endX - ctrlX;
        var hy = endY - ctrlY;
        var hl = Math.sqrt(hx * hx + hy * hy) || 1;
        hx /= hl;
        hy /= hl;
        var baseX = endX - hx * HEAD;
        var baseY = endY - hy * HEAD;
        var line = 'M' + startX + ' ' + startY + ' Q' + ctrlX + ' ' + ctrlY + ' ' + baseX + ' ' + baseY;
        var half = HEAD * 0.55;
        var head = 'M' + endX + ' ' + endY +
            ' L' + (baseX - hy * half) + ' ' + (baseY + hx * half) +
            ' L' + (baseX + hy * half) + ' ' + (baseY - hx * half) + ' Z';

        // Both white outlines go down first and both blue shapes on top, so
        // the head's outline never cuts across the line where they join.
        arrows.appendChild(svgEl('path', { 'class': 'tut-arrow-halo', d: line }));
        arrows.appendChild(svgEl('path', { 'class': 'tut-arrow-head-halo', d: head }));
        arrows.appendChild(svgEl('path', { 'class': 'tut-arrow', d: line }));
        arrows.appendChild(svgEl('path', { 'class': 'tut-arrow-head', d: head }));
    }

    // Rings and arrows over the example image, one per point the step names.
    function drawMarks(picture) {
        clear(marks);
        marks.setAttribute('viewBox', '0 0 ' + picture.width + ' ' + picture.height);
        var unit = picture.width / 100;          // everything scales with the image
        var radius = 2.1 * unit;
        picture.marks.forEach(function (mark) {
            // The arrow comes from above right, or from wherever there is room.
            var fromX = mark.x + (mark.x > picture.width * 0.86 ? -1 : 1) * 6.5 * unit;
            var fromY = mark.y + (mark.y < picture.height * 0.22 ? 1 : -1) * 6.5 * unit;
            var dx = mark.x - fromX;
            var dy = mark.y - fromY;
            var length = Math.sqrt(dx * dx + dy * dy);
            var ux = dx / length;
            var uy = dy / length;
            var tipX = mark.x - ux * (radius + 0.5 * unit);
            var tipY = mark.y - uy * (radius + 0.5 * unit);
            var head = 2.2 * unit;
            var baseX = tipX - ux * head;
            var baseY = tipY - uy * head;
            var half = head * 0.55;
            var line = 'M' + fromX + ' ' + fromY + ' L' + baseX + ' ' + baseY;
            var tip = 'M' + tipX + ' ' + tipY +
                ' L' + (baseX - uy * half) + ' ' + (baseY + ux * half) +
                ' L' + (baseX + uy * half) + ' ' + (baseY - ux * half) + ' Z';
            var ring = { cx: mark.x, cy: mark.y, r: radius };
            marks.appendChild(svgEl('circle', Object.assign({ 'class': 'tut-mark-shadow' }, ring)));
            marks.appendChild(svgEl('path', { 'class': 'tut-mark-shadow', d: line }));
            marks.appendChild(svgEl('path', { 'class': 'tut-mark-tip-shadow', d: tip }));
            marks.appendChild(svgEl('circle', Object.assign({ 'class': 'tut-mark' }, ring)));
            marks.appendChild(svgEl('path', { 'class': 'tut-mark', d: line }));
            marks.appendChild(svgEl('path', { 'class': 'tut-mark-tip', d: tip }));
        });
    }

    function clear(node) {
        while (node.firstChild) node.removeChild(node.firstChild);
    }

    function layout() {
        if (closed) return;
        var vw = viewWidth();
        var vh = viewHeight();
        var all = regions();
        var lit = all.filter(onScreen);
        litNow = lit;

        svg.setAttribute('viewBox', '0 0 ' + vw + ' ' + vh);
        dim.setAttribute('width', vw);
        dim.setAttribute('height', vh);
        mask.setAttribute('x', 0);
        mask.setAttribute('y', 0);
        mask.setAttribute('width', vw);
        mask.setAttribute('height', vh);

        clear(mask);
        clear(rings);
        clear(arrows);
        mask.appendChild(svgEl('rect', { x: 0, y: 0, width: vw, height: vh, fill: '#fff' }));
        lit.forEach(function (r) {
            var shape = { x: r.left, y: r.top, width: r.right - r.left, height: r.bottom - r.top, rx: RADIUS };
            var hole = svgEl('rect', shape);
            hole.setAttribute('fill', '#000');
            mask.appendChild(hole);
            var ring = svgEl('rect', shape);
            ring.setAttribute('class', 'tut-ring');
            rings.appendChild(ring);
        });

        // Said only when there is truly nothing to point at (for example the
        // Services cards while the node is in Spectrum mode).
        var nothing = all.length === 0;
        note.hidden = !nothing;
        if (nothing) note.textContent = 'This part of the page is not shown right now, so there is nothing to point at.';

        var box = placeCard(lit);
        var pointed = lit.filter(function (r) { return r.arrow; });
        if (!pointed.length) pointed = lit;
        pointed.forEach(function (r) { drawArrow(box, r); });
    }

    var queued = false;
    function requestLayout() {
        if (queued || closed) return;
        queued = true;
        window.requestAnimationFrame(function () {
            queued = false;
            layout();
        });
    }

    // While a step's scroll is in flight the card and arrows are hidden. Laid
    // out mid-glide they are drawn for where the region happens to be, and
    // then jump when it stops; a picture card, which stands over the page,
    // can land somewhere else entirely for a moment. The rings stay: they
    // only follow. See docs/features/tutorial.md#the-overlay
    var STILL = 140;      // ms without a scroll event before the page counts as stopped
    var stillTimer = null;
    function holdUntilStill() {
        root.classList.add('tut-moving');
        window.clearTimeout(stillTimer);
        stillTimer = window.setTimeout(function () {
            stillTimer = null;
            if (closed) return;
            root.classList.remove('tut-moving');
            layout();
        }, STILL);
    }
    function onScroll() {
        if (stillTimer !== null) holdUntilStill();
        requestLayout();
    }

    // ── Steps ───────────────────────────────────────────────────

    function render(smooth) {
        var step = steps[current];
        count.textContent = 'Tutorial · Step ' + (current + 1) + ' of ' + steps.length;
        barFill.style.width = ((current + 1) / steps.length * 100) + '%';
        title.textContent = step.title;
        body.textContent = step.body;

        figure.hidden = !step.image;
        // Wide enough to read the picture; see .tut-wide in tutorial.css.
        card.classList.toggle('tut-wide', !!step.image);
        if (step.image) {
            drawMarks(step.image);
            // The size comes with the step, so the card's height is right
            // before the image has loaded.
            image.width = step.image.width;
            image.height = step.image.height;
            image.src = step.image.url;
            image.alt = step.image.alt;
        }
        link.hidden = !step.link;
        if (step.link) {
            link.href = step.link.url;
            link.textContent = step.link.label + ' ↗';
        }

        prevBtn.disabled = current === 0;
        nextBtn.textContent = current === steps.length - 1 ? 'Finish' : 'Next';
        // A click-through step has no Next: the owner goes on by clicking the
        // lit thing itself. See docs/features/tutorial.md#click-through-steps
        nextBtn.hidden = step.click;
        root.classList.toggle('tut-clickable', step.click);

        if (scrollIntoPlace(smooth)) holdUntilStill();
        layout();
        // With no Next to land on, focus goes to the card, so that no other
        // button looks like the suggested one.
        (step.click ? card : nextBtn).focus({ preventScroll: true });
    }

    function go(index) {
        if (index < 0) return;
        if (index >= steps.length) {
            window.location.assign(data.exit_url);
            return;
        }
        var step = steps[index];
        // A step on another page is a real navigation; the server hands that
        // page the same run with this step current.
        if (step.page !== page) {
            window.location.assign(step.url);
            return;
        }
        current = index;
        // So a reload resumes on the step being shown, not the one arrived at.
        window.history.replaceState(null, '', step.url);
        render(true);
    }

    function close() {
        closed = true;
        document.removeEventListener('keydown', onKey, true);
        window.removeEventListener('resize', requestLayout);
        window.removeEventListener('scroll', onScroll, true);
        window.clearTimeout(stillTimer);
        if (observer) observer.disconnect();
        root.parentNode.removeChild(root);
        // Drop ?tutorial= so a reload does not bring the tour back.
        var params = new URLSearchParams(window.location.search);
        params.delete('tutorial');
        var query = params.toString();
        window.history.replaceState(null, '', window.location.pathname + (query ? '?' + query : ''));
    }

    function onKey(event) {
        if (event.key === 'Escape') {
            event.preventDefault();
            close();
        } else if (event.key === 'ArrowRight') {
            event.preventDefault();
            // Not on a click-through step: there the click is the only way on.
            if (!steps[current].click) go(current + 1);
        } else if (event.key === 'ArrowLeft') {
            event.preventDefault();
            go(current - 1);
        } else if (event.key === 'Tab') {
            // Keep the keyboard in the card: everything behind it is dimmed
            // and not meant to be operated mid-tour.
            var stops = [skipBtn, prevBtn, nextBtn];
            if (!link.hidden) stops.unshift(link);
            stops = stops.filter(function (node) { return !node.disabled && !node.hidden; });
            var at = stops.indexOf(document.activeElement);
            var next = event.shiftKey ? at - 1 : at + 1;
            if (at === -1) next = 0;
            event.preventDefault();
            stops[(next + stops.length) % stops.length].focus({ preventScroll: true });
        }
    }

    // On a click-through step the lit regions take the click. The overlay still
    // receives it, not the page: what the lit link really points at is another
    // host's address, and the tour has to stay on the node that is serving it.
    function overRegion(event) {
        if (!steps[current].click || card.contains(event.target)) return false;
        return litNow.some(function (r) {
            return event.clientX >= r.left && event.clientX <= r.right &&
                   event.clientY >= r.top && event.clientY <= r.bottom;
        });
    }
    root.addEventListener('click', function (event) {
        if (overRegion(event)) go(current + 1);
    });
    root.addEventListener('mousemove', function (event) {
        root.style.cursor = overRegion(event) ? 'pointer' : '';
    });

    skipBtn.addEventListener('click', close);
    prevBtn.addEventListener('click', function () { go(current - 1); });
    nextBtn.addEventListener('click', function () { go(current + 1); });
    image.addEventListener('load', requestLayout);

    document.addEventListener('keydown', onKey, true);
    window.addEventListener('resize', requestLayout);
    // Capture, so scrolling inside any nested scroller also moves the regions.
    window.addEventListener('scroll', onScroll, true);
    // Pages keep changing height after load (status rows filling in), which
    // moves the regions without any scroll or resize event.
    var observer = window.ResizeObserver ? new ResizeObserver(requestLayout) : null;
    if (observer) observer.observe(document.body);
    window.addEventListener('load', function () {
        if (closed) return;
        scrollIntoPlace(false);
        requestLayout();
    });

    document.body.appendChild(root);
    render(false);
})();
