from flask import Blueprint, jsonify, request
import subprocess
import threading
import time

bp = Blueprint('mender', __name__, url_prefix='/mender')


@bp.route("/check")
def check():
    """Check for available retina-node updates and install status."""
    from app import mender, device_state, DEV_MODE
    from mender import get_all_stable_versions_from_github, parse_version, DEV_VERSIONS

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
        if current and current in DEV_VERSIONS:
            available_updates = DEV_VERSIONS[:DEV_VERSIONS.index(current)]
        elif current:
            available_updates = []
        else:
            available_updates = list(DEV_VERSIONS)
        return jsonify({
            "installing": False,
            "latest_version": DEV_VERSIONS[0],
            "current_version": current,
            "available_updates": available_updates,
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

    all_versions, error = get_all_stable_versions_from_github()
    if error:
        return jsonify({"error": error})

    latest_meta = all_versions[0] if all_versions else None
    latest = latest_meta["version"] if latest_meta else None
    latest_size_bytes = latest_meta["size_bytes"] if latest_meta else None

    if current:
        current_tuple = parse_version(f"retina-node-{current}")
        if current_tuple is None:
            # Dev/pre-release build — no stable release can be meaningfully compared.
            available_updates = []
        else:
            available_updates = [
                {"version": v["version"], "size_bytes": v["size_bytes"]}
                for v in all_versions
                if (vt := parse_version(f"retina-node-{v['version']}")) and vt > current_tuple
            ]
    else:
        available_updates = [{"version": v["version"], "size_bytes": v["size_bytes"]} for v in all_versions]

    return jsonify({
        "installing": False,
        "latest_version": latest,
        "latest_size_bytes": latest_size_bytes,
        "current_version": current,
        "available_updates": available_updates,
    })


@bp.route("/install", methods=["POST"])
def install():
    """Install a retina-node artifact from Mender.

    Accepts an optional 'version' in the JSON body (e.g. {"version": "v0.3.11"}).
    Defaults to the latest stable release if omitted.
    """
    from app import mender, device_state, app, DEV_MODE
    from mender import get_latest_stable_from_github, DEV_VERSIONS
    import time

    body = request.get_json() or {}
    requested_version = body.get("version")

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
        from routes.mode import _write_mode
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
                _write_mode('radar')
            else:
                device_state.update_install_stage("starting")
                deadline = time.time() + 120
                while time.time() < deadline:
                    if get_retina_node_version_from_docker():
                        break
                    time.sleep(3)
                else:
                    app.logger.warning("Containers did not come up within 2 minutes after install")
                    _write_mode('radar')
        except Exception as e:
            app.logger.error(f"Background install crashed: {e}")
            _write_mode('radar')
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


@bp.route("/install-os", methods=["POST"])
def install_os():
    """Install an OWL-OS update using the same direct-download paradigm as retina-node.

    Flow mirrors /mender/install exactly:
      1. Ensure mender-authd is running so we can obtain a device JWT.
      2. Look up the latest OWL-OS release on GitHub to determine the artifact name.
      3. Fetch the artifact metadata from the Mender server using the JWT.
      4. Get a short-lived signed download URL for the artifact.
      5. Spawn a background thread that runs mender-update install <url>.
         Unlike retina-node, we do NOT commit immediately — the rootfs update
         requires a reboot into the new partition first. commit=False leaves
         Mender in the "awaiting commit" state; the app startup code commits
         on the next boot (see app.py apply_startup_preferences).
      6. On success the thread saves the wizard step and calls systemctl reboot.
         The install lock is intentionally left in place through the reboot and
         cleared by startup code, ensuring check-os stays in "installing" state
         until the device comes back up.
      7. On failure the thread releases the lock and restores radar mode.
    """
    from app import mender, device_state, app, DEV_MODE
    from mender import get_latest_owl_os_from_github
    import time

    if DEV_MODE:
        can_install, reason = device_state.can_start_install()
        if not can_install:
            return jsonify({"success": False, "error": reason}), 409
        if not device_state.acquire_install_lock("owl-os-dev"):
            return jsonify({"success": False, "error": "Install already in progress"}), 409

        def _dev_os_install():
            time.sleep(6)
            device_state.release_install_lock()

        threading.Thread(target=_dev_os_install, daemon=True).start()
        device_state.save_setup_wizard_step("system")
        return jsonify({"success": True, "version": "owl-os-dev", "state": "waiting"})

    success, error = device_state.ensure_cloud_services_enabled(mender.get_jwt)
    if not success:
        return jsonify({"success": False, "error": error})

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

    artifacts, error = mender.list_artifacts(release_name=release_name)
    if error:
        device_state.release_install_lock()
        return jsonify({"success": False, "error": error})

    if not artifacts:
        device_state.release_install_lock()
        return jsonify({"success": False, "error": f"No artifact found for {release_name}"})

    artifact = artifacts[0]
    artifact_id = artifact.get("id")
    if not artifact_id:
        device_state.release_install_lock()
        return jsonify({"success": False, "error": "Artifact missing ID field"})

    url, error = mender.get_download_url(artifact_id)
    if error:
        device_state.release_install_lock()
        return jsonify({"success": False, "error": error})
    if not url:
        device_state.release_install_lock()
        return jsonify({"success": False, "error": "Mender returned no download URL"})

    def _run_os_install(download_url):
        try:
            try:
                subprocess.run(
                    ["mender-update", "rollback"],
                    capture_output=True, timeout=30,
                )
            except Exception:
                pass

            # OS image is ~600 MB — use a 30-minute timeout.
            # commit=False: the new rootfs is written to the inactive A/B
            # partition but NOT committed yet. Commit happens at next startup
            # after the reboot confirms the new OS boots successfully.
            success, error = mender.install_from_url(
                download_url, timeout=1800, commit=False
            )
            if not success:
                app.logger.error(f"OS install failed: {error}")
                device_state.release_install_lock()
                return

            # Save wizard step so the wizard resumes at "system" post-reboot
            # and can confirm the update completed.
            device_state.save_setup_wizard_step("system")
            device_state.update_install_stage("rebooting")

            # Reboot into the new partition. The install lock survives the
            # reboot on disk; startup code clears it after committing.
            try:
                subprocess.run(["systemctl", "reboot"], capture_output=True, timeout=10)
            except Exception as e:
                app.logger.error(f"Reboot failed: {e}")
                device_state.release_install_lock()

        except Exception as e:
            app.logger.error(f"OS install thread crashed: {e}")
            device_state.release_install_lock()

    threading.Thread(target=_run_os_install, args=(url,), daemon=True).start()
    return jsonify({"success": True, "version": release_name})
