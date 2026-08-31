"""Tests for remote access: the password, and the three request pathways.

The tests that matter most here are the pathway ones. A bug in the storage layer
is a broken feature; a bug in the gate is a stranger with an SSH key on somebody
else's node.
"""

import importlib
import json
import os
import stat

import pytest

from remote_access import (
    ENABLED_MARKER,
    LAN,
    MIN_PASSWORD_LENGTH,
    OWNER,
    RemoteAccess,
    classify_host,
    generate_password,
    requires_presence,
    tunnel_status,
)

NODE_ID = "ret7dd2cb0d"
DOMAIN = "retnode.com"
OWNER_URL = f"http://{NODE_ID}.{DOMAIN}"
LAN_URL = "http://owl.local"
# What a Mender port-forward looks like from inside the GUI. Staff reach nodes
# this way instead of through a hostname of their own, so it must be LAN.
PORT_FORWARD_URL = "http://localhost:8080"

GOOD_PASSWORD = "correct-horse-battery"


# ── the store ────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path):
    return RemoteAccess(str(tmp_path / "remote-access.json"))


def test_starts_off_with_no_password(store):
    assert store.is_enabled() is False
    assert store.has_password() is False
    assert store.status()["enabled"] is False


def test_password_round_trip(store):
    assert store.set_password(GOOD_PASSWORD) == (True, None)
    assert store.has_password() is True
    assert store.verify(GOOD_PASSWORD) is True
    assert store.verify("something else") is False


def test_password_is_stored_in_the_clear_deliberately(store):
    """This is the hotspot model: the owner reads it back to share it.

    Pinned as a test rather than left implicit, so switching to a hash later is
    a decision someone argues for rather than a refactor that quietly breaks
    the config page's ability to show the password.
    """
    store.set_password(GOOD_PASSWORD)
    with open(store.state_file) as f:
        assert GOOD_PASSWORD in f.read()
    assert store.get_password() == GOOD_PASSWORD


def test_get_password_is_empty_when_unset(store):
    assert store.get_password() == ""


def test_status_never_carries_the_password(store):
    """status() reaches the template on every pathway and the toggle response as
    JSON. The password travels by its own method or not at all."""
    store.set_password(GOOD_PASSWORD)
    assert GOOD_PASSWORD not in repr(store.status())


def test_state_file_is_not_world_readable(store):
    store.set_password(GOOD_PASSWORD)
    mode = stat.S_IMODE(os.stat(store.state_file).st_mode)
    assert mode == 0o600, f"expected 0600, got {mode:o}"


def test_short_passwords_are_refused(store):
    ok, error = store.set_password("x" * (MIN_PASSWORD_LENGTH - 1))
    assert ok is False
    assert str(MIN_PASSWORD_LENGTH) in error
    assert store.has_password() is False


def test_cannot_enable_without_a_password(store):
    """An advertised hostname that refuses everyone is the worst of the states."""
    ok, error = store.set_enabled(True)
    assert ok is False
    assert "password" in error.lower()
    assert store.is_enabled() is False


def test_enable_after_setting_a_password(store):
    store.set_password(GOOD_PASSWORD)
    assert store.set_enabled(True) == (True, None)
    assert store.is_enabled() is True


def test_disabling_keeps_the_password(store):
    store.set_password(GOOD_PASSWORD)
    store.set_enabled(True)
    store.set_enabled(False)
    assert store.is_enabled() is False
    assert store.has_password() is True
    assert store.verify(GOOD_PASSWORD) is True


def test_replacing_the_password_invalidates_the_old_one(store):
    store.set_password(GOOD_PASSWORD)
    store.set_password("a-different-password")
    assert store.verify(GOOD_PASSWORD) is False
    assert store.verify("a-different-password") is True
    assert store.get_password() == "a-different-password"


def test_a_memorable_password_is_allowed(store):
    """The point of the change: the owner picks something they can say aloud."""
    assert store.set_password("kitchen radar") == (True, None)
    assert store.verify("kitchen radar") is True
    assert store.get_password() == "kitchen radar"


def test_non_ascii_passwords_work(store):
    """compare_digest refuses non-ASCII str, so verify() encodes first."""
    assert store.set_password("cafe\u0301-radar-8") == (True, None)
    assert store.verify("cafe\u0301-radar-8") is True
    assert store.verify("cafe-radar-8") is False


def test_verify_is_false_when_nothing_is_set(store):
    assert store.verify("") is False
    assert store.verify("anything") is False


def test_a_corrupt_state_file_reads_as_unset(store):
    os.makedirs(os.path.dirname(store.state_file), exist_ok=True)
    with open(store.state_file, "w") as f:
        f.write("{ not json")
    assert store.is_enabled() is False
    assert store.verify("anything") is False


def test_generated_passwords_are_long_and_distinct():
    a, b = generate_password(), generate_password()
    assert a != b
    assert len(a.replace("-", "")) >= MIN_PASSWORD_LENGTH
    # Ambiguous glyphs would be read aloud wrong, which is the normal way this
    # password gets shared.
    assert not set("01lIO") & set(a)


# ── the remote shell agreement ───────────────────────────────────
#
# Recorded by its exception, unlike support access, because it defaults ON.
# Every node in the fleet already has interactive access, so an untouched node
# must keep it, and an unwritable /data must not silently revoke it.

def test_shell_is_allowed_by_default(store):
    assert store.is_shell_allowed() is True


def test_declining_shell_is_recorded(store):
    assert store.record_shell_allowed(False) == (True, None)
    assert store.is_shell_allowed() is False


def test_shell_can_be_allowed_again(store):
    store.record_shell_allowed(False)
    assert store.record_shell_allowed(True) == (True, None)
    assert store.is_shell_allowed() is True


def test_allowing_shell_twice_is_harmless(store):
    assert store.record_shell_allowed(True) == (True, None)
    assert store.record_shell_allowed(True) == (True, None)
    assert store.is_shell_allowed() is True


def test_the_two_agreements_are_independent(store):
    """The whole point of two settings. Neither may move the other."""
    store.set_password(GOOD_PASSWORD)
    store.set_enabled(True)
    store.record_shell_allowed(False)
    assert store.is_enabled() is True
    assert store.is_shell_allowed() is False

    store.set_enabled(False)
    assert store.is_shell_allowed() is False
    store.record_shell_allowed(True)
    assert store.is_enabled() is False
    assert store.is_shell_allowed() is True


def test_shell_state_reaches_the_status(store):
    store.record_shell_allowed(False)
    assert store.status()["shell_allowed"] is False


# ── the inventory marker ─────────────────────────────────────────
#
# This file is the entire node-to-server channel. owl-os's
# mender-inventory-retina-remote-access tests for it and reports remote_access
# up to Mender; node-infra reads that and decides whether a tunnel exists. If it
# stops tracking is_enabled(), nodes silently stop getting tunnels, or keep them
# after being switched off.

def _marker(store):
    return os.path.join(os.path.dirname(store.state_file), ENABLED_MARKER)


def test_no_marker_before_anything_is_set(store):
    store.set_password(GOOD_PASSWORD)
    assert not os.path.exists(_marker(store))


def test_marker_appears_when_enabled(store):
    store.set_password(GOOD_PASSWORD)
    store.set_enabled(True)
    assert os.path.exists(_marker(store))


def test_marker_goes_away_when_disabled(store):
    store.set_password(GOOD_PASSWORD)
    store.set_enabled(True)
    store.set_enabled(False)
    assert not os.path.exists(_marker(store))


def test_marker_survives_a_password_change(store):
    """Both inputs are written by different methods, which is why the marker is
    recomputed after every write rather than set at the toggle."""
    store.set_password(GOOD_PASSWORD)
    store.set_enabled(True)
    store.set_password("kitchen radar")
    assert os.path.exists(_marker(store))


def test_marker_is_readable_by_the_inventory_script(store):
    """mender-authd runs the inventory scripts, and there is nothing in the
    marker to protect. The state file beside it stays 0600."""
    store.set_password(GOOD_PASSWORD)
    store.set_enabled(True)
    assert stat.S_IMODE(os.stat(_marker(store)).st_mode) == 0o644


def test_the_marker_holds_no_secrets(store):
    store.set_password(GOOD_PASSWORD)
    store.set_enabled(True)
    with open(_marker(store)) as f:
        assert GOOD_PASSWORD not in f.read()


# ── is the tunnel actually up ────────────────────────────────────

def _fake_systemctl(monkeypatch, returncode):
    import remote_access as module

    def fake_run(*args, **kwargs):
        class R:
            pass
        r = R()
        r.returncode = returncode
        return r

    monkeypatch.setattr(module.subprocess, "run", fake_run)


def test_tunnel_waiting_when_no_token_has_arrived(tmp_path):
    assert tunnel_status(token_path=str(tmp_path / "nope")) == "waiting"


def test_tunnel_waiting_when_the_token_is_empty(tmp_path):
    token = tmp_path / "tunnel-token"
    token.write_text("")
    assert tunnel_status(token_path=str(token)) == "waiting"


def test_tunnel_up_when_the_connector_is_running(tmp_path, monkeypatch):
    token = tmp_path / "tunnel-token"
    token.write_text("eyJhIjoi")
    _fake_systemctl(monkeypatch, 0)
    assert tunnel_status(token_path=str(token)) == "up"


def test_tunnel_down_when_the_connector_is_not(tmp_path, monkeypatch):
    token = tmp_path / "tunnel-token"
    token.write_text("eyJhIjoi")
    _fake_systemctl(monkeypatch, 3)
    assert tunnel_status(token_path=str(token)) == "down"


def test_tunnel_unknown_off_a_node(tmp_path, monkeypatch):
    """A dev machine has no systemctl, and that is not a tunnel being down."""
    import remote_access as module
    token = tmp_path / "tunnel-token"
    token.write_text("eyJhIjoi")

    def boom(*args, **kwargs):
        raise FileNotFoundError("systemctl")

    monkeypatch.setattr(module.subprocess, "run", boom)
    assert tunnel_status(token_path=str(token)) == "unknown"


# ── which pathway a request is on ────────────────────────────────

@pytest.mark.parametrize("host,expected", [
    ("owl.local", LAN),
    (f"{NODE_ID}.local", LAN),
    ("192.168.1.40", LAN),
    ("localhost:8080", LAN),
    (f"{NODE_ID}.{DOMAIN}", OWNER),
    (f"{NODE_ID}.{DOMAIN}:443", OWNER),
    (f"{NODE_ID}.{DOMAIN}.", OWNER),
    (f"{NODE_ID}.{DOMAIN}".upper(), OWNER),
    (f"{NODE_ID}.admin.{DOMAIN}", OWNER),
    ("", LAN),
])
def test_classify_host(host, expected):
    assert classify_host(host, NODE_ID, DOMAIN) == expected


def test_unknown_names_on_the_remote_domain_fail_closed():
    """Anything unrecognised under the zone must ask for a password, not be
    mistaken for the LAN and waved through."""
    assert classify_host(f"surprise.{DOMAIN}", NODE_ID, DOMAIN) == OWNER
    assert classify_host(DOMAIN, NODE_ID, DOMAIN) == OWNER


def test_every_name_under_the_zone_needs_the_password():
    """There is no staff hostname any more, so nothing under the zone is exempt."""
    for host in (f"ret9f2b1e44.{DOMAIN}", f"anything.{DOMAIN}",
                 f"{NODE_ID}.admin.{DOMAIN}"):
        assert classify_host(host, NODE_ID, DOMAIN) == OWNER


def test_an_unreadable_node_id_does_not_open_the_owner_path():
    """read_node_id() returns 'Unknown' when /data/mender is missing. That must
    not turn the tunnel hostname into an unauthenticated one."""
    assert classify_host(f"{NODE_ID}.{DOMAIN}", "Unknown", DOMAIN) == OWNER


def test_a_mender_port_forward_counts_as_local():
    """This is the whole of the staff access story. If it ever stops being LAN,
    engineers are locked out of every node at once."""
    assert classify_host("localhost:8080", NODE_ID, DOMAIN) == LAN
    assert classify_host("127.0.0.1:8080", NODE_ID, DOMAIN) == LAN


@pytest.mark.parametrize("path", [
    "/ssh-keys", "/ssh-keys/delete",
    "/remote-access/password", "/remote-access/toggle",
    "/mender/install",
])
def test_presence_required_paths(path):
    assert requires_presence(path) is True


@pytest.mark.parametrize("path", ["/", "/config", "/mender/check", "/login"])
def test_ordinary_paths_do_not_need_presence(path):
    assert requires_presence(path) is False


# ── Cloudflare Access at the gate ────────────────────────────────
#
# Support access is gated by Access at the edge; the node verifies the assertion
# rather than trusting it, so a deleted or misconfigured Access application
# cannot silently leave a node open.

ACCESS_TEAM = "offworldlab.cloudflareaccess.com"
ACCESS_AUD = "b62aeb13c198cd2118bd5d92b350f2af5c42830c703baeab42aad5fa3e01f29a"
ENGINEER = "jehan@offworldlab.com"


@pytest.fixture(scope="module")
def access_keys():
    from cryptography.hazmat.primitives.asymmetric import rsa
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private, private.public_key()


def _arm_access(client, temp_dir, access_keys):
    """Point the node's verifier at a key set we control, as Cloudflare would."""
    import jwt as pyjwt

    _, public = access_keys
    jwk = json.loads(pyjwt.algorithms.RSAAlgorithm.to_jwk(public))
    jwk.update({"kid": "kid-1", "alg": "RS256", "use": "sig"})

    with open(os.path.join(temp_dir, "access.json"), "w") as f:
        json.dump({"team_domain": ACCESS_TEAM, "aud": ACCESS_AUD}, f)

    class HTTP:
        @staticmethod
        def get(url, timeout=None):
            class R:
                @staticmethod
                def raise_for_status():
                    return None

                @staticmethod
                def json():
                    return {"keys": [jwk]}
            return R()

    client.access_identity.http = HTTP()


def _assertion(access_keys, *, aud=ACCESS_AUD, email=ENGINEER):
    import time as _time

    import jwt as pyjwt
    private, _ = access_keys
    return pyjwt.encode(
        {"aud": aud, "iss": f"https://{ACCESS_TEAM}", "email": email,
         "exp": int(_time.time()) + 300},
        private, algorithm="RS256", headers={"kid": "kid-1"})


def test_a_verified_access_assertion_needs_no_password(live, temp_dir, access_keys):
    _arm_access(live, temp_dir, access_keys)
    r = live.get("/", base_url=OWNER_URL,
                 headers={"Cf-Access-Jwt-Assertion": _assertion(access_keys)})
    assert r.status_code == 200


def test_an_assertion_for_another_application_is_not_accepted(live, temp_dir, access_keys):
    """This team runs Access on other hostnames. A session for one of those is
    perfectly signed and must not open a node."""
    _arm_access(live, temp_dir, access_keys)
    r = live.get("/", base_url=OWNER_URL,
                 headers={"Cf-Access-Jwt-Assertion": _assertion(access_keys, aud="other")})
    assert r.status_code == 302


def test_a_forged_header_is_not_accepted(live, temp_dir, access_keys):
    _arm_access(live, temp_dir, access_keys)
    r = live.get("/", base_url=OWNER_URL,
                 headers={"Cf-Access-Jwt-Assertion": "not-a-token"})
    assert r.status_code == 302


def test_an_assertion_does_not_lift_the_presence_tier(live, temp_dir, access_keys):
    """The invariant the two owner agreements rest on.

    An engineer authenticated by Access still cannot add an SSH key through the
    browser. If they could, support access would be a route to shell access and
    an owner who declined the shell would not actually have declined it.
    """
    _arm_access(live, temp_dir, access_keys)
    headers = {"Cf-Access-Jwt-Assertion": _assertion(access_keys)}
    assert live.get("/", base_url=OWNER_URL, headers=headers).status_code == 200
    for path in ("/ssh-keys", "/remote-access/shell", "/mender/install"):
        r = live.post(path, data={"x": "1"}, base_url=OWNER_URL, headers=headers)
        assert r.status_code == 403, path


def test_no_email_is_ever_written_to_the_device(live, temp_dir, access_keys):
    """Nothing identifying a person may persist on a customer's node.

    Access assertions carry an email, and Cloudflare adds a plain
    Cf-Access-Authenticated-User-Email header alongside. Both are read in
    memory for the duration of a request and neither is stored, logged or
    rendered. Who may access a node lives in the Access policy, in Cloudflare,
    not on the node.

    Pinned here because it is the kind of property that is true until somebody
    adds a log line meaning well.
    """
    _arm_access(live, temp_dir, access_keys)
    headers = {"Cf-Access-Jwt-Assertion": _assertion(access_keys),
               "Cf-Access-Authenticated-User-Email": ENGINEER}

    for url in (OWNER_URL, LAN_URL):
        body = live.get("/config", base_url=url, headers=headers).data
        assert ENGINEER.encode() not in body, f"email rendered into a page on {url}"

    written = []
    for root, _dirs, files in os.walk(temp_dir):
        for name in files:
            path = os.path.join(root, name)
            try:
                with open(path, "rb") as f:
                    if ENGINEER.encode() in f.read():
                        written.append(path)
            except OSError:
                pass
    assert not written, f"email persisted to {written}"


def test_the_access_config_holds_nothing_personal(live, temp_dir, access_keys):
    """What node-infra delivers is a team domain and an opaque application id."""
    _arm_access(live, temp_dir, access_keys)
    with open(os.path.join(temp_dir, "access.json")) as f:
        config = json.load(f)
    assert set(config) == {"team_domain", "aud"}
    assert "@" not in json.dumps(config)


def test_an_unconfigured_verifier_refuses_every_assertion(live, access_keys):
    """Before node-infra delivers the audience there is nothing to check
    against, and admitting anyone meanwhile would be the worst default."""
    assert live.access_identity.is_configured() is False
    r = live.get("/", base_url=OWNER_URL,
                 headers={"Cf-Access-Jwt-Assertion": _assertion(access_keys)})
    assert r.status_code == 302


# ── the gate, end to end ─────────────────────────────────────────

@pytest.fixture
def client(temp_dir, config_files, test_manifests_dir):
    """Like conftest's app_client, but with the session cookie usable over http.

    In production the session only ever exists on the owner pathway, which is
    HTTPS through the tunnel, so the cookie is Secure and the test client would
    silently decline to store it.
    """
    user_path, merged_path = config_files

    mender_dir = os.path.join(temp_dir, "mender")
    os.makedirs(mender_dir, exist_ok=True)
    node_id_file = os.path.join(mender_dir, "node_id")
    with open(node_id_file, "w") as f:
        f.write(NODE_ID)

    os.environ["DATA_DIR"] = temp_dir
    os.environ["USER_CONFIG_PATH"] = user_path
    os.environ["MERGED_CONFIG_PATH"] = merged_path
    os.environ["RETINA_NODE_PATH"] = test_manifests_dir
    os.environ["NODE_ID_FILE"] = node_id_file
    os.environ["REMOTE_ACCESS_DOMAIN"] = DOMAIN
    os.environ["SESSION_COOKIE_SECURE"] = "0"
    os.environ["MENDER_CONNECT_CONF"] = os.path.join(temp_dir, "mender-connect.conf")
    os.environ["ACCESS_CONFIG_PATH"] = os.path.join(temp_dir, "access.json")

    import services as services_module
    importlib.reload(services_module)
    import app as app_module
    importlib.reload(app_module)

    app_module.app.config["TESTING"] = True
    app_module.app.config["WTF_CSRF_ENABLED"] = False
    with app_module.app.test_client() as c:
        c.remote_access = services_module.remote_access
        c.access_identity = services_module.access_identity
        yield c

    del os.environ["SESSION_COOKIE_SECURE"]
    del os.environ["REMOTE_ACCESS_DOMAIN"]
    del os.environ["MENDER_CONNECT_CONF"]
    del os.environ["ACCESS_CONFIG_PATH"]


@pytest.fixture
def live(client):
    """A client whose node has remote access switched on."""
    client.remote_access.set_password(GOOD_PASSWORD)
    client.remote_access.set_enabled(True)
    return client


def _sign_in(client, password=GOOD_PASSWORD):
    return client.post("/login", data={"password": password},
                       base_url=OWNER_URL, follow_redirects=False)


def test_lan_never_asks_for_a_password(live):
    """The LAN is unauthenticated and this change must not alter that."""
    assert live.get("/", base_url=LAN_URL).status_code == 200


def test_a_mender_port_forward_is_not_challenged(live):
    """How Offworld engineers reach a node. Mender already authenticated them."""
    assert live.get("/", base_url=PORT_FORWARD_URL).status_code == 200


def test_owner_pathway_is_404_while_remote_access_is_off(client):
    """Nothing should be answering on that name, so do not confirm it exists."""
    assert client.get("/", base_url=OWNER_URL).status_code == 404


def test_owner_pathway_redirects_to_login(live):
    r = live.get("/config", base_url=OWNER_URL)
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_login_page_is_reachable_without_a_session(live):
    assert live.get("/login", base_url=OWNER_URL).status_code == 200


def test_wrong_password_is_refused(live):
    r = _sign_in(live, "not-the-password")
    assert r.status_code == 401
    assert live.get("/", base_url=OWNER_URL).status_code == 302


def test_correct_password_grants_access(live):
    assert _sign_in(live).status_code == 302
    assert live.get("/", base_url=OWNER_URL).status_code == 200


def test_a_post_without_a_session_is_403_not_a_redirect(live):
    """fetch() callers need a status, not a login page to parse as JSON."""
    r = live.post("/node-name", data={"name": "x"}, base_url=OWNER_URL)
    assert r.status_code == 403


@pytest.mark.parametrize("path,data", [
    ("/ssh-keys", {"ssh_key": "ssh-ed25519 AAAAC3Nz"}),
    ("/ssh-keys/delete", {"ssh_key": "ssh-ed25519 AAAAC3Nz"}),
    ("/remote-access/password", {"password": "another-password-x"}),
    ("/remote-access/toggle", {"enabled": "0"}),
    ("/remote-access/generate", {}),
    ("/mender/install", {}),
])
def test_signed_in_owner_still_cannot_reach_the_presence_tier(live, path, data):
    """The invariant this whole design rests on.

    The password gets shared. If whoever it was shared with can add an SSH key
    or change the password, then rotating it no longer revokes anything and the
    owner's only remedy is reinstalling the node.
    """
    _sign_in(live)
    assert live.post(path, data=data, base_url=OWNER_URL).status_code == 403


def test_the_presence_tier_still_works_on_the_lan(live):
    _sign_in(live)
    r = live.post("/ssh-keys", data={"ssh_key": "ssh-ed25519 AAAAC3Nz"},
                  base_url=LAN_URL)
    assert r.status_code in (200, 302)


def test_the_presence_tier_still_works_over_a_port_forward(live):
    """An engineer on a port-forward gets the same reach as someone at home."""
    r = live.post("/remote-access/toggle", data={"enabled": "0"},
                  base_url=PORT_FORWARD_URL)
    assert r.status_code == 200
    assert live.remote_access.is_enabled() is False


def test_the_sign_out_control_only_appears_on_the_owner_pathway(live):
    """It is also the only thing that renders `pathway`, so this is what keeps
    that template variable honest."""
    _sign_in(live)
    assert b"/logout" in live.get("/", base_url=OWNER_URL).data
    assert b"/logout" not in live.get("/", base_url=LAN_URL).data
    assert b"/logout" not in live.get("/", base_url=PORT_FORWARD_URL).data


def test_the_password_is_never_rendered(live):
    """Support access is gated by Cloudflare Access, so the node password is not
    part of the owner-facing design and must not leak onto a page on any path."""
    live.remote_access.set_password("kitchen radar")
    _sign_in(live, "kitchen radar")
    for url in (LAN_URL, OWNER_URL, PORT_FORWARD_URL):
        assert b"kitchen radar" not in live.get("/config", base_url=url).data


def _write_connect_conf(temp_dir, shell_enabled):
    with open(os.path.join(temp_dir, "mender-connect.conf"), "w") as f:
        json.dump({"Terminal": {"Disable": not shell_enabled},
                   "PortForward": {"Disable": not shell_enabled},
                   "FileTransfer": {"Disable": False}}, f)


def test_page_warns_when_the_setting_was_not_applied(live, temp_dir):
    """The page must show what the node is doing, not what we recorded.

    Without this the owner sees their own choice reflected back while
    mender-connect carries on serving the opposite, which for a setting whose
    entire job is stating what we can and cannot do is the wrong way to fail.
    """
    _write_connect_conf(temp_dir, shell_enabled=True)   # node still allows it
    live.remote_access.record_shell_allowed(False)      # but we recorded declined
    body = live.get("/config", base_url=LAN_URL).data
    assert b"Not applied" in body


def test_page_is_plain_when_record_and_node_agree(live, temp_dir):
    _write_connect_conf(temp_dir, shell_enabled=False)
    live.remote_access.record_shell_allowed(False)
    body = live.get("/config", base_url=LAN_URL).data
    assert b"Not applied" not in body
    assert b"No session can be opened" in body


def test_page_says_so_when_the_node_cannot_be_read(live, temp_dir):
    """No mender-connect.conf at all, which is every dev machine and any node
    where the file has gone missing."""
    path = os.path.join(temp_dir, "mender-connect.conf")
    if os.path.exists(path):
        os.remove(path)
    body = live.get("/config", base_url=LAN_URL).data
    assert b"could not confirm" in body


def test_no_estimate_is_promised_for_the_connection(live):
    """The old copy quoted 15 minutes, a figure that depends on node-infra's
    timer, which this node has no knowledge of and cannot promise."""
    body = live.get("/config", base_url=LAN_URL).data
    assert b"15 minutes" not in body


def test_both_agreements_appear_on_the_config_page(live):
    body = live.get("/config", base_url=LAN_URL).data
    assert b'id="remoteToggle"' in body
    assert b'id="shellToggle"' in body


def test_logout_ends_the_session(live):
    _sign_in(live)
    assert live.post("/logout", base_url=OWNER_URL).status_code == 302
    assert live.get("/", base_url=OWNER_URL).status_code == 302


def test_turning_remote_access_off_locks_an_existing_session_out(live):
    """Revocation must not wait for a cookie to expire."""
    _sign_in(live)
    assert live.get("/", base_url=OWNER_URL).status_code == 200
    live.remote_access.set_enabled(False)
    assert live.get("/", base_url=OWNER_URL).status_code == 404


def test_login_does_not_redirect_off_site(live):
    """`next` comes from the query string, so it is attacker-supplied."""
    r = live.post("/login", data={"password": GOOD_PASSWORD, "next": "//evil.test/x"},
                  base_url=OWNER_URL)
    assert r.headers["Location"] in ("/", f"{OWNER_URL}/")


def test_generate_returns_a_working_password_once(live):
    r = live.post("/remote-access/generate", base_url=LAN_URL)
    assert r.status_code == 200
    password = r.get_json()["password"]
    assert live.remote_access.verify(password) is True


def test_secret_key_survives_a_restart(client, temp_dir):
    """Otherwise every GUI restart and every OTA signs the owner out.

    Deliberately does not reload services to simulate the restart. secret_key()
    reads the file on every call, so the persistence being tested is the file
    itself, and asserting its contents proves it without touching module state.
    An earlier version did reload, which left app.py's singletons bound to the
    previous services objects and broke TestSharedServiceSingletons for whatever
    ran next. The alphabetical suite order hid that.
    """
    import services as services_module

    first = services_module.secret_key()
    assert first
    assert services_module.secret_key() == first

    key_file = os.path.join(temp_dir, "secret-key")
    with open(key_file) as f:
        assert f.read().strip() == first
    assert stat.S_IMODE(os.stat(key_file).st_mode) == 0o600
