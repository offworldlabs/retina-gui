from flask import Blueprint, jsonify, request

bp = Blueprint('network', __name__, url_prefix='/network')


@bp.route('/status', methods=['GET'])
def status():
    from app import network_mgr
    return jsonify(network_mgr.get_network_status(client_ip=request.remote_addr))


@bp.route('/wifi/scan', methods=['GET'])
def wifi_scan():
    from app import network_mgr
    return jsonify({"networks": network_mgr.scan_wifi()})


@bp.route('/wifi/connect', methods=['POST'])
def wifi_connect():
    from app import network_mgr

    data = request.get_json(silent=True) or {}
    ssid = (data.get('ssid') or '').strip()
    if not ssid:
        return jsonify({"success": False, "error": "Missing 'ssid' field"}), 400

    network_mgr.connect_wifi(ssid, data.get('password'), bool(data.get('hidden')))
    return jsonify({"success": True})


@bp.route('/wifi/connect/status', methods=['GET'])
def wifi_connect_status():
    from app import network_mgr
    return jsonify(network_mgr.get_connect_status())
