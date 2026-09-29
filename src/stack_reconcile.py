"""Repair a retina-node compose project left half-recreated.

Compose recreates a container by renaming the old one to `<id-prefix>_<name>`
before removing it. Interrupted in between (a subprocess timeout, or systemd
killing retina-gui's control group), the renamed container blocks every later
apply with a name conflict, across reboots. Runs after a failed recreate and at
startup. Scoped by compose's project label, never a bare name regex, because it
removes what it finds. See docs/architecture.md#stack-reconcile.
"""

import re
import subprocess

PROJECT = "retina-node"
PROJECT_LABEL = "com.docker.compose.project"

# Compose's rename prefix: 12 hex chars of the container id, then the
# original name.
STALE_NAME_RE = re.compile(r"^[0-9a-f]{12}_")


def find_stale_containers(project=PROJECT, timeout=30):
    """Containers in `project` still carrying compose's rename prefix.

    Returns a list of names; empty on any error, since this is only ever
    used to decide whether to attempt a repair.
    """
    try:
        result = subprocess.run(
            ["docker", "ps", "-a",
             "--filter", f"label={PROJECT_LABEL}={project}",
             "--format", "{{.Names}}"],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return []
    if result.returncode != 0:
        return []
    return [name for name in result.stdout.split()
            if STALE_NAME_RE.match(name)]


def reconcile(retina_node_path, project=PROJECT, bring_up=True):
    """Remove half-recreated containers and, optionally, restore the stack.

    Callers must already hold the restart lock: this runs `docker rm -f` and
    `docker compose up`, which must not interleave with another caller's.

    bring_up=False skips the `up`, for callers that must not start the radar
    stack (spectrum/sdrconnect mode, where blah2 is deliberately stopped).

    Returns (removed: list[str], error: str|None). Never raises.
    """
    stale = find_stale_containers(project)
    if not stale:
        return [], None

    try:
        result = subprocess.run(
            ["docker", "rm", "-f", *stale],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            return [], f"could not remove {', '.join(stale)}: {result.stderr or result.stdout}"
    except Exception as e:
        return [], f"could not remove {', '.join(stale)}: {e}"

    if not bring_up:
        return stale, None

    # Plain `up -d`, not --force-recreate: create what is missing and leave
    # healthy containers alone.
    try:
        result = subprocess.run(
            ["docker", "compose", "-p", project, "up", "-d", "--remove-orphans"],
            cwd=retina_node_path, capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            return stale, f"repair restart failed: {result.stderr or result.stdout}"
    except Exception as e:
        return stale, f"repair restart failed: {e}"

    return stale, None
