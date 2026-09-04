"""Tests for the fleet banner.

The banner is a shared component rendered on every page, so most of these go
through real routes rather than calling the helper directly: what matters is
that it survives base.html inheritance and the blocks pages override.
"""

import pytest

from routes.fleet import RESOURCES, banner_nodes, node_url, peer_view


class FakePeers:
    """Stands in for the mDNS PeerDirectory, which needs a LAN to be real."""

    def __init__(self, *nodes):
        self._nodes = list(nodes)

    def peers(self):
        return self._nodes

    def count(self):
        return len(self._nodes)


def node(node_id, address="192.168.1.57", friendly="", is_self=False,
         healthz=None):
    return {"node_id": node_id, "friendly_name": friendly,
            "hostname": f"{node_id}.local", "address": address,
            "port": "80", "is_self": is_self, "healthz": healthz}


def probe(node_ref=None, state="streaming", stale=False, installed=True):
    """A /healthz body, as a peer's last answer to this node's prober."""
    telemetry = (None if not installed
                 else {"node_ref": node_ref, "state": state, "stale": stale})
    return {"ok": True, "node_id": "ret0", "telemetry": telemetry}


def local_status(node_ref=None, state="streaming", stale=False):
    """What telemetry_status.read() returns on this node."""
    return {"state": state, "detail": None, "node_ref": node_ref,
            "node_id": "", "stale": stale, "last_report": None}


class FakeTelemetry:
    """Stands in for the reader of retina-telemetry's status document."""

    def __init__(self, status):
        self._status = status

    def read(self):
        return self._status


SELF = "ret7dd2cb0d"      # the node_id the app_client fixture writes
OTHER = "ret4c844c20"


def tab_strip(body):
    """The parent row's tabs, as a list of raw <a> fragments.

    Parsed rather than string-matched so that adding a class or an icon to a
    tab does not silently break every assertion about which one is active.
    """
    strip = body.split('<div class="nav-tabs">')[1].split("</div>")[0]
    return ["<a" + frag for frag in strip.split("<a")[1:]]


def active_tab(body):
    live = [t for t in tab_strip(body) if " active" in t.split(">")[0]]
    assert len(live) == 1, f"expected exactly one active tab, got {len(live)}"
    return live[0]


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
    tab = active_tab(app_client.get("/").data.decode())
    assert f"http://{SELF}.local/" in tab
    assert OTHER not in tab


def test_node_tabs_carry_the_node_mark(app_client, fleet):
    """So a tab reads as a node at a glance, not another section of the page."""
    fleet(node(SELF, is_self=True), node(OTHER, "192.168.1.58"))
    tabs = tab_strip(app_client.get("/").data.decode())
    node_tabs = [t for t in tabs if ".local/" in t]
    assert len(node_tabs) == 2
    assert all("nav-tab-icon" in t for t in node_tabs)


def test_the_summary_tab_has_no_node_mark(app_client, fleet):
    """It is not a node, and the mark is what separates the two at a glance."""
    fleet(node(SELF, is_self=True))
    summary = [t for t in tab_strip(app_client.get("/").data.decode())
               if 'href="/summary"' in t]
    assert len(summary) == 1
    assert "nav-tab-icon" not in summary[0]


# ── Both external links ────────────────────────────────────────

def test_the_banner_carries_both_outbound_links(app_client, fleet):
    fleet(node(SELF, is_self=True))
    body = app_client.get("/").data.decode()
    assert "https://map.retina.fm" in body and "Retina Network Map" in body
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
    response = app_client.get("/summary")
    assert response.status_code == 200
    # Summary is active, and no node tab is.
    assert 'href="/summary"' in active_tab(response.data.decode())


def card_strip(body):
    """The card grid, as a list of raw <a> fragments."""
    grid = body.split('<div class="node-grid">')[1].split("</main>")[0]
    return ["<a" + frag for frag in grid.split("<a")[1:]]


def node_cards(body):
    """The cards that stand for a node, excluding the invitation to buy one."""
    return [c for c in card_strip(body) if "node-card invite" not in c]


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
    body = app_client.get("/healthz").get_json()
    assert body["ok"] is True and body["node_id"] == SELF


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


# ── The cards ──────────────────────────────────────────────────


@pytest.fixture
def telemetry(monkeypatch):
    """Control what this node believes about its own telemetry."""
    import app as app_module

    def set_status(status):
        monkeypatch.setattr(app_module, "telemetry_status", FakeTelemetry(status))

    set_status(None)
    return set_status


def test_this_node_gets_a_card_even_before_discovery_has_run(app_client, fleet,
                                                            telemetry):
    """The banner already guarantees itself a tab in this window. A page of
    cards that disagreed with the banner directly above it would be worse than
    either answer on its own."""
    fleet()
    cards = node_cards(app_client.get("/summary").data.decode())
    assert len(cards) == 1 and SELF in cards[0]


def test_a_card_per_discovered_node(app_client, fleet, telemetry):
    fleet(node(SELF, is_self=True), node(OTHER, "192.168.1.58"))
    cards = node_cards(app_client.get("/summary").data.decode())
    assert len(cards) == 2


def test_a_card_is_a_link_to_that_node(app_client, fleet, telemetry):
    """Same as the banner tab above it. A card that only looked clickable
    would be the odd one out on this page."""
    fleet(node(SELF, is_self=True), node(OTHER, "192.168.1.58"))
    cards = node_cards(app_client.get("/summary").data.decode())
    assert f'href="http://{OTHER}.local/"' in cards[1]


def test_the_serving_node_is_marked_and_comes_first(app_client, fleet, telemetry):
    fleet(node(SELF, is_self=True), node(OTHER, "192.168.1.58"))
    cards = node_cards(app_client.get("/summary").data.decode())
    assert "This node" in cards[0] and "This node" not in cards[1]


def test_a_named_node_still_shows_its_node_id(app_client, fleet, telemetry):
    """It is the identifier support asks for, so a name never replaces it."""
    fleet(node(SELF, friendly="Garage", is_self=True))
    card = node_cards(app_client.get("/summary").data.decode())[0]
    assert "Garage" in card and SELF in card


def test_an_unnamed_node_is_titled_by_its_id(app_client, fleet, telemetry):
    fleet(node(SELF, is_self=True))
    card = node_cards(app_client.get("/summary").data.decode())[0]
    assert SELF in card and "Not yet named" in card


def test_the_address_is_shown(app_client, fleet, telemetry):
    """The by-IP fallback for when the mDNS name will not resolve. It had
    nowhere to live between the node-list page being deleted and this one."""
    fleet(node(SELF, address="192.168.0.144", is_self=True))
    card = node_cards(app_client.get("/summary").data.decode())[0]
    assert "192.168.0.144" in card


# ── Telemetry on a card ────────────────────────────────────────


def test_a_peers_telemetry_comes_from_its_last_probe(app_client, fleet, telemetry):
    """No fetch of any kind happens when this page is rendered: the answer
    arrived on the /healthz probe that runs every 20 seconds anyway."""
    fleet(node(SELF, is_self=True),
          node(OTHER, "192.168.1.58", healthz=probe(node_ref="ndabc123")))
    card = node_cards(app_client.get("/summary").data.decode())[1]
    assert "ndabc123" in card


def test_a_healthy_node_shows_its_id_and_no_chip(app_client, fleet, telemetry):
    """A working node has nothing to report about itself."""
    telemetry(local_status(node_ref="ndabc123", state="streaming"))
    fleet(node(SELF, is_self=True))
    card = node_cards(app_client.get("/summary").data.decode())[0]
    assert "ndabc123" in card
    assert "node-chip" not in card


def test_an_unregistered_node_keeps_its_identifier(app_client, fleet, telemetry):
    """The two are independent, and a node can hold a node_ref from a past
    registration while the server currently refuses it. Showing only the state
    would throw away the reference support asks for; showing only the
    reference would hide the fault."""
    telemetry(local_status(node_ref="ndabc123", state="unregistered"))
    fleet(node(SELF, is_self=True))
    card = node_cards(app_client.get("/summary").data.decode())[0]
    assert "ndabc123" in card and "Unregistered" in card


def test_a_state_name_is_made_readable(app_client, fleet, telemetry):
    telemetry(local_status(node_ref="ndabc123", state="awaiting_config"))
    fleet(node(SELF, is_self=True))
    card = node_cards(app_client.get("/summary").data.decode())[0]
    assert "Awaiting config" in card


def test_a_stale_document_reads_as_not_reporting(app_client, fleet, telemetry):
    """Whatever state it last claimed, a document too old to believe describes
    a service that is no longer running."""
    telemetry(local_status(node_ref="ndabc123", state="streaming", stale=True))
    fleet(node(SELF, is_self=True))
    card = node_cards(app_client.get("/summary").data.decode())[0]
    assert "Not reporting" in card


def test_a_node_without_the_telemetry_package_says_so(app_client, fleet, telemetry):
    telemetry(None)
    fleet(node(SELF, is_self=True))
    card = node_cards(app_client.get("/summary").data.decode())[0]
    assert "Not installed" in card


def test_a_peer_not_yet_heard_from_has_no_telemetry_row(app_client, fleet, telemetry):
    """Distinct from having no telemetry. We have not asked yet, so the card
    omits the row rather than asserting something we do not know."""
    fleet(node(SELF, is_self=True), node(OTHER, "192.168.1.58"))
    card = node_cards(app_client.get("/summary").data.decode())[1]
    assert "Telemetry" not in card


def test_a_peer_on_an_older_build_has_no_telemetry_row(app_client, fleet, telemetry):
    """Its /healthz predates the field. The card is quieter, not wrong."""
    fleet(node(SELF, is_self=True),
          node(OTHER, "192.168.1.58", healthz={"ok": True, "node_id": OTHER}))
    card = node_cards(app_client.get("/summary").data.decode())[1]
    assert "Telemetry" not in card


# ── A fleet of one ─────────────────────────────────────────────


def test_a_lone_node_is_offered_a_second(app_client, fleet, telemetry):
    fleet(node(SELF, is_self=True))
    body = app_client.get("/summary").data.decode()
    assert "Add another node" in body


def test_the_offer_goes_once_there_are_two(app_client, fleet, telemetry):
    fleet(node(SELF, is_self=True), node(OTHER, "192.168.1.58"))
    body = app_client.get("/summary").data.decode()
    assert "Add another node" not in body


# ── What a peer is told ────────────────────────────────────────


def test_healthz_carries_this_nodes_telemetry(app_client, telemetry):
    """The only channel by which an identifier held on this node's disk
    reaches a card on someone else's Summary page."""
    telemetry(local_status(node_ref="ndabc123", state="streaming"))
    body = app_client.get("/healthz").get_json()
    assert body["telemetry"] == {"node_ref": "ndabc123",
                                 "state": "streaming", "stale": False}


def test_healthz_says_null_when_telemetry_is_absent(app_client, telemetry):
    telemetry(None)
    assert app_client.get("/healthz").get_json()["telemetry"] is None



def test_a_card_with_nothing_to_say_has_no_divider(app_client, fleet, telemetry):
    """A peer can legitimately have neither row: not heard from yet, and no
    address resolved. An empty rows block leaves a rule across the card with
    nothing beneath it."""
    fleet(node(SELF, is_self=True), node(OTHER, address=""))
    card = node_cards(app_client.get("/summary").data.decode())[1]
    assert "node-rows" not in card


# ── Resources ──────────────────────────────────────────────────


def resource_strip(body):
    """The Resources cards, as raw <a> fragments."""
    section = body.split('<div class="section-head resources-head">')[1]
    return ["<a" + frag for frag in section.split("<a")[1:]]


def test_every_resource_is_offered(app_client, fleet, telemetry):
    fleet(node(SELF, is_self=True))
    body = app_client.get("/summary").data.decode()
    for resource in RESOURCES:
        assert f'href="{resource["url"]}"' in body, resource["name"]


def test_a_resource_says_where_it_goes(app_client, fleet, telemetry):
    """These all leave the device, so an owner should see they are about to be
    sent to github.com before the click rather than after."""
    fleet(node(SELF, is_self=True))
    cards = resource_strip(app_client.get("/summary").data.decode())
    assert "github.com" in "".join(cards)


def test_the_host_is_derived_from_the_url():
    """So the line under a name cannot drift from where the card really goes."""
    for resource in RESOURCES:
        assert resource["host"] in resource["url"]


def test_every_resource_opens_away_from_the_page(app_client, fleet, telemetry):
    """A node's own page is a poor thing to lose to an outbound click, and
    rel=noopener is the plain safety requirement for target=_blank."""
    fleet(node(SELF, is_self=True))
    for card in resource_strip(app_client.get("/summary").data.decode()):
        assert 'target="_blank"' in card and 'rel="noopener"' in card


def test_resources_are_not_mixed_in_with_the_nodes(app_client, fleet, telemetry):
    """The antenna mark means "this is a node". It stops meaning anything if a
    link to a website sits in the same grid wearing one."""
    fleet(node(SELF, is_self=True))
    for card in node_cards(app_client.get("/summary").data.decode()):
        assert "link-card" not in card


def test_the_offer_of_a_second_node_points_at_the_store(app_client, fleet, telemetry):
    fleet(node(SELF, is_self=True))
    body = app_client.get("/summary").data.decode()
    assert 'href="https://retina.fm"' in body


# ── How your node sees ─────────────────────────────────────────


def facts_section(body):
    return body.split('<div class="section-head facts-head">')[1].split("</main>")[0]


def test_the_primer_facts_are_shown(app_client, fleet, telemetry):
    fleet(node(SELF, is_self=True))
    facts = facts_section(app_client.get("/summary").data.decode())
    for heading in ("It never transmits", "Two antennas, two paths",
                    "Every detection is one dot", "It only hears a wedge",
                    "One node sees an arc"):
        assert heading in facts, heading


def test_the_facts_cost_the_node_nothing(app_client, fleet, telemetry):
    """The primer earns its keep by being driveable, and none of that can come
    here: a node's compute belongs to the radar. Static markup only, so no
    script and no extra fetch rides in with it."""
    facts = facts_section(app_client.get("/summary").data.decode())
    assert "<script" not in facts
    assert "<img" not in facts and "<canvas" not in facts


def test_the_facts_are_not_dressed_as_cards(app_client, fleet, telemetry):
    """Everything card-shaped on this page is a link. Prose that cannot be
    clicked must not look like something that can."""
    facts = facts_section(app_client.get("/summary").data.decode())
    assert "node-card" not in facts and "link-card" not in facts


def test_no_primer_link_until_there_is_somewhere_to_send_people(app_client, fleet,
                                                               telemetry):
    """The branch carrying the primer is unmerged and the URL 404s today. A
    dead link on an owner's node is worse than no link at all."""
    from routes import fleet as fleet_routes

    assert fleet_routes.PRIMER_URL == "", "turn the link on in the template test too"
    facts = facts_section(app_client.get("/summary").data.decode())
    assert "the full primer" not in facts


def test_the_primer_link_appears_once_it_has_a_home(app_client, fleet, telemetry,
                                                    monkeypatch):
    from routes import fleet as fleet_routes

    monkeypatch.setattr(fleet_routes, "PRIMER_URL", "https://offworldlabs.com/learn/")
    facts = facts_section(app_client.get("/summary").data.decode())
    assert 'href="https://offworldlabs.com/learn/"' in facts
    assert 'rel="noopener"' in facts
