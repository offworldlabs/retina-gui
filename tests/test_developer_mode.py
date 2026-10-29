"""Tests for developer mode, the switch that keeps developer tooling off
customer nodes.

Without the flag file the Configuration page is the customer page and the
developer routes do not exist; with it they appear, with no restart.
See docs/features/config-editor.md#developer-mode.
"""

import os

import yaml


def config_page(app_client):
    return app_client.get("/config").data.decode()


def load(path):
    with open(path) as f:
        return yaml.safe_load(f)


# ── Off: the customer node ─────────────────────────────────────


def test_the_page_has_no_processing_section(app_client):
    page = config_page(app_client)
    assert '<section id="processing"' not in page
    assert 'href="#processing"' not in page
    assert "<h3>Testing</h3>" not in page


def test_the_page_has_no_processing_fields_or_script(app_client):
    page = config_page(app_client)
    assert 'name="process.' not in page
    assert "processingForm" not in page
    assert "data-readout" not in page


def test_the_page_is_not_marked_as_developer(app_client):
    assert "Developer mode" not in config_page(app_client)


def test_the_save_route_does_not_exist(app_client, user_config_file):
    before = load(user_config_file)
    response = app_client.post("/config/processing/save", data={
        "process.detection_minDoppler": "5", "process.detection_pfa": "1e-05"})
    assert response.status_code == 404
    assert load(user_config_file) == before


# ── On: a developer's node ─────────────────────────────────────


def test_the_section_marker_and_script_appear(app_client, developer_mode):
    page = config_page(app_client)
    assert '<section id="processing"' in page
    assert "Developer mode" in page
    assert "// Processing: live readouts" in page
    assert "var sectionIds = ['processing'," in page


def test_the_save_route_exists(app_client, developer_mode):
    response = app_client.post("/config/processing/save", data={})
    assert response.status_code == 200  # rejected as incomplete, but handled


def test_the_flag_takes_effect_without_a_restart(app_client, temp_dir):
    assert '<section id="processing"' not in config_page(app_client)
    flag = os.path.join(temp_dir, "developer-mode")
    open(flag, "w").close()
    assert '<section id="processing"' in config_page(app_client)
    os.remove(flag)
    assert '<section id="processing"' not in config_page(app_client)
