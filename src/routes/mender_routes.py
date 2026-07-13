from flask import Blueprint, jsonify, request
import subprocess
import threading
import time

bp = Blueprint('mender', __name__, url_prefix='/mender')


@bp.route("/check")
def check():
    """Check for available retina-node updates and install status."""
    from app import mender, device_state, DEV_MODE
    from mender import get_all_stable_versions_from_github, DEV_VERSIONS

    if DEV_MODE:
        in_progress, reason = device_state.is_any_update_in_progress()
        if in_progress:
            locked, lock_info = device_state.is_install_locked()
            return jsonify({
                "installing": True,
                "stage": "pulling",
                "version": lock_info["version"] if lock_info else "retina-node",
                "reason": reason,
            })
        current = mender.dev_get_node_version()
        if current and device_state.has_completed_setup_wizard():
            # Once any package is installed and the wizard has completed at
            # least once, updates are handled by the server — no need to
            # show what's available.
            return jsonify({"installing": False, "current_version": current})
        return jsonify({
            "installing": False,
            "latest_version": DEV_VERSIONS[0],
            "current_version": current,
        })

    _, current = mender.get_versions()

    in_progress, reason = device_state.is_any_update_in_progress()
    if in_progress:
        locked, lock_info = device_state.is_install_locked()
        mender_status = device_state._get_mender_update_status()
        if mender_status:
            stage = mender_status.get("state")
        elif lock_info:
            stage = lock_info.get("stage", "downloading")
        else:
            stage = "downloading"
        return jsonify({
            "installing": True,
            "stage": stage,
            "version": lock_info["version"] if lock_info else "system update",
            "started_at": lock_info["started_at"] if lock_info else None,
            "reason": reason,
        })

    if current and device_state.has_completed_setup_wizard():
        # Already have a package installed and the wizard has completed at
        # least once — updates from here on are handled by the server, so
        # there's nothing to check on GitHub for.
        return jsonify({"installing": False, "current_version": current})

    # Either nothing is installed yet, or this is the first time the wizard
    # is running on a node that shipped with retina-node pre-installed — in
    # both cases the user should be able to see/install the latest version.
    all_versions, error = get_all_stable_versions_from_github()
    if error:
        return jsonify({"error": error})

    latest_meta = all_versions[0] if all_versions else None
    latest = latest_meta["version"] if latest_meta else None
    latest_size_bytes = latest_meta["size_bytes"] if latest_meta else None

    return jsonify({
        "installing": False,
        "latest_version": latest,
        "latest_size_bytes": latest_size_bytes,
        "current_version": current,
    })


@bp.route("/install", methods=["POST"])
def install():
    """Install a retina-node artifact from Mender.

    Accepts an optional 'version' in the JSON body (e.g. {"version": "v0.3.11"}).
    Defaults to the latest stable release if omitted.
    """
    from app import mender, device_state, app, DEV_MODE, calibrator
    from mender import get_latest_stable_from_github, DEV_VERSIONS
    import time

    body = request.get_json() or {}
    requested_version = body.get("version")

    # calibrator.is_running() is checked directly (not just
    # device_state.can_start_install()'s lock-file check) because MODE_ADSB
    # has no time limit: a genuine multi-hour run would outlive the lock
    # file's own 20-minute staleness window, but is_running() is always
    # correct regardless of how long the run has been going.
    if calibrator.is_running():
        return jsonify({"success": False,
                        "error": "Auto-calibration is running — cancel it before installing an update"}), 409

    if DEV_MODE:
        version_tag = requested_version or DEV_VERSIONS[0]
        can_install, reason = device_state.can_start_install()
        if not can_install:
            return jsonify({"success": False, "error": reason}), 409
        release_name = f"retina-node-{version_tag}"
        if not device_state.acquire_install_lock(release_name):
            return jsonify({"success": False, "error": "Install already in progress"}), 409

        def _dev_install():
            time.sleep(8)
            mender.dev_set_node_version(version_tag)
            device_state.release_install_lock()

        threading.Thread(target=_dev_install, daemon=True).start()
        return jsonify({"success": True, "version": release_name})

    success, error = device_state.ensure_cloud_services_enabled(mender.get_jwt)
    if not success:
        return jsonify({"success": False, "error": error})

    from mender import get_retina_node_version_from_docker
    already_installed = get_retina_node_version_from_docker() is not None

    can_install, reason = device_state.can_start_install()
    if not can_install:
        return jsonify({"success": False, "error": reason}), 409

    if requested_version:
        version_tag = requested_version
    else:
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
        from mender import get_retina_node_version_from_docker
        from routes.mode import _write_mode, enforce_radar_mode
        from app import RETINA_NODE_PATH

        def _recover():
            # The new artifact failed to apply. If something was already
            # running, bring the previous (still-on-disk) compose stack back
            # up rather than leaving the device with no radar containers at
            # all — the failed install never committed, so RETINA_NODE_PATH
            # still points at the old manifests.
            #
            # Mode stays 'spectrum' (watchdog silenced) until the containers
            # are actually back up — flipping to 'radar' first would let the
            # cron watchdog's own docker compose calls race these ones.
            if already_installed:
                enforce_radar_mode(RETINA_NODE_PATH)
            _write_mode('radar')

        try:
            # Silence the watchdog before touching containers so it cannot see
            # blah2 go down and trigger a spurious radar stack restart mid-install.
            _write_mode('spectrum')
            try:
                subprocess.run(
                    ["mender-update", "rollback"],
                    capture_output=True, timeout=30
                )
            except Exception:
                pass
            if already_installed:
                try:
                    subprocess.run(
                        ["docker", "compose", "-p", "retina-node", "down"],
                        capture_output=True, timeout=60
                    )
                except Exception as e:
                    app.logger.warning(f"Pre-install docker down failed (continuing): {e}")
            success, error = mender.install_from_url(download_url)
            if not success:
                app.logger.error(f"Background install failed: {error}")
                _recover()
            else:
                device_state.update_install_stage("starting")
                deadline = time.time() + 120
                while time.time() < deadline:
                    if get_retina_node_version_from_docker():
                        _write_mode('radar')  # re-enable watchdog now that containers are confirmed up
                        break
                    time.sleep(3)
                else:
                    app.logger.warning("Containers did not come up within 2 minutes after install")
                    _recover()
        except Exception as e:
            app.logger.error(f"Background install crashed: {e}")
            _recover()
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
    from app import mender, device_state, DEV_MODE
    from mender import get_latest_owl_os_from_github, parse_os_version

    if DEV_MODE:
        return jsonify({
            "installing": False,
            "current_version": "2.4.1-dev",
            "update_available": False,
        })

    owl_os_current, _ = mender.get_versions()

    in_progress, reason = device_state.is_any_update_in_progress()
    if in_progress:
        locked, lock_info = device_state.is_install_locked()
        mender_status = device_state._get_mender_update_status()
        if mender_status:
            stage = mender_status.get("state")
        elif lock_info:
            stage = lock_info.get("stage", "downloading")
        else:
            stage = "downloading"
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
