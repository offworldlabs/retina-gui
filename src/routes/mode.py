import os
import subprocess
from flask import Blueprint, jsonify, request

bp = Blueprint('mode', __name__)

_mode_cache = 'radar'  # in-memory fallback when DATA_DIR is not writable (dev)


def get_current_mode():
    """Read persisted mode. Returns 'radar' or 'spectrum'."""
    from app import DATA_DIR
    try:
        with open(os.path.join(DATA_DIR, 'mode.txt')) as f:
            mode = f.read().strip()
            return mode if mode in ('radar', 'spectrum') else 'radar'
    except (FileNotFoundError, OSError):
        return _mode_cache


def _write_mode(mode):
    global _mode_cache
    _mode_cache = mode
    from app import DATA_DIR
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, 'mode.txt'), 'w') as f:
            f.write(mode)
    except OSError:
        pass  # dev: no /data — in-memory cache is the fallback


@bp.route('/api/mode', methods=['GET'])
def get_mode():
    return jsonify({'mode': get_current_mode()})


@bp.route('/api/mode', methods=['POST'])
def set_mode():
    from app import RETINA_NODE_PATH, config_mgr

    data = request.get_json(silent=True) or {}
    mode = data.get('mode')
    if mode not in ('radar', 'spectrum'):
        return jsonify({'success': False, 'error': 'Invalid mode'}), 400

    node_installed = config_mgr.is_retina_node_installed()

    try:
        if not node_installed:
            # Dev / pre-deployment: persist mode but skip docker commands
            _write_mode(mode)
            return jsonify({'success': True, 'mode': mode})

        if mode == 'spectrum':
            result = subprocess.run(
                ['docker', 'compose', '-p', 'retina-node', 'stop',
                 'blah2', 'blah2_api', 'blah2_web', 'blah2_host'],
                cwd=RETINA_NODE_PATH,
                capture_output=True, text=True, timeout=60
            )
            if result.returncode != 0:
                return jsonify({'success': False,
                                'error': f'Failed to stop blah2: {result.stderr or result.stdout}'}), 500

            result = subprocess.run(
                ['docker', 'compose', '-p', 'retina-node', '--profile', 'spectrum', 'up', '-d'],
                cwd=RETINA_NODE_PATH,
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                return jsonify({'success': False,
                                'error': f'Failed to start retina-spectrum: {result.stderr or result.stdout}'}), 500

        else:  # radar
            result = subprocess.run(
                ['docker', 'compose', '-p', 'retina-node', 'stop', 'retina-spectrum'],
                cwd=RETINA_NODE_PATH,
                capture_output=True, text=True, timeout=60
            )
            if result.returncode != 0:
                return jsonify({'success': False,
                                'error': f'Failed to stop retina-spectrum: {result.stderr or result.stdout}'}), 500

            result = subprocess.run(
                ['docker', 'compose', '-p', 'retina-node', 'start',
                 'blah2', 'blah2_api', 'blah2_web', 'blah2_host'],
                cwd=RETINA_NODE_PATH,
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                return jsonify({'success': False,
                                'error': f'Failed to start blah2: {result.stderr or result.stdout}'}), 500

        _write_mode(mode)
        return jsonify({'success': True, 'mode': mode})

    except subprocess.TimeoutExpired:
        return jsonify({'success': False, 'error': 'Command timed out'}), 500
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
