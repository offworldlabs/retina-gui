"""restart_lock: the cross-process mutex every docker-compose caller takes.

Without it, a `docker compose down` from the cron watchdog can land inside a
GUI apply's `up -d --force-recreate` and leave containers renamed to
<hash>_<name> with the daemon rejecting the real name as already in use.
"""
import multiprocessing
import os
import threading
import time

import pytest

from restart_lock import (
    RestartBusy,
    is_locked,
    lock_path,
    restart_lock,
)


class TestMutualExclusion:

    def test_second_acquire_blocks_until_first_releases(self, temp_dir):
        order = []
        first_holding = threading.Event()
        may_release = threading.Event()

        def hold():
            with restart_lock(temp_dir):
                order.append('first-in')
                first_holding.set()
                may_release.wait(timeout=5)
                order.append('first-out')

        t = threading.Thread(target=hold)
        t.start()
        assert first_holding.wait(timeout=5)

        with pytest.raises(RestartBusy):
            with restart_lock(temp_dir, timeout=0.2):
                order.append('second-in-too-early')

        may_release.set()
        t.join(timeout=5)

        with restart_lock(temp_dir, timeout=2):
            order.append('second-in')

        assert order == ['first-in', 'first-out', 'second-in']

    def test_excludes_a_separate_process(self, temp_dir):
        """The watchdog is a different process entirely — a lock that only
        works within this interpreter would not fix anything."""
        holding = multiprocessing.Event()
        release = multiprocessing.Event()

        p = multiprocessing.Process(
            target=_hold_lock_in_child, args=(temp_dir, holding, release))
        p.start()
        try:
            assert holding.wait(timeout=10)
            with pytest.raises(RestartBusy):
                with restart_lock(temp_dir, timeout=0.5):
                    pass
        finally:
            release.set()
            p.join(timeout=10)

        with restart_lock(temp_dir, timeout=2):
            pass  # released with the child


class TestRelease:

    def test_released_when_the_body_raises(self, temp_dir):
        with pytest.raises(ValueError):
            with restart_lock(temp_dir):
                raise ValueError('boom')

        with restart_lock(temp_dir, timeout=1):
            pass  # not wedged by the exception

    def test_released_when_the_holder_process_dies(self, temp_dir):
        """No staleness heuristic to get wrong: the kernel drops an flock
        when its holder dies, however it dies."""
        holding = multiprocessing.Event()

        p = multiprocessing.Process(
            target=_hold_lock_forever_in_child, args=(temp_dir, holding))
        p.start()
        assert holding.wait(timeout=10)
        p.kill()
        p.join(timeout=10)

        with restart_lock(temp_dir, timeout=5):
            pass


class TestStatus:

    def test_is_locked_tracks_the_lock(self, temp_dir):
        assert is_locked(temp_dir) is False
        with restart_lock(temp_dir):
            assert is_locked(temp_dir) is True
        assert is_locked(temp_dir) is False

    def test_lock_file_is_created_under_the_data_dir(self, temp_dir):
        target = os.path.join(temp_dir, 'nested')
        with restart_lock(target):
            assert os.path.exists(lock_path(target))


def _hold_lock_in_child(data_dir, holding, release):
    with restart_lock(data_dir, timeout=10):
        holding.set()
        release.wait(timeout=30)


def _hold_lock_forever_in_child(data_dir, holding):
    with restart_lock(data_dir, timeout=10):
        holding.set()
        time.sleep(60)
