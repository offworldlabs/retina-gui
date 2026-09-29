"""Cross-process mutex for anything that drives the retina-node Docker stack.

Every path that runs `docker compose` against project `retina-node` must hold
this lock for the whole operation, or callers interleave and leave containers
half-recreated (see stack_reconcile.py).

The file path is shared with blah2-arm's cron watchdog
(script/blah2_rspduo_restart.bash), which takes it with flock(1). Do not
rename or move it without changing that script.

NOT re-entrant: exactly one place in a call chain takes it. flock is per file
descriptor, so a nested acquire from the same process blocks against itself.

See docs/architecture.md#the-restart-lock.
"""

import errno
import fcntl
import os
from contextlib import contextmanager

LOCK_FILENAME = "restart.lock"

# Callers on a request thread (mode switch, wizard completion) wait this long.
# A whole restart is ~45s, so this covers one queued operation plus headroom.
DEFAULT_TIMEOUT_SECONDS = 90

# The async apply worker is not attached to a request, so it can afford to
# wait out a long-running operation ahead of it rather than fail.
BACKGROUND_TIMEOUT_SECONDS = 600

# Fire-and-forget callers (GUI startup, the wizard's navigate-away beacon).
# Whoever holds the lock is already doing a restart that subsumes theirs.
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
    defaulting to DEFAULT_TIMEOUT_SECONDS. The default is resolved here, not
    as a default argument, so the module constant stays adjustable at runtime.

    Polls rather than blocking, so the wait is bounded without signals, which
    are unsafe on a Flask worker thread.
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
                        "Wait for it to finish, then try again.") from e
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
