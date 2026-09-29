"""Discovery of the other owl nodes on this LAN.

Browses the `_owl-node._tcp` DNS-SD service (advertised by owl-mdns-identity in
owl-os) and keeps a live list of nodes for the fleet bar and Summary page. A
node counts as present only while it answers an HTTP probe, not merely while it
is in the mDNS cache.

See docs/features/fleet-and-naming.md#peer-discovery.
"""

import ipaddress
import json
import os
import re
import shutil
import subprocess
import threading
import time

import requests as http_requests

SERVICE_TYPE = "_owl-node._tcp"

# See docs/features/fleet-and-naming.md#liveness-probing for these values.
PROBE_INTERVAL_SECONDS = 20
PROBE_TIMEOUT_SECONDS = 2
# Consecutive failures before a peer is treated as gone (hysteresis).
FAILURES_BEFORE_GONE = 2

# How long one `avahi-browse` runs before it is replaced. `-r` resolves each
# service only once, so without a restart a peer's rename (new TXT record) is
# never seen. See docs/features/fleet-and-naming.md#browse-restarts.
BROWSE_RESTART_SECONDS = 60

# avahi-browse escapes non-printables in the instance name as a backslash and
# three decimal digits.
_ESCAPE = re.compile(r"\\(\d{3})")
# TXT records arrive as a run of double-quoted strings on the end of the line.
_TXT = re.compile(r'"((?:[^"\\]|\\.)*)"')


def _unescape(value):
    return _ESCAPE.sub(lambda m: chr(int(m.group(1))), value)


def _is_ipv4(address):
    """Whether this address can be dropped into a URL as it stands.

    Judged from the address, never from avahi's protocol column: an `IPv4`
    line can carry an IPv6 link-local address.
    See docs/features/fleet-and-naming.md#addresses-and-interfaces.
    """
    try:
        return isinstance(ipaddress.ip_address(address), ipaddress.IPv4Address)
    except ValueError:
        return False


def parse_txt(blob):
    """Pull `key=value` pairs out of avahi-browse's quoted TXT field."""
    pairs = {}
    for entry in _TXT.findall(blob or ""):
        entry = entry.replace('\\"', '"').replace("\\\\", "\\")
        key, _, value = entry.partition("=")
        if key:
            pairs[key] = value
    return pairs


def sort_key(peer):
    """How the fleet is ordered, identically on every node: node_id alone.

    Must not depend on which node is drawing the banner or on the friendly
    name, or tabs reorder between nodes and during renames.
    See docs/features/fleet-and-naming.md#fleet-order.
    """
    return peer["node_id"]


def parse_line(line):
    """Turn one line of `avahi-browse -p` output into a dict, or None.

    Only resolved (`=`) and removal (`-`) events carry anything useful. The
    `+` announcement that precedes a resolve is ignored: the `=` follows.
    """
    # maxsplit keeps a TXT value containing a semicolon in one piece; every
    # field before the TXT blob is semicolon-free.
    fields = line.rstrip("\n").split(";", 9)
    if not fields or fields[0] not in ("=", "-"):
        return None
    if fields[0] == "-":
        if len(fields) < 4:
            return None
        return {"event": "remove", "interface": fields[1],
                "protocol": fields[2], "name": _unescape(fields[3])}
    if len(fields) < 9:
        return None
    txt = parse_txt(fields[9] if len(fields) > 9 else "")
    return {
        "event": "resolve",
        "interface": fields[1],
        "protocol": fields[2],
        "name": _unescape(fields[3]),
        "hostname": fields[6],
        "address": fields[7],
        "port": fields[8],
        "node_id": txt.get("node_id") or _unescape(fields[3]),
        "friendly_name": txt.get("name") or "",
    }


class PeerDirectory:
    """Live view of the owl nodes on this LAN, including this one.

    Owns two threads, so exactly one of these may exist per process. It is
    constructed in services.py for that reason.
    """

    def __init__(self, own_node_id_fn, dev_mode=False, fixture_path=None):
        self._own_node_id_fn = own_node_id_fn
        self._dev_mode = dev_mode
        self._fixture_path = fixture_path
        self._lock = threading.Lock()
        # name -> peer dict. Keyed on the DNS-SD instance name because that is
        # the only field a removal event carries.
        self._peers = {}
        self._started = False

    # ── Lifecycle ──────────────────────────────────────────────

    def start(self):
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._browse_forever, daemon=True,
                         name="mdns-browse").start()
        threading.Thread(target=self._probe_forever, daemon=True,
                         name="mdns-probe").start()

    # ── What the routes read ───────────────────────────────────

    def peers(self):
        """Every node believed present, in the fleet's shared order.

        See `sort_key`. Sorted so the page does not reshuffle between
        refreshes, and so it does not reshuffle between nodes either.
        """
        with self._lock:
            live = [dict(p) for p in self._peers.values() if p["alive"]]
        live.sort(key=sort_key)
        return live

    def count(self):
        """How many nodes are present, including this one."""
        return len(self.peers())

    # ── Browsing ───────────────────────────────────────────────

    def _browse_forever(self):
        while True:
            try:
                self._browse_once()
            except Exception as e:  # noqa: BLE001 - a browse must never kill the thread
                print(f"mdns_peers: browse failed: {e}", flush=True)
            # Reached both on the ordinary restart above and when avahi is
            # unavailable. Short enough not to leave a real gap in the fast
            # path, long enough that a daemon which is down is not spun on.
            time.sleep(5)

    def _browse_once(self):
        if self._fixture_path:
            self._load_fixture()
            time.sleep(PROBE_INTERVAL_SECONDS)
            return
        if not shutil.which("avahi-browse"):
            # Ordinary in dev; on a node it means the image is missing
            # avahi-utils, which is worth saying out loud once per retry.
            print("mdns_peers: avahi-browse not installed", flush=True)
            time.sleep(60)
            return

        # -p parsable, -r resolve to address and TXT, -k skip the service-type
        # database lookup, -f keep trying rather than exiting when the daemon
        # is briefly unavailable. Deliberately not -l: this node's own
        # advertisement is wanted, so it can be shown as "this node".
        process = subprocess.Popen(
            ["avahi-browse", "-p", "-r", "-k", "-f", SERVICE_TYPE],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        # Ends the stream from the outside: iterating stdout blocks, so the
        # deadline cannot be enforced from within this loop.
        deadline = threading.Timer(BROWSE_RESTART_SECONDS, process.terminate)
        deadline.daemon = True
        deadline.start()
        try:
            for line in process.stdout:
                event = parse_line(line)
                if event:
                    self._apply(event)
        finally:
            deadline.cancel()
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    def _apply(self, event):
        own = self._own_node_id_fn()
        with self._lock:
            if event["event"] == "remove":
                peer = self._peers.get(event["name"])
                if not peer:
                    return
                peer["sources"].discard((event["interface"], event["protocol"]))
                # A node on both Ethernet and WiFi produces one removal per
                # interface. It has only really gone when the last one goes.
                if not peer["sources"]:
                    del self._peers[event["name"]]
                return

            peer = self._peers.setdefault(event["name"], {
                "node_id": event["node_id"],
                "friendly_name": event["friendly_name"],
                "hostname": event["hostname"],
                # Only ever an address we can use. See _is_ipv4, and the note
                # further down where a later resolve may fill this in.
                "address": event["address"] if _is_ipv4(event["address"]) else "",
                "port": event["port"],
                "sources": set(),
                # Assumed present on first sight; the prober demotes it if not.
                "alive": True,
                "failures": 0,
                # The peer's last good /healthz body. Filled by the prober, and
                # the only channel by which anything a node holds on its own
                # disk reaches another node's page.
                "healthz": None,
                "is_self": event["node_id"] == own,
            })
            peer["sources"].add((event["interface"], event["protocol"]))
            peer["node_id"] = event["node_id"]
            peer["friendly_name"] = event["friendly_name"]
            peer["hostname"] = event["hostname"]
            peer["is_self"] = event["node_id"] == own
            # Keep only a reachable (IPv4) address. With none, the hostname
            # serves the prober and the browser until a later resolve fills it.
            if _is_ipv4(event["address"]):
                peer["address"] = event["address"]
                peer["port"] = event["port"]

    def _load_fixture(self):
        """Dev-only: read the peer list from a JSON file instead of the LAN."""
        try:
            with open(self._fixture_path) as f:
                entries = json.load(f)
        except (OSError, ValueError) as e:
            print(f"mdns_peers: fixture unreadable: {e}", flush=True)
            return
        own = self._own_node_id_fn()
        with self._lock:
            self._peers = {}
            for entry in entries:
                node_id = entry.get("node_id", "")
                self._peers[node_id] = {
                    "node_id": node_id,
                    "friendly_name": entry.get("name", ""),
                    "hostname": entry.get("hostname", f"{node_id}.local"),
                    "address": entry.get("address", ""),
                    "port": entry.get("port", "80"),
                    "sources": {("fixture", "IPv4")},
                    "alive": entry.get("alive", True),
                    "failures": 0,
                    "healthz": entry.get("healthz"),
                    "is_self": node_id == own,
                }

    # ── Probing ────────────────────────────────────────────────

    def _probe_forever(self):
        while True:
            time.sleep(PROBE_INTERVAL_SECONDS)
            try:
                self._probe_once()
            except Exception as e:  # noqa: BLE001 - never kill the thread
                print(f"mdns_peers: probe failed: {e}", flush=True)

    def _probe_once(self):
        if self._fixture_path:
            return
        with self._lock:
            targets = [(name, p["address"], p["hostname"], p["is_self"])
                       for name, p in self._peers.items()]

        for name, address, hostname, is_self in targets:
            # No point probing ourselves over the network to find out we are
            # up: this process is what would be answering.
            if is_self:
                reachable, payload = True, None
            else:
                # By name when no usable address has been seen yet, so a node
                # whose A record has not arrived is not declared gone.
                reachable, payload = self._probe_peer(address or hostname)
            with self._lock:
                peer = self._peers.get(name)
                if not peer:
                    continue
                if reachable:
                    peer["failures"] = 0
                    peer["alive"] = True
                    # Only when the answer parsed. A node that redirects or
                    # errors is still present, and its last good answer beats
                    # blanking the card it feeds.
                    if payload is not None:
                        peer["healthz"] = payload
                else:
                    peer["failures"] += 1
                    if peer["failures"] >= FAILURES_BEFORE_GONE:
                        peer["alive"] = False

    @staticmethod
    def _probe_peer(address):
        """Ask a peer whether it is there, and keep what it says.

        Returns `(reachable, payload)`, deliberately independent. Reachable is
        any HTTP answer at all (a calibrating node redirects, a broken one may
        500); payload is the JSON object body, or None.
        See docs/features/fleet-and-naming.md#liveness-probing.
        """
        if not address:
            return False, None
        try:
            response = http_requests.get(f"http://{address}/healthz",
                                         timeout=PROBE_TIMEOUT_SECONDS)
        except http_requests.RequestException:
            return False, None
        try:
            payload = response.json()
        except ValueError:
            return True, None
        return True, payload if isinstance(payload, dict) else None


def peer_directory_from_env(own_node_id_fn, dev_mode):
    """Build the directory, honouring the dev fixture escape hatch."""
    fixture = os.environ.get("MDNS_PEERS_FIXTURE") if dev_mode else None
    return PeerDirectory(own_node_id_fn, dev_mode=dev_mode, fixture_path=fixture)
