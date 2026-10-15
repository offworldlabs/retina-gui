# Fleet and node naming

Every owl node on a LAN finds the others over mDNS (DNS-SD), and every page retina-gui serves carries a fleet bar: one tab per node, each a link to that node's own address. A Summary page shows the same fleet as cards, with each node's telemetry state. Nodes can be given a friendly name, which is a display label only and travels to the other nodes in the DNS-SD TXT record.

## Where it lives

| File | Role |
| --- | --- |
| [mdns_peers.py](../../src/mdns_peers.py) | `PeerDirectory`: runs `avahi-browse`, parses its output, probes each peer's `/healthz`, and holds the live peer list. Also `sort_key`, the fleet order. |
| [routes/fleet.py](../../src/routes/fleet.py) | `/summary`, `/api/fleet/peers` and `/healthz`. Builds the banner tabs (`banner_nodes`) and Summary cards (`card_view`, `telemetry_view`). |
| [node_name.py](../../src/node_name.py) | `NodeName`: reads, validates and writes the friendly name, then asks owl-os to re-advertise it. |
| [templates/_fleet_bar.html](../../templates/_fleet_bar.html) | The `fleet_bar` macro: brand block, Summary tab, one tab per node, the outbound link. |
| [services.py](../../src/services.py) | Constructs the single `PeerDirectory` (`peers`) and `NodeName` (stored at `node-name` in the data directory). |
| [routes/config.py](../../src/routes/config.py) | `POST /node-name`, the rename endpoint behind the name field on `/config`. |
| owl-mdns-identity (owl-os) | Writes the `_owl-node._tcp` service file at boot and again on rename. |

## Node addresses

Each node has two mDNS names:

| Name | Owner | Use |
| --- | --- | --- |
| `ret<node_id>.local` | This node only | Stable for the life of the board (the node_id is derived from the board serial). The address to bookmark, and what every fleet tab links to. |
| `owl.local` | Shared by every node | A convenient way in. Whichever node replies first answers it, so it may reach any of them. |

Nothing in retina-gui decides what `owl.local` shows. The node that answers serves its own pages exactly as it would under its own name (the Home route is host-agnostic). The only cost of the shared alias is that the URL is ambiguous until you click a tab, which is why every tab is an absolute link to a `ret<node_id>.local` name, including the tab for the current node. Clicking any tab lands on a concrete node address rather than on `owl.local`.

`node_url` builds that link from the peer's advertised hostname, falling back to `<node_id>.local`. The name is used rather than the address because it is stable, it is what the operator should learn to use, and any client that resolved `owl.local` to get here can necessarily resolve a `ret*.local` name too, since both are plain mDNS.

## Peer discovery

Each node advertises the DNS-SD service type `_owl-node._tcp`. The service file is written at boot by owl-mdns-identity in owl-os, and its TXT record carries `node_id` and `name` (the friendly name). `PeerDirectory` keeps a live picture of who is advertising. It owns two daemon threads (browse and probe), so exactly one may exist per process; it is constructed once in `services.py` and started from `app.py`.

### Why a service type

Discovery looks for a service type rather than for host names. DNS-SD is multi-instance by design: every node advertising the same service type is the normal case, so there is nothing to collide over. Host names are different: two nodes wanting one name is a conflict Avahi has to arbitrate. The SRV record DNS-SD generates also points at a host name rather than an address, so it cannot go stale the way an address record can.

### The browse stream

`_browse_once` runs `avahi-browse -p -r -k -f _owl-node._tcp`:

| Flag | Why |
| --- | --- |
| `-p` | Parsable, semicolon-separated output. |
| `-r` | Resolve to address, port and TXT. |
| `-k` | Skip the service-type database lookup. |
| `-f` | Keep trying rather than exiting while the daemon is briefly unavailable. |
| (no `-l`) | Deliberately omitted. This node's own advertisement is wanted, so it can be shown as "this node". |

`parse_line` turns each line into an event. Only resolve (`=`) and removal (`-`) lines matter. The `+` announcement that precedes a resolve says a name exists but not where it is, so it is ignored: the `=` for the same name follows. The line is split with a maxsplit so a TXT value containing a semicolon stays in one piece (every field before the TXT blob is semicolon-free). avahi-browse escapes non-printables in the instance name as a backslash and three decimal digits, which `_unescape` reverses. TXT records arrive as a run of double-quoted strings at the end of the line, parsed by `parse_txt`. If the TXT record has no `node_id`, the instance name is used.

Peers are keyed on the DNS-SD instance name, because that is the only field a removal event carries. A node on both Ethernet and WiFi produces one resolve and one removal per interface and protocol, so each peer tracks a set of `(interface, protocol)` sources and is only deleted when the last one goes.

If `avahi-browse` is not installed, the browser logs it and retries every 60 seconds. That is ordinary in dev; on a node it means the image is missing avahi-utils. After any browse ends (normally or because Avahi is unavailable), the loop waits 5 seconds before starting another: short enough not to leave a real gap, long enough not to spin on a daemon that is down.

### Browse restarts

Each `avahi-browse` process is terminated after `BROWSE_RESTART_SECONDS` (60 s) and replaced. A `threading.Timer` does this from outside, because iterating the process's stdout blocks and the deadline cannot be enforced from inside that loop.

The stream is the fast path: it reports a node appearing or going away the moment it happens. What it will not report is a change to an existing node. `avahi-browse -r` resolves each service once, when it first sees it, and never again. So when a node is renamed, it updates its own TXT record and announces it, every other node hears the announcement at the Avahi layer, and none of them notices, because their browser already considers that service resolved. This was observed in practice: the new name was on the wire and visible to `avahi-browse` run by hand, while the fleet page kept showing the old one indefinitely.

Restarting the browser re-resolves everything, so a rename reaches other nodes within a minute. It costs one short-lived process a minute and keeps the instant add/remove path rather than replacing it with polling.

### Addresses and interfaces

A peer only ever stores an IPv4 address. `_is_ipv4` judges this from the address itself, not from avahi's protocol column, which describes the socket an announcement arrived on rather than what was resolved. An `IPv4` resolve line has been seen in the field carrying an IPv6 link-local address, and trusting that column made a healthy node vanish from every other node's banner 40 seconds after it appeared.

A link-local address needs a zone index to be usable, so it fails every probe, and it is worse than useless to an owner reading it off a card as the fallback for when the name will not resolve. So a peer with no usable address simply holds none. The hostname still serves both the prober and the browser, and a later resolve fills the address in.

## Liveness probing

Appearing in a browse is not evidence a node is reachable. RFC 6762 gives service PTR records a 75-minute TTL (only SRV and A records get the 120-second one), and a node powered off at the wall sends no goodbye packet. Left to the mDNS cache, an unplugged node would keep its tab and card for over an hour after it stopped answering.

So a node counts as present only while it answers an HTTP probe:

| Setting | Value | Reason |
| --- | --- | --- |
| `PROBE_INTERVAL_SECONDS` | 20 | Long enough that a rebooting node does not vanish, short enough that one genuinely gone is cleared while the operator is still looking. |
| `PROBE_TIMEOUT_SECONDS` | 2 | Per request. |
| `FAILURES_BEFORE_GONE` | 2 | Hysteresis. A marginal WiFi link should not make a node flicker in and out on every refresh. |

Rules in `_probe_once` and `_probe_peer`:

- A newly resolved peer is assumed alive. The prober demotes it if that turns out wrong, which is the right way round: a node that just appeared is almost always real.
- This node is never probed over the network. This process is what would be answering.
- A peer is probed by IPv4 address when one is known, otherwise by hostname. mDNS resolution is how every other client reaches a node, so this keeps a node whose A record has not arrived yet from being declared gone while it is running fine.
- `GET http://<address>/healthz`. Reachable means any HTTP answer at all, not a 200 carrying valid JSON: a node mid-calibration redirects most GETs, and one returning 500 is still a node the operator should be able to reach and look at.
- The payload is kept independently of reachability, and only when the body parses as a JSON object. A node that redirects or errors keeps its last good payload rather than blanking its card.
- A success resets the failure count and marks the peer alive; two consecutive failures mark it not alive. Peers that are not alive are hidden by `peers()` but stay in the directory, so a later successful probe brings them back.

The probe runs every 20 seconds whether or not anyone has a page open, which is why the Summary page costs nothing extra: it reads the body that used to be thrown away.

## Fleet order

`sort_key` orders the fleet by `node_id` alone, and every place that lists nodes (`PeerDirectory.peers`, `discovered_nodes`) sorts with it. The point is that every node agrees. Each node draws the banner for itself, so an order that depends on where you are standing gives the operator a different row of tabs on every box. That is what happened while each node sorted itself to the front, and it makes a tab move under the cursor as you click through the fleet.

Everything else available drifts. The friendly name arrives in a TXT record that can be up to a browse restart out of date, so during a rename the nodes genuinely disagree about the value they would sort on. An unnamed node would also sort ahead of every named one until somebody named it, then jump. The node_id is derived from the board serial: unique, fixed for the life of the board, and known to every node as soon as it has seen the peer at all.

The resulting order is arbitrary rather than meaningful, and that is the trade. An arbitrary order that never changes can be learned; a meaningful one that differs on each node cannot. Which node you are looking at is answered by the active tab, not by position.

## The fleet bar

`fleet_bar(nodes, node_id, active_page, show_brand=true)` in `_fleet_bar.html` renders the top row of every page. `app.py` injects `fleet_nodes` (from `banner_nodes`) into every template context; `base.html` and `setup.html` call the macro.

It is a macro rather than a plain include because the wizard needs it without the brand block, and Jinja does not pass `{% with %}` locals into an included template (they live in the compiled function, not the context dict), so the include silently sees nothing. Macro parameters are explicit and cannot fail that way. The wizard passes `show_brand=false` so it does not stack "OWL-OS" directly above its own "OWL-OS Setup" for the same node.

Display rules:

| Element | Rule |
| --- | --- |
| Summary tab | Always first. Active when `active_page == 'summary'`. Carries no antenna icon, because it is not a node. |
| Node tabs | One per entry in `nodes`, in fleet order. Each shows the antenna icon used on node cards and the "listening on" row, so it reads as a node rather than another section of the page. |
| Tab label | The friendly name, or the node_id when unnamed (`peer_view`). |
| Tab tooltip | The node_id, plus the IPv4 address when known. |
| Tab link | The node's absolute `http://ret<node_id>.local/` URL (see [Node addresses](#node-addresses)). |
| Active tab | This node's tab, unless the Summary tab is active. The page is always served by the node you are looking at, so there is nothing to track: the macro compares against the `node_id` passed in. |
| Outbound link | Network Map and Dashboard (`app.retina.fm`). It leaves the app, so it carries the outbound arrow. |
| `data-tutorial` | Every tab carries `data-tutorial="fleet-tabs"`, and this node's tab also `fleet-this-tab`, which are what the guided tutorial lights up. On the tabs rather than the row so the row's markup is unchanged. See [tutorial.md](tutorial.md#targets). |

`discovered_nodes` guarantees this node is in the list. Discovery takes a second or two to populate, and it can come back empty on a network that blocks multicast; neither is a reason to draw a banner with no tabs. If the peer list does not already include this node, a stand-in entry is added (hostname `<node_id>.local`, no address, no friendly name) and the list is re-sorted rather than prepended, so the tab does not sit first for a second and then move. The banner and the Summary cards share this function deliberately: two answers to "which nodes are there" would disagree during exactly the seconds after boot when someone is most likely to be looking.

Note the stand-in carries no friendly name, so during those first seconds this node's own tab shows its node_id even if it has been named.

## The Summary page

`/summary` renders the fleet as cards. It is deliberately not a second node list competing with the banner: the banner answers "which node am I looking at", and the Summary answers "what do I have, and is any of it unwell".

It fetches nothing new. Names, addresses and node IDs come from the browse the banner already needs, and each peer's telemetry arrives on the `/healthz` probe that already runs. Rendering is a dict read plus local file reads. A nice-to-have that polled the fleet would not be worth having.

`card_view` is separate from `peer_view` because the banner renders on every page and needs none of the card's data: a tab is a name and a link, and should not pay for a file read or carry state it never draws. A card shows the friendly name with the node_id underneath, or the node_id with "Not yet named".

### Telemetry on cards

For peers, the card's telemetry comes from the last good `/healthz` payload. For this node it is read locally via `telemetry_payload`, since the prober never probes itself.

`telemetry_view` distinguishes these cases, in this order:

| Case | Card shows | Why |
| --- | --- | --- |
| No payload, or payload has no `telemetry` key | No telemetry row | Not heard from yet, or the node runs a build whose `/healthz` predates the field. Unknown, so nothing is invented. The next probe fills it in. |
| `telemetry` is null | "Not installed", muted | The node has no telemetry package. Ordinary, not a fault. |
| `stale` is true | node_ref (if any) and "Not reporting", warn | Staleness outranks state: a document too old to believe describes a service that is no longer running, whatever it last claimed. |
| State is `streaming` (`HEALTHY_STATE`) | node_ref alone, no chip; "Registered" if node_ref is null | A working node has nothing to report. node_ref can be null for up to a heartbeat after the telemetry container restarts (it holds it in memory), so something is shown rather than an empty row. |
| Any other state | node_ref (if any) and the state in words, warn | Both halves matter: the state alone throws away the reference support asks for, and the reference alone hides that the node is not registered. Unfamiliar future states also land here, because a chip for an unknown state is the right failure and silence is not. |

`_in_words` turns `awaiting_config` into `Awaiting config`. It uppercases only the first character, because `str.capitalize` lowercases the rest and would mangle a state name containing an acronym.

### Resources and help links

The Summary page also carries fixed outbound links, defined in `routes/fleet.py`:

- `RESOURCES`, ordered by distance from the node: the company site, the manual (owl-os wiki), the live view of the wider network (Network Map and Dashboard), then two outside ones: Passive Radar News, a site about the field (written as `passiveradar.com`, without the `www.` it redirects to, because the card shows the host as written), and the blah2 Discord, the community around the radar software the node runs. The network's map and dashboard used to be two sites with a link each; they are one now, at `app.retina.fm`, and the banner's button goes to the same address (`_fleet_bar.html` writes it out, and `tests/test_fleet.py` holds the two together).
- `HELP`: our own channels only, the Retina Discord and a support email address. The blah2 Discord is not here. It is the blah2 project's own community server, and listing it as help would send an owner with a hardware or account problem into a volunteer channel expecting Offworld Labs support, and land that community with questions it cannot answer; as a resource, named for whose it is, it reads as somewhere to learn more. The two Discord cards show the same host (`discord.gg`), so the names are what tell them apart. A Discord invite can be made to expire; the Retina one has to be a permanent invite, or the card on every node goes dead with it.
- `BUY_URL`: where "Add another node" sends an owner with only one node.
- `PRIMER_URL`: empty until the full primer is published. A dead link on an owner's node is worse than no link, so the template omits the line while this is blank; turning it on is one string.

Each resource card shows its host underneath the name. `_resource` derives the host from the URL, so it cannot drift from where the card actually goes; it is there because every one of these leaves the device, and an owner should see they are about to be sent to another site before they click. The email card (`_mailto`) is not marked external, because it opens a mail client rather than a page and the outbound arrow in this interface means "this leaves for another page". It shows the address where a host would go, because that is what someone may need to read and type elsewhere.

## The healthz endpoint

`/healthz` is what other nodes probe. It returns `{"ok": true, "node_id": ..., "telemetry": ...}`.

It is deliberately trivial and dependency-free. It answers "is there a retina-gui serving on this address", which is the only question a peer needs to ask, not whether the radar is healthy (that is what the node's own page is for). Anything heavier would make a busy node look absent to its peers.

`telemetry` rides along because this probe is the only regular contact between nodes, and a card on another node's Summary page has no other way to learn an identifier that lives on this node's disk. It stays within the same rule: `telemetry_payload` does local file reads only, nothing over a socket. A blah2 call here to report the radar would break the rule, which is why the radar is not included.

`/summary`, `/api/fleet/*` and `/healthz` are exempt from the calibration redirect in `app.py`, so a calibrating node still looks present to its peers.

`/api/fleet/peers` returns the discovered nodes as JSON (the `peer_view` shape). Unlike the banner, it does not add a stand-in for this node when discovery has not yet seen it.

## Friendly names

A node_id such as `ret<node_id>` is stable and unambiguous, which is why it is the mDNS host name, but a fleet list of several of them is not navigable. The friendly name is the label shown on tabs and cards instead, with the id underneath on the card.

It is only a label. Nothing addresses the node by it, so a rename cannot break a bookmark or an SSH config.

| Aspect | Behaviour |
| --- | --- |
| Storage | `node-name` in the data directory on `/data`, so it survives an OS update (alongside the SSH keys and the telemetry node_ref cache). Written atomically via a temp file and rename, mode 0644. |
| Empty | Valid, and means "unset". |
| Length | `MAX_LENGTH` = 48. It has to fit on a card and in a TXT record, and a name long enough to need scrolling is not doing its job. `/config` uses the same constant for the field's `maxlength`. |
| Characters | Anything printable except control characters (including newlines), DEL, `<`, `>`, `&`, `"` and `'`. These are refused rather than escaped because no legitimate name needs them and they could break out of the line-oriented name file or the XML the identity script builds. The escaping in owl-mdns-identity is the second line of defence, for a file edited by hand over SSH. |
| Rename | `POST /node-name` calls `NodeName.set`, which saves the file then runs owl-mdns-identity. |

### How a rename reaches the network

Rewriting the advertisement is delegated to owl-mdns-identity (`/usr/local/sbin/owl-mdns-identity`, shipped by owl-os) rather than done in retina-gui. It is the same script that writes the service file at boot, so there is one place that knows the format and one place doing the XML escaping. avahi-daemon watches `/etc/avahi/services` and reloads on its own, so nothing needs restarting and the new TXT record is live within a second or two.

The re-advertise step is best-effort. The name is already saved, and the boot run of the script picks it up regardless, so a failure only delays the rename reaching other nodes. It is skipped in dev mode or when the script does not exist.

Other nodes then see the new name after their next browse restart (up to 60 s, see [Browse restarts](#browse-restarts)), not immediately.

## Dev fixture

In dev mode, setting `MDNS_PEERS_FIXTURE` to a JSON file path makes `PeerDirectory` read the peer list from that file instead of the LAN. The file is a list of objects with `node_id`, and optionally `name`, `hostname` (default `<node_id>.local`), `address`, `port`, `alive` and `healthz`. It is reloaded every 20 seconds, and the prober does nothing while a fixture is in use, so `alive` and `healthz` are taken as given.
