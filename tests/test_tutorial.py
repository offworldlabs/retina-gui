"""Tests for the guided tutorial.

The tour is drawn in the browser, so what can be held here is everything the
browser is handed: where a run starts, which steps it has, and that every
step still has something on its page to point at.
"""

import json
import re
import struct
from pathlib import Path

import pytest

import tutorial

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SELF = "ret7a000001"      # the node_id the app_client fixture writes
OTHER = "ret4a000001"


class FakePeers:
    """Stands in for the mDNS PeerDirectory, which needs a LAN to be real."""

    def __init__(self, *nodes):
        self._nodes = list(nodes)

    def peers(self):
        return self._nodes


def node(node_id, is_self=False):
    return {"node_id": node_id, "friendly_name": "", "hostname": f"{node_id}.local",
            "address": "192.0.2.10", "port": "80", "is_self": is_self, "healthz": None}


@pytest.fixture
def fleet(monkeypatch):
    """Give the running app a peer directory the test controls."""
    import app as app_module

    def set_nodes(*nodes):
        monkeypatch.setattr(app_module, "peers", FakePeers(*nodes))

    set_nodes(node(SELF, is_self=True))
    return set_nodes


def two_nodes(fleet):
    fleet(node(SELF, is_self=True), node(OTHER))


def payload_in(body):
    """The tour payload a page was rendered with, or None when it has none."""
    found = re.search(r"window\.OWL_TUTORIAL = (.*?);</script>", body)
    return json.loads(found.group(1)) if found else None


# ── The steps themselves ───────────────────────────────────────


def test_every_step_says_a_sentence_or_two():
    """A card is a caption, not a manual."""
    for step in tutorial.STEPS:
        body = step["body"]
        assert body.endswith("."), step["title"]
        assert 1 <= len(re.findall(r"[.!?](?:\s|$)", body)) <= 2, step["title"]


def test_the_pages_are_toured_in_order_and_the_tour_closes_on_summary():
    pages = [s["page"] for s in tutorial.STEPS]
    assert pages[-1] == "/summary"
    assert pages[:-1] == sorted(pages[:-1], key=["/summary", "/", "/config"].index)
    assert [s["closing"] for s in tutorial.STEPS] == [False] * (len(pages) - 1) + [True]


def test_a_single_node_skips_summary_s_opening_steps_but_not_the_closing_one():
    steps = tutorial.steps_for(include_summary=False)
    assert steps[0]["page"] == "/"
    assert [s["title"] for s in steps if s["page"] == "/summary"] == ["That's everything"]
    assert len(tutorial.steps_for(include_summary=True)) == len(tutorial.STEPS)


def test_the_closing_step_points_at_the_links_and_the_wiki():
    step = tutorial.STEPS[-1]
    assert [t["name"] for t in step["targets"]] == ["summary-resources", "summary-help"]
    assert step["link"]["url"] == "https://github.com/offworldlabs/owl-os/wiki"


def image_size(path):
    """Width and height of a PNG or JPEG, read from its header."""
    data = path.read_bytes()
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return struct.unpack(">II", data[16:24])       # IHDR is always first
    assert data[:2] == b"\xff\xd8", path
    at = 2
    while at < len(data):
        marker, length = data[at + 1], struct.unpack(">H", data[at + 2:at + 4])[0]
        # The frame header (SOF0-SOF15, bar the three that are not frames).
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[at + 5:at + 9])
            return width, height
        at += 2 + length
    raise AssertionError(f"no frame header in {path}")


def test_every_example_image_exists_at_the_size_the_step_declares():
    """The card is laid out from the declared size, before the image loads."""
    images = [s["image"] for s in tutorial.STEPS if s["image"]]
    assert len({image["url"] for image in images}) == 6
    for image in images:
        path = PROJECT_ROOT / image["url"].lstrip("/")
        assert path.is_file(), image["url"]
        assert image_size(path) == (image["width"], image["height"]), image["url"]
        assert image["alt"]


def test_every_mark_is_on_its_image():
    """A mark is a point in the image's own pixels, so resizing the file moves it."""
    marked = [s["image"] for s in tutorial.STEPS if s["image"] and s["image"]["marks"]]
    assert marked
    for image in marked:
        for mark in image["marks"]:
            assert 0 <= mark["x"] <= image["width"] and 0 <= mark["y"] <= image["height"], image["url"]


def test_the_pages_are_joined_by_click_through_steps():
    """Summary hands over to Home, and Home to Config, by a click on the page.

    Each is the last step on its page, so the click always changes page.
    """
    clicks = [i for i, s in enumerate(tutorial.STEPS) if s["click"]]
    assert [tutorial.STEPS[i]["page"] for i in clicks] == ["/summary", "/"]
    for i in clicks:
        assert tutorial.STEPS[i + 1]["page"] != tutorial.STEPS[i]["page"]


def test_the_last_step_is_not_a_click_through():
    assert not tutorial.STEPS[-1]["click"]


def test_the_controller_step_links_to_the_wiki():
    step = next(s for s in tutorial.STEPS if s["title"] == "Controller")
    assert step["link"]["url"].endswith("/wiki/3-Understanding-Node-Data")


# ── Starting a run ─────────────────────────────────────────────


def test_one_node_starts_on_home(app_client, fleet):
    response = app_client.get("/tutorial")
    assert response.status_code == 302
    assert response.headers["Location"] == "/?tutorial=1"


def test_several_nodes_start_on_summary(app_client, fleet):
    two_nodes(fleet)
    assert app_client.get("/tutorial").headers["Location"] == "/summary?tutorial=1"


def test_demo_tours_summary_on_a_single_node_and_stays_in_demo(app_client, fleet):
    assert app_client.get("/tutorial?demo=1").headers["Location"] == "/summary?tutorial=1&demo=1"
    body = app_client.get("/summary?tutorial=1&demo=1").data.decode()
    run = payload_in(body)
    assert all(step["url"].endswith("&demo=1") for step in run["steps"])
    assert run["exit_url"] == "/summary?demo=1"


def test_a_run_cannot_start_mid_wizard(app_client, fleet):
    import app as app_module

    app_module.device_state.save_setup_wizard_step("location")
    response = app_client.get("/tutorial")
    assert response.headers["Location"] == "/set-up"


# ── What a page is handed ──────────────────────────────────────


def test_a_page_without_the_parameter_has_no_tour(app_client, fleet):
    body = app_client.get("/").data.decode()
    assert payload_in(body) is None
    assert "tutorial.js" not in body
    assert "tutorial.css" not in body


def test_a_step_page_carries_the_whole_run(app_client, fleet):
    body = app_client.get("/?tutorial=1").data.decode()
    run = payload_in(body)
    assert run["current"] == 0
    assert [s["url"] for s in run["steps"][:2]] == ["/?tutorial=1", "/?tutorial=2"]
    assert len(run["steps"]) == len(tutorial.steps_for(include_summary=False))
    assert run["exit_url"] == "/summary"
    assert "/static/tutorial.js" in body
    assert "/static/tutorial.css" in body


def test_step_numbers_follow_the_run_not_the_full_list(app_client, fleet):
    """With Summary toured, Home's first step is no longer step 1."""
    two_nodes(fleet)
    summary_steps = sum(1 for s in tutorial.STEPS if s["page"] == "/summary" and not s["closing"])
    assert payload_in(app_client.get("/?tutorial=1").data.decode()) is None
    run = payload_in(app_client.get(f"/?tutorial={summary_steps + 1}").data.decode())
    assert run["current"] == summary_steps
    assert run["steps"][summary_steps]["title"] == "Welcome to your node"


@pytest.mark.parametrize("url", [
    "/config?tutorial=1",      # a Home step, asked of Config
    "/?tutorial=0",
    "/?tutorial=999",
    "/?tutorial=banana",
    "/?tutorial=",
])
def test_a_link_that_names_no_step_here_renders_the_plain_page(app_client, fleet, url):
    response = app_client.get(url)
    assert response.status_code == 200
    assert payload_in(response.data.decode()) is None


def test_the_wizard_page_never_runs_the_tour(app_client, fleet):
    assert payload_in(app_client.get("/set-up?tutorial=1").data.decode()) is None


# ── Every step has something to point at ───────────────────────


def tokens_on(body):
    found = set()
    for value in re.findall(r'data-tutorial="([^"]+)"', body):
        found.update(value.split())
    return found


def test_every_target_exists_on_its_page(app_client, fleet):
    """A step whose target was renamed or removed would point at nothing.

    Home is fetched in demo mode because the tower row and telemetry card it
    points at are only drawn once a node has a tower and a telemetry package.
    """
    two_nodes(fleet)
    pages = {
        "/summary": tokens_on(app_client.get("/summary").data.decode()),
        "/": tokens_on(app_client.get("/?demo=1").data.decode()),
        "/config": tokens_on(app_client.get("/config").data.decode()),
    }
    for step in tutorial.STEPS:
        for target in step["targets"]:
            assert target["name"] in pages[step["page"]], (step["title"], target["name"])


def test_no_token_is_left_without_a_step(app_client, fleet):
    """The reverse: markup tagged for a step that no longer exists."""
    two_nodes(fleet)
    used = {t["name"] for s in tutorial.STEPS for t in s["targets"]}
    for url in ("/summary", "/?demo=1", "/config"):
        assert tokens_on(app_client.get(url).data.decode()) <= used, url


# ── The two ways in ────────────────────────────────────────────


def test_the_wizard_recommends_the_tour_on_its_last_step(app_client, fleet):
    body = app_client.get("/set-up").data.decode()
    complete = body.split('data-step="complete"')[1].split("<!--")[0]
    assert "recommend" in complete
    assert 'href="/tutorial"' in body.split('id="stepBtns-complete"')[1].split("</div>")[0]


def test_the_wizard_in_demo_mode_starts_the_tour_in_demo_mode(app_client, fleet):
    body = app_client.get("/set-up?demo=1").data.decode()
    assert 'href="/tutorial?demo=1"' in body


def test_config_can_launch_the_tour_alone(app_client, fleet):
    body = app_client.get("/config").data.decode()
    section = body.split('<section id="tutorial"')[1].split("</section>")[0]
    assert 'href="/tutorial"' in section
    assert 'href="#tutorial"' in body.split('<aside class="side"')[1].split("</aside>")[0]


def test_the_closing_step_is_served_on_summary_at_the_end_of_either_run(app_client, fleet):
    """A single node's run never toured Summary, and still ends there."""
    last = len(tutorial.steps_for(include_summary=False))
    run = payload_in(app_client.get(f"/summary?tutorial={last}").data.decode())
    assert run["current"] == last - 1 and run["steps"][-1]["title"] == "That's everything"
    assert payload_in(app_client.get("/summary?tutorial=1").data.decode()) is None

    two_nodes(fleet)
    last = len(tutorial.STEPS)
    assert payload_in(app_client.get(f"/summary?tutorial={last}").data.decode())["current"] == last - 1
