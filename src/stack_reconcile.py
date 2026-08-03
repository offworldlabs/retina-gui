"""Repair a retina-node compose project left half-recreated.

Compose recreates a container by renaming the existing one to
`<id-prefix>_<name>`, creating the replacement under the real name, then
removing the old one. Interrupt it between the rename and the remove and the
project is left with a container squatting a name compose is about to need:

    Error response from daemon: Error when allocating new name: Conflict.
    The container name "/tar1090" is already in use by container
    "56f30a56ce9b..."

Nothing clears that on its own, so **every subsequent apply fails the same
way**, and the state survives a reboot. It is the difference between one bad
restart and a node that can never accept a config change again.

Two things interrupt a recreate mid-flight:

  - subprocess.run's timeout, which SIGKILLs the compose CLI while the
    daemon carries on with the operation;
  - systemd restarting retina-gui.service, which kills the whole control
    group — compose runs as a child of the Flask process, so a crash, a
    `systemctl restart`, or a redeploy during an apply all do this.

The second is why reconcile also runs at startup and not only after a failed
compose call: by the time the GUI is back up, the process that could have
cleaned up is gone.

Scoped by compose's own project label rather than by name pattern. A bare
`^[0-9a-f]{12}_` regex over `docker ps -a` would also match containers from
other projects on the same host, and this removes what it finds.
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

    Callers must already hold the restart lock — this runs `docker rm -f`
    and `docker compose up`, which is exactly the kind of work that must not
    interleave with another caller's.

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

    # Plain `up -d`, deliberately not --force-recreate: this is a repair, so
    # it should create whatever the removals left missing and leave every
    # healthy container alone.
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
