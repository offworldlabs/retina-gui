# Remote access

A node's GUI can be reached two ways: from its own network (the LAN pathway), and over a Cloudflare tunnel on a per-node support hostname (the remote pathway). The LAN is unauthenticated, as it always has been. The remote pathway is opt-in, gated by a Cloudflare Access assertion that the node verifies itself, and refuses a small set of operations even to an authenticated visitor. Two owner agreements sit alongside it: whether support may open the interface remotely, and whether support may open a shell through Mender.

## Where it lives

| File | Role |
| --- | --- |
| [remote_access.py](../../src/remote_access.py) | `RemoteAccess`: the support-access toggle, the (deferred) owner password, the shell agreement marker, the inventory marker. `classify_host`, `requires_presence`, `tunnel_status`. |
| [access_identity.py](../../src/access_identity.py) | `AccessIdentity`: verifies the `Cf-Access-Jwt-Assertion` header and returns an email address or `None`. |
| [mender_connect.py](../../src/mender_connect.py) | `MenderConnect`: enforces the shell agreement by editing `mender-connect.conf` and restarting the daemon. |
| [ssh_keys.py](../../src/ssh_keys.py) | `SSHKeyManager`: validates and stores public keys in the `authorized_keys` file sshd trusts. |
| [routes/remote_access.py](../../src/routes/remote_access.py) | `/login`, `/logout`, and the `/remote-access/*` controls (toggle, shell, password, generate). |
| [routes/config.py](../../src/routes/config.py) | `/ssh-keys` add and delete routes, and the context processor that feeds the Remote support section of the config page. |
| [app.py](../../src/app.py) | `_gate_the_remote_pathways` (the before-request gate), `_PathwaySessionInterface` (cookie flags per pathway), `_reapply_shell_agreement` and the startup `publish_marker` call. |
| [services.py](../../src/services.py) | Builds the shared instances and defines `REMOTE_ACCESS_DOMAIN`. |
| [login.html](../../templates/login.html) | Password prompt for the deferred owner-password design. |
| [remote_denied.html](../../templates/remote_denied.html) | Shown when a remote request carries no assertion the node could verify. |

Files on the node:

| Path | Written by | Contents |
| --- | --- | --- |
| `/data/retina-gui/remote-access.json` | `RemoteAccess._update` | `enabled`, `password`, `updated_at`. Mode 0600. |
| `/data/retina-gui/remote-access-enabled` | `RemoteAccess._sync_marker` | Empty marker, 0644. Presence means support access is on. |
| `/data/retina-gui/remote-shell-disabled` | `RemoteAccess.record_shell_allowed` | Empty marker, 0644. Presence means the shell was declined. |
| `/data/retina-gui/authorized_keys` | `SSHKeyManager` | One public key per line, 0644. |
| `/data/cloudflared/tunnel-token` | node-infra, installed by owl-os | Connector token. Read only to report tunnel state. |
| `/data/cloudflared/access.json` | node-infra, installed by owl-os | `{"team_domain": ..., "aud": ...}` for Access verification. |
| `/etc/mender/mender-connect.conf` | `MenderConnect._write` | mender-connect's own config, on the A/B rootfs. |

## Two pathways

| Pathway | Hostnames | Who uses it | Authentication |
| --- | --- | --- | --- |
| `LAN` | `owl.local`, `ret<node_id>.local`, a bare IP, `localhost` | The owner at home; engineers over `mender-cli port-forward` | None. Being on the network is the credential. |
| `OWNER` | `ret<node_id>.<REMOTE_ACCESS_DOMAIN>` only | Support, over the Cloudflare tunnel | A verified Cloudflare Access assertion (or a password session, see [Owner password](#owner-password)). |

The constant is named `OWNER` for historical reasons (see [History](#history)). In the current code it is the support pathway.

Engineers who open a Mender port-forward land with a Host of `localhost`, which classifies as LAN. Mender has already authenticated them and logged the session, so the node treats them exactly like someone standing in the house. The owner can withdraw this route through the [shell agreement](#remote-shell-agreement), which disables port-forwarding too.

`REMOTE_ACCESS_DOMAIN` comes from the environment (see `services.py`). It is deliberately a zone separate from the product's main domain: a page served from a node can set a cookie scoped to its parent domain, and nodes run on hardware their owners control.

## Classifying a request

`classify_host(host, node_id, domain)` looks only at the `Host` header. The two pathways are the same service on the same port, so the name is the only thing that distinguishes them. cloudflared forwards the hostname the request arrived on, so a visitor reaching the tunnel hostname cannot present themselves as something else.

Rules, in order:

1. The port and any trailing dot are stripped and the host lowercased. An empty host or empty domain is LAN.
2. `node_id` must match `^ret[0-9a-f]{8}$`. If it does not, the blunt rule applies: the domain itself or any name under it is `OWNER`, everything else LAN.
3. Otherwise, exactly `ret<node_id>.<domain>` is `OWNER`, and everything else (including other names under the domain) is LAN.

### Why only this node's own hostname

That is the single name node-infra provisions, puts an Access application in front of, and points at this node. It is the only name the gate has any business challenging.

An earlier version treated every name under the remote domain as `OWNER`, to fail closed against a stale record or a stray wildcard. That assumed the only name under the domain reaching a node's port 80 would be its own. It was not true: hand-built hostnames that predate this feature served nodes the same way. They were swept into the remote pathway, asked for an Access assertion that no Access application existed to issue, and sent to a password page that could not be satisfied because no password could be set. Locked out with no way back.

The old reasoning also overstated the danger. A name under the remote domain is one we created in our own zone. It is our mistake to make, not an attacker's to exploit, and treating an unrecognised one as LAN restores the behaviour it had before this feature existed.

### Why the node id is validated by shape

`read_node_id()` returns the string `"Unknown"` when `/data/mender` cannot be read, and that is truthy. A bare emptiness check would take it as a real id, fail to match the real support hostname, and serve that hostname as LAN, unauthenticated. With no valid node id there is no way to recognise our own hostname, so the domain-wide rule applies instead. That is the one place the classifier still fails closed.

## The gate

`_gate_the_remote_pathways` in `app.py` runs before every request:

1. Classify the host and store it in `g.pathway`. LAN returns immediately.
2. If support access is off (`RemoteAccess.is_enabled()` is false), `404`. The tunnel should not be up in that state, so anything arriving is a stale DNS record or a guess, and neither is owed confirmation that a node answers to this name.
3. Paths under `/login`, `/static` and `/favicon` pass without a session.
4. Verify the Access assertion (see [Cloudflare Access verification](#cloudflare-access-verification)). A verified email goes in `g.access_identity`.
5. With neither a verified identity nor a `remote_authed` session:
   - a `GET` redirects to `/login` if a password is set, otherwise renders `remote_denied.html` with `403`;
   - anything else gets `403`, so `fetch()` callers get a status rather than an HTML page parsed as JSON. CSRFProtect runs first, so a stale page's POST is often refused with `400` before reaching the gate.
6. Authenticated, but the path is presence-required: `403` (see [Device-presence tier](#device-presence-tier)).

The gate is registered before the calibration redirect hook, and that order matters: otherwise an unauthenticated visitor arriving during a calibration would be redirected to `/config` instead of being challenged.

Templates receive `pathway` and `is_remote` from the context processor in `app.py`, so they can hide what the remote pathway cannot reach anyway.

### Session cookies

`_PathwaySessionInterface` sets cookie flags per pathway. On the remote pathway (and only when `SESSION_COOKIE_SECURE` is on) the cookie is `Secure` and named `__Host-session`. The prefix stops one node setting a parent-domain cookie that shadows a sibling node's session; it requires `Secure`, so the two move together. On the LAN the cookie is plain `session`, because browsers discard a `Secure` cookie delivered over HTTP, and CSRFProtect keeps its token in the session. An earlier version applied `Secure` everywhere and every LAN POST failed with `400` (curl hid it, because it keeps the cookie a browser refuses).

## Cloudflare Access verification

Cloudflare puts a signed JWT in `Cf-Access-Jwt-Assertion` on every request it lets through. `AccessIdentity.identity()` turns that into an email address, or into `None`.

### Why verify at all

In normal operation nothing unauthenticated reaches the node: the tunnel serves one hostname and Access intercepts by hostname. Verification is for when that stops being true. An Access application deleted, renamed or misconfigured leaves the hostname open and the node happily serving it, and node-infra's reconciliation would notice eventually rather than immediately. This is the node's own answer to "did Cloudflare actually vouch for you", independent of anything upstream staying correctly configured.

### What is checked

| Check | Why |
| --- | --- |
| Signature (RS256, against the team's published keys) | Without it the header is a claim anyone can type. |
| Audience (`aud`, this node's Access application tag) | Without it, a token minted for any other application in the same team is accepted. The team runs Access on other hostnames too. |
| Issuer (`https://<team_domain>`) | A valid token from another Cloudflare team is not enough. |
| Expiry, with 30 s leeway (`CLOCK_LEEWAY_SECONDS`) | Nodes keep time with chrony but can drift while offline. |
| A non-empty `email` claim | A token that verifies but names nobody is not an identity. |

`exp`, `aud` and `iss` are required claims. Missing any one check turns verification into decoration. Audience is the one most easily left out, because a token that fails it still has a perfectly good signature.

### Configuration

`team_domain` and `aud` arrive from node-infra in `/data/cloudflared/access.json` beside the tunnel token (path overridable with `ACCESS_CONFIG_PATH`). The audience is generated per Access application, so it differs per node. node-infra installs this config before the token, so the node can identify callers from the first request it answers. Absent, unreadable, or missing either value means `config()` returns `(None, None)` and every assertion is refused. Failing closed is the only safe reading of "we cannot tell who this is".

### Signing keys

Keys are fetched from `https://<team_domain>/cdn-cgi/access/certs` and cached for an hour (`JWKS_TTL_SECONDS`). Cloudflare rotates keys, and an hour is well inside the rotation window. The TTL is a ceiling on staleness, not the rotation mechanism: an unrecognised key id refetches immediately, once. After a fresh fetch a key id that still does not exist is not one of Cloudflare's, which distinguishes a rotation from a forged header. The cache is guarded by a lock, because requests are served by threads and two arriving together could each fetch, or install a key set for a team domain the other had just changed away from.

### Failure reporting

`identity()` returns `None` for every failure (no config, no token, bad signature, wrong audience, wrong team, expired, Cloudflare unreachable, malformed). The caller cannot act differently on any of them, and distinguishing them in the return value would invite someone to treat one as good enough.

The log does distinguish them, at WARNING, each line starting `Refusing an Access assertion`. Operationally they are very different: a wrong audience is a misconfiguration nobody spots from outside, and an unreachable Cloudflare is an outage. Returning a bare `None` with no log once cost an afternoon of instrumenting a live node. A wrong audience gets its own message naming the audience the token claimed, read unverified by `_claim()`. That and `_kid()` exist only to describe a rejection and must never decide anything.

The token itself is never logged. It is a bearer credential for its session.

## Device-presence tier

`PRESENCE_REQUIRED_PREFIXES` lists paths refused on the remote pathway even with a valid Access identity or password session. They are prefix-matched by `requires_presence()`.

| Prefix | Why |
| --- | --- |
| `/ssh-keys` | An added key survives every later password rotation or Access revocation. |
| `/remote-access` | The owner's controls over remote access (toggle, shell agreement, password). The control an owner uses to withdraw access must not be reachable through the access being withdrawn. |
| `/mender/install` | Pushes new firmware, and can leave the node unreachable. |

If a remote session could add an SSH key, support access would become a route to shell access and the two owner agreements would stop being independent. Equally, with a shared password, a visitor could keep a way in that outlived a rotation, and the owner's only remedy would be reinstalling. The LAN is unaffected, so an owner at home and an engineer on a port-forward both keep the full interface. Everything else on the GUI is available to whoever is let in remotely.

## Support access toggle

`RemoteAccess.is_enabled()` is the owner's choice alone. The config page labels it "Let support open this node's interface", and it posts to `/remote-access/toggle`.

### The node-to-server channel

Nothing in retina-gui calls a server for this feature. The whole channel is a marker file:

1. `set_enabled()` writes `remote-access.json`, then `_sync_marker()` creates or removes `remote-access-enabled`.
2. owl-os's `mender-inventory-retina-remote-access` script reports the marker's presence as the `remote_access` inventory attribute on Mender's next inventory poll (owl-os sets this to 600 s).
3. node-infra reads that attribute and creates or tears down the tunnel, installing the Access config and then the connector token.

So the owner should expect a delay of up to one inventory cycle, not an immediate result.

A marker rather than the state file, because the state file is 0600 root and holds the password, and the inventory script is POSIX shell. The marker is world-readable because the inventory scripts run as root and it holds nothing.

`_sync_marker()` is called after every write rather than only at the toggle, so it always reflects `is_enabled()` however the state was changed. It is best effort: a marker that fails to appear costs one inventory cycle, whereas failing the save would lose what the owner just entered.

### Defaults to on (temporary)

`is_enabled()` returns `True` when the state file has no `enabled` key, so an untouched node publishes `remote_access=true` and gets a tunnel without anyone opting in. This is deliberate for now and is meant to be reverted: the setting exists to be the owner's choice, and a default of on makes it a choice they have to discover in order to decline. The revert is tracked in the Deployment Issues & Improvements list. Restoring default-off means dropping the `True` default in `is_enabled()`.

Defaulting on does not advertise anything prematurely. The hostname only starts serving once the connector has a token, and node-infra installs the Access config first.

Because an untouched node has no state file, it would never have had a marker written. `publish_marker()` is called at startup (in `app.py`, best effort, outside dev mode) so such a node reports `remote_access=true` without anyone toggling anything.

`is_enabled()` used to also require a password to be set, from when the owner signed in with one. Once support access moved to Cloudflare Access and the page stopped offering a way to set a password, that condition made the setting impossible to turn on.

### Tunnel status

`tunnel_status()` reports the connector's real state by asking systemd, not a server. Nothing on the node is told whether provisioning succeeded, and nothing needs to be: node-infra puts a token on the box, owl-os starts `cloudflared.service`, and the honest answer to "is it working" is whether that unit is up. cloudflared is `Type=notify` and only signals ready once it has connections to the edge, so active means genuinely reachable.

| Value | Meaning |
| --- | --- |
| `waiting` | No token yet (missing or empty file). |
| `up` | Token present and `systemctl is-active` succeeds. |
| `down` | Token present and the unit is not active. |
| `unknown` | `systemctl` could not be run (dev machine, tests). |

The config page shows `off` instead when support access is disabled, and names the hostname before it is provisioned, since it is a pure function of the node id and the zone.

## Owner password

The password, the `/login` form and the `/remote-access/password` and `/remote-access/generate` routes belong to an owner-password design that is currently deferred. The config page has no control to set a password, and nothing else in the GUI calls those two routes. The code is kept working: the gate offers the login form only when `has_password()` is true, and a successful login lets the visitor through exactly like a verified Access identity.

### Stored in the clear, deliberately

This is the phone-hotspot model, not the user-account model: the owner sets something memorable, looks it up when they need to share it, and changes it when they want. A hash cannot do the middle one, and encrypting would be theatre, because any key the node needs to decrypt has to live on the node beside the ciphertext.

The only readers of the plaintext are people who already own the node. `get_password()` is kept separate from `status()` on purpose: `status()` is handed to the config template on every pathway and returned by the toggle endpoint as JSON, so keeping the password out of it means it cannot reach a template or response by being carried inside something else. It is only meant to be read where the pathway allows it, and the LAN pathway is unauthenticated anyway, so anyone who could read it there could already change every setting. Mender's remote terminal is root, so staff are in the same position either way.

Two consequences are deliberate. Someone briefly on the house network could note the password and keep remote access after leaving, which LAN access alone would not give them; the remedy is to change it, as with a hotspot. And a memorable password is the kind people reuse, so the design calls for the config page to say that anyone on the local network can see it.

The password never leaves the node. It is generated, stored and verified here, and node-infra never learns it. That is the point of doing this on the node rather than with an identity provider: no account to create and no email to collect.

The state file is written via `tempfile.mkstemp` (0600 from creation) and an atomic rename, so the password is never briefly world-readable.

### Rules

| Rule | Detail |
| --- | --- |
| Minimum length | 8 (`MIN_PASSWORD_LENGTH`), matching the WPA2 minimum a phone hotspot enforces. Short on purpose; the edge rate limit is what makes it safe. |
| Whitespace | No leading or trailing spaces. |
| Comparison | `secrets.compare_digest` on UTF-8 bytes. Constant-time so the mismatch position does not leak through timing; encoded because `compare_digest` refuses non-ASCII `str`. Returns false when no password is set. |
| Generated passwords | Four hyphenated groups of four from `abcdefghjkmnpqrstuvwxyz23456789` (no `0/O`, `1/l/I`, since they are read aloud or copied off a screen). About 79 bits. |

Rate limiting at Cloudflare's edge is not optional. There is no rate limit in this process. Hashing used to cost about 145 ms per attempt on a Pi 5, which incidentally throttled guessing; a string compare is free, so the edge limit is the only thing standing between the login form and an offline-speed guessing run.

### Login flow

- `GET /login` redirects straight to `next` if the session is already authenticated, otherwise renders `login.html`.
- `POST /login` clears the session and sets `remote_authed` on success. The session is not permanent: the cookie dies with the browser session, because a node password is shared more freely than an account password and a remembered login on a borrowed laptop is a likelier way to lose control of a node.
- A failed login returns `401` with "Incorrect password" and says nothing about whether a password is set, so an attacker gets no signal beyond pass or fail.
- `next` is sanitised by `_safe_next()`: only a bare path starting with a single `/` is accepted. `//host` and `/\host` fall back to `/`, since the gate puts `request.full_path` there and the value is attacker-supplied whenever someone is sent a link. Without this the login page would be an open redirect.
- `POST /logout` clears the session and returns to `/login`.

## Login and denied pages

Neither page extends `base.html`. That template renders the fleet bar and the Home/Config tabs, which link to pages the visitor has not been let into; clicking one lands straight back on the same page, which reads as the node being broken.

`remote_denied.html` exists because the gate used to redirect every unverified remote request to `login.html`. With no password settable from the support pathway, that form could only ever refuse, so someone whose assertion failed was shown a password box instead of the reason. The denied page says what happened, lists the likely causes (no Access config yet, signing keys unreachable), notes that the LAN needs none of this, and points at the `Refusing an Access assertion` log lines.

## Remote shell agreement

The config page's "Let support open a command line" toggle posts to `/remote-access/shell`. It is independent of the support access toggle: declining one does not affect the other.

### Recorded by exception

The two agreements default differently, so they are stored differently. Support access has its own default (see above) and its marker means "wanted". Shell access defaults on because every node already had it, and defaulting off would silently withdraw something owners rely on. So only the exception is recorded: `remote-shell-disabled` present means declined. An untouched node needs no migration, and a missing or unwritable `/data` cannot quietly revoke access nobody declined.

### Enforcement

`MenderConnect.set_shell_enabled()` sets `Disable` on two sections of `mender-connect.conf` and restarts the service:

| Feature | What it gates |
| --- | --- |
| `Terminal` | The remote shell in Mender's dashboard. |
| `PortForward` | Forwarding a local port to one on the node. |

Both have to go. Disabling only the terminal leaves `port-forward` to sshd, and a port-forward to this GUI arrives as `localhost`, classifies as LAN, and bypasses the [device-presence tier](#device-presence-tier) that stops remote sessions adding SSH keys. Both are written in one file write so the config never describes a half-applied state.

The service is restarted, not reloaded: mender-connect reads its config only at startup, so a reload would leave the daemon serving the previous answer.

`is_shell_enabled()` returns `True`, `False`, or `None` when the config cannot be read. `None` is distinct on purpose: "we cannot tell" and "the owner declined" mean very different things. The config page reads this enforced state back (as `shell_enforced`) rather than trusting the recorded choice, so a failed enforcement or a hand-edited config shows as "Not applied".

### Order: enforce, then record

The route calls `mender_connect.set_shell_enabled()` first and only then `remote_access.record_shell_allowed()`, skipping the record if enforcement failed. The reverse order would leave the marker, and therefore the upstream `remote_shell` inventory attribute, claiming the shell was declined while mender-connect kept serving it.

### Re-applied at startup

The choice lives in `/data`, which survives an OS update. The enforcement lives in `/etc/mender/mender-connect.conf` on the A/B rootfs, which does not: an update replaces it with owl-os's template, where both features are enabled. Before `_reapply_shell_agreement()` existed, an owner who declined the shell had it restored by the next update while the marker, the config page and the inventory attribute all still said declined. That divergence was in the permissive direction.

So the `/data` marker is authoritative, and `app.py` re-applies it at startup (outside dev mode). It acts only on a mismatch, so a node that already agrees is not rewritten and restarted every boot, and the second execution of the module body (see `services.py`) finds nothing to do. It never stops the GUI booting.

### What is deliberately left alone

- `FileTransfer` stays enabled. node-infra uses it to deliver and clear the tunnel token, so gating it would make the two agreements depend on each other (a node that declined the shell could never receive a support tunnel). It cannot be used to get a shell: sshd trusts only `/data/retina-gui/authorized_keys`, which is root-owned and outside the chroot Mender file transfer writes into.
- `MenderClient` stays enabled, and `mender-authd` and `mender-updated` are untouched. Enrolment, OTA updates and inventory reporting continue whatever the owner decides. They are separate daemons sharing no process or dependency with mender-connect, which makes that promise structural.

### Refuse rather than repair

A missing or unparseable `mender-connect.conf` is not rewritten from defaults; `set_shell_enabled()` returns an error. The file carries file-transfer limits, the shell user and session caps that are not ours to reconstruct, and a plausible-looking replacement could widen what a session may do. Refusing leaves the node as it was.

`_write()` replaces the file atomically and keeps its existing mode (the image ships it 0644 root). Tightening the mode here would be an unrelated change behind a toggle. `DEFAULT_CONF_MODE` applies only when no file exists, which is the normal case off-device. In dev mode, `set_shell_enabled()` writes the file but does not restart anything, so the config page still reflects the choice.

## SSH keys

`SSHKeyManager` manages `/data/retina-gui/authorized_keys`, which sshd trusts. The routes are `POST /ssh-keys` (add) and `POST /ssh-keys/delete` in `routes/config.py`. Both are presence-required, so they work from the LAN only.

`is_valid_ssh_key()` rejects:

| Check | Reason |
| --- | --- |
| Any `\n` or `\r` | One submission must not inject extra keys (or `authorized_keys` options on a new line). |
| Longer than 2000 characters | An RSA 4096 key is about 750; the rest is headroom for a comment. |
| Any of `` \| ; & $ ` ( ) { } < > ! # `` | Defensive: none of them belong in a public key line. |
| Fewer than two fields | Must be `type base64 [comment]`. |
| A type not in `VALID_KEY_TYPES` | Exact match against an allowlist, so prefix tricks fail. This also rejects a leading `authorized_keys` option such as `command=...`. |
| Key data not matching `^[A-Za-z0-9+/]+=*$` | Must be base64. |

Add and remove both rewrite the whole file through `tempfile.mkstemp` and `os.rename`, so a crash never leaves a half-written file. The file is 0644 so sshd can read it for any user. Adding a key that is already present is a no-op.

## History

- The first design had the owner reach the node from anywhere with a node-local password, no Cloudflare Access application at all, and staff arriving only via Mender port-forward. That is where the `OWNER` constant, the password code and the login page come from. The current code gates the tunnel hostname with Cloudflare Access for support, and the owner password is deferred.
- The password used to be hashed. Generated passwords were shown once because only the hash was kept. It is now stored in plaintext (see [Stored in the clear, deliberately](#stored-in-the-clear-deliberately)).
- The classifier used to treat the whole remote domain as `OWNER` (see [Why only this node's own hostname](#why-only-this-nodes-own-hostname)).
- Unverified remote requests used to be redirected to the login form (see [Login and denied pages](#login-and-denied-pages)).
