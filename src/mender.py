"""Mender client for device-initiated OTA updates."""
import os
import re
import subprocess
import time

import requests

# Fake version history used by all dev-mode routes, newest first.
# DEV_NODE_VERSION sets the simulated installed version (empty = fresh node).
# See docs/features/ota-updates.md#dev-mode.
DEV_VERSIONS = ['v1.1.0', 'v1.0.5', 'v1.0.0', 'v0.9.5', 'v0.9.0']


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
        dev_mode: bool = False,
        dev_data_dir: str | None = None,
    ):
        self.server_url = server_url
        self.release_name = release_name
        self.device_type = device_type
        self.dev_mode = dev_mode
        self.dev_data_dir = dev_data_dir

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

    def dev_get_node_version(self) -> str | None:
        """Read the simulated installed retina-node version from dev_data."""
        if self.dev_data_dir:
            path = os.path.join(self.dev_data_dir, 'dev_node_version.txt')
            if os.path.exists(path):
                with open(path) as fh:
                    v = fh.read().strip()
                return v or None
        v = os.environ.get('DEV_NODE_VERSION', 'v1.0.0')
        return v or None

    def dev_set_node_version(self, version: str):
        """Write the simulated installed retina-node version to dev_data."""
        if self.dev_data_dir:
            os.makedirs(self.dev_data_dir, exist_ok=True)
            with open(os.path.join(self.dev_data_dir, 'dev_node_version.txt'), 'w') as f:
                f.write(version)

    def get_versions(self) -> tuple[str | None, str | None]:
        """Get (owl_os_version, retina_node_version) from Mender provides.

        Falls back to the running blah2 image tag for retina-node when
        provides are missing. Either value may be None.
        See docs/features/ota-updates.md#installed-versions.
        """
        if self.dev_mode:
            return ('2.4.1-dev', self.dev_get_node_version())

        try:
            result = subprocess.run(
                ["mender-update", "show-provides"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                return None, get_retina_node_version_from_docker()

            owl_os = None
            retina_node = None
            for line in result.stdout.splitlines():
                if line.startswith("rootfs-image.owl-os-pi5.version="):
                    owl_os = line.split("=", 1)[1]
                elif line.startswith("data-docker.mender-docker-compose.retina-node.version="):
                    raw = line.split("=", 1)[1]
                    retina_node = raw.removeprefix("retina-node-")

            if retina_node is None:
                retina_node = get_retina_node_version_from_docker()

            return owl_os, retina_node
        except FileNotFoundError:
            # mender-update not installed (dev environment)
            return None, get_retina_node_version_from_docker()
        except Exception:
            return None, get_retina_node_version_from_docker()

    def list_artifacts(self, release_name: str | None = None) -> tuple[list[dict], str | None]:
        """List artifacts for a release/device type.

        Args:
            release_name: Override the configured release name (e.g., "retina-node-v0.3.5")

        Returns (artifacts, error) tuple. On success, error is None.
        """
        token, _ = self.get_jwt()
        if not token:
            return [], "Device not authenticated with Mender"

        name = release_name or self.release_name
        try:
            resp = requests.get(
                f"{self.server_url}/api/devices/v1/deployments/artifacts",
                params={
                    "release_name": name,
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
        """Install artifact from URL via mender-update (standalone).

        Used for retina-node stack updates only. OS updates run in managed
        mode. See docs/features/ota-updates.md#two-kinds-of-update.

        Returns (success, error) tuple.
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
            try:
                subprocess.run(
                    ["mender-update", "commit"],
                    capture_output=True,
                    timeout=30,
                )
            except Exception:
                pass
            return True, None
        except subprocess.TimeoutExpired:
            return False, "Installation timed out"
        except Exception as e:
            return False, str(e)


def get_retina_node_version_from_docker() -> str | None:
    """Get retina-node version from running blah2 Docker containers.

    Returns the image tag (e.g. 'v0.3.10'), or None if no blah2 container
    is running or docker is unavailable.
    """
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Image}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return None
        for image in result.stdout.splitlines():
            if "/blah2:" in image:
                return image.rsplit(":", 1)[-1]
        return None
    except Exception:
        return None


def parse_version(artifact_name: str) -> tuple[int, ...] | None:
    """Extract version tuple from 'retina-node-v0.4.0.2' (or 3-part) format.

    Returns version tuple e.g. (0, 4, 0, 2) for stable releases.
    Returns None for RCs, dev, beta, or non-matching names.
    """
    match = re.match(r"^retina-node-v(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?$", artifact_name)
    if match:
        return tuple(int(x) for x in match.groups() if x is not None)
    return None


# The wizard polls this every 5s, so it is cached to stay inside GitHub's
# unauthenticated rate limit. See docs/features/ota-updates.md#github-release-lookups.
_STABLE_RELEASE_CACHE_TTL = 60  # seconds
_stable_release_cache: dict[str, tuple[float, tuple[list[dict], str | None]]] = {}


def get_all_stable_versions_from_github(
    repo: str = "offworldlabs/retina-node",
    request_timeout: float = 10.0,
) -> tuple[list[dict], str | None]:
    """Get all stable version tags from GitHub releases, newest first.

    Result (including errors) is cached for _STABLE_RELEASE_CACHE_TTL seconds.

    Returns (versions, error). Each entry is {"version": "v0.3.5", "size_bytes": 628000000};
    size_bytes is the .mender asset's size, else the largest asset's, else None.
    """
    cached = _stable_release_cache.get(repo)
    if cached and time.monotonic() - cached[0] < _STABLE_RELEASE_CACHE_TTL:
        return cached[1]

    try:
        resp = requests.get(
            f"https://api.github.com/repos/{repo}/releases",
            headers={"Accept": "application/vnd.github+json"},
            timeout=request_timeout,
        )
        if resp.status_code != 200:
            result = [], f"GitHub API error: {resp.status_code}"
        else:
            stable = []
            for release in resp.json():
                tag = release.get("tag_name", "")
                if parse_version(f"retina-node-{tag}"):
                    assets = release.get("assets", [])
                    mender_asset = next((a for a in assets if a["name"].endswith(".mender")), None)
                    if mender_asset:
                        size_bytes = mender_asset["size"]
                    elif assets:
                        size_bytes = max(a["size"] for a in assets)
                    else:
                        size_bytes = None
                    stable.append({"version": tag, "size_bytes": size_bytes})

            stable.sort(key=lambda v: parse_version(f"retina-node-{v['version']}"), reverse=True)
            result = stable, None
    except requests.RequestException as e:
        result = [], str(e)

    _stable_release_cache[repo] = (time.monotonic(), result)
    return result


def get_latest_stable_from_github(
    repo: str = "offworldlabs/retina-node",
) -> tuple[str | None, str | None]:
    """Get latest stable version tag from GitHub releases.

    Returns (version_tag, error) tuple. version_tag is like "v0.3.5".
    """
    try:
        resp = requests.get(
            f"https://api.github.com/repos/{repo}/releases",
            headers={"Accept": "application/vnd.github+json"},
            timeout=30,
        )
        if resp.status_code != 200:
            return None, f"GitHub API error: {resp.status_code}"

        releases = resp.json()
        stable = []
        for release in releases:
            tag = release.get("tag_name", "")
            artifact_name = f"retina-node-{tag}"
            version = parse_version(artifact_name)
            if version:
                stable.append((tag, version))

        if not stable:
            return None, "No stable releases found"

        stable.sort(key=lambda x: x[1], reverse=True)
        return stable[0][0], None
    except requests.RequestException as e:
        return None, str(e)


def parse_os_version(tag: str) -> tuple[int, ...] | None:
    """Extract semver tuple from owl-os version strings.

    Handles formats: 'os-v0.1.0', 'v0.1.0', '0.1.0'.
    Returns version tuple (0, 1, 0) for stable releases.
    Returns None for RCs, dev, or non-matching strings.
    """
    match = re.match(r"^(?:os-)?v?(\d+)\.(\d+)\.(\d+)$", tag)
    if match:
        return tuple(int(x) for x in match.groups())
    return None


# Polled every 5s by the wizard's System step, so cached to stay inside
# GitHub's 60 requests/hour unauthenticated limit.
_OWL_OS_RELEASE_CACHE_TTL = 300  # seconds
_owl_os_release_cache: dict[str, tuple[float, tuple[str | None, str | None]]] = {}


def get_latest_owl_os_from_github(
    repo: str = "offworldlabs/owl-os",
) -> tuple[str | None, str | None]:
    """Get latest stable owl-os version tag from GitHub releases.

    Only os-vX.Y.Z tags count: pre-releases must never be offered as "latest".
    Result (including errors) is cached for _OWL_OS_RELEASE_CACHE_TTL seconds.
    See docs/features/ota-updates.md#version-parsing.

    Returns (version_tag, error) tuple. version_tag is like 'os-v0.2.0'.
    """
    cached = _owl_os_release_cache.get(repo)
    if cached and time.monotonic() - cached[0] < _OWL_OS_RELEASE_CACHE_TTL:
        return cached[1]

    try:
        resp = requests.get(
            f"https://api.github.com/repos/{repo}/releases",
            headers={"Accept": "application/vnd.github+json"},
            timeout=30,
        )
        if resp.status_code != 200:
            result = None, f"GitHub API error: {resp.status_code}"
        else:
            stable = []
            for release in resp.json():
                tag = release.get("tag_name", "")
                if not tag.startswith("os-v"):
                    continue
                version = parse_os_version(tag)
                if version:
                    stable.append((tag, version))

            if not stable:
                result = None, "No stable owl-os releases found"
            else:
                stable.sort(key=lambda x: x[1], reverse=True)
                result = stable[0][0], None
    except requests.RequestException as e:
        result = None, str(e)

    _owl_os_release_cache[repo] = (time.monotonic(), result)
    return result
