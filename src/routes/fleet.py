"""The fleet landing page, and the endpoint peers use to probe this node.

`owl.local` is answered by every node at once (see owl-mdns-alias in owl-os), so
the browser lands on whichever one replied first. That is deliberate and it does
not matter which: every node serves this same page, listing every node it can
see, and each card links to that node's own permanent `ret*.local` address.
"""

from flask import Blueprint, jsonify, redirect, render_template

bp = Blueprint("fleet", __name__)

# The names every node answers to jointly, as opposed to its own ret*.local.
# A request that arrived on one of these is someone looking for "a node", not
# for this node, which is what makes it safe to send them somewhere else.
SHARED_ALIAS_HOSTS = ("owl.local", "owl")


def node_url(peer):
    """Where to send a browser for this node.

    Its own mDNS name rather than its address: it is stable, it is what the
    operator should learn to use, and any client that resolved owl.local to get
    here can necessarily resolve a ret*.local too — both are plain mDNS. The
    address is shown on the card as a fallback for when that is not true, and
    as something to check a card against.
    """
    return f"http://{peer['hostname'] or peer['node_id'] + '.local'}/"


def peer_view(peer):
    """The card's worth of a peer, without the internal bookkeeping."""
    return {
        "node_id": peer["node_id"],
        "name": peer["friendly_name"] or peer["node_id"],
        "has_friendly_name": bool(peer["friendly_name"]),
        "address": peer["address"],
        "hostname": peer["hostname"],
        "url": node_url(peer),
        "is_self": peer["is_self"],
    }


def is_entry_point(host):
    """Did this request arrive on the shared alias rather than a node's name?"""
    return (host or "").split(":")[0].lower() in SHARED_ALIAS_HOSTS


def entry_point_response():
    """What `owl.local` should do right now, or None to serve the node UI.

    The single place the node count changes anything. Everything else — the
    names published, the service advertised, the records on the wire — is
    identical whether there is one node on the network or ten.

    With one node there is no list worth showing, so the browser is sent
    straight on to that node's own address. Doing it as a redirect rather than
    just rendering the node's page means the operator sees `ret4c844c20.local`
    in the URL bar and bookmarks *that*, so the day a second node arrives and
    owl.local starts showing a list instead, the bookmark they already have
    still goes where it always went.
    """
    from app import peers, read_node_id

    if peers.count() > 1:
        return render_fleet()

    node_id = read_node_id()
    if not node_id or node_id == "Unknown":
        # No name to send them to. Serving this node's page is a better answer
        # than redirecting to something that will not resolve.
        return None
    return redirect(f"http://{node_id}.local/", code=302)


def render_fleet():
    from app import peers

    return render_template("fleet.html",
                           nodes=[peer_view(p) for p in peers.peers()],
                           active_page="fleet")


@bp.route("/fleet")
def fleet():
    """Every node on this network, one card each.

    Always reachable by this path, whatever the node count and whichever name
    was used to get here — the count only decides what `/` does.
    """
    return render_fleet()


@bp.route("/api/fleet/peers")
def fleet_peers():
    """The same list as JSON, so the page can refresh without a reload."""
    from app import peers

    return jsonify({"nodes": [peer_view(p) for p in peers.peers()]})


@bp.route("/healthz")
def healthz():
    """Liveness, for the other nodes' probes.

    Deliberately trivial and dependency-free. It answers "is there a retina-gui
    serving on this address", which is the only question the fleet page needs
    to ask — not whether the radar is healthy, which is what the node's own page
    is for. Anything heavier here would make a busy node look absent.
    """
    from app import read_node_id

    return jsonify({"ok": True, "node_id": read_node_id()})
