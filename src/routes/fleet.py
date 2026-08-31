"""Fleet data for the banner, and the endpoint peers use to probe this node.

Every node on the network gets a tab in the banner at the top of every page
(see `templates/_fleet_bar.html`), and each tab is a plain link to that node's
own `ret<node_id>.local`. There is no shell and no frame: the node you click
serves you its own pages, banner included, with itself marked active.

That is why nothing here decides *what* `owl.local` shows. It is answered by
whichever node replies first, and that node serves its own Home exactly as it
would under its own name. The only thing the shared alias costs you is that the
URL is ambiguous until you click a tab, which is why every tab is absolute.
"""

from flask import Blueprint, jsonify, render_template

bp = Blueprint("fleet", __name__)


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


def banner_nodes():
    """The tabs to draw, guaranteed to include this node.

    Discovery is a background browse that takes a second or two to populate,
    and it can come back empty on a network that blocks multicast. Neither is a
    reason to render a banner with no tabs at all: this node is self-evidently
    present, whatever mDNS believes. So if the peer list does not already carry
    us, put us at the front.
    """
    from app import peers, read_node_id

    nodes = [peer_view(p) for p in peers.peers()]
    if any(n["is_self"] for n in nodes):
        return nodes

    node_id = read_node_id()
    return [{
        "node_id": node_id,
        "name": node_id,
        "has_friendly_name": False,
        "address": "",
        "hostname": f"{node_id}.local",
        "url": f"http://{node_id}.local/",
        "is_self": True,
    }] + nodes


@bp.route("/summary")
def summary():
    """Fleet-scope information page.

    Placeholder content for now. Deliberately not a node list: the banner above
    it already is one, and two views of the same thing would drift apart.
    """
    return render_template("summary.html", active_page="summary")


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
    """
    from app import read_node_id

    return jsonify({"ok": True, "node_id": read_node_id()})
