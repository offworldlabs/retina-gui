"""Tests for the fleet landing page and the owl.local mode switch.

The switch is the only place in the whole design where the node count changes
behaviour — everything published on the wire is identical whether there is one
node or ten — so it is worth pinning down precisely.
"""

import pytest

from routes.fleet import is_entry_point, node_url


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


@pytest.fixture
def fleet(monkeypatch):
    """Give the running app a peer directory the test controls."""
    import app as app_module

    def set_nodes(*nodes):
        monkeypatch.setattr(app_module, "peers", FakePeers(*nodes))

    return set_nodes


# ── Recognising the shared alias ───────────────────────────────

@pytest.mark.parametrize("host", ["owl.local", "OWL.LOCAL", "owl.local:80", "owl"])
def test_the_shared_alias_is_recognised(host):
    assert is_entry_point(host) is True


@pytest.mark.parametrize("host", ["ret7dd2cb0d.local", "192.168.1.57",
                                  "localhost:80", "owl.example.com", ""])
def test_a_node_s_own_address_is_not_the_shared_alias(host):
    assert is_entry_point(host) is False


# ── The mode switch ────────────────────────────────────────────

def test_one_node_redirects_owl_local_to_that_node(app_client, fleet):
    """So the operator learns and bookmarks the name that will not change."""
    fleet(node("ret7dd2cb0d", is_self=True))
    response = app_client.get("/", headers={"Host": "owl.local"})
    assert response.status_code == 302
    assert response.headers["Location"] == "http://ret7dd2cb0d.local/"


def test_no_nodes_discovered_yet_still_redirects(app_client, fleet):
    """Nothing found is the same situation as only ourselves: no list to show."""
    fleet()
    response = app_client.get("/", headers={"Host": "owl.local"})
    assert response.status_code == 302
    assert response.headers["Location"] == "http://ret7dd2cb0d.local/"


def test_two_nodes_makes_owl_local_the_landing_page(app_client, fleet):
    fleet(node("ret7dd2cb0d", is_self=True), node("ret4c844c20", "192.168.1.58"))
    response = app_client.get("/", headers={"Host": "owl.local"})
    assert response.status_code == 200
    body = response.data.decode()
    assert "ret4c844c20" in body
    assert "http://ret4c844c20.local/" in body


def test_a_node_s_own_name_always_serves_that_node(app_client, fleet):
    """Even with a fleet: this host means "this node", not "any node"."""
    fleet(node("ret7dd2cb0d", is_self=True), node("ret4c844c20", "192.168.1.58"))
    response = app_client.get("/", headers={"Host": "ret7dd2cb0d.local"})
    assert response.status_code == 200
    assert "Nodes" not in response.data.decode()[:200]


def test_reaching_a_node_by_address_serves_that_node(app_client, fleet):
    fleet(node("ret7dd2cb0d", is_self=True), node("ret4c844c20", "192.168.1.58"))
    response = app_client.get("/", headers={"Host": "192.168.1.57"})
    assert response.status_code == 200


def test_without_a_node_id_it_serves_rather_than_redirecting(app_client_no_node_id,
                                                             monkeypatch):
    """There is no name to send them to, so a redirect could only loop."""
    import app as app_module
    monkeypatch.setattr(app_module, "peers", FakePeers())

    response = app_client_no_node_id.get("/", headers={"Host": "owl.local"})
    assert response.status_code != 302


# ── The page itself ────────────────────────────────────────────

def test_fleet_page_is_reachable_directly_whatever_the_count(app_client, fleet):
    fleet(node("ret7dd2cb0d", is_self=True))
    assert app_client.get("/fleet").status_code == 200


def test_the_card_links_to_the_node_s_own_mdns_name():
    assert node_url(node("ret4c844c20")) == "http://ret4c844c20.local/"


def test_the_card_falls_back_to_the_node_id_when_the_hostname_is_missing():
    peer = node("ret4c844c20")
    peer["hostname"] = ""
    assert node_url(peer) == "http://ret4c844c20.local/"


def test_this_node_is_labelled_on_the_page(app_client, fleet):
    fleet(node("ret7dd2cb0d", is_self=True), node("ret4c844c20", "192.168.1.58"))
    assert "This node" in app_client.get("/fleet").data.decode()


def test_a_friendly_name_is_shown_with_the_id_beneath_it(app_client, fleet):
    fleet(node("ret7dd2cb0d", is_self=True),
          node("ret4c844c20", "192.168.1.58", friendly="Boston Rooftop"))
    body = app_client.get("/fleet").data.decode()
    assert "Boston Rooftop" in body
    assert "ret4c844c20" in body, "the id stays visible; it is the stable one"


def test_every_card_shows_an_address(app_client, fleet):
    """The operator's check against a rogue advertiser, and their way in when
    the browser cannot resolve .local names."""
    fleet(node("ret7dd2cb0d", "10.0.0.1", is_self=True),
          node("ret4c844c20", "10.0.0.2"))
    body = app_client.get("/fleet").data.decode()
    assert "10.0.0.1" in body
    assert "10.0.0.2" in body


def test_peers_are_available_as_json(app_client, fleet):
    fleet(node("ret4c844c20", "192.168.1.58", friendly="Roof"))
    payload = app_client.get("/api/fleet/peers").get_json()
    assert payload["nodes"] == [{
        "node_id": "ret4c844c20",
        "name": "Roof",
        "has_friendly_name": True,
        "address": "192.168.1.58",
        "hostname": "ret4c844c20.local",
        "url": "http://ret4c844c20.local/",
        "is_self": False,
    }]


def test_an_unnamed_node_is_listed_by_its_id(app_client, fleet):
    fleet(node("ret4c844c20"))
    entry = app_client.get("/api/fleet/peers").get_json()["nodes"][0]
    assert entry["name"] == "ret4c844c20"
    assert entry["has_friendly_name"] is False


# ── Liveness endpoint ──────────────────────────────────────────

def test_healthz_answers_with_this_node_s_id(app_client):
    payload = app_client.get("/healthz").get_json()
    assert payload == {"ok": True, "node_id": "ret7dd2cb0d"}


# ── Interaction with a running calibration ─────────────────────

def test_a_calibrating_node_still_answers_its_peers(app_client, monkeypatch):
    """Peers decide this node exists from /healthz. A calibration is a local
    matter and must not make the node look absent to the rest of the fleet."""
    import app as app_module
    monkeypatch.setattr(app_module.calibrator, "is_running", lambda: True)

    assert app_client.get("/healthz").status_code == 200


def test_a_calibrating_node_still_serves_the_fleet_page(app_client, fleet,
                                                        monkeypatch):
    """Whoever is browsing owl.local is asking about the fleet, and is not
    necessarily the person who started a calibration on this particular node."""
    import app as app_module
    fleet(node("ret7dd2cb0d", is_self=True), node("ret4c844c20", "192.168.1.58"))
    monkeypatch.setattr(app_module.calibrator, "is_running", lambda: True)

    response = app_client.get("/fleet")
    assert response.status_code == 200
    assert "ret4c844c20" in response.data.decode()


def test_a_calibration_still_holds_the_node_s_own_pages(app_client, monkeypatch):
    """The exemption above must not have opened up the rest of the interface."""
    import app as app_module
    monkeypatch.setattr(app_module.calibrator, "is_running", lambda: True)

    response = app_client.get("/", headers={"Host": "ret7dd2cb0d.local"})
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/config")
