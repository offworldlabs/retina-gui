from flask import Blueprint, jsonify, request
import threading

bp = Blueprint('mender', __name__, url_prefix='/mender')


@bp.route("/check")
def check():
    """Check for available retina-node updates and install status."""
    from app import mender, device_state
    from mender import get_latest_stable_from_github

    _, current = mender.get_versions()

    in_progress, reason = device_state.is_any_update_in_progress()
    if in_progress:
        locked, lock_info = device_state.is_install_locked()
        mender_status = device_state._get_mender_update_status()
        stage = mender_status.get("state") if mender_status else "downloading"
        return jsonify({
            "installing": True,
            "stage": stage,
            "version": lock_info["version"] if lock_info else "system update",
            "started_at": lock_info["started_at"] if lock_info else None,
            "reason": reason,
        })

    latest, error = get_latest_stable_from_github()
    if error:
        return jsonify({"error": error})

    return jsonify({
        "installing": False,
        "latest_version": latest,
        "current_version": current,
        "update_available": current is None or latest != current,
    })


@bp.route("/install", methods=["POST"])
def install():
    """Install latest stable retina-node artifact from Mender."""
    from app import mender, device_state, app
    from mender import get_latest_stable_from_github

    success, error = device_state.ensure_cloud_services_enabled(mender.get_jwt)
    if not success:
        return jsonify({"success": False, "error": error})

    _, retina_node_version = mender.get_versions()
    if retina_node_version:
        return jsonify({"success": False, "error": "Already installed"})

    can_install, reason = device_state.can_start_install()
    if not can_install:
        return jsonify({"success": False, "error": reason}), 409

    version_tag, error = get_latest_stable_from_github()
    if error:
        return jsonify({"success": False, "error": f"Failed to get version: {error}"})

    release_name = f"retina-node-{version_tag}"
    if not device_state.acquire_install_lock(release_name):
        return jsonify({"success": False, "error": "Install already in progress"}), 409

    artifacts, error = mender.list_artifacts(release_name=release_name)
    if error:
        device_state.release_install_lock()
        return jsonify({"success": False, "error": error})

    if not artifacts:
        device_state.release_install_lock()
        return jsonify({"success": False, "error": f"No artifact found for {release_name}"})

    artifact = artifacts[0]
    url, error = mender.get_download_url(artifact["id"])
    if error:
        device_state.release_install_lock()
        return jsonify({"success": False, "error": error})

    def _run_install(download_url):
        try:
            success, error = mender.install_from_url(download_url)
            if not success:
                app.logger.error(f"Background install failed: {error}")
        except Exception as e:
            app.logger.error(f"Background install crashed: {e}")
        finally:
            device_state.release_install_lock()

    threading.Thread(target=_run_install, args=(url,), daemon=True).start()
    return jsonify({"success": True, "version": release_name})


@bp.route("/cloud-services", methods=["GET"])
def cloud_services_status():
    """Check if Mender cloud services are enabled."""
    from app import device_state
    return jsonify(device_state.get_cloud_services_status())


@bp.route("/cloud-services", methods=["POST"])
def cloud_services_toggle():
    """Enable or disable Mender cloud services."""
    from app import device_state

    data = request.get_json()
    if not data or "enabled" not in data:
        return jsonify({"success": False, "error": "Missing 'enabled' field"}), 400

    success, error = device_state.set_cloud_services(data["enabled"])
    if not success:
        return jsonify({"success": False, "error": error}), 409

    return jsonify({"success": True})


@bp.route("/check-os")
def check_os():
    """Check for owl-os updates and install status."""
    from app import mender, device_state
    from mender import get_latest_owl_os_from_github, parse_os_version

    owl_os_current, _ = mender.get_versions()

    in_progress, reason = device_state.is_any_update_in_progress()
    if in_progress:
        locked, lock_info = device_state.is_install_locked()
        mender_status = device_state._get_mender_update_status()
        stage = mender_status.get("state") if mender_status else "waiting"
        return jsonify({
            "installing": True,
            "stage": stage,
            "version": lock_info["version"] if lock_info else "system update",
            "started_at": lock_info["started_at"] if lock_info else None,
            "reason": reason,
        })

    latest, error = get_latest_owl_os_from_github()
    if error:
        return jsonify({"error": error})

    update_available = False
    if latest:
        latest_tuple = parse_os_version(latest)
        current_tuple = parse_os_version(owl_os_current) if owl_os_current else None
        if not current_tuple or (latest_tuple and latest_tuple > current_tuple):
            update_available = True

    return jsonify({
        "installing": False,
        "latest_version": latest,
        "current_version": owl_os_current,
        "update_available": update_available,
    })


@bp.route("/install-os", methods=["POST"])
def install_os():
    """Trigger managed OS update via Mender daemon."""
    from app import mender, device_state
    from mender import get_latest_owl_os_from_github

    can_install, reason = device_state.can_start_install()
    if not can_install:
        return jsonify({"success": False, "error": reason}), 409

    version_tag, error = get_latest_owl_os_from_github()
    if error:
        return jsonify({"success": False, "error": f"Failed to get version: {error}"})

    version_suffix = version_tag.replace("os-", "")
    release_name = f"owl-os-pi5-{version_suffix}"

    if not device_state.acquire_install_lock(release_name):
        return jsonify({"success": False, "error": "Install already in progress"}), 409

    success, error = device_state.ensure_cloud_services_enabled(mender.get_jwt)
    if not success:
        device_state.release_install_lock()
        return jsonify({"success": False, "error": error})

    device_state.save_setup_wizard_step("system")

    return jsonify({"success": True, "version": release_name, "state": "waiting"})
