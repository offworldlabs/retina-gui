"""The Passive Radar page (Live and Max-Hold views), and a proxy for the blah2_api data it draws.

retina-gui holds no radar data. See docs/features/radar.md.
"""

import logging
import threading

import requests
from flask import Blueprint, Response, abort, jsonify, redirect, render_template, request

from plot_axes import axis_bounds
from routes.sdr import get_current_mode

log = logging.getLogger(__name__)

bp = Blueprint('radar', __name__, url_prefix='/radar')

JSON = 'application/json'

# The only blah2_api paths the page can reach. The type is set here, not relayed:
# older blah2_api labels these replies text/html, which must not be served from
# this origin. See docs/features/radar.md#the-data-proxy
SOURCES = {
    'timestamp': ('/api/timestamp', 'text/plain'),
    'detection': ('/api/detection', JSON),
    'adsb': ('/api/adsb2dd', JSON),
    'config': ('/api/config', JSON),
    'map': ('/api/map', JSON),
    'maxhold': ('/stash/map', JSON),
}

# blah2_api answers /api/adsb2dd within its own 5 s lookup timeout (HTTP_TIMEOUT
# in blah2-arm's api/server.js, another repo), with {} when the lookup fails, so
# the proxy waits past that rather than cutting it off. Every other source uses
# the client's default. See docs/features/radar.md#the-data-proxy
TIMEOUTS_S = {'adsb': 7}

# The page's views, by the ?view= value that opens each. The first is the default.
# See docs/features/radar.md#the-view-toggle
VIEWS = ('live', 'maxhold')

# The sources in an outage, each logged once when it starts and once when it
# ends: an open page asks several times a second, and one source can fail while
# the rest answer.
_down = set()
_down_lock = threading.Lock()


@bp.route('')
def index():
    """The radar page, or Home when blah2 is not running in this SDR mode."""
    if get_current_mode() != 'radar':
        return redirect('/')
    view = request.args.get('view')
    return render_template('radar.html', view=view if view in VIEWS else VIEWS[0], axes=axis_bounds())


@bp.route('/maxhold')
def maxhold():
    """Where Max-Hold was its own page."""
    return redirect('/radar?view=maxhold')


@bp.route('/data/<source>')
def data(source):
    """Relay one blah2_api reply, status included."""
    from services import blah2_client

    if source not in SOURCES:
        abort(404)
    path, content_type = SOURCES[source]
    params = None
    if source == 'adsb' and 'detection_ts' in request.args:
        params = {'detection_ts': request.args['detection_ts']}

    try:
        upstream = blah2_client.get_raw(path, params=params, timeout=TIMEOUTS_S.get(source))
    except requests.RequestException as e:
        with _down_lock:
            started = source not in _down
            _down.add(source)
        if started:
            log.warning('blah2_api %s unreachable from the radar page: %s', path, e.__class__.__name__)
        return jsonify({'error': f'blah2_api {path} unreachable: {e.__class__.__name__}'}), 502

    with _down_lock:
        ended = source in _down
        _down.discard(source)
    if ended:
        log.info('blah2_api %s reachable from the radar page again', path)
    return Response(upstream.content, status=upstream.status_code, content_type=content_type,
                    headers={'Cache-Control': 'no-store'})
