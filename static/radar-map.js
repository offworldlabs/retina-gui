// The Passive Radar and Max-Hold displays: blah2's delay-Doppler map, with its
// detections and ADS-B truth drawn over it. A port of blah2's plot_map.js, kept
// close to it on purpose. See docs/features/radar.md#what-was-ported
(function () {
    'use strict';

    var urlMap = window.RADAR_MAP_URL;
    var urlTimestamp = '/radar/data/timestamp';
    var urlDetection = '/radar/data/detection';
    var urlAdsbLink = '/radar/data/adsb';
    var urlConfig = '/radar/data/config';

    var timestamp = -1;
    var nRows = 3;

    // A failed request leaves the plot as it was and the next tick tries again.
    // Only the request is caught: an error while drawing still reaches the console.
    function get(url, read, done) {
        fetch(url).then(function (r) {
            if (!r.ok) throw new Error(url + ': ' + r.status);
            return read(r);
        }).then(done, function () { /* the next tick retries */ });
    }
    function getText(url, done) { get(url, function (r) { return r.text(); }, done); }
    function getJson(url, done) { get(url, function (r) { return r.json(); }, done); }

    // get truth flag
    var isTruth = false;
    getJson(urlConfig, function (data_config) {
        if (data_config.truth.adsb.enabled === true) {
            isTruth = true;
        }
    });

    // setup plotly
    var layout = {
        autosize: true,
        margin: {
            l: 50,
            r: 50,
            b: 50,
            t: 10,
            pad: 0
        },
        hoverlabel: {
            namelength: 0
        },
        plot_bgcolor: "#ffffff",
        paper_bgcolor: "#ffffff",
        font: {
            family: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
            color: "#1a1a18"
        },
        annotations: [],
        displayModeBar: false,
        xaxis: {
            title: {
                text: 'Bistatic Range (km)',
                font: {
                    size: 16
                }
            },
            ticks: '',
            side: 'bottom'
        },
        yaxis: {
            title: {
                text: 'Bistatic Doppler (Hz)',
                font: {
                    size: 16
                }
            },
            ticks: '',
            ticksuffix: ' ',
            autosize: false,
            categoryorder: "total descending"
        },
        showlegend: false
    };
    var config = {
        responsive: true,
        displayModeBar: false
    };

    // setup plotly data
    var data = [
        {
            z: [[0, 0, 0], [0, 0, 0], [0, 0, 0]],
            colorscale: 'Jet',
            colorbar: {
                title: {
                    text: 'SNR (dB)',
                    side: 'right'
                }
            },
            type: 'heatmap'
        }
    ];
    var detection = [];
    var adsb = {};

    Plotly.newPlot('data', data, layout, config);

    // Reset to full range. Plotly's own double-click reset is hidden without the
    // modebar, and it tracks click timing off raw mousedown/mouseup on the
    // drag-capture rect rather than the native dblclick event, so proxy that.
    var resetViewBtn = document.createElement('button');
    resetViewBtn.className = 'reset-view-btn';
    resetViewBtn.type = 'button';
    resetViewBtn.textContent = 'Reset View';
    resetViewBtn.addEventListener('click', function () {
        var dragLayer = document.querySelector('#data .nsewdrag');
        if (!dragLayer) return;
        var rect = dragLayer.getBoundingClientRect();
        var opts = {
            bubbles: true,
            cancelable: true,
            view: window,
            clientX: rect.left + rect.width / 2,
            clientY: rect.top + rect.height / 2,
            button: 0
        };
        dragLayer.dispatchEvent(new MouseEvent('mousedown', opts));
        dragLayer.dispatchEvent(new MouseEvent('mouseup', opts));
        setTimeout(function () {
            dragLayer.dispatchEvent(new MouseEvent('mousedown', opts));
            dragLayer.dispatchEvent(new MouseEvent('mouseup', opts));
        }, 80);
    });
    document.querySelector('.plot-wrapper').appendChild(resetViewBtn);

    function drawMap(data) {
        // case draw new plot
        if (data.nRows != nRows) {
            nRows = data.nRows;

            // lock range before other trace
            var layout_update = {
                'xaxis.range': [data.delay[0], data.delay.slice(-1)[0]],
                'yaxis.range': [data.doppler[0], data.doppler.slice(-1)[0]]
            };
            Plotly.relayout('data', layout_update);

            var trace1 = {
                z: data.data,
                x: data.delay,
                y: data.doppler,
                colorscale: 'Viridis',
                colorbar: {
                    title: {
                        text: 'SNR (dB)',
                        side: 'right'
                    }
                },
                zauto: false,
                zmin: 0,
                zmax: Math.max(13, data.maxPower),
                type: 'heatmap'
            };
            var trace2 = {
                x: detection.delay,
                y: detection.doppler,
                mode: 'markers',
                type: 'scatter',
                marker: {
                    size: 16,
                    opacity: 0.9,
                    color: '#FFFFFF',
                    line: {
                        color: '#000000',
                        width: 1.5
                    }
                }
            };
            var trace3 = {
                x: adsb.delay,
                y: adsb.doppler,
                mode: 'markers',
                type: 'scatter',
                marker: {
                    size: 16,
                    opacity: 0.9,
                    color: '#D55E00',
                    line: {
                        color: '#000000',
                        width: 1.5
                    }
                }
            };

            var data_trace = [trace1, trace2, trace3];
            Plotly.newPlot('data', data_trace, layout, config);
        }
        // case update plot
        else {
            var trace_update = {
                x: [data.delay, detection.delay, adsb.delay],
                y: [data.doppler, detection.doppler, adsb.doppler],
                z: [data.data, [], []],
                zmax: [Math.max(13, data.maxPower), [], []],
                text: [[], [], adsb.flight]
            };
            Plotly.update('data', trace_update);
        }
    }

    // callback function
    window.setInterval(function () {

        // check if timestamp is updated
        getText(urlTimestamp, function (data) {
            if (timestamp != data) {
                timestamp = data;

                // get detection data (no detection lag)
                getJson(urlDetection, function (data_detection) {
                    detection = data_detection;
                });

                // get ADS-B data if enabled in config, pass detection timestamp for synchronization
                if (isTruth) {
                    getJson(urlAdsbLink + '?detection_ts=' + encodeURIComponent(timestamp), function (data_adsb) {
                        adsb['delay'] = [];
                        adsb['doppler'] = [];
                        adsb['flight'] = [];
                        for (const aircraft in data_adsb) {
                            if ('doppler' in data_adsb[aircraft]) {
                                adsb['delay'].push(data_adsb[aircraft]['delay']);
                                adsb['doppler'].push(data_adsb[aircraft]['doppler']);
                                adsb['flight'].push(data_adsb[aircraft]['flight']);
                            }
                        }
                    });
                }

                // get new map data
                getJson(urlMap, drawMap);
            }
        });
    }, 1000);
})();
