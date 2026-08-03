"""Cross-process mutex for anything that drives the retina-node Docker stack.

Every path that runs `docker compose` against project `retina-node` must
hold this lock for the whole operation. Without it these callers can and do
collide — a `docker compose down` from the cron watchdog landing inside a
GUI apply's `up -d --force-recreate` leaves containers renamed to
`<hash>_<name>` and the daemon rejecting the real name as "already in use",
which then breaks every subsequent apply until someone cleans up by hand.

An fcntl.flock on a file, rather than the timestamp-file locks in
device_state.py, for two reasons that both matter here:

  - The kernel releases it when the holding process dies, so there is no
    staleness heuristic that can be wrong. device_state's locks need
    timeouts (INSTALL_LOCK_TIMEOUT and friends) precisely because a crashed
    holder would otherwise wedge them forever; a restart is short and
    frequent enough that guessing at a staleness window would be worse than
    the problem it solves.
  - flock(1) makes the same lock available to shell callers, so the cron
    watchdog (blah2-arm/script/blah2_rspduo_restart.bash) can eventually
    replace its point-in-time `pgrep -f "docker compose"` guard — which
    cannot see the settle window in run_config_merger_and_restart, where no
    compose process exists for ~30s but the operation is very much still in
    flight — with `flock -n <file> -c ...` against this exact file.

Deliberately NOT re-entrant: exactly one place in a call chain should take
it (run_config_merger_and_restart for the config paths, set_mode for the
mode transitions). flock is per-file-descriptor, so a nested acquire from
the same process opens a second fd and blocks against itself forever.
"""

import errno
import fcntl
import os
from contextlib import contextmanager

LOCK_FILENAME = "restart.lock"

# Synchronous HTTP callers (mode switch, tower select, calibrate apply) wait
# this long for an in-flight restart before giving up and telling the user.
# A whole restart measures ~45s on real hardware, most of it the settle
# window, so this covers one queued operation plus headroom without leaving
# a request hanging indefinitely.
DEFAULT_TIMEOUT_SECONDS = 90

# The async apply worker is not attached to a request, so it can afford to
# wait out a long-running operation ahead of it rather than fail.
BACKGROUND_TIMEOUT_SECONDS = 600

# Fire-and-forget callers — GUI startup, and the wizard's navigate-away
# beacon — where nobody is waiting on the result and whoever holds the lock
# is already performing a restart that subsumes what this caller wanted
# (both only stop/remove retina-spectrum, which every restart path does
# defensively anyway). They give up quickly rather than queue: blocking GUI
# startup behind a two-minute restart would be strictly worse than skipping.
OPPORTUNISTIC_TIMEOUT_SECONDS = 10

POLL_SECONDS = 0.25


class RestartBusy(Exception):
    """Raised when the lock could not be acquired within the timeout."""


def lock_path(data_dir):
    return os.path.join(data_dir, LOCK_FILENAME)


@contextmanager
def restart_lock(data_dir, timeout=None):
    """Hold the stack-restart lock for the duration of the block.

    Raises RestartBusy if it can't be acquired within `timeout` seconds,
    defaulting to DEFAULT_TIMEOUT_SECONDS. Resolved here rather than as a
    default argument so the module constant stays adjustable at runtime — a
    default argument binds once at import and silently ignores any later
    change, which makes the wait untunable and every contention test pay the
    full production timeout.

    Polls rather than using a blocking flock so the wait is bounded without
    needing signals/alarms, which would not be safe on a Flask worker thread.
    """
    import time

    if timeout is None:
        timeout = DEFAULT_TIMEOUT_SECONDS
    path = lock_path(data_dir)
    try:
        os.makedirs(data_dir, exist_ok=True)
    except OSError:
        pass

    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if e.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise RestartBusy(
                        "Another restart is already in progress. "
                        "Wait for it to finish, then try again.")
                time.sleep(POLL_SECONDS)

        # Recorded for humans reading the file during an incident; the lock
        # itself is held by the kernel, not by this content.
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"pid={os.getpid()}\n".encode())
        except OSError:
            pass

        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def is_locked(data_dir):
    """True if some process currently holds the lock. Advisory only — the
    answer can be stale the instant it is returned, so this is for status
    display, never for deciding whether it is safe to proceed.
    """
    path = lock_path(data_dir)
    if not os.path.exists(path):
        return False
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)
