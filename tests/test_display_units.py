"""The node's display units: metric or imperial.

A presentation choice stored on /data and shared by every page, with a route
that saves on click. See docs/features/config-editor.md#display-units.
"""

import os

import pytest

from display_units import DisplayUnits


@pytest.fixture
def units(tmp_path):
    return DisplayUnits(str(tmp_path / "display-units"))


def test_unset_reads_as_metric(units):
    assert units.get() == "metric"


def test_round_trip(units):
    assert units.set("imperial") == (True, None)
    assert units.get() == "imperial"
    assert units.set("metric") == (True, None)
    assert units.get() == "metric"


@pytest.mark.parametrize("bad", ["", None, "Imperial", "miles", "metric\nimperial", 7])
def test_anything_but_the_two_names_is_refused(units, bad):
    ok, error = units.set(bad)
    assert ok is False
    assert "metric or imperial" in error
    assert units.get() == "metric"


def test_a_corrupt_file_reads_as_metric(units):
    """A hand-edited or truncated file must not put a page into an unknown unit."""
    with open(units.units_file, "w") as f:
        f.write("furlongs")
    assert units.get() == "metric"


def test_surrounding_whitespace_in_the_file_is_tolerated(units):
    with open(units.units_file, "w") as f:
        f.write("imperial\n")
    assert units.get() == "imperial"


def test_the_file_is_world_readable(units):
    units.set("imperial")
    assert oct(os.stat(units.units_file).st_mode & 0o777) == "0o644"


def test_an_unwritable_location_reports_rather_than_raises(tmp_path, monkeypatch):
    units = DisplayUnits(str(tmp_path / "display-units"))

    def refuse(*a, **k):
        raise OSError("read-only file system")

    monkeypatch.setattr("display_units.tempfile.mkstemp", refuse)
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
