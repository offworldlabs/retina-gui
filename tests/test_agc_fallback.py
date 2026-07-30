"""Tests for AgcFallbackClient — Auto-Calibrate's AGC last-resort config
write + restart mechanism. FakeAgcFallbackClient (test_calibrate.py) fakes
this whole class for calibrator.py's own tests, bypassing the real
implementation entirely — these tests cover the real thing directly,
especially the fresh-thread wrapper around the restart call (see
apply()'s own docstring for why that thread exists)."""
import time
from unittest.mock import patch

import pytest

import agc_fallback as agc_fallback_module
from agc_fallback import AgcFallbackClient


class FakeConfigManager:
    """Minimal stand-in for ConfigManager — just the surface
    AgcFallbackClient.apply() actually touches."""

    def __init__(self, installed=True, retina_node_path="/data/mender-docker-compose/current/manifests"):
        self.retina_node_path = retina_node_path
        self._installed = installed
        self._user_config = {}
        self.saved_configs = []

    def is_retina_node_installed(self):
        return self._installed

    def load_user_config(self):
        return dict(self._user_config)

    def save_user_config(self, config):
        self.saved_configs.append(config)
        self._user_config = config


@pytest.fixture
def fast_join(monkeypatch):
    """Shrink the thread-join backstop so a deliberately-hanging restart
    doesn't cost real wall-clock time in tests."""
    monkeypatch.setattr(agc_fallback_module, "RESTART_THREAD_JOIN_TIMEOUT_SECONDS", 0.2)


class TestAgcFallbackClient:
    def test_dev_mode_skips_everything(self):
        config_mgr = FakeConfigManager()
        client = AgcFallbackClient(config_mgr, dev_mode=True)
        ok, error = client.apply(98_000_000, 5, 59, 59, 9)
        assert ok is True
        assert error is None
        assert config_mgr.saved_configs == []  # never even wrote the config

    def test_not_installed_returns_error_without_writing(self):
        config_mgr = FakeConfigManager(installed=False)
        client = AgcFallbackClient(config_mgr, dev_mode=False)
        ok, error = client.apply(98_000_000, 5, 59, 59, 9)
        assert ok is False
        assert "not installed" in error
        assert config_mgr.saved_configs == []

    def test_writes_config_before_attempting_restart(self):
        config_mgr = FakeConfigManager()
        client = AgcFallbackClient(config_mgr, dev_mode=False)
        with patch("routes.mode.run_config_merger_and_restart", return_value=None) as mock_restart:
            ok, error = client.apply(98_000_000, 5, 39, 39, 6)
        assert ok is True
        assert error is None
        assert len(config_mgr.saved_configs) == 1
        device = config_mgr.saved_configs[0]["capture"]["device"]
        assert device["bandwidthNumber"] == 5
        assert device["gainReduction"] == [39, 39]
        assert device["lnaState"] == 6
        assert config_mgr.saved_configs[0]["capture"]["fc"] == 98_000_000
        mock_restart.assert_called_once_with(config_mgr.retina_node_path)

    def test_propagates_restart_error(self):
        config_mgr = FakeConfigManager()
        client = AgcFallbackClient(config_mgr, dev_mode=False)
        with patch("routes.mode.run_config_merger_and_restart",
                   return_value="restart failed: boom"):
            ok, error = client.apply(98_000_000, 0, 59, 59, 9)
        assert ok is False
        assert error == "restart failed: boom"

    def test_catches_timeout_expired_raised_on_the_fresh_thread(self):
        import subprocess as subprocess_module
        config_mgr = FakeConfigManager()
        client = AgcFallbackClient(config_mgr, dev_mode=False)

        def raise_timeout(path):
            raise subprocess_module.TimeoutExpired(cmd="docker compose", timeout=120)

        with patch("routes.mode.run_config_merger_and_restart", side_effect=raise_timeout):
            ok, error = client.apply(98_000_000, 5, 59, 59, 9)
        assert ok is False
        assert error == "Command timed out"

    def test_reports_timeout_if_the_thread_never_finishes(self, fast_join):
        """The defensive backstop: if the restart call hangs past
        RESTART_THREAD_JOIN_TIMEOUT_SECONDS for any reason, apply()
        reports failure and returns rather than blocking the caller
        forever — even though the thread itself is left running."""
        config_mgr = FakeConfigManager()
        client = AgcFallbackClient(config_mgr, dev_mode=False)

        def hang_forever(path):
            time.sleep(5)
            return None

        with patch("routes.mode.run_config_merger_and_restart", side_effect=hang_forever):
            ok, error = client.apply(98_000_000, 5, 59, 59, 9)
        assert ok is False
        assert error == "Command timed out"

    def test_restart_runs_on_a_different_thread_than_the_caller(self):
        """The whole point of this fix: the actual restart call must not
        run on the calling thread."""
        config_mgr = FakeConfigManager()
        client = AgcFallbackClient(config_mgr, dev_mode=False)
        caller_thread_id = None
        restart_thread_id = None

        def record_thread(path):
            nonlocal restart_thread_id
            import threading
            restart_thread_id = threading.get_ident()
            return None

        import threading
        caller_thread_id = threading.get_ident()
        with patch("routes.mode.run_config_merger_and_restart", side_effect=record_thread):
            client.apply(98_000_000, 5, 59, 59, 9)
        assert restart_thread_id is not None
        assert restart_thread_id != caller_thread_id
