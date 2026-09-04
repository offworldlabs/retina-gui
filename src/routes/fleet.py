"""Fleet data for the banner, the Summary page, and the endpoint peers probe.

Every node on the network gets a tab in the banner at the top of every page
(see `templates/_fleet_bar.html`), and each tab is a plain link to that node's
own `ret<node_id>.local`. There is no shell and no frame: the node you click
serves you its own pages, banner included, with itself marked active.

That is why nothing here decides *what* `owl.local` shows. It is answered by
whichever node replies first, and that node serves its own Home exactly as it
would under its own name. The only thing the shared alias costs you is that the
URL is ambiguous until you click a tab, which is why every tab is absolute.

## Where the Summary page gets its data

Nowhere new. The names, addresses and node IDs come from the mDNS browse the
banner needs anyway, and each peer's telemetry arrives on the `/healthz` probe
that already runs every 20 seconds whether or not anyone has the page open.
Rendering the page is a dict read and some local file reads. It is a
nice-to-have, and a nice-to-have that polls the fleet would not be worth
having.
"""

from urllib.parse import urlsplit

from flask import Blueprint, jsonify, render_template

bp = Blueprint("fleet", __name__)

# Where "Add another node" sends an owner with only one.
BUY_URL = "https://retina.fm"


def _resource(name, url, icon):
    """One Resources card.

    The host is derived rather than written out, so the line under the name
    cannot drift from where the card actually goes. It is there because every
    one of these leaves the device: an owner should be able to see they are
    about to be sent to github.com before they click, not after.
    """
    return {"name": name, "url": url, "icon": icon, "host": urlsplit(url).netloc}


# Where the full, driveable primer lives. Empty until it is published: the
# branch carrying it is unmerged, and offworldlabs.com/learn/ 404s today. A
# dead link on an owner's node is worse than no link, so the template omits
# the line entirely while this is blank, and turning it on is one string.
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

# The one telemetry state with nothing to say for itself. Everything else gets
# a chip, including states retina-telemetry grows later: an unfamiliar state
# showing up on a card is the right failure, and silence is not.
HEALTHY_STATE = "streaming"


def node_url(peer):
    """Where to send a browser for this node.

    Its own mDNS name rather than its address: it is stable, it is what the
    operator should learn to use, and any client that resolved owl.local to get
    here can necessarily resolve a ret*.local too, since both are plain mDNS.
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

    Discovery is a background browse that takes a second or two to populate,
    and it can come back empty on a network that blocks multicast. Neither is a
    reason to draw a banner with no tabs or a Summary with no cards: this node
    is self-evidently present, whatever mDNS believes. So if the peer list does
    not already carry us, put us at the front.

    Shared by the banner and the cards deliberately. Two answers to "which
    nodes are there" would disagree during exactly the seconds after boot when
    someone is most likely to be looking.
    """
    from app import peers, read_node_id

    nodes = list(peers.peers())
    if any(n["is_self"] for n in nodes):
        return nodes

    node_id = read_node_id()
    return [{
        "node_id": node_id,
        "friendly_name": "",
        "hostname": f"{node_id}.local",
        "address": "",
        "port": "80",
        "healthz": None,
        "is_self": True,
    }] + nodes


def banner_nodes():
    """The tabs to draw."""
    return [peer_view(p) for p in discovered_nodes()]


# ── Telemetry, as a card needs it ──────────────────────────────

def telemetry_payload():
    """This node's telemetry reduced to what a card draws, or None if absent.

    Local file reads only, and that is a constraint rather than an
    implementation detail: this goes out over /healthz, which is how every
    other node decides whether this one still exists. Anything here that could
    block or fail slowly would make a busy node look absent to its peers.
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

    Only the first character moves. `capitalize` lowercases the remainder,
    which is wrong the moment a state name carries an acronym.
    """
    if not state:
        return "Unknown"
    words = state.replace("_", " ")
    return words[0].upper() + words[1:]


def telemetry_view(payload):
    """The telemetry line for one card, or None to say nothing at all.

    Three silences, deliberately told apart:

        no payload      We have not heard from this node yet, or it runs a
                        build whose /healthz predates this field. We do not
                        know, so the card omits the row rather than inventing
                        an answer. Self-correcting: the next probe fills it.
        telemetry null  We asked, and the node has no telemetry package. That
                        is ordinary rather than a fault, so it is stated
                        plainly and not flagged.
        healthy         Registered and streaming. The identifier alone, no
                        chip, because a working node has nothing to report.
    """
    if payload is None or "telemetry" not in payload:
        return None

    status = payload["telemetry"]
    if status is None:
        return {"node_ref": None, "label": "Not installed", "kind": "muted"}

    node_ref = status.get("node_ref")

    # Staleness outranks state: a document too old to believe describes a
    # service that is no longer running, whatever it last claimed to be doing.
    if status.get("stale"):
        return {"node_ref": node_ref, "label": "Not reporting", "kind": "warn"}

    if status.get("state") == HEALTHY_STATE:
        # A healthy node can carry node_ref null for up to a heartbeat after
        # its container restarts, because telemetry holds it in memory. Say
        # something rather than drawing an empty row.
        return {
            "node_ref": node_ref,
            "label": None if node_ref else "Registered",
            "kind": None,
        }

    # An identifier and an unhappy state are independent, and both are worth
    # having: the state alone throws away the reference support asks for, and
    # the reference alone hides that the node is not currently registered.
    return {
        "node_ref": node_ref,
        "label": _in_words(status.get("state")),
        "kind": "warn",
    }


def card_view(peer):
    """One node's worth of Summary card.

    Separate from peer_view because the banner renders on every page and needs
    none of this. A tab is a name and a link; it should not pay for a file read
    or carry a state it never draws.
    """
    card = peer_view(peer)
    # The prober deliberately skips this node, since this process is what would
    # be answering, so our own telemetry is read here rather than arriving over
    # the network from ourselves.
    payload = ({"telemetry": telemetry_payload()} if peer["is_self"]
               else peer.get("healthz"))
    card["telemetry"] = telemetry_view(payload)
    return card


# ── Routes ─────────────────────────────────────────────────────

@bp.route("/summary")
def summary():
    """The fleet, as cards.

    Deliberately not a second node list competing with the banner above it:
    the banner answers "which node am I looking at", and this answers "what do
    I have, and is any of it unwell".
    """
    return render_template("summary.html",
                           active_page="summary",
                           cards=[card_view(p) for p in discovered_nodes()],
                           resources=RESOURCES,
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

    Deliberately trivial and dependency-free. It answers "is there a retina-gui
    serving on this address", which is the only question a peer needs to ask,
    not whether the radar is healthy, which is what the node's own page is for.
    Anything heavier here would make a busy node look absent.

    `telemetry` rides along because this probe is the only regular contact
    between nodes, and a card on someone else's Summary page has no other way
    to learn an identifier that lives on this node's disk. It stays inside the
    rule above: local file reads, nothing over a socket. A blah2 call here to
    report the radar would break it, which is why the radar is not here.
    """
    from app import read_node_id

    return jsonify({
        "ok": True,
        "node_id": read_node_id(),
        "telemetry": telemetry_payload(),
    })
