"""Re-applying the remote shell agreement when the GUI starts.

The owner's choice lives in /data and survives an OS update. The enforcement
lives in /etc/mender/mender-connect.conf on the A/B rootfs and does not: an
update restores owl-os's template, where both gated features are enabled.

Before this ran at startup, an owner who declined a shell had it silently
restored by the next update, while the marker, the config page and the
`remote_shell` inventory attribute all still said declined.
"""

import pytest

import app as app_module


class FakeRemoteAccess:
    def __init__(self, allowed):
        self._allowed = allowed

    def is_shell_allowed(self):
        return self._allowed


class FakeMenderConnect:
    """Records what startup asked of it."""

    def __init__(self, enabled, ok=True, error=None):
        self._enabled = enabled
        self.ok = ok
        self.error = error
        self.calls = []

    def is_shell_enabled(self):
        return self._enabled

    def set_shell_enabled(self, enabled):
        self.calls.append(enabled)
        return (self.ok, self.error)


@pytest.fixture
def startup(monkeypatch):
    """Runs _reapply_shell_agreement against fakes, as a node would."""
    def run(*, recorded, on_disk, ok=True, error=None):
        mc = FakeMenderConnect(on_disk, ok=ok, error=error)
        monkeypatch.setattr(app_module, "DEV_MODE", False)
        monkeypatch.setattr(app_module, "remote_access", FakeRemoteAccess(recorded))
        monkeypatch.setattr(app_module, "mender_connect", mc)
        app_module._reapply_shell_agreement()
        return mc
    return run


# ── the repair ───────────────────────────────────────────────────

def test_an_update_that_re_enabled_the_shell_is_repaired(startup):
    """The defect this exists for: owner declined, update reverted it."""
    mc = startup(recorded=False, on_disk=True)
    assert mc.calls == [False]


def test_the_reverse_mismatch_is_also_repaired(startup):
    """Less likely, but the marker is the authority in both directions. A node
    refusing sessions its owner allows is a support call nobody can diagnose."""
    mc = startup(recorded=True, on_disk=False)
    assert mc.calls == [True]


# ── leaving well alone ───────────────────────────────────────────

def test_a_config_that_already_agrees_is_not_rewritten(startup):
    """Writing anyway would restart mender-connect on every boot, dropping any
    live session, to change nothing."""
    assert startup(recorded=True, on_disk=True).calls == []
    assert startup(recorded=False, on_disk=False).calls == []


def test_an_unreadable_config_is_not_replaced(startup):
    """None means we could not read it. mender_connect deliberately refuses to
    invent a replacement, and startup must not talk it into one: a reconstructed
    config would carry transfer limits and a shell user we have no basis for."""
    assert startup(recorded=False, on_disk=None).calls == []


def test_dev_mode_does_nothing(monkeypatch):
    mc = FakeMenderConnect(True)
    monkeypatch.setattr(app_module, "DEV_MODE", True)
    monkeypatch.setattr(app_module, "remote_access", FakeRemoteAccess(False))
    monkeypatch.setattr(app_module, "mender_connect", mc)
    app_module._reapply_shell_agreement()
    assert mc.calls == []


# ── never breaking the boot ──────────────────────────────────────

def test_a_failed_repair_is_logged_not_raised(startup, caplog):
    """The GUI must still come up. A node that will not serve its own config
    page is worse than one whose shell setting needs a click."""
    mc = startup(recorded=False, on_disk=True, ok=False, error="unit not found")
    assert mc.calls == [False]
    assert any("Could not re-apply" in r.getMessage() for r in caplog.records)


def test_an_exception_does_not_stop_startup(monkeypatch):
    class Exploding:
        def is_shell_allowed(self):
            raise RuntimeError("disk gone")

    monkeypatch.setattr(app_module, "DEV_MODE", False)
    monkeypatch.setattr(app_module, "remote_access", Exploding())
    monkeypatch.setattr(app_module, "mender_connect", FakeMenderConnect(True))
    app_module._reapply_shell_agreement()  # must not raise


def test_a_repair_says_so_in_the_log(startup, caplog):
    """Silent repairs hide a real event: this only happens after an update
    reverted somebody's choice, and that is worth a line in the journal."""
    startup(recorded=False, on_disk=True)
    assert any("Re-applied the remote shell agreement" in r.getMessage()
               for r in caplog.records)
