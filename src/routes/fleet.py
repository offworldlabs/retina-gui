"""Fleet data for the banner, the Summary page, and the endpoint peers probe.

Each banner tab is an absolute link to that node's own ret<node_id>.local, so
nothing here decides what the shared owl.local shows. The Summary page reuses
the mDNS browse and /healthz probe data and fetches nothing new.
See docs/features/fleet-and-naming.md.
"""

from urllib.parse import urlsplit

from flask import Blueprint, jsonify, render_template

from mdns_peers import sort_key

bp = Blueprint("fleet", __name__)

# Where "Add another node" sends an owner with only one.
BUY_URL = "https://retina.fm"


def _resource(name, url, icon):
    """One Resources card, with the host derived from the URL so it cannot drift.

    See docs/features/fleet-and-naming.md#resources-and-help-links.
    """
    return {"name": name, "url": url, "icon": icon,
            "host": urlsplit(url).netloc, "external": True}


def _mailto(name, address):
    """A support address, as a card. Not external: it opens a mail client.

    See docs/features/fleet-and-naming.md#resources-and-help-links.
    """
    return {"name": name, "url": "mailto:" + address, "icon": "mail",
            "host": address, "external": False}


# Where the full, driveable primer lives. Empty until it is published (the
# target 404s today); the template omits the line while this is blank.
PRIMER_URL = ""

# Fixed links out, ordered by distance from the node: the company, the manual
# for this box, then the two live views of the wider network.
RESOURCES = (
    _resource("Offworld Labs", "https://offworldlabs.com", "globe"),
    _resource("Retina Wiki",
              "https://github.com/offworldlabs/owl-os/wiki/4-Troubleshooting-and-Tuning",
              "book"),
    _resource("Retina Network Map", "https://map.retina.fm", "map"),
    _resource("Retina Dashboard", "https://dash.retina.fm", "chart"),
)

# Where to go when something is wrong. The Discord is the blah2 project's
# community server, not Offworld Labs support, and is labelled as such.
# See docs/features/fleet-and-naming.md#resources-and-help-links.
HELP = (
    _resource("blah2 Discord", "https://discord.gg/ewNQbeK5Zn", "chat"),
    _mailto("Email us", "info@offworldlabs.com"),
)

# The one telemetry state with nothing to say for itself. Everything else,
# including states retina-telemetry adds later, gets a chip.
HEALTHY_STATE = "streaming"


def node_url(peer):
    """Where to send a browser for this node: its own mDNS name, not its address.

    See docs/features/fleet-and-naming.md#node-addresses.
    """
    return f"http://{peer['hostname'] or peer['node_id'] + '.local'}/"


def peer_view(peer):
    """One node's worth of banner tab, without the internal bookkeeping."""
    return {
        "node_id": peer["node_id"],
        "name": peer["friendly_name"] or peer["node_id"],
        "has_friendly_name": bool(peer["friendly_name"]),
        "address": peer["address"],
        "hostname": peer["hostname"],
        "url": node_url(peer),
        "is_self": peer["is_self"],
    }


def discovered_nodes():
    """Every node believed present, guaranteed to include this one.

    Adds a stand-in for this node when discovery has not seen it yet, then
    re-sorts with `mdns_peers.sort_key` (never prepends) so the order matches
    every other node. Shared by the banner and the cards so they cannot
    disagree. See docs/features/fleet-and-naming.md#the-fleet-bar.
    """
    from app import peers, read_node_id

    nodes = list(peers.peers())
    if any(n["is_self"] for n in nodes):
        return nodes

    node_id = read_node_id()
    nodes.append({
        "node_id": node_id,
        "friendly_name": "",
        "hostname": f"{node_id}.local",
        "address": "",
        "port": "80",
        "healthz": None,
        "is_self": True,
    })
    return sorted(nodes, key=sort_key)


def banner_nodes():
    """The tabs to draw."""
    return [peer_view(p) for p in discovered_nodes()]


# ── Telemetry, as a card needs it ──────────────────────────────

def telemetry_payload():
    """This node's telemetry reduced to what a card draws, or None if absent.

    Local file reads only: this goes out over /healthz, and anything that could
    block would make a busy node look absent to its peers.
    """
    from app import telemetry_status

    status = telemetry_status.read()
    if status is None:
        return None
    return {
        "node_ref": status["node_ref"],
        "state": status["state"],
        "stale": status["stale"],
    }


def _in_words(state):
    """`awaiting_config` as `Awaiting config`.

    Only the first character changes; `capitalize` would lowercase acronyms.
    """
    if not state:
        return "Unknown"
    words = state.replace("_", " ")
    return words[0].upper() + words[1:]


def telemetry_view(payload):
    """The telemetry line for one card, or None to say nothing at all.

    None means not heard from yet (or a /healthz that predates the field), as
    distinct from telemetry null ("Not installed") and healthy (node_ref only).
    See docs/features/fleet-and-naming.md#telemetry-on-cards.
    """
    if payload is None or "telemetry" not in payload:
        return None

    status = payload["telemetry"]
    if status is None:
        return {"node_ref": None, "label": "Not installed", "kind": "muted"}

    node_ref = status.get("node_ref")

    # Staleness outranks state: a stale document says nothing about now.
    if status.get("stale"):
        return {"node_ref": node_ref, "label": "Not reporting", "kind": "warn"}

    if status.get("state") == HEALTHY_STATE:
        # node_ref can be null for up to a heartbeat after a telemetry
        # restart; say something rather than drawing an empty row.
        return {
            "node_ref": node_ref,
            "label": None if node_ref else "Registered",
            "kind": None,
        }

    # Show both: support needs the reference, and the state says it is unwell.
    return {
        "node_ref": node_ref,
        "label": _in_words(status.get("state")),
        "kind": "warn",
    }


def card_view(peer):
    """One node's worth of Summary card.

    Separate from peer_view so the banner, drawn on every page, never pays for
    the telemetry file read.
    """
    card = peer_view(peer)
    # The prober skips this node, so our own telemetry is read locally.
    payload = ({"telemetry": telemetry_payload()} if peer["is_self"]
               else peer.get("healthz"))
    card["telemetry"] = telemetry_view(payload)
    return card


# ── Routes ─────────────────────────────────────────────────────

@bp.route("/summary")
def summary():
    """The fleet, as cards: what do I have, and is any of it unwell.

    See docs/features/fleet-and-naming.md#the-summary-page.
    """
    return render_template("summary.html",
                           active_page="summary",
                           cards=[card_view(p) for p in discovered_nodes()],
                           resources=RESOURCES,
                           help_links=HELP,
                           primer_url=PRIMER_URL,
                           buy_url=BUY_URL)


@bp.route("/api/fleet/peers")
def fleet_peers():
    """The discovered nodes as JSON."""
    from app import peers

    return jsonify({"nodes": [peer_view(p) for p in peers.peers()]})


@bp.route("/healthz")
def healthz():
    """Liveness, for the other nodes' probes.

    Must stay trivial: local file reads only, nothing over a socket (so no
    blah2 call), or a busy node looks absent to its peers.
    See docs/features/fleet-and-naming.md#the-healthz-endpoint.
    """
    from app import read_node_id

    return jsonify({
        "ok": True,
        "node_id": read_node_id(),
        "telemetry": telemetry_payload(),
    })
