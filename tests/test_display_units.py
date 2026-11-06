"""The node's display units: metric or imperial, as display.units in user.yml.

A presentation choice written to user.yml without an apply, shared by the
Display section and the wizard. See docs/features/config-editor.md#display-units.
"""

import pytest
import yaml

from config_manager import ConfigManager
from display_units import DisplayUnits


@pytest.fixture
def user_yml(tmp_path):
    return tmp_path / "user.yml"


@pytest.fixture
def units(user_yml, tmp_path):
    mgr = ConfigManager(str(user_yml), str(tmp_path / "config.yml"), str(tmp_path / "node"))
    return DisplayUnits(mgr)


def stored(user_yml):
    return yaml.safe_load(user_yml.read_text()) if user_yml.exists() else None


def test_unset_reads_as_metric(units):
    assert units.get() == "metric"


def test_round_trip(units, user_yml):
    assert units.set("imperial") == (True, None)
    assert units.get() == "imperial"
    assert stored(user_yml) == {"display": {"units": "imperial"}}
    assert units.set("metric") == (True, None)
    assert units.get() == "metric"


def test_the_rest_of_user_yml_is_kept(units, user_yml):
    """The owner's overrides sit beside the key, untouched."""
    user_yml.write_text("capture:\n  fc: 503000000\nlocation:\n  rx:\n    name: Example RX\n")
    units.set("imperial")
    assert stored(user_yml) == {
        "capture": {"fc": 503000000},
        "location": {"rx": {"name": "Example RX"}},
        "display": {"units": "imperial"},
    }


def test_other_keys_in_the_display_section_are_kept(units, user_yml):
    user_yml.write_text("display:\n  theme: dark\n")
    units.set("imperial")
    assert stored(user_yml) == {"display": {"theme": "dark", "units": "imperial"}}


@pytest.mark.parametrize("bad", ["", None, "Imperial", "miles", "metric\nimperial", 7])
def test_anything_but_the_two_names_is_refused(units, user_yml, bad):
    ok, error = units.set(bad)
    assert ok is False
    assert "metric or imperial" in error
    assert units.get() == "metric"
    assert not user_yml.exists()


def test_a_hand_edited_value_reads_as_metric(units, user_yml):
    """An unknown word must not put a page into an unknown unit."""
    user_yml.write_text("display:\n  units: furlongs\n")
    assert units.get() == "metric"


def test_a_broken_user_yml_reads_as_metric_and_reports_on_write(units, user_yml):
    user_yml.write_text("display: [unclosed\n")
    assert units.get() == "metric"
    ok, error = units.set("imperial")
    assert ok is False
    assert "Could not save" in error


def test_an_unwritable_location_reports_rather_than_raises(units, monkeypatch):
    def refuse(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr(units.config_mgr, "save_user_config", refuse)
    ok, error = units.set("imperial")
    assert ok is False
    assert "Could not save" in error
    assert units.get() == "metric"


# ── Over HTTP ──────────────────────────────────────────────────

def test_saving_units_over_http(app_client):
    import app as app_module

    response = app_client.post("/display-units", json={"units": "imperial"})
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "units": "imperial"}
    assert app_module.display_units.get() == "imperial"
    assert app_module.config_mgr.load_user_config()["display"] == {"units": "imperial"}


def test_saving_units_queues_no_apply(app_client, monkeypatch):
    """A display choice must never restart the stack."""
    import app as app_module
    calls = []
    monkeypatch.setattr(app_module.apply_service, "request",
                        lambda *a, **k: calls.append(1))
    app_client.post("/display-units", json={"units": "imperial"})
    assert calls == []
    assert app_module.apply_service.is_running() is False


@pytest.mark.parametrize("body", [{"units": "furlongs"}, {}, None])
def test_a_rejected_choice_comes_back_as_a_400_with_a_reason(app_client, body):
    response = app_client.post("/display-units", json=body)
    assert response.status_code == 400
    payload = response.get_json()
    assert payload["ok"] is False
    assert payload["error"]


def test_the_config_page_shows_the_stored_choice(app_client):
    body = app_client.get("/config").data.decode()
    assert 'data-units="metric">Metric' in body
    assert '<div class="name" id="unitsLabel">Metric</div>' in body

    app_client.post("/display-units", json={"units": "imperial"})
    body = app_client.get("/config").data.decode()
    assert '<div class="name" id="unitsLabel">Imperial</div>' in body
    # The pressed button is the stored one.
    assert 'active" data-units="imperial"' in body
    assert 'active" data-units="metric"' not in body


def test_the_choice_survives_a_rejected_config_save(app_client):
    """The validation-error branch of /config/save renders the page too, and
    must carry the same choice."""
    app_client.post("/display-units", json={"units": "imperial"})
    # A partial location is refused and re-renders the page.
    response = app_client.post("/config/save", data={"location.rx_latitude": "40.0"})
    assert response.status_code == 200
    body = response.data.decode()
    assert '<div class="name" id="unitsLabel">Imperial</div>' in body
