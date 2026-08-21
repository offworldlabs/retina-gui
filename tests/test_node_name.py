"""Tests for the operator-assigned node name."""

import os
import stat

import pytest

from node_name import MAX_LENGTH, NodeName


@pytest.fixture
def names(tmp_path):
    return NodeName(str(tmp_path / "node-name"), dev_mode=True)


def test_unset_reads_as_empty(names):
    assert names.get() == ""


def test_round_trip(names):
    assert names.set("Boston Rooftop") == (True, None)
    assert names.get() == "Boston Rooftop"


def test_surrounding_whitespace_is_dropped(names):
    names.set("  Roof  ")
    assert names.get() == "Roof"


def test_clearing_the_name_is_allowed(names):
    """Empty means "list me by identifier", which is a legitimate choice."""
    names.set("Roof")
    assert names.set("") == (True, None)
    assert names.get() == ""


def test_a_name_can_be_replaced(names):
    names.set("Old")
    names.set("New")
    assert names.get() == "New"


def test_too_long_is_refused(names):
    ok, error = names.set("x" * (MAX_LENGTH + 1))
    assert ok is False
    assert "characters" in error
    assert names.get() == "", "nothing written when validation fails"


def test_exactly_the_limit_is_allowed(names):
    assert names.set("x" * MAX_LENGTH)[0] is True


@pytest.mark.parametrize("bad", ['<script>', 'a & b', 'say "hi"', "it's",
                                 "line\nbreak", "tab\there", "nul\x00byte"])
def test_characters_that_would_break_the_advertisement_are_refused(names, bad):
    """These land in an XML service file and a DNS-SD TXT record. The identity
    script escapes them as a second line of defence, but no legitimate node
    name needs them, so they are refused outright here."""
    ok, error = names.set(bad)
    assert ok is False
    assert error


def test_unicode_is_fine(names):
    assert names.set("Rooftop Café Boston 🦉")[0] is True
    assert names.get() == "Rooftop Café Boston 🦉"


def test_the_file_is_world_readable(names):
    """owl-mdns-identity reads it at boot to build the advertisement."""
    names.set("Roof")
    mode = stat.S_IMODE(os.stat(names.name_file).st_mode)
    assert mode & stat.S_IROTH


def test_an_unwritable_location_reports_rather_than_raises(tmp_path):
    names = NodeName(str(tmp_path / "missing" / "sub" / "node-name"),
                     dev_mode=True)
    (tmp_path / "missing").mkdir(mode=0o500)
    try:
        ok, error = names.set("Roof")
    finally:
        (tmp_path / "missing").chmod(0o700)
    assert ok is False
    assert "Could not save" in error


def test_a_rename_re_advertises(tmp_path):
    """The rename only reaches other nodes when the identity script reruns."""
    called = []
    script = tmp_path / "owl-mdns-identity"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o755)

    names = NodeName(str(tmp_path / "node-name"), dev_mode=False,
                     identity_script=str(script))
    names._republish = lambda: called.append(True)
    names.set("Roof")
    assert called == [True]


def test_re_advertising_failure_does_not_lose_the_name(tmp_path, monkeypatch):
    """The name is saved before the script runs, so a failure there delays the
    rename reaching the network rather than dropping it."""
    script = tmp_path / "owl-mdns-identity"
    script.write_text("#!/bin/sh\nexit 1\n")
    script.chmod(0o755)

    def boom(*a, **k):
        raise OSError("no exec")

    monkeypatch.setattr("node_name.subprocess.run", boom)
    names = NodeName(str(tmp_path / "node-name"), dev_mode=False,
                     identity_script=str(script))
    assert names.set("Roof") == (True, None)
    assert names.get() == "Roof"


def test_dev_mode_does_not_shell_out(tmp_path, monkeypatch):
    monkeypatch.setattr("node_name.subprocess.run",
                        lambda *a, **k: pytest.fail("should not run"))
    names = NodeName(str(tmp_path / "node-name"), dev_mode=True)
    assert names.set("Roof")[0] is True


def test_a_missing_identity_script_is_not_an_error(tmp_path, monkeypatch):
    """Standalone installs have retina-gui without the owl-os units."""
    monkeypatch.setattr("node_name.subprocess.run",
                        lambda *a, **k: pytest.fail("should not run"))
    names = NodeName(str(tmp_path / "node-name"), dev_mode=False,
                     identity_script=str(tmp_path / "nope"))
    assert names.set("Roof")[0] is True


# ── Over HTTP ──────────────────────────────────────────────────

def test_saving_a_name_over_http(app_client):
    import app as app_module

    response = app_client.post("/node-name", data={"name": "Boston Rooftop"})
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "name": "Boston Rooftop"}
    assert app_module.node_name.get() == "Boston Rooftop"


def test_a_rejected_name_comes_back_as_a_400_with_a_reason(app_client):
    response = app_client.post("/node-name", data={"name": "<script>"})
    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["error"]


def test_the_name_appears_on_the_config_page(app_client):
    app_client.post("/node-name", data={"name": "Boston Rooftop"})
    body = app_client.get("/config").data.decode()
    assert "Boston Rooftop" in body


def test_the_config_page_shows_this_node_s_own_address(app_client):
    """owl.local is shared, so the address worth bookmarking is the node's."""
    body = app_client.get("/config").data.decode()
    assert "http://ret7dd2cb0d.local" in body


def test_renaming_is_not_offered_on_the_home_page(app_client):
    """It lives under Administration on the config page, with the other
    settings that save on their own rather than with the config form."""
    body = app_client.get("/", headers={"Host": "ret7dd2cb0d.local"}).data.decode()
    assert "nodeNameInput" not in body
