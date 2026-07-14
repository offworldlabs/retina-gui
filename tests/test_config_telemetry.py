"""Tests for config_telemetry: full config snapshots sent whenever a node's
configuration is applied or its mode changes.

send_config_snapshot() dispatches the actual POST on a background thread
(fire-and-forget — see the module docstring for why), so these tests replace
threading.Thread with a synchronous stand-in that runs the target
immediately, keeping assertions on the "background" POST deterministic.
"""
from unittest.mock import patch

import pytest

import config_telemetry


class _ImmediateThread:
    """Runs the target synchronously instead of spawning a real thread."""
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


@pytest.fixture
def sync_thread(monkeypatch):
    monkeypatch.setattr(config_telemetry.threading, "Thread", _ImmediateThread)


class TestBuildConfigSnapshot:
    def test_payload_shape(self):
        payload = config_telemetry.build_config_snapshot(
            "node123", "radar", {"capture": {"fc": 98_000_000}}, "tower_select")
        assert payload["schema"] == config_telemetry.SCHEMA_VERSION
        assert payload["event"] == "config_snapshot"
        assert payload["node_id"] == "node123"
        assert payload["mode"] == "radar"
        assert payload["trigger"] == "tower_select"
        assert payload["config"] == {"capture": {"fc": 98_000_000}}
        assert "sent_at" in payload


class TestSendConfigSnapshot:
    def test_empty_url_sends_nothing(self, sync_thread):
        with patch("config_telemetry.requests.post") as mock_post:
            sent = config_telemetry.send_config_snapshot("", "n", "radar", {}, "config_apply")
        assert sent is False
        mock_post.assert_not_called()

    def test_valid_url_posts_the_snapshot(self, sync_thread):
        with patch("config_telemetry.requests.post") as mock_post:
            sent = config_telemetry.send_config_snapshot(
                "http://example.invalid/ingest", "node123", "radar",
                {"capture": {"fc": 1}}, "mode_switch")
        assert sent is True
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "http://example.invalid/ingest"
        assert kwargs["json"]["trigger"] == "mode_switch"
        assert kwargs["json"]["config"] == {"capture": {"fc": 1}}
        assert kwargs["timeout"] == config_telemetry.TELEMETRY_TIMEOUT_SECONDS

    def test_send_failure_is_swallowed(self, sync_thread):
        with patch("config_telemetry.requests.post", side_effect=Exception("boom")):
            # Must not raise — the background POST fails silently.
            sent = config_telemetry.send_config_snapshot(
                "http://example.invalid/ingest", "n", "radar", {}, "config_apply")
        assert sent is True  # dispatch itself succeeded; the POST failure is swallowed

    def test_real_dispatch_uses_a_background_thread(self):
        """Without the sync_thread fixture, a real thread is spawned — confirms
        the caller (a synchronous Flask route) never blocks on the network call."""
        with patch("config_telemetry.requests.post") as mock_post:
            import time
            mock_post.side_effect = lambda *a, **k: time.sleep(0.2) or None
            started = time.monotonic()
            sent = config_telemetry.send_config_snapshot(
                "http://example.invalid/ingest", "n", "radar", {}, "config_apply")
            elapsed = time.monotonic() - started
        assert sent is True
        assert elapsed < 0.1  # returned long before the (mocked) 0.2s "network call" finished
