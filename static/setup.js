function formatSize(bytes) {
    var mb = bytes ? '~' + Math.round(bytes / 1024 / 1024) + ' MB' : '~600 MB';
    return mb + ' · 5–10 minutes';
}

// ── Tower presentation helpers ───────────────────────────────────────────
//
// Ports of tower-finder's frontend/src/utils/rankTier.ts, format.ts and
// basemap.ts, kept as close to the originals as ES5 allows so a tower reads
// the same on both surfaces. See docs/features/setup-wizard.md#tower-presentation

// Appends the CARTO key to CARTO tile URLs only. The parameter must be `key`:
// a wrong name fails silently with a watermarked tile.
// See docs/features/setup-wizard.md#basemap-key
var CARTO_HOST = 'basemaps.cartocdn.com';

function withCartoKey(url) {
    var key = window._cartoApiKey || '';
    if (!key || url.indexOf(CARTO_HOST) === -1) return url;
    return url + (url.indexOf('?') === -1 ? '?' : '&') + 'key=' + encodeURIComponent(key);
}

// Number.isFinite semantics without ES6: the global isFinite coerces, so
// isFinite('5') is true and a string would sail through every guard below.
function isNum(v) { return typeof v === 'number' && isFinite(v); }

// Quintile of rank within the returned list. Deliberately from rank, not
// expected_area_km2, so a tier never contradicts the # beside it.
// See docs/features/setup-wizard.md#tower-presentation
var RANK_TIERS = [
    { tier: 1, label: 'Best',   cls: 'rank-1', color: 'var(--rank-1)' },
    { tier: 2, label: 'Upper',  cls: 'rank-2', color: 'var(--rank-2)' },
    { tier: 3, label: 'Middle', cls: 'rank-3', color: 'var(--rank-3)' },
    { tier: 4, label: 'Lower',  cls: 'rank-4', color: 'var(--rank-4)' },
    { tier: 5, label: 'Worst',  cls: 'rank-5', color: 'var(--rank-5)' }
];

function rankTier(rank, total) {
    if (!isNum(rank) || !isNum(total) || total < 1 || rank < 1) return RANK_TIERS[0];
    var t = Math.min(5, Math.max(1, Math.floor(((rank - 1) / total) * 5) + 1));
    return RANK_TIERS[t - 1];
}

// 16-point compass for best_azimuth_deg, which the finder sends without a
// cardinal. Must match bearing_to_cardinal in tower-finder's tower_ranking.py.
var COMPASS_POINTS = [
    'N', 'NNE', 'NE', 'ENE', 'E', 'ESE', 'SE', 'SSE',
    'S', 'SSW', 'SW', 'WSW', 'W', 'WNW', 'NW', 'NNW'
];

function bearingCardinal(deg) {
    if (!isNum(deg)) return '';
    var wrapped = ((deg % 360) + 360) % 360;
    return COMPASS_POINTS[Math.round(wrapped / 22.5) % 16];
}

var AREA_FORMAT = (typeof Intl !== 'undefined' && Intl.NumberFormat)
    ? new Intl.NumberFormat('en-GB', { maximumFractionDigits: 0 })
    : null;

// Whole km2 with thousands separators. Absent renders empty, never 0 or NaN:
// an older finder omits the field.
function formatAreaKm2(km2) {
    if (!isNum(km2)) return '';
    return AREA_FORMAT ? AREA_FORMAT.format(km2) : String(Math.round(km2));
}

// Past its own radio horizon: shown muted rather than dropped.
function beyondHorizon(distanceKm, horizonKm) {
    if (!isNum(horizonKm) || !isNum(distanceKm)) return false;
    return distanceKm > horizonKm;
}

// Step engine and per-step hooks. See docs/features/setup-wizard.md.
function initSetupWizard(resumeStep, devMode, isRerun, demoMode) {
    var steps = [];
    var currentIndex = 0;
    var pollTimer = null;

    document.querySelectorAll('[data-step]').forEach(function(el) {
        steps.push({ name: el.getAttribute('data-step'), el: el });
    });

    var stepNames = {
        agreements: 'Agreements',
        contact: 'Contact and support',
        system: 'System Update',
        radar: 'Packages',
        location: 'Where is your receiver?',
        towers: 'Choose a tower',
        calibrate: 'Tune the receiver',
        name: 'Name your node',
        account: 'Connect your account',
        complete: 'You\'re all set'
    };

    var track = document.getElementById('progressTrack');
    steps.forEach(function(s, i) {
        var dot = document.createElement('div');
        dot.className = 'progress-dot';
        dot.setAttribute('data-dot', i);
        dot.title = stepNames[s.name] || '';
        // Insert before progressLabel so dots appear left of the label
        var lbl = document.getElementById('progressLabel');
        if (lbl) { track.insertBefore(dot, lbl); } else { track.appendChild(dot); }
    });

    function updateProgress(index) {
        var label = document.getElementById('progressLabel');
        var fill = document.getElementById('progressFill');
        var total = steps.length;

        label.textContent = 'Step ' + (index + 1) + ' of ' + total;

        var pct = total > 1 ? (index / (total - 1)) * 100 : 0;
        fill.style.width = pct + '%';

        track.querySelectorAll('.progress-dot').forEach(function(dot, i) {
            dot.className = 'progress-dot';
            if (i === index) {
                dot.classList.add('active');
            } else if (i < index) {
                dot.classList.add('complete');
            }
        });
    }

    function showStep(index) {
        var leaveFn = leaveHooks[steps[currentIndex].name];
        var leavePromise = Promise.resolve(leaveFn ? leaveFn() : null);

        // Disable navigation while an async leave hook (e.g. mode revert) completes.
        var navBtns = document.querySelectorAll('.step-foot-btns button');
        navBtns.forEach(function(b) { b.disabled = true; });

        function doTransition() {
            navBtns.forEach(function(b) { b.disabled = false; });

            if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }

            var card = document.querySelector('.setup-card');
            var towersIndex = -1;
            for (var i = 0; i < steps.length; i++) {
                if (steps[i].name === 'towers') { towersIndex = i; break; }
            }
            card.classList.toggle('wide', index === towersIndex);

            steps.forEach(function(s, i) {
                s.el.style.display = (i === index) ? '' : 'none';
            });
            currentIndex = index;
            updateProgress(index);

            document.querySelectorAll('.step-foot-btns').forEach(function(el) {
                el.style.display = 'none';
            });
            var activeBtns = document.getElementById('stepBtns-' + steps[index].name);
            if (activeBtns) activeBtns.style.display = '';

            // Best effort: a failed save only costs the resume point, and
            // postJSON already raises the session notice if that is the cause.
            // The last step is the exception, because its save is what ends the
            // wizard rather than just recording a place in it: enterHooks
            // .complete sends that one itself and waits for it.
            // See docs/features/setup-wizard.md#completion
            if (steps[index].name !== 'complete') {
                postJSON('/set-up/save-step', { step: steps[index].name })
                    .catch(function() {});
            }

            var enterFn = enterHooks[steps[index].name];
            if (enterFn) enterFn();
        }

        // Always transition regardless of leave hook success or failure.
        leavePromise.then(doTransition, doTransition);
    }

    function advance() {
        if (currentIndex < steps.length - 1) {
            showStep(currentIndex + 1);
        }
    }

    // ── Helpers ──────────────────────────────────────────

    function esc(s) {
        var d = document.createElement('div');
        d.textContent = s;
        return d.innerHTML;
    }

    var csrfToken = (document.querySelector('meta[name="csrf-token"]') || {}).content || '';

    // Rejects on any non-2xx, since fetch() resolves for every status and a
    // .then()-only caller would treat a 400 as success.
    // See docs/features/setup-wizard.md#session-expiry
    // `keepalive` lets the request outlive the page: a navigation cancels an
    // ordinary fetch that has not left the browser yet.
    function postJSON(url, body, keepalive) {
        return fetch(url, {
            method: 'POST',
            keepalive: !!keepalive,
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken
            },
            body: body ? JSON.stringify(body) : undefined
        }).then(function(r) {
            if (r.ok) return r;
            return r.json().then(function(data) {
                throw failure(r, data);
            }, function() {
                throw failure(r, null);
            });
        });
    }

    function failure(r, data) {
        if (data && data.session_expired) showSessionExpired();
        var err = new Error((data && data.error) || ('Request failed (' + r.status + ')'));
        err.status = r.status;
        // Per-field refusals from a 400, so a step can show the server's
        // reason rather than a connection error.
        err.errors = (data && data.errors) || null;
        return err;
    }

    // Once only: every button stops working when the session goes, and a
    // reload (which resumes at the recorded step) is the only fix.
    var sessionExpiredShown = false;
    function showSessionExpired() {
        if (sessionExpiredShown) return;
        sessionExpiredShown = true;
        // Reloading is the instruction, so don't also challenge them with
        // the browser's "leave site?" dialog on the way out.
        window.removeEventListener('beforeunload', handleBeforeUnload);
        var overlay = document.createElement('div');
        overlay.className = 'wiz-expired';
        var box = document.createElement('div');
        box.className = 'wiz-expired-box';
        var h = document.createElement('h3');
        h.textContent = 'This page timed out';
        var p = document.createElement('p');
        p.textContent = 'Your setup session expired while this page was open. '
            + 'Reload to continue from where you left off. Nothing you have '
            + 'already confirmed is lost.';
        var btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'ds-btn primary';
        btn.textContent = 'Reload and continue';
        btn.addEventListener('click', function() { window.location.reload(); });
        box.appendChild(h);
        box.appendChild(p);
        box.appendChild(btn);
        overlay.appendChild(box);
        document.body.appendChild(overlay);
    }

    // Single beforeunload guard for the whole wizard. On the location step it
    // also releases the SDR so retina-spectrum stops if the owner leaves.
    function handleBeforeUnload(e) {
        if (steps[currentIndex] && steps[currentIndex].name === 'location') {
            var fd = new FormData();
            fd.append('csrf_token', csrfToken);
            if (navigator.sendBeacon) navigator.sendBeacon('/api/mode/release-spectrum', fd);
        }
        e.preventDefault();
        e.returnValue = '';
    }
    window.addEventListener('beforeunload', handleBeforeUnload);

    // ── Demo mode: fakes some API calls, not all ─────────
    // See docs/features/setup-wizard.md#demo-and-dev-modes
    if (demoMode) {
        var _demoNodeInstalling = false;
        var _realFetch = window.fetch;
        window.fetch = function(url, opts) {
            var path = url.split('?')[0];
            function ok(body) {
                return Promise.resolve({ ok: true, json: function() { return Promise.resolve(body); } });
            }
            if (path === '/mender/cloud-services')  return ok({ success: true, enabled: true });
            if (path === '/mender/check-os')         return ok({ current_version: '2.4.1-demo', update_available: false });
            if (path === '/mender/check') {
                if (_demoNodeInstalling) {
                    return ok({ installing: true, stage: 'pulling', reason: 'Installing retina-node-v1.0.0-demo' });
                }
                return ok({ installing: false, current_version: 'v0.9.0-demo' });
            }
            if (path === '/mender/install') {
                _demoNodeInstalling = true;
                setTimeout(function() { _demoNodeInstalling = false; }, 8000);
                return ok({ success: true });
            }
            if (path === '/towers/select')               return ok({ success: true, applied: false });
            if (path === '/towers/cache/set')            return ok({ success: true, towers: [] });
            if (path === '/api/mode' && (!opts || opts.method !== 'POST')) return ok({ mode: 'spectrum' });
            if (path === '/api/mode') return ok({ success: true, mode: 'spectrum' });
            if (path === '/set-up/save-step')          return ok({ success: true });
            if (path === '/set-up/complete')           return ok({ success: true });
            // Settings a demo must not change on a live node: the filters
            // (a stack restart), the support switches and the name.
            if (path === '/remote-access/toggle')      return ok({ ok: true, enabled: /enabled=1/.test((opts && opts.body) || '') });
            if (path === '/remote-access/shell')       return ok({ ok: true, shell_allowed: /allowed=1/.test((opts && opts.body) || '') });
            if (path === '/node-name')                 return ok({ ok: true, name: 'demo' });
            return _realFetch(url, opts);
        };

    }

    // ── Enter / leave hooks ──────────────────────────────

    var enterHooks = {};
    var leaveHooks = {};
    var hookInitialized = {};

    // Shared state for spectrum wizard activation (location step)
    var rfSse = null;
    var rfSseReconnectTimer = null;
    var wizardWasMode = null;
    var connectRfSse = null; // defined inside enterHooks.location on first entry
    var locationActive = false;    // guards against dangling fetch resolving after leave
    var pendingModeSwitch = null;  // tracks in-flight /api/mode POST so the leave hook can serialise the revert
    var spectrumGating = false;    // true while scan is in progress; gates Find Towers
    var abortAddressLookups = null; // defined inside enterHooks.location on first entry
    var refreshLocationMap = null;  // defined inside enterHooks.location on first entry
    var refreshFindBtn = null;      // defined inside enterHooks.location on first entry

    // Agreements. Three boxes: the EULA, the Privacy Policy, then cloud
    // services. See docs/features/setup-wizard.md#agreements
    enterHooks.agreements = function() {
        var boxes = ['eulaCheck', 'privacyCheck', 'cloudCheck'];
        var btn = document.getElementById('agreementsContinueBtn');

        function update() {
            var allChecked = boxes.every(function(id) {
                return document.getElementById(id).checked;
            });
            btn.disabled = !allChecked;
        }

        // Derived on every entry, not just the first, so re-entry never finds
        // a stale "Connecting..." button. See docs/features/setup-wizard.md#navigation
        btn.textContent = 'Continue';
        update();

        if (hookInitialized.agreements) return;
        hookInitialized.agreements = true;

        boxes.forEach(function(id) {
            document.getElementById(id).addEventListener('change', update);
        });

        if (demoMode) {
            boxes.forEach(function(id) {
                var el = document.getElementById(id);
                if (el) el.checked = true;
            });
            btn.disabled = false;
        }

        btn.addEventListener('click', function() {
            btn.disabled = true;
            btn.textContent = 'Connecting...';
            // Never in demo mode: the boxes are pre-ticked, so it would record
            // an acceptance nobody gave.
            var recordConsent = demoMode
                ? Promise.resolve()
                : postJSON('/set-up/consent', {}).then(function(r) { return r.json(); });
            // Consent first: it is what unblocks telemetry, even if the cloud
            // services toggle then fails.
            recordConsent
            .then(function() { return postJSON('/mender/cloud-services', {enabled: true}); })
            .then(function(r) { return r.json(); })
            .then(function() { advance(); })
            .catch(function() {
                btn.disabled = false;
                btn.textContent = 'Continue';
            });
        });
    };

    // Contact details. Optional, and skipping writes nothing.
    // See docs/features/setup-wizard.md#contact-details
    enterHooks.contact = function() {
        var fields = {
            first_name: 'contactFirstName',
            last_name:  'contactLastName',
            email:      'contactEmail',
            phone:      'contactPhone',
            country:    'contactCountry'
        };
        var saveBtn = document.getElementById('contactSaveBtn');
        var skipBtn = document.getElementById('contactSkipBtn');
        var msg = document.getElementById('contactMsg');

        function collect() {
            var out = {};
            Object.keys(fields).forEach(function(key) {
                out[key] = document.getElementById(fields[key]).value;
            });
            return out;
        }

        function say(text, danger) {
            msg.textContent = text;
            msg.style.color = danger ? 'var(--danger)' : 'var(--ink-3)';
            msg.style.display = text ? '' : 'none';
        }

        // The boxes are prefilled server-side (routes/setup.py).
        say('');
        saveBtn.disabled = false;
        saveBtn.textContent = 'Save and continue';

        if (hookInitialized.contact) return;
        hookInitialized.contact = true;

        saveBtn.addEventListener('click', function() {
            saveBtn.disabled = true;
            saveBtn.textContent = 'Saving...';
            say('');
            // postJSON rejects on any refusal, carrying the server's per-field
            // reason when there is one, such as a bad country code.
            postJSON('/set-up/contact', collect())
            .then(function() { advance(); })
            .catch(function(err) {
                var errors = err.errors || {};
                var first = Object.keys(errors)[0];
                say(first ? errors[first] : 'Could not save. Check the connection and try again.', true);
                saveBtn.disabled = false;
                saveBtn.textContent = 'Save and continue';
            });
        });

        // Writes nothing: stored details are left alone, not withdrawn.
        skipBtn.addEventListener('click', function() { advance(); });

        // The Support section. Each switch drives the Configuration page's own
        // route the moment it is flipped, and reverts on failure, so neither
        // Save nor Skip has anything to do with them. Form-encoded, as those
        // routes read request.form. See docs/features/setup-wizard.md#remote-support
        var supportMsg = document.getElementById('wizSupportMsg');
        function saySupport(text, danger) {
            supportMsg.textContent = text;
            supportMsg.style.color = danger ? 'var(--danger)' : 'var(--ink-3)';
            supportMsg.style.display = text ? '' : 'none';
        }
        function wireSwitch(id, url, field, describe) {
            var input = document.getElementById(id);
            if (!input) return;
            input.addEventListener('change', function() {
                var want = input.checked;
                input.disabled = true;
                saySupport('');
                fetch(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/x-www-form-urlencoded',
                               'X-CSRFToken': csrfToken },
                    body: field + '=' + (want ? '1' : '0')
                })
                .then(function(r) {
                    return r.json().then(function(j) { return { ok: r.ok, body: j || {} }; },
                                         function() { return { ok: false, body: {} }; });
                })
                .then(function(res) {
                    if (!res.ok) {
                        input.checked = !want;
                        saySupport(res.body.error || 'Could not change this setting.', true);
                        return;
                    }
                    saySupport(describe(res.body));
                })
                .catch(function() {
                    input.checked = !want;
                    saySupport('Could not reach the node.', true);
                })
                .then(function() { input.disabled = false; });
            });
        }
        wireSwitch('wizRemoteToggle', '/remote-access/toggle', 'enabled', function(b) {
            return b.enabled
                ? 'Support may open this node’s interface. The connection comes up on its own.'
                : 'Support can only reach this node from your own network.';
        });
        // An ok response means enforced: the route enforces before it records.
        wireSwitch('wizShellToggle', '/remote-access/shell', 'allowed', function(b) {
            return b.shell_allowed
                ? 'Support may open a command line on this node.'
                : 'No command-line session can be opened, from anywhere.';
        });
    };

    // Name your node. Optional; an empty box on Save, or Skip, leaves the
    // name alone. Posts to /node-name, the Configuration page's rename route,
    // form-encoded as it expects. See docs/features/setup-wizard.md#name-your-node
    enterHooks.name = function() {
        var box = document.getElementById('wizNodeName');
        var saveBtn = document.getElementById('wizNodeNameSaveBtn');
        var skipBtn = document.getElementById('wizNodeNameSkipBtn');
        var msg = document.getElementById('wizNodeNameMsg');

        function say(text, danger) {
            msg.textContent = text;
            msg.style.color = danger ? 'var(--danger)' : 'var(--ink-3)';
            msg.style.display = text ? '' : 'none';
        }

        say('');
        saveBtn.disabled = false;
        saveBtn.textContent = 'Save and continue';

        if (hookInitialized.name) return;
        hookInitialized.name = true;

        skipBtn.addEventListener('click', function() { advance(); });

        saveBtn.addEventListener('click', function() {
            var name = box.value.trim();
            // Nothing typed is Skip by another name: the stored name, if any,
            // is left alone rather than cleared.
            if (!name) { advance(); return; }
            saveBtn.disabled = true;
            saveBtn.textContent = 'Saving...';
            say('');
            fetch('/node-name', {
                method: 'POST',
                headers: { 'Content-Type': 'application/x-www-form-urlencoded',
                           'X-CSRFToken': csrfToken },
                body: 'name=' + encodeURIComponent(name)
            })
            .then(function(r) {
                return r.json().then(function(j) { return { ok: r.ok, body: j || {} }; },
                                     function() { return { ok: false, body: {} }; });
            })
            .then(function(res) {
                if (!res.ok) {
                    say(res.body.error || 'Could not save the name.', true);
                    saveBtn.disabled = false;
                    saveBtn.textContent = 'Save and continue';
                    return;
                }
                advance();
            })
            .catch(function() {
                say('Could not save. Check the connection and try again.', true);
                saveBtn.disabled = false;
                saveBtn.textContent = 'Save and continue';
            });
        });
    };

    // Node claim. Optional; skipping writes nothing. The last step before
    // completion. The step key is `account`, not `claim`: the old flow had
    // the claim third, and its saved step name must not match this one.
    // See docs/features/setup-wizard.md#node-claim
    enterHooks.account = function() {
        var box = document.getElementById('wizClaimEmail');
        var sendBtn = document.getElementById('wizClaimSendBtn');
        var skipBtn = document.getElementById('wizClaimSkipBtn');
        var msg = document.getElementById('wizClaimMsg');

        function say(text, danger) {
            msg.textContent = text;
            msg.style.color = danger ? 'var(--danger)' : 'var(--ink-3)';
            msg.style.display = text ? '' : 'none';
        }

        function reset() {
            sendBtn.disabled = false;
            sendBtn.textContent = 'Send link and continue';
        }

        say('');

        // No Send button means the node is already claimed: the page shows the
        // owner and offers only Skip. See templates/setup/_claim.html.
        if (sendBtn) {
            reset();
            // Suggest the email from the contact step when nothing is stored
            // yet. Only a suggestion: nothing is sent until Send is pressed.
            if (!box.value.trim()) {
                box.value = document.getElementById('contactEmail').value.trim();
            }
        }

        if (hookInitialized.account) return;
        hookInitialized.account = true;

        skipBtn.addEventListener('click', function() { advance(); });
        if (!sendBtn) return;

        sendBtn.addEventListener('click', function() {
            // An empty box would clear the stored address on this route, which
            // is not what Send means here. Skip is the way past with nothing.
            if (!box.value.trim()) {
                say('Enter an email address, or skip this step.', true);
                return;
            }
            sendBtn.disabled = true;
            sendBtn.textContent = 'Sending...';
            say('');
            // postJSON rejects on any refusal, carrying the server's per-field
            // reason when there is one, such as a malformed address.
            postJSON('/set-up/claim', { email: box.value })
            .then(function() { advance(); })
            .catch(function(err) {
                var errors = err.errors || {};
                var first = Object.keys(errors)[0];
                say(first ? errors[first] : 'Could not send. Check the connection and try again.', true);
                reset();
            });
        });
    };

    // System update. Automatic; this step only reports progress.
    // See docs/features/setup-wizard.md#system-update
    enterHooks.system = function() {
        if (hookInitialized.system) return;
        hookInitialized.system = true;
        var status = document.getElementById('systemStatus');
        var nextBtn = document.getElementById('systemNextBtn');
        var installStatus = document.getElementById('systemInstallStatus');
        var cardStatus = document.getElementById('systemCardStatus');
        var stuckTimer = null;

        // On a re-run OS updates are managed remotely, so just inform.
        if (isRerun) {
            fetch('/mender/check-os')
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.current_version) {
                        document.getElementById('systemCurrentVersion').textContent = data.current_version;
                    }
                    status.innerHTML = 'System updates are managed remotely &#10003;';
                    cardStatus.innerHTML = '<span class="text-success">&#10003;</span>';
                    nextBtn.style.display = '';
                    nextBtn.textContent = 'Continue \u2192';
                })
                .catch(function() {
                    status.textContent = 'Unable to check system version.';
                    nextBtn.style.display = '';
                    nextBtn.textContent = 'Continue \u2192';
                });
            nextBtn.addEventListener('click', advance);
            return;
        }

        nextBtn.addEventListener('click', advance);
        startSystemPoll();

        // Only for a wait that is actually coming.
        // See docs/features/setup-wizard.md#while-you-wait-checklist
        function showUpdateWait(on) {
            var el = document.getElementById('systemUpdateWait');
            if (el) el.style.display = on ? '' : 'none';
        }

        function showTarget(version) {
            if (!version) return;
            document.getElementById('systemVersionArrow').style.display = '';
            document.getElementById('systemLatestVersion').textContent = version;
        }

        function showStage(stage, version) {
            if (stage === 'waiting') {
                status.textContent = 'Connecting to update server...';
            } else if (stage === 'downloading') {
                status.textContent = 'Downloading OS update...';
            } else if (stage === 'installing') {
                status.textContent = 'Installing OS update...';
            } else if (stage === 'rebooting') {
                status.textContent = 'Rebooting device...';
            } else {
                status.textContent = 'Updating...';
            }
            showTarget(version);
            showUpdateWait(true);
            cardStatus.innerHTML = '<span class="spinner-border spinner-border-sm text-primary"></span>';
            installStatus.innerHTML = '<span class="text-warning">Do not power off the device.</span>';
        }

        // Deliberately no way past: Packages is not safe to enter until the
        // update starts downloading or turns out not to be needed.
        function showPreparing(version) {
            status.textContent = 'Preparing system update...';
            showTarget(version);
            showUpdateWait(true);
            cardStatus.innerHTML = '<span class="spinner-border spinner-border-sm text-primary"></span>';
            if (!stuckTimer) {
                stuckTimer = setTimeout(function() {
                    installStatus.innerHTML = '<span class="text-warning">Taking longer than expected. Please keep waiting.</span>';
                }, 120000);
            }
        }

        function clearStuckTimer() {
            if (stuckTimer) {
                clearTimeout(stuckTimer);
                stuckTimer = null;
            }
        }

        function startSystemPoll() {
            if (pollTimer) clearInterval(pollTimer);

            function poll() {
                fetch('/mender/check-os')
                    .then(function(r) { return r.json(); })
                    .then(function(data) {
                        if (data.current_version) {
                            document.getElementById('systemCurrentVersion').textContent = data.current_version;
                        }
                        if (data.installing) {
                            clearStuckTimer();
                            nextBtn.style.display = 'none';
                            // data.version is a generic placeholder for
                            // server-pushed updates, so it is not shown.
                            showStage(data.stage);
                            return;
                        }
                        if (data.error) {
                            // Keep polling: never let the owner past an
                            // unconfirmed update state.
                            status.textContent = 'Unable to check: ' + data.error + ' \u2014 retrying...';
                            return;
                        }
                        if (data.update_available) {
                            showPreparing(data.latest_version);
                            return;
                        }
                        // Up to date, or the update finished and the node rebooted.
                        clearInterval(pollTimer);
                        pollTimer = null;
                        clearStuckTimer();
                        showUpdateWait(false);
                        status.innerHTML = 'System is up to date &#10003;';
                        cardStatus.innerHTML = '<span class="text-success">&#10003;</span>';
                        installStatus.innerHTML = '<span class="text-success">Complete!</span>';
                        nextBtn.style.display = '';
                        nextBtn.textContent = 'Continue \u2192';
                    })
                    .catch(function() {
                        status.textContent = 'Reconnecting...';
                    });
            }

            poll();
            pollTimer = setInterval(poll, 5000);
        }
    };

    // Packages. See docs/features/setup-wizard.md#packages
    enterHooks.radar = function() {
        if (hookInitialized.radar) return;
        hookInitialized.radar = true;
        var status = document.getElementById('radarStatus');
        var installBtn = document.getElementById('radarInstallBtn');
        var nextBtn = document.getElementById('radarNextBtn');
        var installStatus = document.getElementById('radarInstallStatus');
        var regionCheck = document.getElementById('regionCheck');
        var packageStatus = document.getElementById('radarPackageStatus');

        // Re-run: updates are managed remotely, so just inform. The version
        // lookup is cosmetic and never gates Continue.
        if (isRerun) {
            document.getElementById('regionCheckRow').style.display = 'none';
            document.getElementById('radarPackageRadio').style.display = 'none';
            document.getElementById('radarDescription').textContent =
                'RETINA package updates are managed remotely.';
            document.getElementById('radarPackageSub').textContent = 'Managed automatically';
            packageStatus.innerHTML = '<span class="text-success">&#10003;</span>';
            status.innerHTML = 'Package updates are managed remotely &#10003;';
            nextBtn.textContent = 'Continue →';
            nextBtn.style.display = '';

            fetch('/mender/check')
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.current_version) {
                        document.getElementById('radarLatestVersion').textContent = data.current_version;
                    }
                })
                .catch(function() {});

            nextBtn.addEventListener('click', advance);
            return;
        }

        // First run: installing the latest RETINA is required.
        document.getElementById('radarDescription').textContent = 'RETINA is not yet installed on this node. Select a version below to continue.';
        installBtn.classList.remove('ghost');
        installBtn.classList.add('primary');

        function updateInstallGate() {
            if (installBtn.style.display !== 'none') {
                installBtn.disabled = !regionCheck.checked;
            }
        }
        regionCheck.addEventListener('change', updateInstallGate);

        // Only for a wait that is actually coming.
        // See docs/features/setup-wizard.md#while-you-wait-checklist
        function showRadarWait(on) {
            var el = document.getElementById('radarInstallWait');
            if (el) el.style.display = on ? '' : 'none';
        }

        var latestVersion = null;

        // Retries every 5s, since this step cannot be skipped. The backend
        // caches the GitHub call for 60s, keeping retries inside its rate limit.
        function checkAvailability() {
            fetch('/mender/check')
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.installing) {
                        // Resuming a running install after a reload: recover
                        // latestVersion from the lock's "retina-node-vX" name so
                        // the success check does not compare against null.
                        if (!latestVersion && data.version) {
                            latestVersion = data.version.replace(/^retina-node-/, '');
                        }
                        showRadarWait(true);
                        status.textContent = data.reason || 'Installation in progress...';
                        installStatus.innerHTML = '<span class="text-warning">Do not power off the device.</span>';
                        packageStatus.innerHTML = '<span class="spinner-border spinner-border-sm text-primary"></span>';
                        startRadarPoll();
                        return;
                    }
                    if (data.error) {
                        status.textContent = 'Unable to check: ' + data.error + '. Retrying...';
                        setTimeout(checkAvailability, 5000);
                        return;
                    }
                    if (data.current_version && data.current_version === data.latest_version) {
                        showRadarWait(false);
                        status.innerHTML = 'Packages are up to date &#10003;';
                        packageStatus.innerHTML = '<span class="text-success">&#10003;</span>';
                        document.getElementById('radarLatestVersion').textContent = data.current_version;
                        document.getElementById('radarPackageSub').textContent = formatSize(data.latest_size_bytes);
                        nextBtn.style.display = '';
                    } else {
                        showRadarWait(true);
                        latestVersion = data.latest_version;
                        document.getElementById('radarLatestVersion').textContent = data.latest_version;
                        document.getElementById('radarPackageSub').textContent = formatSize(data.latest_size_bytes);
                        installBtn.style.display = '';
                        updateInstallGate();
                        if (data.current_version) {
                            // Pre-installed but outdated: still mandatory on first run.
                            document.getElementById('radarDescription').textContent =
                                'A newer version of RETINA is available (currently ' + data.current_version + '). Select a version below to install it before continuing.';
                        }
                    }
                })
                .catch(function() {
                    status.textContent = 'Unable to check for available packages. Retrying...';
                    setTimeout(checkAvailability, 5000);
                });
        }
        checkAvailability();

        installBtn.addEventListener('click', function() {
            installBtn.style.display = 'none';
            packageStatus.innerHTML = '<span class="spinner-border spinner-border-sm text-primary"></span>';
            status.textContent = 'Installing...';
            installStatus.innerHTML = '<span class="text-warning">Do not power off the device.</span>';

            postJSON('/mender/install', latestVersion ? {version: latestVersion} : undefined)
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.success) {
                        startRadarPoll();
                    } else {
                        installStatus.innerHTML = '<span class="text-danger">' + data.error + '</span>';
                        packageStatus.innerHTML = '';
                        status.textContent = '';
                        installBtn.style.display = '';
                        updateInstallGate();
                    }
                })
                .catch(function(err) {
                    // Show the server's reason: a 409 says why a retry cannot
                    // succeed yet.
                    installStatus.innerHTML = '<span class="text-danger">'
                        + esc(err.message || 'Request failed. Please try again.')
                        + '</span>';
                    packageStatus.innerHTML = '';
                    status.textContent = '';
                    installBtn.style.display = '';
                    updateInstallGate();
                });
        });

        nextBtn.addEventListener('click', advance);

        function startRadarPoll() {
            if (pollTimer) clearInterval(pollTimer);
            pollTimer = setInterval(function() {
                fetch('/mender/check')
                    .then(function(r) { return r.json(); })
                    .then(function(data) {
                        if (!data.installing) {
                            clearInterval(pollTimer);
                            pollTimer = null;
                            if (data.current_version === latestVersion) {
                                packageStatus.innerHTML = '<span class="text-success">&#10003;</span>';
                                status.textContent = '';
                                installStatus.innerHTML = '';
                                advance();
                            } else if (data.current_version) {
                                // Failed, previous version restored: offer an explicit
                                // way on rather than trapping or faking success.
                                installStatus.innerHTML = '<span class="text-danger">Update failed. Your previous version (' +
                                    data.current_version + ') has been restored and is running.</span>';
                                packageStatus.innerHTML = '';
                                status.textContent = '';
                                installBtn.style.display = '';
                                installBtn.textContent = 'Try again';
                                nextBtn.textContent = 'Continue without updating';
                                nextBtn.style.display = '';
                                updateInstallGate();
                            } else {
                                installStatus.innerHTML = '<span class="text-danger">Install may have failed. Try again.</span>';
                                packageStatus.innerHTML = '';
                                status.textContent = '';
                                installBtn.style.display = '';
                                updateInstallGate();
                            }
                        } else {
                            var stageText = {
                                downloading: 'Downloading...',
                                starting: 'Starting retina-node...'
                            };
                            status.textContent = stageText[data.stage] || 'Installing...';
                        }
                    });
            }, 5000);
        }
    };

    // Location. See docs/features/setup-wizard.md#location
    leaveHooks.location = function() {
        locationActive = false;
        if (abortAddressLookups) abortAddressLookups();
        clearTimeout(rfSseReconnectTimer); rfSseReconnectTimer = null;
        if (rfSse) { rfSse.close(); rfSse = null; }
        var targetMode = wizardWasMode;
        wizardWasMode = null;
        var pending = pendingModeSwitch;
        pendingModeSwitch = null;
        // No revert: spectrum never started, or it was already the mode.
        if (!targetMode || targetMode === 'spectrum') return;
        var scanStatus = document.getElementById('scanStatus');
        if (scanStatus) { scanStatus.textContent = 'Reverting to radar mode…'; scanStatus.style.display = ''; }
        // Wait for any in-flight spectrum switch first, so two docker
        // operations never race.
        return (pending || Promise.resolve()).then(function() {
            return postJSON('/api/mode', { mode: targetMode });
        }).then(
            function() { if (scanStatus) scanStatus.style.display = 'none'; },
            function() { if (scanStatus) scanStatus.style.display = 'none'; }
        );
    };

    enterHooks.location = function() {
        // Start retina-spectrum on every entry. Idempotent, and a no-op if
        // retina-node is not installed yet.
        locationActive = true;
        spectrumGating = true;
        document.getElementById('findTowersBtn').disabled = true;
        var scanStatus = document.getElementById('scanStatus');
        scanStatus.textContent = 'Starting spectrum analyser…';
        scanStatus.style.display = '';
        pendingModeSwitch = fetch('/api/mode')
            .then(function(r) { return r.json(); })
            .then(function(d) {
                if (!locationActive) return;
                wizardWasMode = d.mode || 'radar';
                return postJSON('/api/mode', { mode: 'spectrum' });
            })
            .then(function() {
                if (locationActive && connectRfSse) connectRfSse();
            })
            .catch(function() {
                if (!locationActive) return;
                scanStatus.textContent = 'Spectrum analyser unavailable, will search by location only';
                spectrumGating = false;
                if (refreshFindBtn) refreshFindBtn();
            });

        // The map was laid out while its panel was hidden; measure it again.
        if (refreshLocationMap) refreshLocationMap();

        if (hookInitialized.location) return;
        hookInitialized.location = true;

        var rxLat = document.getElementById('rxLat');
        var rxLon = document.getElementById('rxLon');
        var rxAlt = document.getElementById('rxAlt');
        var coordMsg = document.getElementById('rxCoordMsg');

        // ── Coordinates ──────────────────────────────────────
        // Text boxes holding whatever the owner typed or pasted, at the
        // precision they gave it: nothing here rounds a hand-entered value.
        // See docs/features/setup-wizard.md#coordinates
        // The whole box must be one decimal number: parseFloat would read
        // "38.8977abc", or a pair left in one box, as its first number.
        function coordValue(box) {
            var s = String(box.value).trim().replace(/[−]/g, '-');
            return /^[-+]?(\d+(\.\d*)?|\.\d+)$/.test(s) ? Number(s) : NaN;
        }
        function coordsValid() {
            var lat = coordValue(rxLat), lon = coordValue(rxLon);
            return !isNaN(lat) && !isNaN(lon) &&
                   lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180;
        }
        function sayCoords(text, danger) {
            coordMsg.textContent = text || '';
            coordMsg.style.color = danger ? 'var(--danger)' : 'var(--ink-3)';
            coordMsg.style.display = text ? '' : 'none';
        }
        function checkCoords() {
            var lat = coordValue(rxLat), lon = coordValue(rxLon);
            if (rxLat.value.trim() === '' && rxLon.value.trim() === '') { sayCoords(''); return; }
            if (isNaN(lat) || isNaN(lon)) { sayCoords('Enter both as decimal degrees, for example 38.8977 and -77.0365.', true); return; }
            if (lat < -90 || lat > 90) { sayCoords('Latitude must be between -90 and 90.', true); return; }
            if (lon < -180 || lon > 180) { sayCoords('Longitude must be between -180 and 180.', true); return; }
            sayCoords('');
        }

        // A pair pasted from a map ("38.8895, -77.0353", or with a
        // space, a tab or a semicolon between) lands in one box: split it.
        function splitPair(box) {
            var m = String(box.value).match(/^\s*(-?\d+(?:\.\d+)?)\s*[,;\t ]\s*(-?\d+(?:\.\d+)?)\s*$/);
            if (!m) return false;
            rxLat.value = m[1];
            rxLon.value = m[2];
            return true;
        }
        // One handler for both boxes: a split pair changes both, and
        // everything below reads both anyway. Split only a paste, or a typed
        // pair once the box loses focus (change): splitting on every key
        // press cut a pair typed by hand at "38.8977, -7" and sent the rest
        // into the latitude box.
        function onCoordInput(e) {
            if (e.inputType === 'insertFromPaste') splitPair(e.target);
            checkCoords();
            updateFindBtn();
            scheduleElevation();
            placePinFromBoxes();
        }
        function onCoordChange(e) {
            if (splitPair(e.target)) onCoordInput(e);
        }

        if (demoMode) {
            rxLat.value = '37.7749';
            rxLon.value = '-122.4194';
            rxAlt.value = '16';
        } else if (!rxLat.value && !rxLon.value && window._towerSearchParams) {
            // A reload: the boxes are empty but the page rehydrated the last
            // search position for the tower step, so start from it here too.
            // See docs/features/setup-wizard.md#rehydrating-the-tower-search
            rxLat.value = String(window._towerSearchParams.lat);
            rxLon.value = String(window._towerSearchParams.lon);
        }
        var useMyLocBtn = document.getElementById('useMyLocationBtn');
        var findBtn = document.getElementById('findTowersBtn');
        var geoError = document.getElementById('locationGeoError');
        var skipBtn = document.getElementById('locationSkipBtn');

        // ── The map ──────────────────────────────────────────
        // Shown once there is a point. A click or a drag moves the pin and
        // writes the boxes at seven decimals (about a centimetre), which is
        // the one place this step rounds, and only because a click has no
        // precision of its own. See docs/features/setup-wizard.md#coordinates
        var rxMap = null, rxPin = null;
        var mapWrap = document.getElementById('rxMapWrap');
        // A divIcon, as the tower step's markers are: Leaflet's stock marker
        // image is fetched from wherever leaflet.js came from, and this one
        // needs nothing fetched.
        function pinIcon() {
            return L.divIcon({
                className: 'user-marker',
                html: '<div style="width:18px;height:18px;background:#3b82f6;border:3px solid #fff;border-radius:50%;box-shadow:0 0 0 3px rgba(59,130,246,.25), 0 2px 8px rgba(0,0,0,.25);cursor:grab;"></div>',
                iconSize: [18, 18],
                iconAnchor: [9, 9]
            });
        }
        function newPin(ll) {
            rxPin = L.marker(ll, { draggable: true, icon: pinIcon(), title: 'Your receiver. Drag to move.' }).addTo(rxMap);
            rxPin.on('dragend', function() { writeBoxesFromPin(rxPin.getLatLng()); });
        }

        function writeBoxesFromPin(ll) {
            rxLat.value = ll.lat.toFixed(7);
            rxLon.value = ll.lng.toFixed(7);
            checkCoords();
            updateFindBtn();
            scheduleElevation();
        }
        function ensureMap() {
            if (rxMap || typeof L === 'undefined') return;
            mapWrap.style.display = '';
            rxMap = L.map('rxMap', { zoomControl: true });
            L.tileLayer(withCartoKey('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png'), {
                attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>',
                maxZoom: 20
            }).addTo(rxMap);
            rxMap.on('click', function(e) {
                if (!rxPin) newPin(e.latlng);
                else rxPin.setLatLng(e.latlng);
                writeBoxesFromPin(e.latlng);
            });
        }
        // Puts the pin where the boxes say, zooming in on the first point and
        // following without re-zooming after that.
        function placePinFromBoxes(zoomTo) {
            if (!coordsValid()) return;
            ensureMap();
            if (!rxMap) return;
            var ll = L.latLng(coordValue(rxLat), coordValue(rxLon));
            var first = !rxPin;
            if (first) newPin(ll);
            else rxPin.setLatLng(ll);
            // The panel was display:none when the map was made, so its size
            // is stale until it is measured again.
            rxMap.invalidateSize();
            if (first || zoomTo) rxMap.setView(ll, 17);
            else if (!rxMap.getBounds().contains(ll)) rxMap.panTo(ll);
        }


        // RF scan over SSE, opened once retina-spectrum is up.
        // See docs/features/setup-wizard.md#spectrum-scan
        var scanResult = document.getElementById('scanResult');
        var rfMeasurements = [];
        var rfPhase = 'idle'; // idle | waiting | scanning | done
        var rfPass = 0;       // sweep passes completed on this connection

        function normaliseBand(id) {
            if (id === 'fm') return 'FM';
            if (id === 'vhf_hi' || id === 'vhf_lo') return 'VHF';
            if (id === 'uhf') return 'UHF';
            return id.toUpperCase();
        }

        // Matches retina-spectrum's METRICS_MIN_ENTRIES: no channels are
        // exported before then, so an empty early pass is normal.
        var RF_PASSES_TO_DATA = 5;

        function updateRfUI() {
            var n = rfMeasurements.length;
            if (rfPhase === 'waiting') {
                scanStatus.textContent = rfPass === 0
                    ? 'Waiting for the sweep to start. Each pass takes about a minute…'
                    : 'Averaging the spectrum, pass ' + (rfPass + 1) + ' of about ' +
                      RF_PASSES_TO_DATA + '. Channels appear after about 3 minutes…';
                scanStatus.style.display = '';
                scanResult.style.display = 'none';
            } else if (rfPhase === 'scanning') {
                scanStatus.textContent = 'Scanning…' + (n > 0 ? ', ' + n + ' channel' + (n !== 1 ? 's' : '') + ' measured' : '');
                scanStatus.style.display = '';
                scanResult.style.display = 'none';
            } else if (rfPhase === 'done') {
                scanStatus.style.display = 'none';
                scanResult.textContent = 'RF profile captured: ' + n + ' channel' + (n !== 1 ? 's' : '') + ' measured';
                scanResult.style.display = '';
            }
        }

        // Outer-scope so leaveHooks.location and re-entries can reach it.
        connectRfSse = function() {
            if (rfSse) return;
            rfMeasurements = [];
            rfPass = 0;
            rfPhase = 'waiting';
            updateRfUI();
            rfSse = new EventSource('/towers/spectrum/events');
            rfSse.onmessage = function(e) {
                var msg = JSON.parse(e.data);
                if (msg.type === 'start') {
                    if (rfPhase !== 'waiting') return;
                    rfMeasurements = [];
                    rfPhase = 'scanning';
                    updateRfUI();
                } else if (msg.type === 'step') {
                    if (rfPhase !== 'scanning') return;
                    if (msg.channels) {
                        msg.channels.forEach(function(ch) {
                            if (rfMeasurements.some(function(m) { return m.freq_mhz === ch.fc_mhz; })) return;
                            var m = { freq_mhz: ch.fc_mhz, band: normaliseBand(ch.band), score: ch.score || 0, snr_db: null, obw_fraction: null, power_db: null };
                            if (ch.pilot_mhz == null) {
                                m.snr_db = ch.snr_db != null ? ch.snr_db : null;
                                m.obw_fraction = ch.obw_fraction != null ? ch.obw_fraction : null;
                            } else {
                                var pilot = ch.peaks && ch.peaks.find(function(p) { return p.is_pilot; });
                                if (pilot) m.power_db = pilot.power_db;
                            }
                            rfMeasurements.push(m);
                        });
                        updateRfUI();
                    }
                } else if (msg.type === 'complete') {
                    if (rfPhase !== 'scanning') return;
                    rfPass++;
                    // Empty pass: the averaging ring is not ready, so re-arm.
                    // Find Towers is ungated after any full pass.
                    rfPhase = rfMeasurements.length === 0 ? 'waiting' : 'done';
                    spectrumGating = false;
                    updateRfUI();
                    updateFindBtn();
                }
            };
            rfSse.onerror = function() {
                rfSse.close(); rfSse = null;
                if (locationActive) rfSseReconnectTimer = setTimeout(connectRfSse, 3000);
            };
        };

        // Enable Find Towers when lat/lon are valid and the scan is done or unavailable.
        function updateFindBtn() {
            findBtn.disabled = !coordsValid() || spectrumGating;
        }
        refreshFindBtn = updateFindBtn;
        refreshLocationMap = function() { placePinFromBoxes(); };
        rxLat.addEventListener('input', onCoordInput);
        rxLon.addEventListener('input', onCoordInput);
        rxLat.addEventListener('change', onCoordChange);
        rxLon.addEventListener('change', onCoordChange);

        // ── Address lookup and altitude prefill ──────────────
        // Both only fill boxes; nothing in here can block the step.
        // See docs/features/setup-wizard.md#address-lookup
        var addressInput = document.getElementById('rxAddress');
        var addressBtn = document.getElementById('rxAddressBtn');
        var addressMsg = document.getElementById('rxAddressMsg');
        var addressLoading = false;
        var geocodeAbort = null;
        var elevationAbort = null;
        var elevationTimer = null;
        // Whether the altitude is ours (replaceable) or the owner's (never
        // replaced by the manual path).
        var altAutoFilled = false;

        // Coarse matches must say so: these coordinates become the receiver
        // position in the radar config.
        var PRECISION_WARNINGS = {
            postcode: 'Postcode centre only. Add the street address for a precise fix.',
            locality: 'City centre only. Add the street address for a precise fix.'
        };

        // The ZIP in a US address: the last five-digit group, after dropping
        // a leading house number, which is often five digits on a rural road
        // ("12345 County Road 5") and was read as the ZIP.
        function zipOf(address) {
            var rest = String(address || '').replace(/^\s*\d+[A-Za-z]?\b/, '');
            var all = rest.match(/\b\d{5}(?:-\d{4})?\b/g);
            return all ? all[all.length - 1].slice(0, 5) : undefined;
        }

        function sayAddress(text, tone) {
            addressMsg.textContent = text || '';
            addressMsg.style.color = tone === 'error' ? 'var(--danger)'
                : tone === 'warn' ? 'var(--warn-ink)'
                : 'var(--ink-3)';
            addressMsg.style.display = text ? '' : 'none';
        }

        function updateAddressBtn() {
            addressBtn.disabled = addressLoading || addressInput.value.trim() === '';
        }

        function lookupAddress() {
            var query = addressInput.value.trim();
            if (!query || addressLoading) return;

            // Stops a late answer writing into a step the owner has left.
            if (geocodeAbort) geocodeAbort.abort();
            var controller = window.AbortController ? new AbortController() : null;
            geocodeAbort = controller;

            addressLoading = true;
            updateAddressBtn();
            addressBtn.textContent = 'Looking up\u2026';
            sayAddress('');

            fetch('/towers/geocode', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': csrfToken },
                body: JSON.stringify({ query: query }),
                signal: controller ? controller.signal : undefined
            })
            .then(function(r) {
                // Not postJSON: both failure statuses carry a sentence to show.
                return r.json().then(
                    function(data) { return { ok: r.ok, data: data || {} }; },
                    function() { return { ok: false, data: {} }; });
            })
            .then(function(res) {
                if (!locationActive) return;
                if (!res.ok) {
                    sayAddress(res.data.error || 'Address lookup failed.', 'error');
                    return;
                }
                var d = res.data;
                // As returned, not rounded: the finder already fixes the
                // precision, and a figure typed over it later is kept whole.
                rxLat.value = String(d.latitude);
                rxLon.value = String(d.longitude);
                checkCoords();
                updateFindBtn();
                placePinFromBoxes(true);

                // A ZIP the owner typed that the match does not carry is the
                // map data disagreeing with them, which is worth saying: the
                // pin is what gets saved, so the map is where to check it.
                var typedZip = zipOf(query);
                var matchedZip = zipOf(d.matched_address);
                var zipNote = (typedZip && matchedZip && typedZip !== matchedZip)
                    ? ' The map data lists ZIP ' + matchedZip + ' here rather than ' + typedZip +
                      ', so check the pin below and drag it if it is off.'
                    : '';

                var warning = PRECISION_WARNINGS[d.precision];
                sayAddress(warning || ('Matched: ' + d.matched_address + zipNote),
                           (warning || zipNote) ? 'warn' : '');

                // Forced: an explicit lookup makes the old altitude stale.
                fetchElevation(d.latitude, d.longitude, true);
            })
            .catch(function(err) {
                if (err && err.name === 'AbortError') return;
                if (!locationActive) return;
                sayAddress('Address lookup failed. Check the connection and try again.', 'error');
            })
            .then(function() {
                // Aborted: a newer request owns the button label.
                if (controller && controller.signal.aborted) return;
                addressLoading = false;
                addressBtn.textContent = 'Look up';
                updateAddressBtn();
            });
        }

        // Ground elevation for the altitude box. Silent on failure.
        // See docs/features/setup-wizard.md#altitude-prefill
        function fetchElevation(lat, lon, force) {
            if (!force && rxAlt.value.trim() !== '' && !altAutoFilled) return;
            if (elevationAbort) elevationAbort.abort();
            var controller = window.AbortController ? new AbortController() : null;
            elevationAbort = controller;
            fetch('/towers/elevation?lat=' + encodeURIComponent(lat) +
                  '&lon=' + encodeURIComponent(lon),
                  { signal: controller ? controller.signal : undefined })
                .then(function(r) { return r.ok ? r.json() : null; })
                .then(function(d) {
                    if (!d || !locationActive) return;
                    if (controller && controller.signal.aborted) return;
                    if (d.elevation_m == null) return;
                    rxAlt.value = Math.round(d.elevation_m);
                    altAutoFilled = true;
                })
                .catch(function() {});
        }

        // The manual path fills the altitude too. Debounced per keystroke.
        function scheduleElevation() {
            clearTimeout(elevationTimer);
            elevationTimer = setTimeout(function() {
                if (!coordsValid()) return;
                fetchElevation(coordValue(rxLat), coordValue(rxLon), false);
            }, 800);
        }

        // An altitude the owner types is theirs from then on.
        rxAlt.addEventListener('input', function() { altAutoFilled = false; });

        addressInput.addEventListener('input', updateAddressBtn);
        addressBtn.addEventListener('click', lookupAddress);
        addressInput.addEventListener('keydown', function(e) {
            // Enter means "look up", not "run the step".
            if (e.key === 'Enter' || e.keyCode === 13) {
                e.preventDefault();
                lookupAddress();
            }
        });
        updateAddressBtn();

        // Published so leaveHooks.location can drop in-flight work.
        abortAddressLookups = function() {
            clearTimeout(elevationTimer);
            if (geocodeAbort) { geocodeAbort.abort(); geocodeAbort = null; }
            if (elevationAbort) { elevationAbort.abort(); elevationAbort = null; }
            addressLoading = false;
            addressBtn.textContent = 'Look up';
            updateAddressBtn();
        };

        // Use My Location: inert while the button is commented out in the
        // markup (needs HTTPS). See docs/features/setup-wizard.md#address-lookup
        if (useMyLocBtn) useMyLocBtn.addEventListener('click', function() {
            if (!navigator.geolocation) {
                geoError.textContent = 'Geolocation not supported by your browser';
                geoError.style.display = '';
                return;
            }
            geoError.style.display = 'none';
            useMyLocBtn.disabled = true;
            useMyLocBtn.textContent = 'Getting location\u2026';
            navigator.geolocation.getCurrentPosition(
                function(pos) {
                    useMyLocBtn.disabled = false;
                    useMyLocBtn.textContent = 'Use My Location';
                    rxLat.value = String(pos.coords.latitude);
                    rxLon.value = String(pos.coords.longitude);
                    if (pos.coords.altitude != null) {
                        rxAlt.value = Math.round(pos.coords.altitude);
                    }
                    checkCoords();
                    updateFindBtn();
                    placePinFromBoxes(true);
                },
                function(err) {
                    useMyLocBtn.disabled = false;
                    useMyLocBtn.textContent = 'Use My Location';
                    var msgs = {
                        1: 'Location access denied \u2014 please allow location in browser settings',
                        2: 'Location unavailable',
                        3: 'Location request timed out'
                    };
                    geoError.textContent = msgs[err.code] || err.message;
                    geoError.style.display = '';
                },
                { timeout: 10000, maximumAge: 60000, enableHighAccuracy: false }
            );
        });

        findBtn.addEventListener('click', function() {
            if (!coordsValid()) { checkCoords(); return; }
            window._towerSearchParams = {
                lat: coordValue(rxLat),
                lon: coordValue(rxLon),
                alt: parseFloat(rxAlt.value) || 0,
                measurements: rfMeasurements
            };
            advance();
        });

        // Seeded coordinates (demo, or a reload) drive the map and the button.
        if (rxLat.value || rxLon.value) { checkCoords(); updateFindBtn(); placePinFromBoxes(true); scheduleElevation(); }

        skipBtn.addEventListener('click', function() {
            // Skip both location and towers
            showStep(currentIndex + 2);
        });
    };

    // Tower selection. See docs/features/setup-wizard.md#tower-selection
    enterHooks.towers = (function() {
        var towerMarkers = [];
        var selectedTower = null;
        var listenersAdded = false;
        // The Keep column: which towers this node caches as presets. The page
        // is the list's only writer, and the list outlives a search: "Show
        // every tower" and back keep the owner's picks. null until the first
        // list of the run decides it. savedKept is the last list the node
        // accepted, which a failed save falls back to.
        // See docs/features/setup-wizard.md#cached-towers
        var keptTowers = null;
        var savedKept = null;
        var cacheSyncTimer = null;
        // Whether the current list was searched with the sweep's measurements
        // (and so omits every tower the sweep did not measure) or by location.
        var searchedWithSweep = false;

        var loadingEl, errorEl, resultsEl, summaryEl, tableBody;
        var selectedCard, selectedName, selectedDetail, saveBtn, skipBtn;

        return function() {
        loadingEl = document.getElementById('towerLoading');
        errorEl = document.getElementById('towerError');
        resultsEl = document.getElementById('towerResults');
        summaryEl = document.getElementById('towerSummary');
        tableBody = document.getElementById('towerTableBody');
        selectedCard = document.getElementById('selectedTowerCard');
        selectedName = document.getElementById('selectedTowerName');
        selectedDetail = document.getElementById('selectedTowerDetail');
        saveBtn = document.getElementById('towerSaveBtn');
        skipBtn = document.getElementById('towerSkipBtn');

        // Per search, not per entry: "Show every tower" searches again on
        // the same entry, and Leaflet refuses a container it already owns.
        function resetResults() {
            towerMarkers = [];
            selectedTower = null;
            // Not keptTowers, and not a pending save: both outlive the list.

            var mapEl = document.getElementById('towerMap');
            if (window._towerMap) {
                try {
                    window._towerMap.off();
                    window._towerMap.remove();
                } catch(e) {}
                window._towerMap = null;
            }
            mapEl.innerHTML = '';
            delete mapEl._leaflet_id;

            selectedCard.style.display = 'none';
            var guideEl = document.getElementById('antennaGuide');
            if (guideEl) { guideEl.style.display = 'none'; guideEl.innerHTML = ''; }
            saveBtn.disabled = true;
            saveBtn.textContent = 'Save & Continue';
            tableBody.innerHTML = '';
            summaryEl.innerHTML = '';
            errorEl.style.display = 'none';
        }
        resetResults();

        // Chips use .tower-badge.band-* and .rank-* classes from common.css.
        var BAND_CLASS = { FM: 'band-fm', VHF: 'band-vhf', UHF: 'band-uhf' };

        // Takes a colour, not a class: it lands in an inline style inside the
        // divIcon, where a var() still resolves.
        function makeTowerIcon(color, highlighted) {
            var size = highlighted ? 16 : 11;
            var border = highlighted ? 3 : 2;
            var shadow = highlighted
                ? '0 0 0 3px rgba(59,130,246,.3), 0 2px 6px rgba(0,0,0,.25)'
                : '0 1px 4px rgba(0,0,0,.3)';
            return L.divIcon({
                className: 'tower-marker',
                html: '<div style="width:' + size + 'px;height:' + size + 'px;background:' + color +
                      ';border:' + border + 'px solid #fff;border-radius:50%;box-shadow:' + shadow +
                      ';transition:all 0.15s;"></div>',
                iconSize: [size, size],
                iconAnchor: [size / 2, size / 2]
            });
        }

        var userIcon = L.divIcon({
            className: 'user-marker',
            html: '<div style="width:16px;height:16px;background:#3b82f6;border:3px solid #fff;border-radius:50%;box-shadow:0 0 0 3px rgba(59,130,246,.25), 0 2px 8px rgba(0,0,0,.2);"></div>',
            iconSize: [16, 16],
            iconAnchor: [8, 8]
        });

        var params = window._towerSearchParams;
        if (!params) {
            // Only when no search was ever cached. Forward-only, so offer the
            // way on. See docs/features/setup-wizard.md#rehydrating-the-tower-search
            loadingEl.style.display = 'none';
            errorEl.textContent = 'No location has been saved for this device, '
                + 'so there is nothing to search with yet. Skip this step and '
                + 'choose a tower on the Config page once setup has finished.';
            errorEl.style.display = '';
            skipBtn.style.display = '';
            return;
        }

        loadingEl.style.display = '';
        errorEl.style.display = 'none';
        resultsEl.style.display = 'none';

        // With the sweep's measurements when there are any: the finder then
        // lists only towers the sweep measured. "Show every tower" searches
        // again by location alone.
        // See docs/features/setup-wizard.md#towers-the-sweep-did-not-measure
        function search(useSweep) {
            var measurements = useSweep ? (params.measurements || []) : [];
            searchedWithSweep = measurements.length > 0;
            resetResults();
            loadingEl.style.display = '';
            errorEl.style.display = 'none';
            resultsEl.style.display = 'none';
            postJSON('/towers/search', {
                lat: params.lat,
                lon: params.lon,
                measurements: measurements,
                // This page saves the Keep list itself (syncCache).
                cache: false
            })
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    loadingEl.style.display = 'none';
                    if (data.error) {
                        errorEl.textContent = data.error;
                        errorEl.style.display = '';
                        return;
                    }
                    renderResults(data.towers || [], data.query);
                })
                .catch(function(err) {
                    loadingEl.style.display = 'none';
                    errorEl.textContent = 'Failed to search for towers: ' + err.message;
                    errorEl.style.display = '';
                });
        }
        search(true);

        // ── The Keep column ──────────────────────────────────
        // A tick replaces the node's list with the kept set. Debounced, so a
        // run of ticks is one request.
        var MAX_KEEP_DEFAULT = window._maxCachedTowers || 5;
        var MAX_KEEP = window._maxKeptTowers || 10;
        var cacheStatus = document.getElementById('towerCacheStatus');
        var pendingNote = '';

        function sayCache(text, danger) {
            if (!cacheStatus) return;
            cacheStatus.textContent = text || '';
            cacheStatus.style.color = danger ? 'var(--danger)' : 'var(--ok)';
        }

        // The swept and the location-only lists hold different copies of the
        // same tower, so a tower is matched by callsign and frequency.
        function towerKey(t) {
            return (t.callsign || t.name || '') + '|' + Number(t.frequency_mhz);
        }
        // Added by hand on the Configuration page (Cached Towers): kept as
        // they are, never shown or unticked here, and not counted toward
        // MAX_KEEP, since that page has no cap of its own.
        function isManual(t) { return !!t && t.source === 'manual'; }
        function isKept(t) {
            var key = towerKey(t);
            return (keptTowers || []).some(function(k) { return towerKey(k) === key; });
        }
        function pickedCount() {
            return keptTowers.filter(function(k) { return !isManual(k); }).length;
        }
        function keepTower(t, keep) {
            var key = towerKey(t);
            keptTowers = keptTowers.filter(function(k) { return towerKey(k) !== key; });
            if (keep) keptTowers.push(t);
        }
        function refreshKeepBoxes() {
            tableBody.querySelectorAll('tr').forEach(function(row) {
                var box = row.querySelector('.tower-keep');
                if (box && row._tower) box.checked = isKept(row._tower);
            });
        }
        // Saved best first, because Auto-Calibrate tries the first cached
        // towers as its alternates; hand-added towers have no rank and stay
        // last, in their own order.
        // See docs/features/towers.md#choosing-what-to-cache
        function bestFirst(list) {
            var picked = list.filter(function(t) { return !isManual(t); });
            picked.sort(function(a, b) {
                return (Number(a.rank) || Infinity) - (Number(b.rank) || Infinity);
            });
            return picked.concat(list.filter(isManual));
        }
        function keptSummary(list) {
            var manual = list.filter(isManual).length;
            return (list.length - manual) + ' kept' +
                (manual ? ', plus ' + manual + ' you added on the Configuration page' : '') + '.';
        }

        // Where the owner's picks start. The list saved for this position (a
        // reload, or a re-run at the same point) is the owner's own, edits on
        // the Configuration page included. Anywhere else, the towers added by
        // hand still carry over, and the finder's best few join them when the
        // first list arrives.
        var manualCarry = [];
        if (keptTowers === null) {
            var saved = window._cachedTowers;
            var savedTowers = (saved && saved.towers) || [];
            if (savedTowers.length && saved.lat === params.lat && saved.lon === params.lon) {
                keptTowers = savedTowers.slice();
                savedKept = savedTowers.slice();
            } else {
                manualCarry = savedTowers.filter(isManual);
            }
        }

        function syncCache(now) {
            clearTimeout(cacheSyncTimer);
            sayCache('Saving…');
            cacheSyncTimer = setTimeout(function() {
                var sending = bestFirst(keptTowers);
                var note = pendingNote;
                pendingNote = '';
                postJSON('/towers/cache/set', {
                    lat: params.lat, lon: params.lon, towers: sending
                })
                .then(function() {
                    savedKept = sending;
                    sayCache(note + keptSummary(sending));
                })
                .catch(function(err) {
                    // Never show a list the node did not take: back to the
                    // last one it did.
                    if (savedKept) {
                        keptTowers = savedKept.slice();
                        refreshKeepBoxes();
                        sayCache('Could not save that change, so the list is back to what was saved: ' +
                                 err.message, true);
                    } else {
                        sayCache('Could not save the list: ' + err.message, true);
                    }
                });
            }, now ? 0 : 600);
        }

        function renderResults(towers, query) {
            resultsEl.style.display = '';

            // What the sweep hid, and the way to see it.
            var scope = document.getElementById('towerScope');
            if (scope) {
                scope.innerHTML = '';
                if (searchedWithSweep) {
                    scope.appendChild(document.createTextNode(
                        'Listing the towers your spectrum sweep measured. A tower the sweep ' +
                        'did not measure is not shown, however strong it is. '));
                    var all = document.createElement('a');
                    all.href = '#';
                    all.textContent = 'Show every tower near you';
                    all.style.color = 'var(--accent)';
                    all.addEventListener('click', function(e) { e.preventDefault(); search(false); });
                    scope.appendChild(all);
                    scope.appendChild(document.createTextNode(' (ranked by location alone).'));
                    scope.style.display = '';
                } else if (params.measurements && params.measurements.length) {
                    scope.appendChild(document.createTextNode(
                        'Listing every tower near you, ranked by location alone. '));
                    var swept = document.createElement('a');
                    swept.href = '#';
                    swept.textContent = 'Back to the towers your sweep measured';
                    swept.style.color = 'var(--accent)';
                    swept.addEventListener('click', function(e) { e.preventDefault(); search(true); });
                    scope.appendChild(swept);
                    scope.appendChild(document.createTextNode('.'));
                    scope.style.display = '';
                } else {
                    scope.style.display = 'none';
                }
            }

            // Only the first list of the run, with nothing saved for this
            // position, starts from the finder's best few, and saves them at
            // once: the search no longer caches anything by itself.
            var dflt = document.getElementById('towerCacheDefault');
            if (dflt) dflt.textContent = Math.min(MAX_KEEP_DEFAULT, towers.length);
            sayCache('');
            if (keptTowers === null) {
                keptTowers = manualCarry.concat(towers.slice(0, MAX_KEEP_DEFAULT));
                if (keptTowers.length) syncCache(true);
            } else if (savedKept) {
                sayCache(keptSummary(savedKept));
            }

            if (towers.length === 0) {
                summaryEl.innerHTML = '';
                tableBody.innerHTML = '';
                errorEl.textContent = 'No towers found in range. Try adjusting your location or increasing the search radius.';
                errorEl.style.display = '';
                skipBtn.style.display = '';
                return;
            }

            // Summary. Best detect area is the figure the ranking is built on.
            var bands = [];
            towers.forEach(function(t) { if (bands.indexOf(t.band) === -1) bands.push(t.band); });
            var best = towers[0];
            var bestArea = best ? formatAreaKm2(best.expected_area_km2) : '';
            summaryEl.innerHTML =
                '<div class="stat-card"><span class="stat-value">' + towers.length + '</span><span class="stat-label">Towers Found</span></div>' +
                '<div class="stat-card"><span class="stat-value">' + (bestArea || '\u2014') + '</span><span class="stat-label">Best Detect Area (km\u00b2)</span></div>' +
                '<div class="stat-card"><span class="stat-value">' + esc(bands.join(', ')) + '</span><span class="stat-label">Bands</span></div>' +
                (best ? '<div class="stat-card"><span class="stat-value">' + esc(best.callsign || '\u2014') + '</span><span class="stat-label">Top Pick \u2014 ' + esc(best.distance_km) + ' km</span></div>' : '');

            // Map
            towerMarkers = [];

            var towerMap = L.map('towerMap');
            window._towerMap = towerMap;
            L.tileLayer(withCartoKey('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png'), {
                attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>'
            }).addTo(towerMap);

            var points = [];
            if (query) {
                var userLatLng = [query.latitude, query.longitude];
                points.push(userLatLng);
                L.marker(userLatLng, { icon: userIcon }).addTo(towerMap)
                    .bindPopup('<span class="popup-callsign">Your Location</span>');
                // The radius the finder actually searched, not a literal.
                L.circle(userLatLng, {
                    radius: (isNum(query.radius_km) ? query.radius_km : 80) * 1000,
                    color: '#3b82f6', weight: 1.5, fillOpacity: 0.04, dashArray: '6 4'
                }).addTo(towerMap);
            }

            towers.forEach(function(t) {
                var tier = rankTier(t.rank, towers.length);
                var ll = [t.latitude, t.longitude];
                points.push(ll);
                var marker = L.marker(ll, {
                    icon: makeTowerIcon(tier.color, false),
                    // Co-located stations share a position; keep the best-ranked on top.
                    zIndexOffset: 1000 - (isNum(t.rank) ? t.rank : 0)
                }).addTo(towerMap);
                var area = formatAreaKm2(t.expected_area_km2);
                marker.bindPopup(
                    '<span class="popup-callsign">' + esc(t.callsign || 'Unknown') + '</span><br>' +
                    '<span class="popup-detail">' + esc(t.name || '') + '</span><br>' +
                    '<span class="popup-detail">' + esc(t.latitude + ', ' + t.longitude) +
                    (t.altitude_m != null ? ' &middot; ' + esc(t.altitude_m) + ' m ASL' : '') + '</span><br>' +
                    '<span class="popup-freq">' + esc(t.frequency_mhz) + ' MHz</span> (' + esc(t.band) + ')<br>' +
                    '<span class="popup-detail">' + esc(t.distance_km) + ' km ' + esc(t.bearing_cardinal) +
                    ' &middot; ' + esc(t.received_power_dbm) + ' dBm</span><br>' +
                    (area
                        ? '<span class="popup-detail">Detect area ' + esc(area) + ' km&sup2;'
                          + (isNum(t.best_azimuth_deg)
                              ? ' &middot; point ' + Math.round(t.best_azimuth_deg) + '\u00b0' : '')
                          + '</span><br>'
                        : '') +
                    '<span style="color:' + tier.color + ';font-weight:600;font-size:0.78rem;">#' +
                    esc(t.rank) + ' \u00b7 ' + esc(tier.label) + '</span>' +
                    ((t.shared_callsigns && t.shared_callsigns.length)
                        ? '<br><span class="popup-detail">Shares transmitter with '
                          + esc(t.shared_callsigns.join(', ')) + '</span>'
                        : '')
                );
                marker.on('click', function() { selectTower(t); });
                towerMarkers.push({ marker: marker, tower: t, tier: tier });
            });

            if (points.length > 1) {
                towerMap.fitBounds(points, { padding: [40, 40], maxZoom: 13 });
            } else if (points.length === 1) {
                towerMap.setView(points[0], 10);
            }

            // Table
            tableBody.innerHTML = '';
            towers.forEach(function(t) {
                var tier = rankTier(t.rank, towers.length);
                var tr = document.createElement('tr');

                if (beyondHorizon(t.distance_km, t.horizon_km)) {
                    tr.className = 'beyond-horizon';
                    tr.title = 'Beyond radio horizon';
                }

                var shared = (t.shared_callsigns && t.shared_callsigns.length)
                    ? '<span class="shared-callsigns" title="Also licensed on this transmitter (channel sharing)">+ '
                      + esc(t.shared_callsigns.join(', ')) + '</span>'
                    : '';
                var matched = t.frequency_matched
                    ? '<span class="freq-match" title="Matches a frequency the spectrum sweep measured">&#10003;</span>'
                    : '';
                var point = isNum(t.best_azimuth_deg)
                    ? Math.round(t.best_azimuth_deg) + '\u00b0 <span class="cardinal">'
                      + esc(bearingCardinal(t.best_azimuth_deg)) + '</span>'
                    : '\u2014';

                tr.innerHTML =
                    '<td class="keep"><input type="checkbox" class="tower-keep" title="Keep this tower on the node as a preset"' +
                        (isKept(t) ? ' checked' : '') + '></td>' +
                    '<td class="rank"><span class="tower-badge rank-badge ' + tier.cls + '">' + esc(t.rank) + '</span></td>' +
                    '<td class="mono">' + esc(formatAreaKm2(t.expected_area_km2) || '\u2014') + '</td>' +
                    '<td>' + point + '</td>' +
                    '<td class="callsign">' + esc(t.callsign || '\u2014') + shared + '</td>' +
                    '<td class="hide-mobile">' + esc((t.name || '') + (t.state ? ', ' + t.state : '')) + '</td>' +
                    '<td class="mono hide-mobile">' + esc(t.latitude) + '</td>' +
                    '<td class="mono hide-mobile">' + esc(t.longitude) + '</td>' +
                    '<td class="mono hide-mobile">' + (t.altitude_m != null ? esc(t.altitude_m) : '\u2014') + '</td>' +
                    '<td class="mono hide-mobile">' + (t.antenna_height_m != null ? esc(t.antenna_height_m) : '\u2014') + '</td>' +
                    '<td class="mono">' + esc(t.frequency_mhz) + matched + '</td>' +
                    '<td><span class="tower-badge ' + (BAND_CLASS[t.band] || '') + '">' + esc(t.band) + '</span></td>' +
                    '<td class="mono hide-mobile">' + esc(t.eirp_dbm) + ' dBm</td>' +
                    '<td class="mono">' + esc(t.distance_km) + ' km</td>' +
                    '<td class="hide-mobile">' + esc(t.bearing_deg) + '\u00b0 <span class="cardinal">' + esc(t.bearing_cardinal) + '</span></td>' +
                    '<td class="mono hide-mobile">' + esc(t.received_power_dbm) + ' dBm</td>' +
                    '<td><span class="tower-badge ' + tier.cls + '">' + esc(tier.label) + '</span></td>';

                tr._tower = t;
                tr.addEventListener('mouseenter', function() {
                    towerMarkers.forEach(function(m) {
                        if (m.tower === t) m.marker.setIcon(makeTowerIcon(m.tier.color, true));
                    });
                });
                tr.addEventListener('mouseleave', function() {
                    towerMarkers.forEach(function(m) {
                        if (m.tower === t) m.marker.setIcon(makeTowerIcon(m.tier.color, false));
                    });
                });

                tr.style.cursor = 'pointer';
                tr.addEventListener('click', function() { selectTower(t); });

                // The tick is its own control: it must not also select the row.
                var keep = tr.querySelector('.tower-keep');
                keep.addEventListener('click', function(e) { e.stopPropagation(); });
                keep.addEventListener('change', function() {
                    if (keep.checked && pickedCount() >= MAX_KEEP) {
                        keep.checked = false;
                        sayCache('Keep at most ' + MAX_KEEP + ' towers.', true);
                        return;
                    }
                    // The chosen tower is always kept.
                    if (!keep.checked && t === selectedTower) {
                        keep.checked = true;
                        sayCache('The tower you chose is always kept.', true);
                        return;
                    }
                    keepTower(t, keep.checked);
                    syncCache();
                });

                tableBody.appendChild(tr);
            });
        }

        // ── Antenna aiming guide ─────────────────────────────
        // best_azimuth_deg is used exactly as given, never derived from the
        // tower bearing. See docs/features/setup-wizard.md#antenna-aiming-guide

        // Screen y grows downward, so a true-north bearing maps to
        // (sin, -cos): 0 deg lands at the top, 90 at the right.
        function rosePoint(deg, radius, centre) {
            var rad = deg * Math.PI / 180;
            return {
                x: centre + Math.sin(rad) * radius,
                y: centre - Math.cos(rad) * radius
            };
        }

        function compassRose(refDeg, surDeg) {
            var C = 56, R = 46;
            function arm(deg, color) {
                var p = rosePoint(deg, R - 7, C);
                return '<line x1="' + C + '" y1="' + C + '" x2="' + p.x.toFixed(1) +
                       '" y2="' + p.y.toFixed(1) + '" stroke="' + color +
                       '" stroke-width="3" stroke-linecap="round"/>' +
                       '<circle cx="' + p.x.toFixed(1) + '" cy="' + p.y.toFixed(1) +
                       '" r="4.5" fill="' + color + '"/>';
            }
            var svg = '<svg class="antenna-rose" width="112" height="112" viewBox="0 0 112 112" ' +
                      'role="img" aria-label="Antenna bearings from your site">' +
                      '<circle cx="56" cy="56" r="46" fill="none" stroke="var(--line)" stroke-width="1"/>';
            var marks = [['N', 56, 15], ['E', 103, 60], ['S', 56, 105], ['W', 9, 60]];
            marks.forEach(function(m) {
                svg += '<text x="' + m[1] + '" y="' + m[2] + '" text-anchor="middle" ' +
                       'font-size="10" fill="var(--ink-3)">' + m[0] + '</text>';
            });
            // Surveillance first, so the reference arm paints on top where the
            // two nearly coincide.
            if (surDeg !== null) svg += arm(surDeg, 'var(--ink)');
            if (refDeg !== null) svg += arm(refDeg, 'var(--accent)');
            return svg + '<circle cx="56" cy="56" r="3" fill="var(--ink-3)"/></svg>';
        }

        function dirCard(role, swatch, deg, note) {
            return '<div class="antenna-dir">' +
                '<div class="antenna-dir-role"><span class="swatch" style="background:' + swatch + ';"></span>' +
                esc(role) + '</div>' +
                '<div class="antenna-dir-value">' + Math.round(deg) + '\u00b0 ' +
                esc(bearingCardinal(deg)) + '</div>' +
                '<div class="antenna-dir-note">' + note + '</div>' +
            '</div>';
        }

        function renderAntennaGuide(t) {
            var guide = document.getElementById('antennaGuide');
            if (!guide) return;

            var refDeg = isNum(t.bearing_deg) ? t.bearing_deg : null;
            var surDeg = isNum(t.best_azimuth_deg) ? t.best_azimuth_deg : null;

            // No bearing, no guide: a panel of dashes would read as a finding.
            if (refDeg === null) {
                guide.style.display = 'none';
                guide.innerHTML = '';
                return;
            }

            var refNote = 'Straight at the tower, ' + esc(t.distance_km) + ' km away.';
            var surCard;
            if (surDeg !== null) {
                var area = formatAreaKm2(t.expected_area_km2);
                surCard = dirCard('Surveillance antenna (input 2)', 'var(--ink)', surDeg,
                    'Where the largest area is expected' +
                    (area ? ', about ' + esc(area) + ' km&sup2;' : '') + '.');
            } else {
                // An older finder omits it. Say so rather than invent one.
                surCard = '<div class="antenna-dir">' +
                    '<div class="antenna-dir-role"><span class="swatch" style="background:var(--ink);"></span>' +
                    'Surveillance antenna (input 2)</div>' +
                    '<div class="antenna-dir-value">\u2014</div>' +
                    '<div class="antenna-dir-note">No recommended azimuth was returned for this tower.</div>' +
                '</div>';
            }

            var foot = [];
            if (surDeg !== null) {
                // Smallest angle between the two bearings, so 350 and 10 read
                // as 20 apart rather than 340.
                var sep = Math.abs((((surDeg - refDeg) % 360 + 540) % 360) - 180);
                foot.push('The two point about ' + Math.round(sep) + '\u00b0 apart.');
            }
            if (beyondHorizon(t.distance_km, t.horizon_km)) {
                foot.push('This tower is past your radio horizon of ' + esc(t.horizon_km) +
                          ' km, so expect a weak reference signal.');
            }

            guide.innerHTML =
                '<div class="antenna-guide-head">Aim your antennas</div>' +
                '<div class="antenna-guide-sub">For ' + esc(t.callsign || 'this tower') + ' at ' +
                    esc(t.frequency_mhz) + ' MHz. Bearings are degrees true, measured from your ' +
                    'receiver site.</div>' +
                '<div class="antenna-guide-body">' +
                    compassRose(refDeg, surDeg) +
                    '<div class="antenna-dirs">' +
                        dirCard('Reference antenna (input 1)', 'var(--accent)', refDeg, refNote) +
                        surCard +
                    '</div>' +
                '</div>' +
                (foot.length ? '<p class="antenna-guide-foot">' + foot.join(' ') + '</p>' : '');
            guide.style.display = '';
        }

        function selectTower(t) {
            selectedTower = t;
            // Always kept: it is the one preset the node is about to run on.
            if (!isKept(t)) {
                if (pickedCount() >= MAX_KEEP) {
                    // Make room by dropping the lowest-ranked pick, and say so.
                    var picks = bestFirst(keptTowers).filter(function(k) { return !isManual(k); });
                    var dropped = picks[picks.length - 1];
                    keepTower(dropped, false);
                    pendingNote = (dropped.callsign || dropped.name) + ' was dropped to make room. ';
                }
                keepTower(t, true);
                refreshKeepBoxes();
                syncCache();
            }
            selectedCard.style.display = '';
            selectedName.textContent = (t.callsign || 'Unknown') + ' \u2014 ' + t.frequency_mhz + ' MHz ' + t.band;
            selectedDetail.textContent = t.distance_km + ' km ' + t.bearing_cardinal + ' \u00b7 ' + (t.name || '') + (t.state ? ', ' + t.state : '');
            saveBtn.disabled = false;
            renderAntennaGuide(t);

            // Highlight selected row, clear others
            tableBody.querySelectorAll('tr').forEach(function(row) {
                row.classList.toggle('selected', row._tower === t);
            });

            towerMarkers.forEach(function(m) {
                var hl = (m.tower === t);
                m.marker.setIcon(makeTowerIcon(m.tier.color, hl));
                if (hl) m.marker.openPopup();
            });
        }

        if (!listenersAdded) {
            listenersAdded = true;

            var statusEl = document.getElementById('towerSaveStatus');

            saveBtn.addEventListener('click', function() {
                if (!selectedTower || !params) return;
                saveBtn.disabled = true;
                skipBtn.style.display = 'none';
                statusEl.innerHTML = '<span class="spinner-border spinner-border-sm text-primary"></span> Saving configuration\u2026';

                var payload = {
                    rx_latitude: params.lat,
                    rx_longitude: params.lon,
                    rx_altitude: params.alt,
                    tx_latitude: selectedTower.latitude,
                    tx_longitude: selectedTower.longitude,
                    tx_altitude: selectedTower.altitude_m || 0,
                    tx_callsign: selectedTower.callsign || 'Tower',
                    frequency_mhz: selectedTower.frequency_mhz
                };

                var pollFailures = 0;

                function pollApply() {
                    fetch('/config/apply/status')
                    .then(function(r) { return r.json(); })
                    .then(function(s) {
                        pollFailures = 0;
                        if (s.state === 'running') {
                            var label = s.phase_label || 'Applying configuration';
                            if (s.phase === 'settling' && s.settle_remaining) {
                                label += ' (' + s.settle_remaining + 's)';
                            }
                            statusEl.innerHTML = '<span class="text-muted">' + label + '…</span>';
                            setTimeout(pollApply, 1000);
                        } else if (s.state === 'failed') {
                            statusEl.innerHTML = '<span class="text-warning">Configuration saved but services failed to restart: ' + (s.error || 'Unknown error') + '</span>';
                            saveBtn.disabled = false;
                            saveBtn.textContent = 'Retry';
                            skipBtn.style.display = '';
                            skipBtn.textContent = 'Continue anyway →';
                        } else {
                            statusEl.innerHTML = '<span class="text-success">Configuration saved and services restarted.</span>';
                            setTimeout(advance, 1500);
                        }
                    })
                    .catch(function() {
                        // A few misses are normal during the restart; a run of
                        // them gives back the way past instead of spinning forever.
                        if (++pollFailures >= 5) {
                            statusEl.innerHTML = '<span class="text-warning">Configuration saved, but progress could not be read.</span>';
                            saveBtn.disabled = false;
                            saveBtn.textContent = 'Retry';
                            skipBtn.style.display = '';
                            skipBtn.textContent = 'Continue anyway \u2192';
                            return;
                        }
                        setTimeout(pollApply, 2000);
                    });
                }

                postJSON('/towers/select', payload)
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (!data.success) {
                        statusEl.innerHTML = '<span class="text-danger">Failed to save: ' + (data.error || 'Unknown error') + '</span>';
                        saveBtn.disabled = false;
                        saveBtn.textContent = 'Retry';
                        skipBtn.style.display = '';
                        return;
                    }
                    if (data.status) {
                        // Wait for the queued restart: later steps expect the
                        // radar to be back up.
                        pollApply();
                    } else if (data.error) {
                        statusEl.innerHTML = '<span class="text-warning">Configuration saved but services failed to restart: ' + data.error + '</span>';
                        saveBtn.disabled = false;
                        saveBtn.textContent = 'Retry';
                        skipBtn.style.display = '';
                        skipBtn.textContent = 'Continue anyway \u2192';
                    } else {
                        // retina-node not installed yet: config saved, advance.
                        statusEl.innerHTML = '<span class="text-success">Configuration saved. Services will start when retina-node is installed.</span>';
                        setTimeout(advance, 1500);
                    }
                })
                .catch(function(err) {
                    statusEl.innerHTML = '<span class="text-danger">Failed to save: ' + err.message + '</span>';
                    saveBtn.disabled = false;
                    saveBtn.textContent = 'Save & Continue';
                    skipBtn.style.display = '';
                });
            });

            skipBtn.addEventListener('click', advance);
        }
    };
    })();

    // Auto-Calibrate against the tower just chosen, using CAL.QUICK_RUN (the
    // shape shared with Quick Calibrate lives in calibrate.js). The soak it
    // keeps is not optional; see calibrator.py's SOAK_SECONDS.
    // See docs/features/setup-wizard.md#auto-calibrate-step
    enterHooks.calibrate = (function() {
        var stopPoll = null;
        var listenersAdded = false;
        var advancing = false;

        function el(id) { return document.getElementById(id); }

        function show(intro, running) {
            el('calWizIntro').style.display = intro ? '' : 'none';
            el('calWizRunning').style.display = running ? '' : 'none';
        }

        function setButtons(state) {
            el('calWizStartBtn').style.display  = state === 'idle' ? '' : 'none';
            el('calWizSkipBtn').style.display   = state === 'running' ? 'none' : '';
            el('calWizCancelBtn').style.display = state === 'running' ? '' : 'none';
            el('calWizNextBtn').style.display   = state === 'terminal' ? '' : 'none';
            el('calWizCancelBtn').disabled = false;
        }

        // Persists any tuning without asking, as the Configuration page's
        // calibrate window does: /set-up/complete restarts the stack seconds
        // later and would discard it.
        function persistThenAllowNext(status) {
            var CAL = window.RetinaCalibrate;
            var tuning = CAL.tuningOf(status);
            if (!tuning) { setButtons('terminal'); return; }
            var statusEl = el('calWizStatus');
            statusEl.textContent = 'Saving settings…';
            CAL.apply().then(function(d) {
                if (!d.success) throw new Error(d.error || 'Apply failed');
                // Wait before allowing advance: an overlapping restart from
                // /set-up/complete races the 90s restart lock, and
                // enforce_radar_mode swallows that failure silently.
                return CAL.pollApply(function(s) {
                    var label = s.phase_label || 'Applying';
                    if (s.phase === 'settling' && s.settle_remaining) {
                        label += ' (' + s.settle_remaining + 's)';
                    }
                    statusEl.textContent = label + '…';
                });
            }).then(function() {
                statusEl.textContent = 'Settings saved.';
                setButtons('terminal');
            }).catch(function(err) {
                // Never blocks completion: the tuning is live either way.
                statusEl.textContent = 'Could not save settings: ' + err.message;
                setButtons('terminal');
            });
        }

        function renderTerminal(status) {
            var CAL = window.RetinaCalibrate;
            var resultEl = el('calWizResult');
            var errorEl = el('calWizError');
            show(false, false);
            el('calWizSpinner').style.display = 'none';

            var tuning = CAL.tuningOf(status);
            if (tuning) {
                var before = status.original;
                // Show what changed, not just the end state.
                var wasRow = before
                    ? '<div style="font-size:12.5px;color:var(--ink-3);margin-top:6px;">'
                      + 'Previously gain reduction A ' + before.gain_a + ' dB / B '
                      + before.gain_b + ' dB, LNA state ' + before.lna_state + '.</div>'
                    : '';
                // What the soak proved; shared with Quick Calibrate.
                var soak = CAL.soakSummary(status);
                var held = soak
                    ? '<div style="font-size:12.5px;color:var(--ink-3);margin-top:6px;">'
                      + soak + '</div>'
                    : '';
                resultEl.style.display = '';
                resultEl.innerHTML =
                    '<div style="padding:14px 16px;border:1px solid var(--ok-edge);'
                    + 'background:var(--ok-soft);border-radius:var(--r-md);">'
                    + '<div style="font-weight:600;font-size:14px;color:var(--ok);">Receiver tuned</div>'
                    + '<div style="font-size:13px;color:var(--ink-2);margin-top:4px;">'
                    + 'Using <strong>' + CAL.escapeHtml(tuning.tower_name || 'Tower')
                    + '</strong> at ' + CAL.mhz(tuning.fc) + '.</div>'
                    + '<div style="font-size:13px;color:var(--ink-2);margin-top:8px;">'
                    + 'Gain reduction A <strong>' + tuning.gain_a + ' dB</strong>'
                    + ' / B <strong>' + tuning.gain_b + ' dB</strong>'
                    + ', LNA state <strong>' + tuning.lna_state + '</strong>.</div>'
                    + wasRow + held + '</div>';
                // Static copy lives in the template: see #calWizNext there.
                el('calWizNext').style.display = '';
            } else {
                errorEl.style.display = '';
                errorEl.innerHTML = '<span style="color:var(--ink-2);">'
                    + CAL.escapeHtml(status.error || 'Tuning did not complete.')
                    + ' You can run this again later from Configuration.</span>';
            }
            errorEl.innerHTML += CAL.updateWarning(status);
            if (errorEl.innerHTML) errorEl.style.display = '';
            persistThenAllowNext(status);
        }

        function renderRunning(status) {
            var CAL = window.RetinaCalibrate;
            show(false, true);
            el('calWizSpinner').style.display = '';
            el('calWizPhase').textContent = CAL.PHASE_LABELS[status.phase] || 'Starting…';
            if (status.current) {
                el('calWizDetail').style.display = '';
                el('calWizTower').textContent = status.current.tower_name || '-';
                el('calWizFc').textContent = CAL.mhz(status.current.fc);
                el('calWizGains').textContent =
                    'A ' + status.current.gain_a + ' dB / B ' + status.current.gain_b + ' dB';
                el('calWizLna').textContent =
                    status.current.lna_state == null ? '-' : status.current.lna_state;
                var rf = status.rf || {};
                el('calWizOverload').textContent = rf.overload_a == null ? '-'
                    : ('A ' + (rf.overload_a ? 'OVERLOAD' : 'ok')
                       + ' / B ' + (rf.overload_b ? 'OVERLOAD' : 'ok'));
            }
        }

        function watch() {
            if (stopPoll) stopPoll();
            setButtons('running');
            stopPoll = window.RetinaCalibrate.pollStatus(function(status) {
                if (status.state === 'running') { renderRunning(status); return; }
                stopPoll = null;
                window._calWizStopPoll = null;
                renderTerminal(status);
            });
            // Published so leaveHooks.calibrate, which lives outside this
            // closure, can stop the timer when the step goes off screen.
            window._calWizStopPoll = stopPoll;
        }

        return function() {
            var CAL = window.RetinaCalibrate;
            el('calWizResult').style.display = 'none';
            el('calWizResult').innerHTML = '';
            el('calWizError').style.display = 'none';
            el('calWizError').innerHTML = '';
            el('calWizNext').style.display = 'none';
            el('calWizStatus').textContent = '';
            advancing = false;

            // Never auto-start: this fires on every entry, including after a
            // reload. Reattach to the run on the node, which this tab does not own.
            CAL.getStatus().then(function(status) {
                if (status.state === 'running') { watch(); return; }
                if (CAL.isTerminal(status) && CAL.tuningOf(status)) {
                    // A finished run: show it rather than offer to redo it.
                    renderTerminal(status);
                    return;
                }
                // Includes idle after a reboot mid-run, which is normal.
                show(true, false);
                setButtons('idle');
            }).catch(function() {
                show(true, false);
                setButtons('idle');
            });

            if (listenersAdded) return;
            listenersAdded = true;

            el('calWizStartBtn').addEventListener('click', function() {
                el('calWizStatus').textContent = '';
                el('calWizResult').style.display = 'none';
                el('calWizError').style.display = 'none';
                el('calWizNext').style.display = 'none';
                show(false, true);
                setButtons('running');
                el('calWizPhase').textContent = 'Starting…';
                window.RetinaCalibrate.start(
                    window.RetinaCalibrate.QUICK_RUN
                ).then(function(d) {
                    if (!d.success) {
                        show(true, false);
                        setButtons('idle');
                        el('calWizError').style.display = '';
                        el('calWizError').innerHTML =
                            '<span style="color:var(--danger,#b91c1c);">'
                            + window.RetinaCalibrate.escapeHtml(d.error || 'Could not start') + '</span>';
                        return;
                    }
                    watch();
                }).catch(function() {
                    show(true, false);
                    setButtons('idle');
                    el('calWizError').style.display = '';
                    el('calWizError').textContent = 'Could not start tuning.';
                });
            });

            el('calWizCancelBtn').addEventListener('click', function() {
                el('calWizCancelBtn').disabled = true;
                el('calWizStatus').textContent = 'Cancelling…';
                window.RetinaCalibrate.cancel().catch(function() {});
            });

            el('calWizSkipBtn').addEventListener('click', function() {
                if (advancing) return;
                advancing = true;
                advance();
            });

            el('calWizNextBtn').addEventListener('click', function() {
                if (advancing) return;
                advancing = true;
                advance();
            });
        };
    })();

    // Leaving the step must not leave a timer running against a view that is
    // no longer on screen. The run itself continues on the node, and
    // re-entering reattaches to it.
    leaveHooks.calibrate = function() {
        if (window._calWizStopPoll) { window._calWizStopPoll(); window._calWizStopPoll = null; }
    };

    // Complete. See docs/features/setup-wizard.md#completion
    //
    // save-step {complete} is the request that clears the wizard state and
    // writes the completed flag, so until it lands Home, Configuration and
    // /tutorial all redirect back here. Both ways out stay hidden until it
    // succeeds: a Start tutorial button offered early sends the owner straight
    // back into the wizard, which is what made this worth gating rather than
    // firing and forgetting.
    enterHooks.complete = function() {
        window.removeEventListener('beforeunload', handleBeforeUnload);
        if (!hookInitialized.complete) {
            hookInitialized.complete = true;
            document.getElementById('completeRetryBtn')
                .addEventListener('click', finishSetup);
        }
        finishSetup();
    };

    function finishSetup() {
        var status = document.getElementById('completeStatus');
        var retry = document.getElementById('completeRetryBtn');
        var ways = [document.getElementById('completeHomeBtn'),
                    document.getElementById('startTutorialBtn')];

        status.textContent = 'Finishing setup…';
        retry.style.display = 'none';
        ways.forEach(function(a) { a.style.display = 'none'; });

        postJSON('/set-up/save-step', { step: 'complete' }).then(function() {
            // Not awaited, and deliberately after the gate: /set-up/complete
            // brings the radar stack up, which takes tens of seconds, and
            // nothing on this step depends on it having finished. It also never
            // raises on the node, so there is nothing here to report.
            // Sent before the links appear and with keepalive, because a click
            // on either one navigates away, and that cancelled it while it was
            // still in the browser, leaving a node in Spectrum with no radar.
            // See docs/features/setup-wizard.md#completion
            postJSON('/set-up/complete', null, true).catch(function() {});
            status.textContent = '';
            ways.forEach(function(a) { a.style.display = ''; });
        }).catch(function(err) {
            status.textContent = err.message
                ? 'Could not finish setup: ' + err.message
                : 'Could not finish setup. Check this node’s connection.';
            retry.style.display = '';
        });
    }

    // ── Init ─────────────────────────────────────────────

    var startIndex = 0;

    if (demoMode) {
        // Demo: start from the top and seed the tower search.
        window._towerSearchParams = { lat: 37.7749, lon: -122.4194, alt: 16, measurements: [] };
        startIndex = 0;
    } else if (devMode) {
        for (var i = 0; i < steps.length; i++) {
            if (steps[i].name === 'location') { startIndex = i; break; }
        }
    } else if (resumeStep) {
        for (var i = 0; i < steps.length; i++) {
            if (steps[i].name === resumeStep) { startIndex = i; break; }
        }
    }
    showStep(startIndex);
}
