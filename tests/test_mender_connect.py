"""Tests for enforcing the remote shell agreement.

This module is what makes the agreement true rather than merely recorded, so
the tests that matter are the ones asserting what it must never do: leave the
two gated features disagreeing, touch file transfer, or invent a config it
could not read.
"""

import json
import os
import stat

import pytest

from mender_connect import GATED_FEATURES, MenderConnect

# Mirrors what the OS image ships, including the parts that must survive a write.
SHIPPED_CONF = {
    "ReconnectIntervalSeconds": 5,
    "SkipVerify": False,
    "Limits": {
        "Enabled": True,
        "FileTransfer": {
            "Chroot": "/home/node",
            "OwnerGet": ["node"],
            "MaxFileSize": 536870912,
        },
    },
    "FileTransfer": {"Disable": False},
    "MenderClient": {"Disable": False},
    "PortForward": {"Disable": False},
    "ShellCommand": "/usr/bin/bash",
    "ShellArguments": [],
    "Sessions": {"ExpireAfterIdle": 3600, "MaxPerUser": 5},
    "Terminal": {"Disable": False, "Height": 40, "Width": 80},
    "User": "node",
}


@pytest.fixture
def conf(tmp_path):
    path = tmp_path / "mender-connect.conf"
    path.write_text(json.dumps(SHIPPED_CONF, indent=2))
    os.chmod(path, 0o644)
    return path


@pytest.fixture
def connect(conf, monkeypatch):
    """A real MenderConnect with systemctl stubbed, recording what it was asked."""
    import mender_connect as module

    calls = []

    class Result:
        returncode = 0
        stderr = b""

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Result()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    mc = MenderConnect(conf_path=str(conf))
    mc.calls = calls
    return mc


def _load(conf):
    return json.loads(conf.read_text())


# ── reading ──────────────────────────────────────────────────────

def test_shipped_config_reads_as_allowed(connect):
    assert connect.is_shell_enabled() is True


def test_either_feature_disabled_reads_as_declined(conf, connect):
    """Not "both". A config with one of them off is not honouring the agreement,
    and reporting it as allowed would hide a half-applied state."""
    for feature in GATED_FEATURES:
        data = dict(SHIPPED_CONF)
        data[feature] = {"Disable": True}
        conf.write_text(json.dumps(data))
        assert connect.is_shell_enabled() is False, feature


def test_unreadable_config_reads_as_unknown(tmp_path):
    """None, not False. "We cannot tell" and "the owner declined" are different
    facts and a boolean would collapse them."""
    missing = MenderConnect(conf_path=str(tmp_path / "nope.conf"))
    assert missing.is_shell_enabled() is None

    broken = tmp_path / "broken.conf"
    broken.write_text("{ not json")
    assert MenderConnect(conf_path=str(broken)).is_shell_enabled() is None


# ── writing ──────────────────────────────────────────────────────

def test_declining_disables_both_features(conf, connect):
    assert connect.set_shell_enabled(False) == (True, None)
    data = _load(conf)
    for feature in GATED_FEATURES:
        assert data[feature]["Disable"] is True, feature


def test_allowing_enables_both_features(conf, connect):
    connect.set_shell_enabled(False)
    assert connect.set_shell_enabled(True) == (True, None)
    data = _load(conf)
    for feature in GATED_FEATURES:
        assert data[feature]["Disable"] is False, feature


def test_file_transfer_is_never_touched(conf, connect):
    """The load-bearing one. File transfer delivers the Cloudflare tunnel token,
    so gating it here would make the two agreements dependent: a node whose
    owner declined the shell could never receive a support tunnel."""
    for allowed in (False, True, False):
        connect.set_shell_enabled(allowed)
        assert _load(conf)["FileTransfer"] == {"Disable": False}


def test_everything_else_survives(conf, connect):
    """The file carries transfer limits, the shell user and session caps. A
    write that dropped them would widen or narrow what a session may do, behind
    a setting that says nothing about either."""
    connect.set_shell_enabled(False)
    data = _load(conf)
    for key in ("Limits", "MenderClient", "ShellCommand", "ShellArguments",
                "Sessions", "User", "ReconnectIntervalSeconds", "SkipVerify"):
        assert data[key] == SHIPPED_CONF[key], key


def test_a_feature_missing_from_the_config_is_added(conf, connect):
    data = {k: v for k, v in SHIPPED_CONF.items() if k != "PortForward"}
    conf.write_text(json.dumps(data))
    connect.set_shell_enabled(False)
    assert _load(conf)["PortForward"]["Disable"] is True


def test_sibling_keys_within_a_gated_feature_survive(conf, connect):
    """Terminal carries Height and Width alongside Disable."""
    connect.set_shell_enabled(False)
    terminal = _load(conf)["Terminal"]
    assert terminal["Height"] == 40 and terminal["Width"] == 80


def test_the_file_mode_is_preserved(conf, connect):
    connect.set_shell_enabled(False)
    assert stat.S_IMODE(os.stat(conf).st_mode) == 0o644


def test_the_service_is_restarted(connect):
    """mender-connect reads its config only at startup, so without this the
    daemon keeps serving the previous answer while the file claims otherwise."""
    connect.set_shell_enabled(False)
    assert connect.calls == [["systemctl", "restart", "mender-connect"]]


# ── refusing ─────────────────────────────────────────────────────

def test_a_missing_config_is_refused_not_created(tmp_path, monkeypatch):
    """Writing a plausible replacement would invent transfer limits and a shell
    user we have no business reconstructing, and could silently widen access."""
    import mender_connect as module
    monkeypatch.setattr(module.subprocess, "run",
                        lambda *a, **k: pytest.fail("must not restart"))

    path = tmp_path / "absent.conf"
    ok, error = MenderConnect(conf_path=str(path)).set_shell_enabled(False)
    assert ok is False
    assert "missing or unreadable" in error
    assert not path.exists()


def test_an_unparseable_config_is_left_alone(tmp_path, monkeypatch):
    import mender_connect as module
    monkeypatch.setattr(module.subprocess, "run",
                        lambda *a, **k: pytest.fail("must not restart"))

    path = tmp_path / "broken.conf"
    path.write_text("{ not json")
    ok, error = MenderConnect(conf_path=str(path)).set_shell_enabled(False)
    assert ok is False
    assert path.read_text() == "{ not json"


def test_a_failed_restart_is_reported(conf, monkeypatch):
    """The file is already written at that point, so the caller must not record
    the choice as applied."""
    import mender_connect as module

    class Failed:
        returncode = 1
        stderr = b"Unit mender-connect.service not found."

    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: Failed())
    ok, error = MenderConnect(conf_path=str(conf)).set_shell_enabled(False)
    assert ok is False
    assert "not found" in error


def test_dev_mode_writes_without_restarting(tmp_path, monkeypatch):
    import mender_connect as module
    monkeypatch.setattr(module.subprocess, "run",
                        lambda *a, **k: pytest.fail("must not restart off-device"))

    path = tmp_path / "dev.conf"
    mc = MenderConnect(conf_path=str(path), dev_mode=True)
    assert mc.set_shell_enabled(False) == (True, None)
    assert mc.is_shell_enabled() is False


# ── surviving an OS update ───────────────────────────────────────
#
# The owner's choice lives in /data and survives an update. The enforcement
# lives on the rootfs and does not: owl-os ships mender-connect.conf with both
# gated features enabled, so an update silently restores a shell the owner had
# declined, while the marker, the config page and the inventory attribute all
# still reported it declined.

def _shipped(conf):
    """Put the config back exactly as owl-os ships it, which is what an OS
    update does to the rootfs."""
    conf.write_text(json.dumps(SHIPPED_CONF, indent=2))


def test_startup_reapplies_a_declined_agreement(conf, connect):
    """The defect, from the owner's side: they declined, an update happened,
    and without this the daemon would accept sessions again."""
    connect.set_shell_enabled(False)
    assert connect.is_shell_enabled() is False

    _shipped(conf)  # the update
    assert connect.is_shell_enabled() is True, "precondition: update re-enabled it"

    # What startup does: recorded choice wins over whatever is on disk.
    connect.set_shell_enabled(False)
    data = _load(conf)
    for feature in GATED_FEATURES:
        assert data[feature]["Disable"] is True, feature


def test_reapplying_still_leaves_file_transfer_alone(conf, connect):
    """A repair must not widen or narrow anything the agreement does not cover.
    File transfer carries the tunnel token, so gating it here would couple the
    two agreements together through the back door."""
    _shipped(conf)
    connect.set_shell_enabled(False)
    assert _load(conf)["FileTransfer"] == {"Disable": False}


def test_reapplying_preserves_what_the_update_shipped(conf, connect):
    """The update's config is the authority on everything except the two gated
    features: transfer limits, the shell user, session caps. A repair that
    reverted those would undo part of the update it is reacting to."""
    updated = dict(SHIPPED_CONF)
    updated["Sessions"] = {"ExpireAfterIdle": 7200, "MaxPerUser": 9}
    conf.write_text(json.dumps(updated))

    connect.set_shell_enabled(False)
    assert _load(conf)["Sessions"] == {"ExpireAfterIdle": 7200, "MaxPerUser": 9}
