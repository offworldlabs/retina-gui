"""ApplyService: the background config-apply worker.

The behaviours here are the ones that fix the reported bug — a slow apply
being clicked a second time and turning into two concurrent `docker compose`
runs against the same project (container-name conflicts, then timeouts).
"""
import subprocess
import threading
import time

import pytest

from apply_service import ApplyService


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class FakeRestart:
    """Stands in for run_config_merger_and_restart: blocks until released,
    records each call, and reports whatever phases the test asks for."""

    def __init__(self, error=None, phases=(), raises=None):
        self.calls = 0
        self.error = error
        self.phases = phases
        self.raises = raises
        self.entered = threading.Event()
        self.release = threading.Event()
        self.concurrent = False
        self._in_flight = 0
        self._lock = threading.Lock()

    def __call__(self, path, on_phase=None, lock_timeout=None):
        with self._lock:
            self._in_flight += 1
            if self._in_flight > 1:
                self.concurrent = True
        try:
            self.calls += 1
            self.entered.set()
            for phase, detail in self.phases:
                if on_phase:
                    on_phase(phase, detail)
            self.release.wait(timeout=5)
            if self.raises:
                raise self.raises
            return self.error
        finally:
            with self._lock:
                self._in_flight -= 1


@pytest.fixture
def service():
    """Builds an ApplyService wired to a FakeRestart.

    The restart callable is injected rather than monkeypatched onto
    routes.mode: conftest's app_client fixture calls importlib.reload(app),
    which rebuilds that module, so a patched attribute there would silently
    stop being the one this service calls.
    """
    def build(fake):
        return ApplyService('/nonexistent', restart_fn=fake)
    return build


class TestAsyncBehaviour:

    def test_request_returns_without_waiting_for_the_work(self, service):
        fake = FakeRestart()
        svc = service(fake)

        status = svc.request()

        assert status['state'] == 'running'
        assert fake.entered.wait(timeout=5)
        assert svc.get_status()['state'] == 'running'
        fake.release.set()
        assert wait_until(lambda: svc.get_status()['state'] == 'done')

    def test_success_reports_done(self, service):
        fake = FakeRestart()
        svc = service(fake)
        svc.request()
        fake.entered.wait(timeout=5)
        fake.release.set()

        assert wait_until(lambda: svc.get_status()['state'] == 'done')
        status = svc.get_status()
        assert status['error'] is None
        assert status['finished_at'] is not None

    def test_failure_is_captured_not_raised(self, service):
        fake = FakeRestart(error='restart failed: boom')
        svc = service(fake)
        svc.request()
        fake.entered.wait(timeout=5)
        fake.release.set()

        assert wait_until(lambda: svc.get_status()['state'] == 'failed')
        assert svc.get_status()['error'] == 'restart failed: boom'

    def test_timeout_exception_is_captured(self, service):
        fake = FakeRestart(raises=subprocess.TimeoutExpired(cmd='docker', timeout=120))
        svc = service(fake)
        svc.request()
        fake.entered.wait(timeout=5)
        fake.release.set()

        assert wait_until(lambda: svc.get_status()['state'] == 'failed')
        assert 'timed out' in svc.get_status()['error'].lower()


class TestCoalescing:
    """The actual bug: a second click must never become a second run."""

    def test_second_request_while_running_does_not_run_concurrently(self, service):
        fake = FakeRestart()
        svc = service(fake)

        svc.request()
        assert fake.entered.wait(timeout=5)
        second = svc.request()

        assert second['queued'] is True
        assert fake.calls == 1, "a second concurrent restart was started"
        fake.release.set()
        assert wait_until(lambda: svc.get_status()['state'] == 'done')
        assert fake.concurrent is False

    def test_queued_request_triggers_exactly_one_more_pass(self, service):
        """However many clicks land during a run, they merge the same
        user.yml, so one extra pass is enough — and is what picks up config
        saved while the first pass was already in flight."""
        fake = FakeRestart()
        svc = service(fake)

        svc.request()
        assert fake.entered.wait(timeout=5)
        svc.request()
        svc.request()
        svc.request()

        fake.entered.clear()
        fake.release.set()
        assert wait_until(lambda: svc.get_status()['state'] == 'done', timeout=10)
        assert fake.calls == 2

    def test_failed_run_does_not_retry_the_queued_request(self, service):
        """A queued re-run after a failure would loop against a node that is
        already unhealthy; surface the error instead."""
        fake = FakeRestart(error='config-merger failed: bad yaml')
        svc = service(fake)

        svc.request()
        assert fake.entered.wait(timeout=5)
        svc.request()
        fake.release.set()

        assert wait_until(lambda: svc.get_status()['state'] == 'failed')
        assert fake.calls == 1
        assert svc.get_status()['queued'] is False


class TestProgress:

    def test_phases_are_exposed_with_labels(self, service):
        fake = FakeRestart(phases=[('merging', None), ('settling', 17)])
        svc = service(fake)
        svc.request()
        assert fake.entered.wait(timeout=5)

        assert wait_until(lambda: svc.get_status()['phase'] == 'settling')
        status = svc.get_status()
        assert status['phase_label'] == 'Waiting for the SDR to settle'
        assert status['settle_remaining'] == 17

        fake.release.set()
        assert wait_until(lambda: svc.get_status()['state'] == 'done')
        # Terminal status carries no stale phase from the run
        assert svc.get_status()['phase'] is None
        assert svc.get_status()['settle_remaining'] is None


class TestDevMode:

    def test_dev_mode_short_circuits(self):
        called = []
        svc = ApplyService('/nonexistent', dev_mode=True,
                           restart_fn=lambda *a, **k: called.append(1))
        status = svc.request()

        assert status['state'] == 'done'
        assert called == []
