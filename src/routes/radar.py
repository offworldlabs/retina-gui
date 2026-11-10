"""The Passive Radar page (Live and Max-Hold views), and a proxy for the blah2_api data it draws.

retina-gui holds no radar data. See docs/features/radar.md.
"""

import logging

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

# The page's views, by the ?view= value that opens each. The first is the default.
# See docs/features/radar.md#the-view-toggle
VIEWS = ('live', 'maxhold')

# Logged once per outage: an open page asks several times a second.
_unreachable = False


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
    global _unreachable
    from services import blah2_client

    if source not in SOURCES:
        abort(404)
    path, content_type = SOURCES[source]
    params = None
    if source == 'adsb' and 'detection_ts' in request.args:
        params = {'detection_ts': request.args['detection_ts']}

    try:
        upstream = blah2_client.get_raw(path, params=params)
    except requests.RequestException as e:
        if not _unreachable:
            log.warning('blah2_api unreachable from the radar page: %s', e.__class__.__name__)
        _unreachable = True
        return jsonify({'error': f'blah2_api unreachable: {e.__class__.__name__}'}), 502

    if _unreachable:
        log.info('blah2_api reachable from the radar page again')
    _unreachable = False
    return Response(upstream.content, status=upstream.status_code, content_type=content_type,
                    headers={'Cache-Control': 'no-store'})
