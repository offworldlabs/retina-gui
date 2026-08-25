"""Tests for the fleet banner.

The banner is a shared component rendered on every page, so most of these go
through real routes rather than calling the helper directly: what matters is
that it survives base.html inheritance and the blocks pages override.
"""

import pytest

from routes.fleet import banner_nodes, node_url, peer_view


class FakePeers:
    """Stands in for the mDNS PeerDirectory, which needs a LAN to be real."""

    def __init__(self, *nodes):
        self._nodes = list(nodes)

    def peers(self):
        return self._nodes

    def count(self):
        return len(self._nodes)


def node(node_id, address="192.168.1.57", friendly="", is_self=False):
    return {"node_id": node_id, "friendly_name": friendly,
            "hostname": f"{node_id}.local", "address": address,
            "port": "80", "is_self": is_self}


SELF = "ret7dd2cb0d"      # the node_id the app_client fixture writes
OTHER = "ret4c844c20"


@pytest.fixture
def fleet(monkeypatch):
    """Give the running app a peer directory the test controls."""
    import app as app_module

    def set_nodes(*nodes):
        monkeypatch.setattr(app_module, "peers", FakePeers(*nodes))

    return set_nodes


# ── Which tabs get drawn ───────────────────────────────────────

def test_a_tab_per_discovered_node(app_client, fleet):
    fleet(node(SELF, is_self=True), node(OTHER, "192.168.1.58"))
    body = app_client.get("/").data.decode()
    assert f'href="http://{SELF}.local/"' in body
    assert f'href="http://{OTHER}.local/"' in body


def test_this_node_appears_even_before_discovery_has_run(app_client, fleet):
    """A browse takes a second to populate and can come back empty on a network
    that blocks multicast. Neither is a reason to draw a banner with no tabs."""
    fleet()
    nodes = banner_nodes()
    assert [n["node_id"] for n in nodes] == [SELF]
    assert nodes[0]["is_self"] is True


def test_this_node_is_not_duplicated_once_discovery_finds_it(app_client, fleet):
    fleet(node(SELF, is_self=True), node(OTHER, "192.168.1.58"))
    assert [n["node_id"] for n in banner_nodes()] == [SELF, OTHER]


def test_a_friendly_name_labels_the_tab(app_client, fleet):
    fleet(node(SELF, is_self=True),
          node(OTHER, "192.168.1.58", friendly="Boston Rooftop"))
    body = app_client.get("/").data.decode()
    assert "Boston Rooftop" in body


def test_tabs_are_absolute_so_a_click_leaves_the_shared_alias(app_client, fleet):
    """owl.local is answered by any node, so it is not worth landing on."""
    fleet(node(SELF, is_self=True), node(OTHER, "192.168.1.58"))
    body = app_client.get("/", headers={"Host": "owl.local"}).data.decode()
    assert f'href="http://{SELF}.local/"' in body
    assert 'href="/"' not in body.split('<div class="nav-tabs">')[1].split("</div>")[0]


def test_the_serving_node_is_the_active_tab(app_client, fleet):
    fleet(node(SELF, is_self=True), node(OTHER, "192.168.1.58"))
    tabs = app_client.get("/").data.decode() \
        .split('<div class="nav-tabs">')[1].split("</div>")[0]

    # Exactly one tab is active, and it is the node serving this page.
    assert tabs.count("nav-tab active") == 1
    active_tab = tabs.split("nav-tab active")[1].split("</a>")[0]
    assert f"http://{SELF}.local/" in active_tab
    assert OTHER not in active_tab


# ── Both external links ────────────────────────────────────────

def test_the_banner_carries_both_outbound_links(app_client, fleet):
    fleet(node(SELF, is_self=True))
    body = app_client.get("/").data.decode()
    assert "https://map.retina.fm" in body and "Server" in body
    assert "https://dash.retina.fm" in body and "Retina Dashboard" in body


# ── The child row ──────────────────────────────────────────────

def test_the_child_row_is_present_on_a_node_page(app_client, fleet):
    fleet(node(SELF, is_self=True))
    body = app_client.get("/").data.decode()
    assert 'class="subnav"' in body
    assert ">Home</a>" in body and ">Config</a>" in body


def test_the_child_row_is_absent_on_summary(app_client, fleet):
    """Summary is fleet scope. Home and Config below it would be about
    whichever node happened to serve the page, which is not what is being
    asked about there."""
    fleet(node(SELF, is_self=True))
    body = app_client.get("/summary").data.decode()
    assert 'class="subnav"' not in body


def test_summary_renders_and_marks_its_own_tab(app_client, fleet):
    fleet(node(SELF, is_self=True), node(OTHER, "192.168.1.58"))
    body = app_client.get("/summary").data.decode()
    assert "<h1>Summary</h1>" in body
    tabs = body.split('<div class="nav-tabs">')[1].split("</div>")[0]
    # Summary is active, and no node tab is.
    assert tabs.count("nav-tab active") == 1
    assert 'href="/summary"' in tabs


# ── The setup wizard ───────────────────────────────────────────

def test_the_wizard_keeps_the_fleet_bar(app_client, fleet):
    """Without it, a node part-way through setup is a dead end in every other
    node's banner, and an abandoned wizard keeps it that way for 24h."""
    fleet(node(SELF, is_self=True), node(OTHER, "192.168.1.58"))
    body = app_client.get("/set-up").data.decode()
    assert f'href="http://{OTHER}.local/"' in body, "cannot leave this node"


def test_the_wizard_does_not_get_the_child_row(app_client, fleet):
    """Home and Config stay unreachable mid-setup. That lock is the point."""
    fleet(node(SELF, is_self=True))
    body = app_client.get("/set-up").data.decode()
    assert 'class="subnav"' not in body


# ── owl.local no longer behaves differently ────────────────────

@pytest.mark.parametrize("host", ["owl.local", f"{SELF}.local", "192.168.1.57"])
def test_every_host_serves_the_same_home_page(app_client, fleet, host):
    """The mode switch is gone: the shared alias is a way in, not a mode."""
    fleet(node(SELF, is_self=True), node(OTHER, "192.168.1.58"))
    response = app_client.get("/", headers={"Host": host})
    assert response.status_code == 200
    assert "<h1>Home</h1>" in response.data.decode()


def test_one_node_no_longer_redirects(app_client, fleet):
    fleet(node(SELF, is_self=True))
    assert app_client.get("/", headers={"Host": "owl.local"}).status_code == 200


def test_the_old_fleet_page_is_gone(app_client, fleet):
    """The banner lists every node; a page doing the same would drift."""
    fleet(node(SELF, is_self=True))
    assert app_client.get("/fleet").status_code == 404


# ── What the banner is built from ──────────────────────────────

def test_peers_are_available_as_json(app_client, fleet):
    fleet(node(OTHER, "192.168.1.58", friendly="Roof"))
    payload = app_client.get("/api/fleet/peers").get_json()
    assert payload["nodes"] == [{
        "node_id": OTHER,
        "name": "Roof",
        "has_friendly_name": True,
        "address": "192.168.1.58",
        "hostname": f"{OTHER}.local",
        "url": f"http://{OTHER}.local/",
        "is_self": False,
    }]


def test_an_unnamed_node_is_labelled_by_its_id():
    view = peer_view(node(OTHER))
    assert view["name"] == OTHER
    assert view["has_friendly_name"] is False


def test_the_url_falls_back_to_the_node_id_without_a_hostname():
    peer = node(OTHER)
    peer["hostname"] = ""
    assert node_url(peer) == f"http://{OTHER}.local/"


def test_healthz_answers_with_this_node_s_id(app_client):
    assert app_client.get("/healthz").get_json() == {"ok": True, "node_id": SELF}


# ── Interaction with a running calibration ─────────────────────

def test_a_calibrating_node_still_answers_its_peers(app_client, monkeypatch):
    """Peers decide this node exists from /healthz. A calibration is a local
    matter and must not make the node look absent to the rest of the fleet."""
    import app as app_module
    monkeypatch.setattr(app_module.calibrator, "is_running", lambda: True)

    assert app_client.get("/healthz").status_code == 200


def test_a_calibrating_node_still_serves_summary(app_client, fleet, monkeypatch):
    """Whoever is reading Summary is asking about the fleet, and is not
    necessarily the person who started a calibration on this node."""
    import app as app_module
    fleet(node(SELF, is_self=True))
    monkeypatch.setattr(app_module.calibrator, "is_running", lambda: True)

    assert app_client.get("/summary").status_code == 200


def test_a_calibration_still_holds_the_node_s_own_pages(app_client, monkeypatch):
    """The exemptions above must not have opened up the rest of the interface."""
    import app as app_module
    monkeypatch.setattr(app_module.calibrator, "is_running", lambda: True)

    response = app_client.get("/", headers={"Host": f"{SELF}.local"})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/config")
