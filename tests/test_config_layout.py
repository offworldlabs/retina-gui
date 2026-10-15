"""Tests for how the Configuration page is laid out.

The page is grouped around how an owner thinks about the node. These hold the
order, and the property the grouping must not break: every radar field is
inside the form Apply changes submits, and nothing else is.
See docs/features/config-editor.md#page-layout.
"""

import re

import pytest

# The side nav, as (group, [(section id, label), ...]).
LAYOUT = [
    ("Node", [("identifiers", "Identifiers"), ("mode", "Mode"), ("claim", "Node Claim"),
              ("network", "Network")]),
    ("Radar", [("cached-towers", "Cached Towers"), ("tower", "Tower"), ("capture", "Capture"),
               ("tracking", "Tracking")]),
    ("ADS-B", [("truth", "ADS-B Truth"), ("tar1090", "tar1090")]),
    ("Other", [("ssh", "SSH Access"), ("wizard", "Setup Wizard")]),
    ("Support", [("cloud", "Cloud Services"), ("remote", "Remote Support"),
                 ("contact", "How We Reach You")]),
]
# The groups Apply changes saves: their sections are the form's, in this order.
FORM_GROUPS = ("Radar", "ADS-B")
SECTION_IDS = [section for _, sections in LAYOUT for section, _ in sections]


class FakeTelemetry:
    """Stands in for the reader of retina-telemetry's status document."""

    def __init__(self, status):
        self._status = status

    def read(self):
        return self._status


@pytest.fixture
def telemetry(monkeypatch):
    """Control what this node believes about its own telemetry."""
    import app as app_module

    def set_status(status):
        monkeypatch.setattr(app_module, "telemetry_status", FakeTelemetry(status))

    set_status(None)
    return set_status


@pytest.fixture
def page(app_client, telemetry):
    return app_client.get("/config").data.decode()


def section(body, section_id):
    """One section's markup, from its opening tag to its closing one."""
    return body.split(f'<section id="{section_id}"')[1].split("</section>")[0]


def named_fields(markup):
    """Every opening tag of a field that would be posted, as raw text."""
    return re.findall(r'<(?:input|select|textarea)\b[^>]*\bname="[^"]+"[^>]*>', markup)


def field_name(tag):
    return re.search(r'name="([^"]+)"', tag).group(1)


# ── Order ──────────────────────────────────────────────────────


def test_the_side_nav_is_grouped_as_designed(page):
    nav = page.split('<aside class="side"')[1].split("</aside>")[0]
    found = re.findall(r'<h3>([^<]+)</h3>|<a href="#([^"]+)"[^>]*>([^<]+)</a>', nav)
    groups = []
    for heading, section_id, label in found:
        if heading:
            groups.append((heading, []))
        else:
            groups[-1][1].append((section_id, label))
    assert groups == LAYOUT


def test_the_page_is_in_the_same_order_as_the_side_nav(page):
    assert re.findall(r'<section id="([^"]+)" class="cfg-section"', page) == SECTION_IDS


def test_each_group_is_titled_in_the_page_above_its_first_section(page):
    main = page.split("<main>")[1].split("</main>")[0]
    found = re.findall(r'<h2 class="cfg-group">([^<]+)</h2>|<section id="([^"]+)" class="cfg-section"', main)
    groups = []
    for title, section_id in found:
        if title:
            groups.append((title, []))
        else:
            groups[-1][1].append(section_id)
    assert groups == [(title, [section_id for section_id, _ in sections])
                      for title, sections in LAYOUT]


def test_each_section_is_headed_with_its_side_nav_label(page):
    """One name per section, written the same in both places."""
    for _, sections in LAYOUT:
        for section_id, label in sections:
            assert f"<h2>{label}</h2>" in section(page, section_id), section_id


def test_the_scrollspy_knows_every_section_in_page_order(page):
    listed = re.search(r"var sectionIds = \[(.*?)\];", page, re.S).group(1)
    assert re.findall(r"'([^']+)'", listed) == SECTION_IDS


# ── The form ───────────────────────────────────────────────────


def form_markup(page):
    return page.split('<form action="/config/save"')[1].split("</form>")[0]


def test_the_form_wraps_the_radar_and_ads_b_groups_and_nothing_else(page):
    expected = [section_id for group, sections in LAYOUT if group in FORM_GROUPS
                for section_id, _ in sections]
    assert re.findall(r'<section id="([^"]+)"', form_markup(page)) == expected


def test_the_form_posts_every_radar_field_on_the_page(page):
    """A radar field outside the form is silently left out of the POST, which
    the server reads as that setting having been cleared."""
    main = page.split("<main>")[1].split("</main>")[0]
    radar = {field_name(f) for f in named_fields(main) if "." in field_name(f)}
    inside = {field_name(f) for f in named_fields(form_markup(page))}

    assert {n.split(".")[0] for n in radar} == {
        "capture", "location", "truth", "tar1090", "retina_tracker"}
    assert radar <= inside, sorted(radar - inside)
    # No field reaches into the form from outside it either.
    assert not [f for f in named_fields(main) if 'form="configForm"' in f]


def test_a_save_stores_fields_from_every_group_in_the_form(app_client, user_config_file):
    """The server's side of the same thing: Radar and ADS-B fields arrive in
    one POST and are stored."""
    import yaml

    response = app_client.post("/config/save", data={
        "capture.fs": "1000000",
        "capture.fc": "500000000",
        "capture.device_type": "RspDuo",
        "capture.device_agcSetPoint": "-40",
        "capture.device_gainReductionA": "35",
        "capture.device_gainReductionB": "30",
        "capture.device_lnaState": "5",
        "capture.device_dabNotch": "on",
        "capture.device_rfNotch": "on",
        "capture.device_bandwidthNumber": "5",
        "retina_tracker.min_snr": "9.5",
        "tar1090.adsblol_fallback": "on",
        "tar1090.adsblol_radius": "77",
    }, follow_redirects=False)
    assert response.status_code == 302

    with open(user_config_file) as f:
        saved = yaml.safe_load(f)
    assert saved["retina_tracker"]["min_snr"] == 9.5
    assert saved["tar1090"]["adsblol_radius"] == 77


# ── Tower ──────────────────────────────────────────────────────


def test_the_center_frequency_is_shown_with_the_tower_not_in_capture(page):
    """Shown in the Tower section's Transmitter block, but still the capture
    setting it always was. See docs/features/config-editor.md#tower-section."""
    tower = section(page, "tower")
    names = [field_name(f) for f in named_fields(tower)]
    assert names.index("location.tx_name") < names.index("capture.fc") < names.index("location.tx_latitude")
    assert "capture.fc" not in [field_name(f) for f in named_fields(section(page, "capture"))]
    main = page.split("<main>")[1].split("</main>")[0]
    assert [field_name(f) for f in named_fields(main)].count("capture.fc") == 1


def test_the_center_frequency_reports_its_own_error_where_it_is_shown(app_client, telemetry):
    """The Tower section is hand-built, so it has to look the error up itself."""
    page = app_client.post("/config/save", data={"capture.fc": "not a number"}).data.decode()
    field = [f for f in named_fields(section(page, "tower")) if field_name(f) == "capture.fc"][0]
    assert "is-invalid" in field


def test_cached_towers_holds_the_tower_list_and_no_radar_field(page):
    cached = section(page, "cached-towers")
    assert 'id="towerPresetSelect"' in cached and 'id="towerManageList"' in cached
    assert not named_fields(cached)


# ── Tracking ───────────────────────────────────────────────────


def test_minimum_snr_has_its_own_section_after_capture(page):
    """Tracking is what happens to detections afterwards, not part of how the
    signal is captured, so it is not a Capture setting."""
    assert 'name="retina_tracker.min_snr"' in section(page, "tracking")
    assert 'name="retina_tracker.min_snr"' not in section(page, "capture")
    assert SECTION_IDS[SECTION_IDS.index("capture") + 1] == "tracking"


# ── The receiver goes by the node's name ───────────────────────


@pytest.fixture
def named(monkeypatch):
    """Give the node a name, or none."""
    import app as app_module

    def set_name(name):
        monkeypatch.setattr(app_module.node_name, "get", lambda: name)

    return set_name


def test_the_tower_section_shows_the_node_s_name_and_cannot_change_it(app_client, telemetry, named):
    named("Sample Rooftop")
    tower = section(app_client.get("/config").data.decode(), "tower")
    assert re.search(r'id="rxNameDisplay">\s*Sample Rooftop\s*<', tower)
    assert '<input type="text" name="location.rx_name"' not in tower
    assert 'href="#identifiers"' in tower


def test_the_tower_section_posts_the_node_s_name_as_the_receiver_name(app_client, telemetry, named):
    """So the radar config follows the name rather than keeping a second one."""
    named("Sample Rooftop")
    tower = section(app_client.get("/config").data.decode(), "tower")
    assert '<input type="hidden" name="location.rx_name" id="rxNameValue" value="Sample Rooftop">' in tower


def test_an_unnamed_node_s_receiver_goes_by_its_id(app_client, telemetry, named):
    """The same value the setup wizard writes."""
    named("")
    tower = section(app_client.get("/config").data.decode(), "tower")
    assert re.search(r'id="rxNameDisplay">\s*ret7a000001\s*<', tower)
    assert 'name="location.rx_name" id="rxNameValue" value="ret7a000001"' in tower


# ── Identifiers ────────────────────────────────────────────────


def test_identifiers_shows_the_name_the_address_and_the_local_id(page):
    identifiers = section(page, "identifiers")
    assert 'id="nodeNameInput"' in identifiers
    assert "http://ret7a000001.local" in identifiers
    assert re.search(r'id="nodeLocalId">\s*ret7a000001\s*<', identifiers)


def test_identifiers_shows_the_server_id_once_the_node_has_one(app_client, telemetry):
    telemetry({"state": "streaming", "detail": None, "node_ref": "ndexample0000001",
               "node_id": "ret7a000001", "stale": False, "last_report": None, "claim": None})
    identifiers = section(app_client.get("/config").data.decode(), "identifiers")
    assert re.search(r'id="nodeServerId">\s*ndexample0000001\s*<', identifiers)


@pytest.mark.parametrize("status", [
    None,                                               # no telemetry package
    {"state": "registering", "detail": None, "node_ref": None, "node_id": "ret7a000001",
     "stale": False, "last_report": None, "claim": None},   # not registered yet
])
def test_identifiers_says_so_when_there_is_no_server_id(app_client, telemetry, status):
    telemetry(status)
    identifiers = section(app_client.get("/config").data.decode(), "identifiers")
    assert "Not assigned yet" in identifiers
    assert "None" not in identifiers.split('id="nodeServerId"')[1].split("</div>")[0]
