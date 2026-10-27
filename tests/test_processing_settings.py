"""Tests for the Processing section of the Configuration page.

It sits at the top of the page in its own form, and saves the detection
settings a test engineer tunes. See docs/features/config-editor.md#processing-section.
"""

import re

import pytest
import yaml

from config_manager import ConfigManager
from config_schema import ProcessingFormConfig

# What the section posts when nothing on it has been changed, matching
# conftest's sample_merged_config.
UNCHANGED = {
    "process.detection_minDoppler": "15",
    "process.detection_maxReportedDoppler": "1000",
    "process.detection_minDelay": "5",
    "process.ambiguity_delayMax": "400",
    "process.detection_pfa": "1e-05",
    "process.detection_nTrain": "6",
    "process.detection_nGuard": "2",
    "process.detection_nCentroid": "6",
    "process.ambiguity_dopplerWindow": "none",
    "process.clutter_enable": "on",
    "process.ambiguity_dopplerMin": "-1000",
    "process.ambiguity_dopplerMax": "1000",
    "process.data_cpi": "0.5",
    "process.clutter_delayMax": "400",
}

# A first-boot user.yml holds a whole copy of default.yml's process section.
FIRST_BOOT_PROCESS = {
    "data": {"cpi": 0.5, "buffer": 1.5, "overlap": 0},
    "ambiguity": {"delayMin": -10, "delayMax": 400, "dopplerMin": -1000, "dopplerMax": 1000},
    "clutter": {"enable": True, "delayMin": -10, "delayMax": 400},
    "detection": {"enable": True, "pfa": 0.00001, "nGuard": 2, "nTrain": 6,
                  "minDelay": 5, "minDoppler": 15, "nCentroid": 6},
}


def post(app_client, **changes):
    data = dict(UNCHANGED)
    data.update({f"process.{k}": v for k, v in changes.items()})
    return app_client.post("/config/processing/save", data=data, follow_redirects=False)


def load(path):
    with open(path) as f:
        return yaml.safe_load(f)


@pytest.fixture
def first_boot_user(user_config_file):
    config = load(user_config_file)
    config["process"] = FIRST_BOOT_PROCESS
    config["other_section"] = {"foo": "bar"}
    with open(user_config_file, "w") as f:
        yaml.dump(config, f)
    return user_config_file


@pytest.fixture
def page(app_client):
    return app_client.get("/config").data.decode()


def processing(page):
    return page.split('<section id="processing"')[1].split("</section>")[0]


# ── The page ───────────────────────────────────────────────────


def test_the_section_is_the_first_on_the_page(page):
    assert re.findall(r'<section id="([^"]+)"', page)[0] == "processing"


def test_the_fields_show_the_running_values(page):
    section = processing(page)
    assert re.search(r'name="process.detection_minDoppler"[^>]*value="15', section)
    assert re.search(r'name="process.detection_pfa"[^>]*value="1e-05"', section)
    assert re.search(r'<option value="none" selected>none</option>', section)


def test_every_editable_number_has_a_readout(page):
    section = processing(page)
    for name in ("detection_minDoppler", "detection_maxReportedDoppler", "detection_minDelay",
                 "ambiguity_delayMax", "detection_pfa", "detection_nTrain",
                 "detection_nGuard", "detection_nCentroid"):
        assert f'data-readout="process.{name}"' in section, name


def test_the_fixed_settings_are_shown_locked(page):
    section = processing(page)
    for name in ("ambiguity_dopplerMin", "ambiguity_dopplerMax", "data_cpi", "clutter_delayMax"):
        assert re.search(rf'<input type="hidden" name="process.{name}"', section), name
        assert not re.search(rf'<input type="number" name="process.{name}"', section), name


def test_save_settings_are_not_on_the_page(page):
    assert 'name="save.' not in page


def test_save_and_apply_starts_disabled(page):
    assert re.search(r'id="processingSaveBtn" disabled', processing(page))


def test_a_node_without_a_process_section_still_renders(app_client, config_files):
    _, merged_path = config_files
    merged = load(merged_path)
    del merged["process"]
    with open(merged_path, "w") as f:
        yaml.dump(merged, f)
    response = app_client.get("/config")
    assert response.status_code == 200
    assert 'name="process.detection_minDoppler"' in response.data.decode()


# ── Saving ─────────────────────────────────────────────────────


def test_a_save_writes_only_what_changed_and_applies(app_client, user_config_file):
    response = post(app_client, detection_minDoppler="5", detection_maxReportedDoppler="150")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/config?saved=1")
    assert load(user_config_file)["process"] == {
        "detection": {"minDoppler": 5, "maxReportedDoppler": 150}}


def test_an_unchanged_save_writes_nothing(app_client, user_config_file):
    post(app_client)
    assert "process" not in load(user_config_file)


def test_a_save_keeps_process_keys_the_section_does_not_edit(app_client, first_boot_user):
    post(app_client, detection_minDoppler="5", ambiguity_dopplerWindow="hann")
    saved = load(first_boot_user)
    assert saved["process"]["data"] == {"cpi": 0.5, "buffer": 1.5, "overlap": 0}
    assert saved["process"]["ambiguity"]["dopplerMin"] == -1000
    assert saved["process"]["clutter"]["delayMin"] == -10
    assert saved["process"]["detection"]["enable"] is True
    assert saved["process"]["detection"]["minDoppler"] == 5
    assert saved["process"]["ambiguity"]["dopplerWindow"] == "hann"
    assert saved["other_section"] == {"foo": "bar"}


def test_a_save_keeps_the_other_sections(app_client, user_config_file):
    before = load(user_config_file)
    post(app_client, detection_minDoppler="5")
    after = load(user_config_file)
    del after["process"]
    assert after == before


def test_the_fixed_settings_are_never_written(app_client, user_config_file):
    post(app_client, ambiguity_dopplerMin="-150", ambiguity_dopplerMax="150",
         data_cpi="0.25", clutter_delayMax="100", detection_minDoppler="5")
    assert load(user_config_file)["process"] == {"detection": {"minDoppler": 5}}


@pytest.mark.parametrize("typed", ["4e-05", "4e-5", "0.00004"])
def test_pfa_is_stored_as_a_number_however_it_is_typed(app_client, user_config_file, typed):
    post(app_client, detection_pfa=typed)
    assert load(user_config_file)["process"]["detection"]["pfa"] == pytest.approx(4e-5)


def test_an_unchecked_clutter_filter_is_saved_as_off(app_client, user_config_file):
    data = dict(UNCHANGED)
    del data["process.clutter_enable"]
    app_client.post("/config/processing/save", data=data)
    assert load(user_config_file)["process"] == {"clutter": {"enable": False}}


def test_an_empty_optional_setting_is_not_written(app_client, config_files, user_config_file):
    """A node still on a default.yml without the newer keys shows them empty."""
    _, merged_path = config_files
    merged = load(merged_path)
    del merged["process"]["detection"]["maxReportedDoppler"]
    del merged["process"]["ambiguity"]["dopplerWindow"]
    with open(merged_path, "w") as f:
        yaml.dump(merged, f)
    post(app_client, detection_maxReportedDoppler="", detection_minDoppler="5")
    assert load(user_config_file)["process"]["detection"] == {"minDoppler": 5}


# ── Refusals ───────────────────────────────────────────────────


def refused(response, user_config_file, before):
    assert response.status_code == 200
    assert load(user_config_file) == before
    return response.data.decode()


def test_a_report_limit_below_the_minimum_is_refused(app_client, user_config_file):
    """blah2 refuses to start on this, so it must never reach user.yml."""
    before = load(user_config_file)
    body = refused(post(app_client, detection_minDoppler="20", detection_maxReportedDoppler="10"),
                   user_config_file, before)
    assert "must be at least Minimum Doppler" in body
    assert re.search(r'name="process.detection_maxReportedDoppler"[^>]*is-invalid'
                     r'|is-invalid[^>]*name="process.detection_maxReportedDoppler"', body)


def test_a_minimum_delay_at_or_past_the_maximum_is_refused(app_client, user_config_file):
    before = load(user_config_file)
    body = refused(post(app_client, detection_minDelay="100", ambiguity_delayMax="100"),
                   user_config_file, before)
    assert "must be below Maximum Delay" in body


@pytest.mark.parametrize("name,value", [
    ("detection_nTrain", "0"),
    ("detection_pfa", "0.5"),
    ("detection_minDoppler", "-1"),
    ("ambiguity_delayMax", "5000"),
    ("ambiguity_dopplerWindow", "blackman"),
])
def test_an_out_of_range_value_is_refused(app_client, user_config_file, name, value):
    before = load(user_config_file)
    body = refused(post(app_client, **{name: value}), user_config_file, before)
    assert "Please fix the highlighted fields below." in body


def test_a_refused_save_shows_what_was_submitted(app_client, user_config_file):
    body = post(app_client, detection_nTrain="0", detection_minDoppler="7").data.decode()
    assert re.search(r'name="process.detection_minDoppler"[^>]*value="7"', processing(body))


def test_a_save_is_refused_while_calibrating(app_client, user_config_file, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module.calibrator, "is_running", lambda: True)
    before = load(user_config_file)
    body = refused(post(app_client, detection_minDoppler="5"), user_config_file, before)
    assert "Auto-calibration is running" in body


# ── Helpers ────────────────────────────────────────────────────


def test_merge_drops_an_edited_key_that_has_no_override():
    merged = ConfigManager.merge_processing_overrides(
        {"detection": {"minDoppler": 5, "enable": True}, "data": {"cpi": 0.5}}, None)
    assert merged == {"detection": {"enable": True}, "data": {"cpi": 0.5}}


def test_the_model_accepts_the_shipped_defaults():
    flat = ConfigManager.flatten_processing_for_form(FIRST_BOOT_PROCESS)
    flat["detection_maxReportedDoppler"] = 1000
    ProcessingFormConfig(**flat)
