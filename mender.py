"""Mender client for device-initiated OTA updates."""
import re
import subprocess

import requests


class MenderClient:
    """Client for pulling artifacts from Mender.

    Supports device-initiated OTA updates by listing available artifacts
    from the Mender server and installing them via mender-update.
    """

    def __init__(
        self,
        server_url: str = "https://hosted.mender.io",
        release_name: str = "retina-node",
        device_type: str = "pi5-v3-arm64",
    ):
        self.server_url = server_url
        self.release_name = release_name
        self.device_type = device_type

    def get_jwt(self) -> tuple[str, str] | tuple[None, None]:
        """Get device JWT via D-Bus from mender-auth.

        Returns (token, server_url) tuple, or (None, None) if not authenticated.
        """
        try:
            result = subprocess.run(
                [
                    "busctl",
                    "call",
                    "io.mender.AuthenticationManager",
                    "/io/mender/AuthenticationManager",
                    "io.mender.Authentication1",
                    "GetJwtToken",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return None, None

            # Output format: ss "token" "server_url"
            output = result.stdout.strip()
            if not output.startswith("ss "):
                return None, None

            # Extract the two quoted strings
            parts = output[3:].split('" "')
            if len(parts) != 2:
                return None, None

            token = parts[0].strip('"')
            server_url = parts[1].strip('"')
            return token, server_url
        except Exception:
            return None, None

    def get_versions(self) -> tuple[str | None, str | None]:
        """Get owl-os and retina-node versions from Mender provides.

        Returns (owl_os_version, retina_node_version) tuple.
        On fresh bootstrap, only owl-os version exists. retina-node version
        appears after the first app OTA update.
        """
        try:
            result = subprocess.run(
                ["mender-update", "show-provides"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return None, None

            owl_os = None
            retina_node = None
            for line in result.stdout.splitlines():
                if line.startswith("rootfs-image.owl-os-pi5.version="):
                    owl_os = line.split("=", 1)[1]
                elif line.startswith("rootfs-image.retina-node.version="):
                    retina_node = line.split("=", 1)[1]
            return owl_os, retina_node
        except FileNotFoundError:
            # mender-update not installed (dev environment)
            return None, None
        except Exception:
            return None, None

    def list_artifacts(self) -> tuple[list[dict], str | None]:
        """List artifacts for configured release/device type.

        Returns (artifacts, error) tuple. On success, error is None.
        """
        token, _ = self.get_jwt()
        if not token:
            return [], "Device not authenticated with Mender"

        try:
            resp = requests.get(
                f"{self.server_url}/api/devices/v1/deployments/artifacts",
                params={
                    "release_name": self.release_name,
                    "device_type": self.device_type,
                },
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            if resp.status_code != 200:
                return [], f"Mender API error: {resp.status_code}"
            return resp.json(), None
        except requests.RequestException as e:
            return [], str(e)

    def get_download_url(self, artifact_id: str) -> tuple[str | None, str | None]:
        """Get signed download URL for artifact.

        Returns (url, error) tuple. On success, error is None.
        """
        token, _ = self.get_jwt()
        if not token:
            return None, "Not authenticated"

        try:
            resp = requests.get(
                f"{self.server_url}/api/devices/v1/deployments/artifacts/{artifact_id}/download",
                headers={"Authorization": f"Bearer {token}"},
                timeout=30,
            )
            if resp.status_code != 200:
                return None, f"Failed to get download URL: {resp.status_code}"
            return resp.json().get("uri"), None
        except requests.RequestException as e:
            return None, str(e)

    def install_from_url(self, url: str, timeout: int = 600) -> tuple[bool, str | None]:
        """Install artifact from URL via mender-update.

        Returns (success, error) tuple. On success, error is None.
        """
        try:
            result = subprocess.run(
                ["mender-update", "install", url],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                return False, result.stderr or "Install failed"
            return True, None
        except subprocess.TimeoutExpired:
            return False, "Installation timed out"
        except Exception as e:
            return False, str(e)


def parse_version(artifact_name: str) -> tuple[int, ...] | None:
    """Extract semver tuple from 'retina-node-v0.3.2' format.

    Returns version tuple (0, 3, 2) for stable releases.
    Returns None for RCs, dev, beta, or non-matching names.
    """
    match = re.match(r"^retina-node-v(\d+)\.(\d+)\.(\d+)$", artifact_name)
    if match:
        return tuple(int(x) for x in match.groups())
    return None


def find_latest_stable(artifacts: list[dict]) -> dict | None:
    """Find latest stable artifact (excludes RCs, dev, beta).

    Returns the artifact dict with the highest semver version,
    or None if no stable artifacts found.
    """
    stable = []
    for artifact in artifacts:
        name = artifact.get("artifact_name", "")
        version = parse_version(name)
        if version:
            stable.append((artifact, version))

    if not stable:
        return None

    stable.sort(key=lambda x: x[1], reverse=True)
    return stable[0][0]
