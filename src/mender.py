"""Mender client for device-initiated OTA updates."""
import os
import re
import subprocess
import time

import requests


# Fake version history used by all dev-mode routes — newest first.
# Set DEV_NODE_VERSION env var to control the simulated installed version:
#   DEV_NODE_VERSION=v1.0.0  (default) → re-run, package already installed
#   DEV_NODE_VERSION=                  → fresh install (no package installed)
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
                v = open(path).read().strip()
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
        """Get owl-os and retina-node versions.

        Checks mender-update show-provides first; falls back to inspecting
        running Docker containers if mender hasn't committed provides yet
        (e.g. when install_from_url succeeded but provides lag behind).

        Returns (owl_os_version, retina_node_version) tuple.
        On fresh bootstrap, only owl-os version exists. retina-node version
        appears after the first app OTA update.
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

        Used for app updates only (no reboot needed). OS updates use managed
        mode via the mender-updated daemon, driven by server-side deployments.

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

    Inspects 'docker ps' output for any offworldlabs/blah2 image and extracts
    the image tag. Used as a fallback when mender-update show-provides has not
    yet committed the artifact provides.

    Returns the image tag string (e.g. 'v0.3.10'), or None if no blah2
    containers are running or docker is unavailable.
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


# Polled every 5s while the wizard's Packages step is open on a fresh node
# (no skip available, so a failure here keeps retrying), so this needs a
# cache or it blows through GitHub's 60-req/hour unauthenticated rate limit
# — see _OWL_OS_RELEASE_CACHE_TTL above for the same reasoning.
_STABLE_RELEASE_CACHE_TTL = 60  # seconds
_stable_release_cache: dict[str, tuple[float, tuple[list[dict], str | None]]] = {}


def get_all_stable_versions_from_github(
    repo: str = "offworldlabs/retina-node",
    request_timeout: float = 10.0,
) -> tuple[list[dict], str | None]:
    """Get all stable version tags from GitHub releases, newest first.

    Queries GitHub releases API, filters to stable versions (excludes rc, dev, beta),
    and returns all matching entries sorted by semver descending.
    Result (including errors) is cached for _STABLE_RELEASE_CACHE_TTL seconds.

    Returns (versions, error) tuple. Each entry is {"version": "v0.3.5", "size_bytes": 628000000}.
    size_bytes is the size of the .mender artifact asset, or None if no assets are present.
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

    Queries GitHub releases API, filters to stable versions (excludes rc, dev, beta),
    and returns the highest semver version.

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
        # Filter to stable versions using existing parse_version logic
        stable = []
        for release in releases:
            tag = release.get("tag_name", "")
            # Construct artifact name format for parsing
            artifact_name = f"retina-node-{tag}"
            version = parse_version(artifact_name)
            if version:
                stable.append((tag, version))

        if not stable:
            return None, "No stable releases found"

        # Sort by version tuple, highest first
        stable.sort(key=lambda x: x[1], reverse=True)
        return stable[0][0], None
    except requests.RequestException as e:
        return None, str(e)


def parse_os_version(tag: str) -> tuple[int, ...] | None:
    """Extract semver tuple from owl-os version strings.

    Handles formats: 'os-v0.1.0', 'v0.1.0', '0.1.0',
    and pre-release variants like 'os-v0.1.0-dev', 'v0.1.0-rc1'.
    Returns the numeric version tuple (0, 1, 0) — suffix is ignored for comparison.
    Returns None for non-matching strings.
    """
    match = re.match(r"^(?:os-)?v?(\d+)\.(\d+)\.(\d+)(?:[-.].+)?$", tag)
    if match:
        return tuple(int(x) for x in match.groups())
    return None


# Polled every 5s while the wizard's System step is open, so this needs a
# cache or it blows through GitHub's 60-req/hour unauthenticated rate limit.
_OWL_OS_RELEASE_CACHE_TTL = 300  # seconds
_owl_os_release_cache: dict[str, tuple[float, tuple[str | None, str | None]]] = {}


def get_latest_owl_os_from_github(
    repo: str = "offworldlabs/owl-os",
) -> tuple[str | None, str | None]:
    """Get latest owl-os version tag from GitHub releases.

    Queries GitHub releases API, includes both stable and pre-release builds
    (tags matching os-v*.*.*[-suffix]), and returns the highest semver version.
    Result (including errors) is cached for _OWL_OS_RELEASE_CACHE_TTL seconds.

    Returns (version_tag, error) tuple. version_tag is like 'os-v0.2.0' or
    'os-v0.2.1-dev'.
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
            found = []
            for release in resp.json():
                tag = release.get("tag_name", "")
                if not tag.startswith("os-v"):
                    continue
                version = parse_os_version(tag)
                if version:
                    found.append((tag, version))

            if not found:
                result = None, "No owl-os releases found"
            else:
                found.sort(key=lambda x: x[1], reverse=True)
                result = found[0][0], None
    except requests.RequestException as e:
        result = None, str(e)

    _owl_os_release_cache[repo] = (time.monotonic(), result)
    return result
