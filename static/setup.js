function formatSize(bytes) {
    var mb = bytes ? '~' + Math.round(bytes / 1024 / 1024) + ' MB' : '~600 MB';
    return mb + ' · 5–10 minutes';
}

function initSetupWizard(resumeStep, highestStepName, devMode, isRerun, demoMode) {
    var steps = [];
    var currentIndex = 0;
    var highestStep = 0;
    var pollTimer = null;

    document.querySelectorAll('[data-step]').forEach(function(el) {
        steps.push({ name: el.getAttribute('data-step'), el: el });
    });

    // Restore highest step from server state
    if (highestStepName) {
        for (var i = 0; i < steps.length; i++) {
            if (steps[i].name === highestStepName) { highestStep = i; break; }
        }
    }

    var stepNames = {
        agreements: 'Agreements',
        system: 'System Update',
        radar: 'Packages',
        location: 'Where is your receiver?',
        towers: 'Choose a tower',
        complete: 'You\'re all set'
    };

    // Build dots once
    var track = document.getElementById('progressTrack');
    steps.forEach(function(s, i) {
        var dot = document.createElement('div');
        dot.className = 'progress-dot';
        dot.setAttribute('data-dot', i);
        dot.title = stepNames[s.name] || '';
        dot.addEventListener('click', function() {
            if (i <= highestStep && i !== currentIndex) showStep(i);
        });
        // Insert before progressLabel so dots appear left of the label
        var lbl = document.getElementById('progressLabel');
        if (lbl) { track.insertBefore(dot, lbl); } else { track.appendChild(dot); }
    });

    // Wire back/exit button
    var backBtn = document.getElementById('wizBackBtn');
    if (backBtn) {
        backBtn.addEventListener('click', function() {
            if (currentIndex === 0) {
                window.location.href = '/';
            } else {
                showStep(currentIndex - 1);
            }
        });
    }

    function updateProgress(index) {
        highestStep = Math.max(highestStep, index);
        var label = document.getElementById('progressLabel');
        var fill = document.getElementById('progressFill');
        var total = steps.length;

        label.textContent = 'Step ' + (index + 1) + ' of ' + total;

        // Fill width: from first dot to current dot
        var pct = total > 1 ? (index / (total - 1)) * 100 : 0;
        fill.style.width = pct + '%';

        // Update dots — only dots behind current are blue, future dots stay grey
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
        var navBtns = document.querySelectorAll('.step-foot-btns button, #wizBackBtn');
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

            var backBtn = document.getElementById('wizBackBtn');
            if (backBtn) {
                backBtn.style.display = index === 0 ? 'none' : '';
                if (index > 0) backBtn.textContent = '← Back';
            }

            document.querySelectorAll('.step-foot-btns').forEach(function(el) {
                el.style.display = 'none';
            });
            var activeBtns = document.getElementById('stepBtns-' + steps[index].name);
            if (activeBtns) activeBtns.style.display = '';

            postJSON('/set-up/save-step', { step: steps[index].name });

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

    function postJSON(url, body) {
        return fetch(url, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': csrfToken
            },
            body: body ? JSON.stringify(body) : undefined
        });
    }

    // Single beforeunload guard for the entire wizard lifetime.
    // Shows the browser's native "leave site?" dialog on all steps.
    // On the location step it also fires the SDR-release beacon so
    // retina-spectrum stops even if the user confirms and leaves.
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

    // ── Demo mode: mock all API calls ────────────────────
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
            if (path === '/mender/install-os')       return ok({ success: true });
            if (path === '/mender/check') {
                if (_demoNodeInstalling) {
                    return ok({ installing: true, stage: 'pulling', reason: 'Installing retina-node-v1.0.0-demo' });
                }
                return ok({
                    current_version: 'v0.9.0-demo',
                    latest_version: 'v1.0.0-demo',
                    latest_size_bytes: 641000000,
                    available_updates: [
                        { version: 'v1.0.0-demo', size_bytes: 641000000 },
                        { version: 'v0.9.5-demo', size_bytes: 635000000 },
                    ],
                });
            }
            if (path === '/mender/install') {
                _demoNodeInstalling = true;
                setTimeout(function() { _demoNodeInstalling = false; }, 8000);
                return ok({ success: true });
            }
            if (path === '/towers/select')               return ok({ success: true, applied: false });
            if (path === '/api/mode' && (!opts || opts.method !== 'POST')) return ok({ mode: 'spectrum' });
            if (path === '/api/mode') return ok({ success: true, mode: 'spectrum' });
            if (path === '/set-up/save-step')          return ok({ success: true });
            if (path === '/set-up/complete')           return ok({ success: true });
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

    // Step 1: Agreements
    enterHooks.agreements = function() {
        if (hookInitialized.agreements) return;
        hookInitialized.agreements = true;
        var boxes = ['eulaCheck', 'cloudCheck'];
        var btn = document.getElementById('agreementsContinueBtn');

        function update() {
            var allChecked = boxes.every(function(id) {
                return document.getElementById(id).checked;
            });
            btn.disabled = !allChecked;
        }

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
            postJSON('/mender/cloud-services', {enabled: true})
            .then(function(r) { return r.json(); })
            .then(function() { advance(); })
            .catch(function() {
                btn.disabled = false;
                btn.textContent = 'Continue';
            });
        });
    };

    // Step 2: System Update
    enterHooks.system = function() {
        if (hookInitialized.system) return;
        hookInitialized.system = true;
        var status = document.getElementById('systemStatus');
        var updateBtn = document.getElementById('systemUpdateBtn');
        var nextBtn = document.getElementById('systemNextBtn');
        var installStatus = document.getElementById('systemInstallStatus');
        var cardStatus = document.getElementById('systemCardStatus');
        var versionArrow = document.getElementById('systemVersionArrow');

        // On re-run, skip updates — OS updates are managed remotely after onboarding
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

        fetch('/mender/check-os')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.current_version) {
                    document.getElementById('systemCurrentVersion').textContent = data.current_version;
                }
                if (data.installing) {
                    showStage(data.stage);
                    cardStatus.innerHTML = '<span class="spinner-border spinner-border-sm text-primary"></span>';
                    installStatus.innerHTML = '<span class="text-warning">Do not power off the device.</span>';
                    startSystemPoll();
                    return;
                }
                if (data.error) {
                    status.textContent = 'Unable to check: ' + data.error;
                    nextBtn.style.display = '';
                    return;
                }
                if (data.update_available) {
                    status.textContent = '';
                    versionArrow.style.display = '';
                    document.getElementById('systemLatestVersion').textContent = data.latest_version;
                    updateBtn.style.display = '';
                } else {
                    status.innerHTML = 'System is up to date &#10003;';
                    cardStatus.innerHTML = '<span class="text-success">&#10003;</span>';
                    nextBtn.style.display = '';
                    nextBtn.textContent = 'Continue \u2192';
                }
            })
            .catch(function() {
                status.textContent = 'Unable to check for system updates.';
                nextBtn.style.display = '';
                nextBtn.textContent = 'Skip \u2192';
            });

        updateBtn.addEventListener('click', function() {
            updateBtn.style.display = 'none';
            cardStatus.innerHTML = '<span class="spinner-border spinner-border-sm text-primary"></span>';
            installStatus.innerHTML = '<span class="text-warning">Do not power off the device.</span>';

            postJSON('/mender/install-os')
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.success) {
                        startSystemPoll();
                    } else {
                        installStatus.innerHTML = '<span class="text-danger">' + data.error + '</span>';
                        cardStatus.innerHTML = '';
                        updateBtn.style.display = '';
                    }
                })
                .catch(function() {
                    installStatus.innerHTML = '<span class="text-danger">Request failed. Please try again.</span>';
                    cardStatus.innerHTML = '';
                    updateBtn.style.display = '';
                });
        });

        nextBtn.addEventListener('click', advance);

        function showStage(stage) {
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
            cardStatus.innerHTML = '<span class="spinner-border spinner-border-sm text-primary"></span>';
        }

        function startSystemPoll() {
            showStage('waiting');
            if (backBtn) backBtn.style.display = 'none';
            if (pollTimer) clearInterval(pollTimer);
            pollTimer = setInterval(function() {
                fetch('/mender/check-os')
                    .then(function(r) { return r.json(); })
                    .then(function(data) {
                        if (!data.installing) {
                            clearInterval(pollTimer);
                            pollTimer = null;
                            if (backBtn) backBtn.style.display = '';
                            if (!data.update_available) {
                                status.innerHTML = 'Update complete &#10003;';
                                installStatus.innerHTML = '<span class="text-success">Complete!</span>';
                            } else {
                                status.innerHTML = 'Device may reboot &mdash; reconnect to continue.';
                            }
                            nextBtn.style.display = '';
                        } else {
                            showStage(data.stage);
                        }
                    })
                    .catch(function() {
                        status.textContent = 'Rebooting... reconnect shortly.';
                    });
            }, 5000);
        }
    };

    // Step 3: Packages
    enterHooks.radar = function() {
        if (hookInitialized.radar) return;
        hookInitialized.radar = true;
        var status = document.getElementById('radarStatus');
        var installBtn = document.getElementById('radarInstallBtn');
        var nextBtn = document.getElementById('radarNextBtn');
        var installStatus = document.getElementById('radarInstallStatus');
        var regionCheck = document.getElementById('regionCheck');
        var packageStatus = document.getElementById('radarPackageStatus');

        // Re-run: RETINA is already installed — show installed version for reference
        // and any available updates below. No reinstall option.
        if (isRerun) {
            function rerunUpdateGate() {
                if (installBtn.style.display !== 'none') {
                    installBtn.disabled = !regionCheck.checked;
                }
            }
            regionCheck.addEventListener('change', rerunUpdateGate);

            fetch('/mender/check')
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.installing) {
                        status.textContent = data.reason || 'Installation in progress...';
                        installStatus.innerHTML = '<span class="text-warning">Do not power off the device.</span>';
                        if (backBtn) backBtn.style.display = 'none';
                        startRadarPoll();
                        return;
                    }

                    var updates = data.available_updates || [];

                    var packageList = document.getElementById('radarPackageList');
                    var availableSection = document.getElementById('radarAvailableSection');
                    var availableHeading = document.getElementById('radarAvailableHeading');
                    var description = document.getElementById('radarDescription');

                    if (data.current_version) {
                        document.getElementById('radarCurrentList').innerHTML =
                            '<div class="step-card">' +
                            '<div class="step-card-body">' +
                            '<div class="step-card-title">Retina Passive Radar <span style="font-weight:400;color:var(--ink-3);font-size:13px;margin-left:4px;">' + esc(data.current_version) + '</span></div>' +
                            '<div class="step-card-sub">Installed</div>' +
                            '</div></div>';
                        document.getElementById('radarCurrentSection').style.display = '';
                    }

                    if (updates.length > 0) {
                        description.textContent = 'A newer version of RETINA is available.';
                        var cards = updates.map(function(v, i) {
                            var safeId = 'pkg-' + v.version.replace(/[^a-z0-9]/gi, '-');
                            return '<div class="step-card">' +
                                '<div><input type="radio" name="packageSelect" id="' + safeId + '" value="' + esc(v.version) + '"' +
                                (i === 0 ? ' checked' : '') +
                                ' style="accent-color:var(--ink);margin-right:12px;"></div>' +
                                '<label class="step-card-body" for="' + safeId + '" style="cursor:pointer;">' +
                                '<div class="step-card-title">Retina Passive Radar <span style="font-weight:400;color:var(--ink-3);font-size:13px;margin-left:4px;">' + esc(v.version) + '</span></div>' +
                                '<div class="step-card-sub">' + formatSize(v.size_bytes) + '</div>' +
                                '</label></div>';
                        });
                        packageList.innerHTML = cards.join('');
                        availableHeading.textContent = 'Available updates';
                        availableHeading.style.display = '';
                        installBtn.textContent = 'Install selected';
                        installBtn.style.display = '';
                        rerunUpdateGate();
                    } else {
                        description.textContent = 'RETINA is up to date.';
                        availableSection.style.display = 'none';
                    }

                    nextBtn.textContent = 'Skip \u2192';
                    nextBtn.style.display = '';
                })
                .catch(function() {
                    status.textContent = 'Unable to check for updates.';
                    nextBtn.textContent = 'Skip \u2192';
                    nextBtn.style.display = '';
                });

            installBtn.addEventListener('click', function() {
                var selected = document.querySelector('input[name="packageSelect"]:checked');
                installBtn.style.display = 'none';
                nextBtn.style.display = 'none';
                if (backBtn) backBtn.style.display = 'none';
                status.textContent = 'Installing...';
                installStatus.innerHTML = '<span class="text-warning">Do not power off the device.</span>';

                postJSON('/mender/install', selected ? {version: selected.value} : undefined)
                    .then(function(r) { return r.json(); })
                    .then(function(data) {
                        if (data.success) {
                            startRadarPoll();
                        } else {
                            installStatus.innerHTML = '<span class="text-danger">' + data.error + '</span>';
                            status.textContent = '';
                            installBtn.style.display = '';
                            nextBtn.style.display = '';
                            if (backBtn) backBtn.style.display = '';
                            rerunUpdateGate();
                        }
                    })
                    .catch(function() {
                        installStatus.innerHTML = '<span class="text-danger">Request failed. Please try again.</span>';
                        status.textContent = '';
                        installBtn.style.display = '';
                        nextBtn.style.display = '';
                        if (backBtn) backBtn.style.display = '';
                        rerunUpdateGate();
                    });
            });

            nextBtn.addEventListener('click', advance);
            return;
        }

        // Fresh install — RETINA is not yet on this node, installation is required.
        document.getElementById('radarDescription').textContent = 'RETINA is not yet installed on this node. Select a version below to continue.';
        installBtn.classList.remove('ghost');
        installBtn.classList.add('primary');

        function updateInstallGate() {
            if (installBtn.style.display !== 'none') {
                installBtn.disabled = !regionCheck.checked;
            }
        }
        regionCheck.addEventListener('change', updateInstallGate);

        var latestVersion = null;

        fetch('/mender/check')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.installing) {
                    status.textContent = data.reason || 'Installation in progress...';
                    installStatus.innerHTML = '<span class="text-warning">Do not power off the device.</span>';
                    packageStatus.innerHTML = '<span class="spinner-border spinner-border-sm text-primary"></span>';
                    if (backBtn) backBtn.style.display = 'none';
                    startRadarPoll();
                    return;
                }
                if (data.error) {
                    status.textContent = 'Unable to check: ' + data.error;
                    return;
                }
                if (data.current_version) {
                    status.innerHTML = 'Packages are up to date &#10003;';
                    packageStatus.innerHTML = '<span class="text-success">&#10003;</span>';
                    document.getElementById('radarLatestVersion').textContent = data.current_version;
                    document.getElementById('radarPackageSub').textContent = formatSize(data.latest_size_bytes);
                    nextBtn.style.display = '';
                } else {
                    latestVersion = data.latest_version;
                    document.getElementById('radarLatestVersion').textContent = data.latest_version;
                    document.getElementById('radarPackageSub').textContent = formatSize(data.latest_size_bytes);
                    installBtn.style.display = '';
                    updateInstallGate();
                    // No skip — installation is required on a fresh node
                }
            })
            .catch(function() {
                status.textContent = 'Unable to check for available packages.';
            });

        installBtn.addEventListener('click', function() {
            installBtn.style.display = 'none';
            if (backBtn) backBtn.style.display = 'none';
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
                        if (backBtn) backBtn.style.display = '';
                        updateInstallGate();
                    }
                })
                .catch(function() {
                    installStatus.innerHTML = '<span class="text-danger">Request failed. Please try again.</span>';
                    packageStatus.innerHTML = '';
                    status.textContent = '';
                    installBtn.style.display = '';
                    if (backBtn) backBtn.style.display = '';
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
                            if (data.current_version) {
                                packageStatus.innerHTML = '<span class="text-success">&#10003;</span>';
                                status.textContent = '';
                                installStatus.innerHTML = '';
                                advance();
                            } else {
                                installStatus.innerHTML = '<span class="text-danger">Install may have failed. Try again.</span>';
                                packageStatus.innerHTML = '';
                                status.textContent = '';
                                installBtn.style.display = '';
                                if (isRerun) nextBtn.style.display = '';
                                if (backBtn) backBtn.style.display = '';
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

    // Step 4: Location input
    leaveHooks.location = function() {
        locationActive = false;
        clearTimeout(rfSseReconnectTimer); rfSseReconnectTimer = null;
        if (rfSse) { rfSse.close(); rfSse = null; }
        var targetMode = wizardWasMode;
        wizardWasMode = null;
        var pending = pendingModeSwitch;
        pendingModeSwitch = null;
        // No revert needed: spectrum was never started (user left before the mode
        // switch completed), or we were already in spectrum mode.
        if (!targetMode || targetMode === 'spectrum') return;
        var scanStatus = document.getElementById('scanStatus');
        if (scanStatus) { scanStatus.textContent = 'Reverting to radar mode…'; scanStatus.style.display = ''; }
        // Wait for any in-flight spectrum switch to finish before sending the
        // radar revert — prevents concurrent docker operations racing each other.
        return (pending || Promise.resolve()).then(function() {
            return postJSON('/api/mode', { mode: targetMode });
        }).then(
            function() { if (scanStatus) scanStatus.style.display = 'none'; },
            function() { if (scanStatus) scanStatus.style.display = 'none'; }
        );
    };

    enterHooks.location = function() {
        // Start retina-spectrum on every entry (idempotent — no-op if already in
        // spectrum mode or if retina-node is not yet installed).
        locationActive = true;
        var scanBtn = document.getElementById('scanRfBtn');
        var scanStatus = document.getElementById('scanStatus');
        scanBtn.disabled = true;
        scanBtn.textContent = 'Starting analyser…';
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
                scanStatus.textContent = 'Analyser unavailable — RF scan disabled';
            });

        // Event listeners and inner state set up only once
        if (hookInitialized.location) return;
        hookInitialized.location = true;

        var rxLat = document.getElementById('rxLat');
        var rxLon = document.getElementById('rxLon');
        var rxAlt = document.getElementById('rxAlt');

        if (demoMode) {
            rxLat.value = '37.7749';
            rxLon.value = '-122.4194';
            rxAlt.value = '16';
            // Fire input events so dependent listeners (find button gate) update
            rxLat.dispatchEvent(new Event('input'));
            rxLon.dispatchEvent(new Event('input'));
        }
        var useMyLocBtn = document.getElementById('useMyLocationBtn');
        var findBtn = document.getElementById('findTowersBtn');
        var geoError = document.getElementById('locationGeoError');
        var skipBtn = document.getElementById('locationSkipBtn');


        // RF scan — SSE connected after retina-spectrum starts; button sets 'waiting'
        // phase and the next sweep 'start' event begins accumulation.
        var scanResult = document.getElementById('scanResult');
        var rfMeasurements = [];
        var rfPhase = 'idle'; // idle | waiting | scanning | done
        var rfHwReady = false;

        function normaliseBand(id) {
            if (id === 'fm') return 'FM';
            if (id === 'vhf_hi' || id === 'vhf_lo') return 'VHF';
            if (id === 'uhf') return 'UHF';
            return id.toUpperCase();
        }

        function updateRfUI() {
            var n = rfMeasurements.length;
            if (rfPhase === 'idle' || rfPhase === 'waiting') {
                scanStatus.textContent = rfPhase === 'waiting' ? 'Waiting for sweep to start…' : '';
                scanStatus.style.display = rfPhase === 'waiting' ? '' : 'none';
                scanResult.style.display = 'none';
                scanBtn.disabled = !rfHwReady;
                scanBtn.textContent = 'Scan RF signals';
            } else if (rfPhase === 'scanning') {
                scanStatus.textContent = 'Scanning…' + (n > 0 ? ' — ' + n + ' signal' + (n !== 1 ? 's' : '') + ' found' : '');
                scanStatus.style.display = '';
                scanResult.style.display = 'none';
                scanBtn.disabled = true;
                scanBtn.textContent = 'Scanning…';
            } else if (rfPhase === 'done') {
                scanStatus.style.display = 'none';
                scanResult.textContent = n + ' signal' + (n !== 1 ? 's' : '') + ' detected';
                scanResult.style.display = '';
                scanBtn.disabled = false;
                scanBtn.textContent = 'Rescan';
            }
        }

        // Assign to outer-scope var so leaveHooks.location and re-entries can reach it
        connectRfSse = function() {
            if (rfSse) return;
            rfSse = new EventSource('/towers/spectrum/events');
            rfSse.onmessage = function(e) {
                var msg = JSON.parse(e.data);
                if (msg.type === 'start') {
                    if (rfPhase !== 'waiting') return;
                    rfMeasurements = [];
                    rfPhase = 'scanning';
                    updateRfUI();
                } else if (msg.type === 'step') {
                    if (!rfHwReady) { rfHwReady = true; updateRfUI(); }
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
                    rfPhase = 'done';
                    updateRfUI();
                }
            };
            rfSse.onerror = function() {
                rfSse.close(); rfSse = null;
                if (locationActive) rfSseReconnectTimer = setTimeout(connectRfSse, 3000);
            };
        };

        scanBtn.addEventListener('click', function() {
            rfMeasurements = [];
            rfPhase = 'waiting';
            updateRfUI();
        });

        // Enable Find Towers when lat/lon filled
        function updateFindBtn() {
            var lat = parseFloat(rxLat.value);
            var lon = parseFloat(rxLon.value);
            findBtn.disabled = isNaN(lat) || isNaN(lon);
        }
        rxLat.addEventListener('input', updateFindBtn);
        rxLon.addEventListener('input', updateFindBtn);

        // Use My Location
        useMyLocBtn.addEventListener('click', function() {
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
                    rxLat.value = pos.coords.latitude.toFixed(6);
                    rxLon.value = pos.coords.longitude.toFixed(6);
                    if (pos.coords.altitude != null) {
                        rxAlt.value = Math.round(pos.coords.altitude);
                    }
                    updateFindBtn();
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

        // Find Towers → advance to towers step and trigger search
        findBtn.addEventListener('click', function() {
            window._towerSearchParams = {
                lat: parseFloat(rxLat.value),
                lon: parseFloat(rxLon.value),
                alt: parseFloat(rxAlt.value) || 0,
                measurements: rfMeasurements
            };
            advance();
        });

        skipBtn.addEventListener('click', function() {
            // Skip both location and towers
            showStep(currentIndex + 2);
        });
    };

    // Step 5: Tower Selection
    enterHooks.towers = (function() {
        // Closure to hold state across re-entries
        var towerMarkers = [];
        var selectedTower = null;
        var listenersAdded = false;

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

        towerMarkers = [];
        selectedTower = null;

        // Clean up map from previous visit
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

        // Reset UI
        selectedCard.style.display = 'none';
        saveBtn.disabled = true;
        saveBtn.textContent = 'Save & Continue';
        tableBody.innerHTML = '';
        summaryEl.innerHTML = '';
        errorEl.style.display = 'none';

        // Read colors from CSS custom properties (defined in common.css)
        var cs = getComputedStyle(document.documentElement);
        function cv(name) { return cs.getPropertyValue(name).trim(); }

        var CLASS_COLORS = { Ideal: cv('--suit-ideal'), Good: cv('--suit-good'), Far: cv('--suit-far'), 'Too Close': cv('--suit-close') };
        var CLASS_BG = { Ideal: cv('--suit-ideal-bg'), Good: cv('--suit-good-bg'), Far: cv('--suit-far-bg'), 'Too Close': cv('--suit-close-bg') };
        var BAND_COLORS = { VHF: cv('--band-vhf'), UHF: cv('--band-uhf'), FM: cv('--band-fm') };
        var BAND_BG = { VHF: cv('--band-vhf-bg'), UHF: cv('--band-uhf-bg'), FM: cv('--band-fm-bg') };

        function makeTowerIcon(distClass, highlighted) {
            var color = CLASS_COLORS[distClass] || '#94a3b8';
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

        // Start search immediately on entering this step
        var params = window._towerSearchParams;
        if (!params) {
            loadingEl.style.display = 'none';
            errorEl.textContent = 'No location set. Go back and enter your location.';
            errorEl.style.display = '';
            return;
        }

        loadingEl.style.display = '';
        errorEl.style.display = 'none';
        resultsEl.style.display = 'none';

        postJSON('/towers/search', {
            lat: params.lat,
            lon: params.lon,
            measurements: params.measurements || []
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

        function renderResults(towers, query) {
            resultsEl.style.display = '';

            if (towers.length === 0) {
                summaryEl.innerHTML = '';
                tableBody.innerHTML = '';
                errorEl.textContent = 'No towers found in range. Try adjusting your location or increasing the search radius.';
                errorEl.style.display = '';
                skipBtn.style.display = '';
                return;
            }

            // Summary
            var ideal = towers.filter(function(t) { return t.distance_class === 'Ideal'; }).length;
            var bands = [];
            towers.forEach(function(t) { if (bands.indexOf(t.band) === -1) bands.push(t.band); });
            var best = towers[0];
            summaryEl.innerHTML =
                '<div class="stat-card"><span class="stat-value">' + towers.length + '</span><span class="stat-label">Towers Found</span></div>' +
                '<div class="stat-card"><span class="stat-value">' + ideal + '</span><span class="stat-label">Ideal Range</span></div>' +
                '<div class="stat-card"><span class="stat-value">' + esc(bands.join(', ')) + '</span><span class="stat-label">Bands</span></div>' +
                (best ? '<div class="stat-card"><span class="stat-value">' + esc(best.callsign || '\u2014') + '</span><span class="stat-label">Top Pick \u2014 ' + esc(best.distance_km) + ' km</span></div>' : '');

            // Map
            towerMarkers = [];

            var towerMap = L.map('towerMap');
            window._towerMap = towerMap;
            L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
                attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OSM</a> &copy; <a href="https://carto.com/">CARTO</a>'
            }).addTo(towerMap);

            var points = [];
            if (query) {
                var userLatLng = [query.latitude, query.longitude];
                points.push(userLatLng);
                L.marker(userLatLng, { icon: userIcon }).addTo(towerMap)
                    .bindPopup('<span class="popup-callsign">Your Location</span>');
                L.circle(userLatLng, {
                    radius: 80000,
                    color: '#3b82f6', weight: 1.5, fillOpacity: 0.04, dashArray: '6 4'
                }).addTo(towerMap);
            }

            towers.forEach(function(t) {
                var ll = [t.latitude, t.longitude];
                points.push(ll);
                var marker = L.marker(ll, { icon: makeTowerIcon(t.distance_class, false) }).addTo(towerMap);
                marker.bindPopup(
                    '<span class="popup-callsign">' + esc(t.callsign || 'Unknown') + '</span><br>' +
                    '<span class="popup-detail">' + esc(t.name || '') + '</span><br>' +
                    '<span class="popup-detail">' + esc(t.latitude + ', ' + t.longitude) +
                    (t.altitude_m != null ? ' &middot; ' + esc(t.altitude_m) + ' m ASL' : '') + '</span><br>' +
                    '<span class="popup-freq">' + esc(t.frequency_mhz) + ' MHz</span> (' + esc(t.band) + ')<br>' +
                    '<span class="popup-detail">' + esc(t.distance_km) + ' km ' + esc(t.bearing_cardinal) +
                    ' &middot; ' + esc(t.received_power_dbm) + ' dBm</span><br>' +
                    '<span style="color:' + (CLASS_COLORS[t.distance_class] || '#6b7280') +
                    ';font-weight:600;font-size:0.78rem;">' + esc(t.distance_class) + '</span>'
                );
                marker.on('click', function() { selectTower(t); });
                towerMarkers.push({ marker: marker, tower: t });
            });

            if (points.length > 1) {
                towerMap.fitBounds(points, { padding: [40, 40], maxZoom: 13 });
            } else if (points.length === 1) {
                towerMap.setView(points[0], 10);
            }

            // Table
            tableBody.innerHTML = '';
            towers.forEach(function(t) {
                var tr = document.createElement('tr');
                var bandColor = BAND_COLORS[t.band] || '#6b7280';
                var bandBg = BAND_BG[t.band] || 'rgba(107,114,128,0.08)';
                var classColor = CLASS_COLORS[t.distance_class] || '#6b7280';
                var classBg = CLASS_BG[t.distance_class] || 'rgba(107,114,128,0.08)';

                tr.innerHTML =
                    '<td class="rank">' + esc(t.rank) + '</td>' +
                    '<td class="callsign">' + esc(t.callsign || '\u2014') + '</td>' +
                    '<td class="mono hide-mobile">' + esc(t.latitude) + '</td>' +
                    '<td class="mono hide-mobile">' + esc(t.longitude) + '</td>' +
                    '<td class="mono">' + esc(t.frequency_mhz) + '</td>' +
                    '<td><span class="tower-badge" style="color:' + bandColor + ';background:' + bandBg + ';">' + esc(t.band) + '</span></td>' +
                    '<td class="mono">' + esc(t.distance_km) + '</td>' +
                    '<td>' + esc(t.bearing_deg) + '\u00b0 <span class="cardinal">' + esc(t.bearing_cardinal) + '</span></td>' +
                    '<td class="mono hide-mobile">' + esc(t.received_power_dbm) + '</td>' +
                    '<td><span class="tower-badge" style="color:' + classColor + ';background:' + classBg + ';">' + esc(t.distance_class) + '</span></td>';

                tr._tower = t;
                tr.addEventListener('mouseenter', function() {
                    towerMarkers.forEach(function(m) {
                        if (m.tower === t) m.marker.setIcon(makeTowerIcon(t.distance_class, true));
                    });
                });
                tr.addEventListener('mouseleave', function() {
                    towerMarkers.forEach(function(m) {
                        if (m.tower === t) m.marker.setIcon(makeTowerIcon(t.distance_class, false));
                    });
                });

                tr.style.cursor = 'pointer';
                tr.addEventListener('click', function() { selectTower(t); });

                tableBody.appendChild(tr);
            });
        }

        function selectTower(t) {
            selectedTower = t;
            selectedCard.style.display = '';
            selectedName.textContent = (t.callsign || 'Unknown') + ' \u2014 ' + t.frequency_mhz + ' MHz ' + t.band;
            selectedDetail.textContent = t.distance_km + ' km ' + t.bearing_cardinal + ' \u00b7 ' + (t.name || '') + (t.state ? ', ' + t.state : '');
            saveBtn.disabled = false;

            // Highlight selected row, clear others
            tableBody.querySelectorAll('tr').forEach(function(row) {
                row.classList.toggle('selected', row._tower === t);
            });

            towerMarkers.forEach(function(m) {
                var hl = (m.tower === t);
                m.marker.setIcon(makeTowerIcon(m.tower.distance_class, hl));
                if (hl) m.marker.openPopup();
            });
        }

        // Bind listeners only once
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
                    if (data.applied) {
                        statusEl.innerHTML = '<span class="text-success">Configuration saved and services restarted.</span>';
                        setTimeout(advance, 1500);
                    } else if (data.error) {
                        statusEl.innerHTML = '<span class="text-warning">Configuration saved but services failed to restart: ' + data.error + '</span>';
                        saveBtn.disabled = false;
                        saveBtn.textContent = 'Retry';
                        skipBtn.style.display = '';
                        skipBtn.textContent = 'Continue anyway \u2192';
                    } else {
                        // retina-node not installed yet — config saved, advance
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

    // Step 6: Complete
    enterHooks.complete = function() {
        window.removeEventListener('beforeunload', handleBeforeUnload);
        if (backBtn) backBtn.style.display = 'none';
        postJSON('/set-up/complete');
    };

    // ── Init ─────────────────────────────────────────────

    var startIndex = 0;

    if (demoMode) {
        // Demo: start from the top, seed tower search params so towers step works
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
