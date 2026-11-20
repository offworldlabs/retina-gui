"""Tests for the Processing section of the Configuration page.

It sits at the top of the page in its own form, and saves the detection
settings a test engineer tunes. It exists only in developer mode, which every
test here turns on; tests/test_developer_mode.py covers the node without it.
See docs/features/config-editor.md#processing-section.
"""

import re

import pytest
import yaml

from config_manager import ConfigManager
from config_schema import ProcessingFormConfig

pytestmark = pytest.mark.usefixtures("developer_mode")

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
    assert re.search(r'name="process.detection_pfa"[^>]*value="0.00001"', section)
    assert re.search(r'<option value="none" selected>none</option>', section)


def test_small_numbers_are_shown_in_plain_decimal(page):
    """1e-05 reads as a different setting from the 0.00001 the copy gives."""
    field = re.search(r'<input[^>]*name="process.detection_pfa"[^>]*>', processing(page)).group(0)
    assert 'value="0.00001"' in field
    assert 'min="0.0000001"' in field
    assert "e-0" not in field


def test_a_pfa_the_merger_wrote_as_text_is_shown_in_plain_decimal(app_client, config_files):
    """PyYAML reads an exponent without a dot ("1e-05") as a string."""
    _, merged_path = config_files
    merged = load(merged_path)
    merged["process"]["detection"]["pfa"] = "1e-05"
    with open(merged_path, "w") as f:
        yaml.dump(merged, f)
    assert re.search(r'name="process.detection_pfa"[^>]*value="0.00001"',
                     processing(app_client.get("/config").data.decode()))


@pytest.mark.parametrize("shown", ["0.00001", "1e-05"])
def test_the_value_the_page_shows_saves_back_unchanged(app_client, user_config_file, shown):
    response = post(app_client, detection_pfa=shown)
    assert response.status_code == 302
    assert "process" not in load(user_config_file)


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


# ── Apply changes saves this section too ───────────────────────

# What #configForm posts for the capture section when nothing on it changed.
CAPTURE_UNCHANGED = {
    "capture.fs": "2000000", "capture.fc": "503000000", "capture.device_type": "RspDuo",
    "capture.device_agcSetPoint": "-50", "capture.device_gainReductionA": "40",
    "capture.device_gainReductionB": "40", "capture.device_lnaState": "4",
    "capture.device_bandwidthNumber": "0",
}


def post_both(app_client, capture=None, **changes):
    data = dict(UNCHANGED)
    data.update({f"process.{k}": v for k, v in changes.items()})
    data.update(CAPTURE_UNCHANGED)
    data.update(capture or {})
    return app_client.post("/config/save", data=data, follow_redirects=False)


def test_apply_changes_saves_processing_edits_posted_with_it(app_client, user_config_file):
    """The page sends the section along when it has edits; before, pressing
    Apply changes dropped them without a word."""
    response = post_both(app_client, detection_minDoppler="5", ambiguity_dopplerWindow="hann")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/config?saved=1")
    assert load(user_config_file)["process"] == {
        "detection": {"minDoppler": 5}, "ambiguity": {"dopplerWindow": "hann"}}


def test_apply_changes_saves_both_forms_in_one_write(app_client, user_config_file):
    response = post_both(app_client, capture={"capture.fc": "545000000"}, detection_minDoppler="5")
    assert response.status_code == 302
    saved = load(user_config_file)
    assert saved["process"] == {"detection": {"minDoppler": 5}}
    assert saved["capture"]["fc"] == 545000000


def test_apply_changes_without_processing_fields_leaves_the_section_alone(app_client, first_boot_user):
    before = load(first_boot_user)["process"]
    response = app_client.post("/config/save", data=dict(CAPTURE_UNCHANGED, **{"capture.fc": "545000000"}))
    assert response.status_code == 302
    assert load(first_boot_user)["process"] == before


def test_apply_changes_keeps_process_keys_the_section_does_not_edit(app_client, first_boot_user):
    post_both(app_client, detection_minDoppler="5")
    saved = load(first_boot_user)["process"]
    assert saved["data"] == {"cpi": 0.5, "buffer": 1.5, "overlap": 0}
    assert saved["detection"]["minDoppler"] == 5


def test_a_bad_processing_value_refuses_the_whole_apply(app_client, user_config_file):
    """Refuse before writing: the other sections must not be saved either."""
    before = load(user_config_file)
    response = post_both(app_client, capture={"capture.fc": "545000000"}, detection_nTrain="0",
                         detection_minDoppler="7")
    body = refused(response, user_config_file, before)
    section = processing(body)
    assert re.search(r'name="process.detection_nTrain"[^>]*is-invalid', section)
    assert re.search(r'name="process.detection_minDoppler"[^>]*value="7"', section), \
        "the submission is shown, not the running value"
    assert re.search(r'name="capture.fc"[^>]*value="545000000"', body)


def test_a_bad_value_elsewhere_keeps_the_processing_edit_pending(app_client, user_config_file):
    """Nothing in the section is marked, so the form itself says its values
    are not the saved ones and the next Apply changes sends them again."""
    before = load(user_config_file)
    response = post_both(app_client, capture={"capture.fc": "not-a-number"}, detection_minDoppler="7")
    body = refused(response, user_config_file, before)
    assert re.search(r'<form[^>]*id="processingForm"[^>]*data-submitted', body)
    assert re.search(r'name="process.detection_minDoppler"[^>]*value="7"', processing(body))


def test_the_form_is_not_marked_submitted_on_a_plain_load(page):
    assert not re.search(r'<form[^>]*id="processingForm"[^>]*data-submitted', page)


def test_a_save_refused_by_a_calibration_keeps_the_edit_pending(app_client, user_config_file, monkeypatch):
    import app as app_module
    monkeypatch.setattr(app_module.calibrator, "is_running", lambda: True)
    body = post(app_client, detection_minDoppler="5").data.decode()
    assert re.search(r'<form[^>]*id="processingForm"[^>]*data-submitted', body)


def test_apply_changes_ignores_processing_fields_outside_developer_mode(app_client, user_config_file):
    import os

    import app as app_module
    os.remove(app_module.device_state.developer_mode_flag)
    response = post_both(app_client, detection_minDoppler="5")
    assert response.status_code == 302
    assert "process" not in load(user_config_file)


def test_the_page_says_apply_changes_covers_the_section(page):
    assert "Apply changes at the bottom of the page saves this section too." in processing(page)
    assert "does not save this one" not in page


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
