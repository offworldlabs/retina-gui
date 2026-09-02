"""Tests for peer discovery over mDNS.

The parsing tests use real avahi-browse -p output shapes. The directory tests
drive _apply() and _probe_once() directly rather than starting the threads —
starting them would mean a subprocess and real network traffic, which the
block_network fixture in conftest rightly refuses.
"""

import mdns_peers
from mdns_peers import PeerDirectory, parse_line, parse_txt

RESOLVED = ('=;eth0;IPv4;ret4c844c20;_owl-node._tcp;local;ret4c844c20.local;'
            '192.168.1.57;80;"node_id=ret4c844c20" "name=Boston Rooftop"')


# ── Line parsing ───────────────────────────────────────────────

def test_parses_a_resolved_service():
    event = parse_line(RESOLVED)
    assert event["event"] == "resolve"
    assert event["interface"] == "eth0"
    assert event["protocol"] == "IPv4"
    assert event["hostname"] == "ret4c844c20.local"
    assert event["address"] == "192.168.1.57"
    assert event["port"] == "80"
    assert event["node_id"] == "ret4c844c20"
    assert event["friendly_name"] == "Boston Rooftop"


def test_ignores_the_announcement_that_precedes_a_resolve():
    # A '+' says a name exists but not where — the '=' for it follows.
    assert parse_line("+;eth0;IPv4;ret4c844c20;_owl-node._tcp;local") is None


def test_parses_a_removal():
    event = parse_line("-;eth0;IPv4;ret4c844c20;_owl-node._tcp;local")
    assert event == {"event": "remove", "interface": "eth0",
                     "protocol": "IPv4", "name": "ret4c844c20"}


def test_ignores_junk_and_blank_lines():
    assert parse_line("") is None
    assert parse_line("\n") is None
    assert parse_line("Failed to resolve service") is None
    # Truncated resolve line: no address to use, so nothing to report.
    assert parse_line("=;eth0;IPv4;ret4c844c20;_owl-node._tcp") is None


def test_unescapes_decimal_escapes_in_the_instance_name():
    line = RESOLVED.replace("ret4c844c20;_owl-node", "my\\032node;_owl-node")
    assert parse_line(line)["name"] == "my node"


def test_a_txt_value_containing_a_semicolon_survives_field_splitting():
    line = ('=;eth0;IPv4;ret1;_owl-node._tcp;local;ret1.local;10.0.0.2;80;'
            '"node_id=ret1" "name=a;b"')
    assert parse_line(line)["friendly_name"] == "a;b"


def test_txt_parsing():
    assert parse_txt('"node_id=ret1" "name=Roof"') == {"node_id": "ret1",
                                                       "name": "Roof"}
    # A value may itself contain '='.
    assert parse_txt('"k=a=b"') == {"k": "a=b"}
    # An empty value is how an unnamed node advertises.
    assert parse_txt('"name="') == {"name": ""}
    assert parse_txt("") == {}
    assert parse_txt(None) == {}


def test_node_id_falls_back_to_the_instance_name_when_txt_is_missing():
    line = "=;eth0;IPv4;ret9;_owl-node._tcp;local;ret9.local;10.0.0.9;80;"
    assert parse_line(line)["node_id"] == "ret9"


# ── The directory ──────────────────────────────────────────────

def directory():
    return PeerDirectory(own_node_id_fn=lambda: "retself")


def resolve(name, address, interface="eth0", protocol="IPv4", friendly=""):
    return {"event": "resolve", "interface": interface, "protocol": protocol,
            "name": name, "hostname": f"{name}.local", "address": address,
            "port": "80", "node_id": name, "friendly_name": friendly}


def test_a_resolved_node_appears_and_is_counted():
    d = directory()
    d._apply(resolve("ret4c844c20", "192.168.1.57", friendly="Roof"))
    assert d.count() == 1
    peer = d.peers()[0]
    assert peer["node_id"] == "ret4c844c20"
    assert peer["friendly_name"] == "Roof"
    assert peer["is_self"] is False


def test_this_node_is_marked_as_itself():
    d = directory()
    d._apply(resolve("retself", "192.168.1.10"))
    assert d.peers()[0]["is_self"] is True


def test_this_node_sorts_first_then_by_name():
    d = directory()
    d._apply(resolve("retzzz", "10.0.0.3", friendly="Zeta"))
    d._apply(resolve("retaaa", "10.0.0.2", friendly="Alpha"))
    d._apply(resolve("retself", "10.0.0.1"))
    assert [p["node_id"] for p in d.peers()] == ["retself", "retaaa", "retzzz"]


def test_an_ipv4_address_is_preferred_over_ipv6():
    d = directory()
    d._apply(resolve("ret1", "fe80::1", protocol="IPv6"))
    d._apply(resolve("ret1", "192.168.1.57", protocol="IPv4"))
    assert d.peers()[0]["address"] == "192.168.1.57"
    # ...and a later IPv6 sighting does not overwrite it again.
    d._apply(resolve("ret1", "fe80::2", protocol="IPv6"))
    assert d.peers()[0]["address"] == "192.168.1.57"


def test_a_node_seen_on_two_interfaces_survives_losing_one():
    d = directory()
    d._apply(resolve("ret1", "192.168.1.57", interface="eth0"))
    d._apply(resolve("ret1", "192.168.1.58", interface="wlan0"))
    assert d.count() == 1

    d._apply({"event": "remove", "interface": "eth0", "protocol": "IPv4",
              "name": "ret1"})
    assert d.count() == 1, "still reachable on wlan0"

    d._apply({"event": "remove", "interface": "wlan0", "protocol": "IPv4",
              "name": "ret1"})
    assert d.count() == 0


def test_removing_a_node_that_was_never_seen_is_harmless():
    d = directory()
    d._apply({"event": "remove", "interface": "eth0", "protocol": "IPv4",
              "name": "ghost"})
    assert d.count() == 0


# ── Liveness ───────────────────────────────────────────────────

def test_one_failed_probe_does_not_remove_a_node(monkeypatch):
    """The hysteresis that stops a marginal link flipping the page."""
    d = directory()
    d._apply(resolve("ret1", "192.168.1.57"))
    monkeypatch.setattr(PeerDirectory, "_probe_peer", staticmethod(lambda a: (False, None)))

    d._probe_once()
    assert d.count() == 1, "one miss is not evidence a node is gone"

    d._probe_once()
    assert d.count() == 0


def test_a_node_comes_back_when_it_answers_again(monkeypatch):
    d = directory()
    d._apply(resolve("ret1", "192.168.1.57"))
    monkeypatch.setattr(PeerDirectory, "_probe_peer", staticmethod(lambda a: (False, None)))
    d._probe_once()
    d._probe_once()
    assert d.count() == 0

    monkeypatch.setattr(PeerDirectory, "_probe_peer", staticmethod(lambda a: (True, None)))
    d._probe_once()
    assert d.count() == 1


def test_a_recovered_node_gets_a_full_allowance_again(monkeypatch):
    """A success must reset the counter, not leave it one miss from death."""
    d = directory()
    d._apply(resolve("ret1", "192.168.1.57"))
    monkeypatch.setattr(PeerDirectory, "_probe_peer", staticmethod(lambda a: (False, None)))
    d._probe_once()
    monkeypatch.setattr(PeerDirectory, "_probe_peer", staticmethod(lambda a: (True, None)))
    d._probe_once()
    monkeypatch.setattr(PeerDirectory, "_probe_peer", staticmethod(lambda a: (False, None)))
    d._probe_once()
    assert d.count() == 1


def test_this_node_is_never_probed_over_the_network(monkeypatch):
    """It is the thing that would be answering; asking proves nothing."""
    probed = []
    monkeypatch.setattr(PeerDirectory, "_probe_peer",
                        staticmethod(lambda a: (probed.append(a) or False, None)))
    d = directory()
    d._apply(resolve("retself", "192.168.1.10"))
    d._probe_once()
    d._probe_once()
    assert probed == []
    assert d.count() == 1


def test_a_peer_with_no_address_is_not_reachable():
    assert PeerDirectory._probe_peer("") == (False, None)
    assert PeerDirectory._probe_peer(None) == (False, None)


def test_a_probe_that_answers_at_all_counts_as_alive(monkeypatch):
    """Not a 200 check: a calibrating node redirects, and a broken one 500s.

    Both are nodes the operator should still be able to open and look at, so
    both are alive. Neither carries a body worth keeping.
    """
    class Response:
        status_code = 302

        def json(self):
            raise ValueError("not JSON")

    monkeypatch.setattr(mdns_peers.http_requests, "get",
                        lambda *a, **k: Response())
    assert PeerDirectory._probe_peer("192.168.1.57") == (True, None)


def test_a_connection_error_is_not_alive(monkeypatch):
    def boom(*a, **k):
        raise mdns_peers.http_requests.ConnectionError("refused")

    monkeypatch.setattr(mdns_peers.http_requests, "get", boom)
    assert PeerDirectory._probe_peer("192.168.1.57") == (False, None)


def test_a_json_answer_comes_back_with_the_verdict(monkeypatch):
    """The body is the whole point: it is what a peer's card is drawn from."""
    class Response:
        status_code = 200

        def json(self):
            return {"ok": True, "node_id": "ret1"}

    monkeypatch.setattr(mdns_peers.http_requests, "get",
                        lambda *a, **k: Response())
    assert PeerDirectory._probe_peer("192.168.1.57") == (
        True, {"ok": True, "node_id": "ret1"})


# ── What the probe brings back ─────────────────────────────────
#
# The Summary card for a peer is drawn from whatever its last good /healthz
# said, because that request is the only regular contact between two nodes.


def test_a_good_answer_is_kept_for_the_card(monkeypatch):
    body = {"ok": True, "node_id": "ret1", "telemetry": {"node_ref": "ndabc"}}
    monkeypatch.setattr(PeerDirectory, "_probe_peer",
                        staticmethod(lambda a: (True, body)))
    d = directory()
    d._apply(resolve("ret1", "192.168.1.57"))
    d._probe_once()

    assert d.peers()[0]["healthz"] == body


def test_a_bad_answer_does_not_wipe_the_last_good_one(monkeypatch):
    """Better a card a few seconds out of date than one that empties itself
    every time a peer happens to be busy."""
    body = {"ok": True, "telemetry": {"node_ref": "ndabc"}}
    monkeypatch.setattr(PeerDirectory, "_probe_peer",
                        staticmethod(lambda a: (True, body)))
    d = directory()
    d._apply(resolve("ret1", "192.168.1.57"))
    d._probe_once()

    monkeypatch.setattr(PeerDirectory, "_probe_peer",
                        staticmethod(lambda a: (True, None)))
    d._probe_once()

    assert d.peers()[0]["healthz"] == body


def test_a_node_just_discovered_has_no_answer_yet():
    """So a card can tell "not heard from yet" apart from "has no telemetry"."""
    d = directory()
    d._apply(resolve("ret1", "192.168.1.57"))
    assert d.peers()[0]["healthz"] is None
