function initSetupWizard(resumeStep, highestStepName, devMode, isRerun) {
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
        track.appendChild(dot);
    });

    function updateProgress(index) {
        highestStep = Math.max(highestStep, index);
        var label = document.getElementById('progressLabel');
        var fill = document.getElementById('progressFill');
        var name = stepNames[steps[index].name] || '';
        var total = steps.length;

        label.textContent = name;

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
        // Clear any active polling timer when leaving a step
        if (pollTimer) {
            clearInterval(pollTimer);
            pollTimer = null;
        }

        var card = document.querySelector('.setup-card');
        // Wide card only for towers step
        var towersIndex = -1;
        for (var i = 0; i < steps.length; i++) {
            if (steps[i].name === 'towers') { towersIndex = i; break; }
        }
        if (index === towersIndex) {
            card.classList.add('wide');
        } else {
            card.classList.remove('wide');
        }

        steps.forEach(function(s, i) {
            s.el.style.display = (i === index) ? '' : 'none';
        });
        currentIndex = index;
        updateProgress(index);

        postJSON('/set-up/save-step', {step: steps[index].name});

        var enterFn = enterHooks[steps[index].name];
        if (enterFn) enterFn();
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

    // ── Enter hooks ──────────────────────────────────────

    var enterHooks = {};
    var hookInitialized = {};

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
                    nextBtn.textContent = 'Skip \u2192';
                }
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
            } else {
                status.textContent = 'Updating...';
            }
            cardStatus.innerHTML = '<span class="spinner-border spinner-border-sm text-primary"></span>';
        }

        function startSystemPoll() {
            showStage('waiting');
            if (pollTimer) clearInterval(pollTimer);
            pollTimer = setInterval(function() {
                fetch('/mender/check-os')
                    .then(function(r) { return r.json(); })
                    .then(function(data) {
                        if (!data.installing) {
                            clearInterval(pollTimer);
                            pollTimer = null;
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

        // On re-run, skip updates — package updates are managed remotely after onboarding
        if (isRerun) {
            fetch('/mender/check')
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.current_version) {
                        document.getElementById('radarLatestVersion').textContent = data.current_version;
                    }
                    status.innerHTML = 'Package updates are managed remotely &#10003;';
                    packageStatus.innerHTML = '<span class="text-success">&#10003;</span>';
                    nextBtn.style.display = '';
                    nextBtn.textContent = 'Continue \u2192';
                });
            nextBtn.addEventListener('click', advance);
            return;
        }

        function updateInstallGate() {
            if (installBtn.style.display !== 'none') {
                installBtn.disabled = !regionCheck.checked;
            }
        }
        regionCheck.addEventListener('change', updateInstallGate);

        fetch('/mender/check')
            .then(function(r) { return r.json(); })
            .then(function(data) {
                if (data.installing) {
                    status.textContent = data.reason || 'Installation in progress...';
                    installStatus.innerHTML = '<span class="text-warning">Do not power off the device.</span>';
                    packageStatus.innerHTML = '<span class="spinner-border spinner-border-sm text-primary"></span>';
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
                    nextBtn.style.display = '';
                } else {
                    status.textContent = '';
                    document.getElementById('radarLatestVersion').textContent = data.latest_version;
                    installBtn.style.display = '';
                    updateInstallGate();
                }
            });

        installBtn.addEventListener('click', function() {
            installBtn.style.display = 'none';
            packageStatus.innerHTML = '<span class="spinner-border spinner-border-sm text-primary"></span>';
            status.textContent = 'Installing...';
            installStatus.innerHTML = '<span class="text-warning">Do not power off the device.</span>';

            postJSON('/mender/install')
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
                                updateInstallGate();
                            }
                        } else {
                            status.textContent = (data.stage || 'Installing') + '...';
                        }
                    });
            }, 5000);
        }
    };

    // Step 4: Location input
    enterHooks.location = function() {
        if (hookInitialized.location) return;
        hookInitialized.location = true;
        var rxLat = document.getElementById('rxLat');
        var rxLon = document.getElementById('rxLon');
        var rxAlt = document.getElementById('rxAlt');
        var useMyLocBtn = document.getElementById('useMyLocationBtn');
        var findBtn = document.getElementById('findTowersBtn');
        var geoError = document.getElementById('locationGeoError');
        var skipBtn = document.getElementById('locationSkipBtn');
        var altManual = false;
        var elevTimer = null;

        // Frequency inputs
        var freqToggle = document.getElementById('freqToggle');
        var freqSection = document.getElementById('freqSection');
        var freqInputs = document.getElementById('freqInputs');
        var addFreqBtn = document.getElementById('addFreqBtn');
        var freqVisible = false;

        freqToggle.addEventListener('click', function(e) {
            e.preventDefault();
            freqVisible = !freqVisible;
            freqSection.style.display = freqVisible ? '' : 'none';
            freqToggle.textContent = freqVisible ? 'Hide Measured Frequencies' : '+ Add Measured Frequencies';
        });

        addFreqBtn.addEventListener('click', function() {
            var count = freqInputs.querySelectorAll('input').length;
            if (count >= 10) return;
            var div = document.createElement('div');
            div.className = 'input-group input-group-sm mb-1';
            div.innerHTML = '<input type="number" class="form-control" step="any" min="0" placeholder="Freq ' + (count + 1) + ' (MHz)">' +
                '<button type="button" class="btn btn-outline-secondary" title="Remove">&times;</button>';
            div.querySelector('button').addEventListener('click', function() { div.remove(); });
            freqInputs.appendChild(div);
        });

        // Enable Find Towers when lat/lon filled
        function updateFindBtn() {
            var lat = parseFloat(rxLat.value);
            var lon = parseFloat(rxLon.value);
            findBtn.disabled = isNaN(lat) || isNaN(lon);
        }
        rxLat.addEventListener('input', updateFindBtn);
        rxLon.addEventListener('input', updateFindBtn);

        // Auto-lookup elevation (debounced)
        function lookupElevation() {
            if (altManual) return;
            var lat = parseFloat(rxLat.value);
            var lon = parseFloat(rxLon.value);
            if (isNaN(lat) || isNaN(lon)) return;
            fetch('/towers/elevation?lat=' + lat + '&lon=' + lon)
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.elevation_m != null && !altManual) {
                        rxAlt.value = Math.round(data.elevation_m);
                    }
                })
                .catch(function() {});
        }
        function debouncedElevation() {
            clearTimeout(elevTimer);
            elevTimer = setTimeout(lookupElevation, 800);
        }
        rxLat.addEventListener('input', debouncedElevation);
        rxLon.addEventListener('input', debouncedElevation);
        rxAlt.addEventListener('input', function() { altManual = rxAlt.value !== ''; });

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
                        altManual = true;
                    }
                    updateFindBtn();
                    lookupElevation();
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
            var freqs = [];
            freqInputs.querySelectorAll('input').forEach(function(inp) {
                var v = parseFloat(inp.value);
                if (!isNaN(v) && v > 0) freqs.push(v);
            });
            window._towerSearchParams = {
                lat: parseFloat(rxLat.value),
                lon: parseFloat(rxLon.value),
                alt: parseFloat(rxAlt.value) || 0,
                frequencies: freqs
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

        var CLASS_COLORS = { Ideal: cv('--color-suit-ideal'), Good: cv('--color-suit-good'), Far: cv('--color-suit-far'), 'Too Close': cv('--color-suit-close') };
        var CLASS_BG = { Ideal: cv('--color-suit-ideal-bg'), Good: cv('--color-suit-good-bg'), Far: cv('--color-suit-far-bg'), 'Too Close': cv('--color-suit-close-bg') };
        var BAND_COLORS = { VHF: cv('--color-band-vhf'), UHF: cv('--color-band-uhf'), FM: cv('--color-band-fm') };
        var BAND_BG = { VHF: cv('--color-band-vhf-bg'), UHF: cv('--color-band-uhf-bg'), FM: cv('--color-band-fm-bg') };

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

        var url = '/towers/search?lat=' + params.lat + '&lon=' + params.lon + '&altitude=' + params.alt + '&limit=20';
        if (params.frequencies.length > 0) url += '&frequencies=' + params.frequencies.join(',');

        fetch(url)
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
                    '<td class="hide-mobile"><span class="tower-badge" style="color:' + bandColor + ';background:' + bandBg + ';">' + esc(t.band) + '</span></td>' +
                    '<td class="mono">' + esc(t.distance_km) + '</td>' +
                    '<td class="hide-mobile">' + esc(t.bearing_deg) + '\u00b0 <span class="cardinal">' + esc(t.bearing_cardinal) + '</span></td>' +
                    '<td class="mono hide-mobile">' + esc(t.received_power_dbm) + '</td>' +
                    '<td><span class="tower-badge" style="color:' + classColor + ';background:' + classBg + ';">' + esc(t.distance_class) + '</span></td>' +
                    '<td><button type="button" class="btn btn-outline-primary btn-sm py-0 px-2">Select</button></td>';

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

                tr.querySelector('button').addEventListener('click', function() {
                    selectTower(t);
                });

                tableBody.appendChild(tr);
            });
        }

        function selectTower(t) {
            selectedTower = t;
            selectedCard.style.display = '';
            selectedName.textContent = (t.callsign || 'Unknown') + ' \u2014 ' + t.frequency_mhz + ' MHz ' + t.band;
            selectedDetail.textContent = t.distance_km + ' km ' + t.bearing_cardinal + ' \u00b7 ' + (t.name || '') + (t.state ? ', ' + t.state : '');
            saveBtn.disabled = false;

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
        postJSON('/set-up/complete');
    };

    // ── Init ─────────────────────────────────────────────

    var startIndex = 0;

    if (devMode) {
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
