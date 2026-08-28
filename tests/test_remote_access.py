"""Tests for remote access: the password, and the three request pathways.

The tests that matter most here are the pathway ones. A bug in the storage layer
is a broken feature; a bug in the gate is a stranger with an SSH key on somebody
else's node.
"""

import importlib
import os
import stat

import pytest

from remote_access import (
    ADMIN,
    LAN,
    MIN_PASSWORD_LENGTH,
    OWNER,
    RemoteAccess,
    classify_host,
    generate_password,
    requires_presence,
)

NODE_ID = "ret7dd2cb0d"
DOMAIN = "retnode.com"
OWNER_URL = f"http://{NODE_ID}.{DOMAIN}"
ADMIN_URL = f"http://{NODE_ID}.admin.{DOMAIN}"
LAN_URL = "http://owl.local"

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


def test_password_is_not_stored_in_the_clear(store):
    """The file holds a hash. A password read off disk is the whole feature gone."""
    store.set_password(GOOD_PASSWORD)
    with open(store.state_file) as f:
        assert GOOD_PASSWORD not in f.read()


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
    (f"{NODE_ID}.admin.{DOMAIN}", ADMIN),
    ("", LAN),
])
def test_classify_host(host, expected):
    assert classify_host(host, NODE_ID, DOMAIN) == expected


def test_unknown_names_on_the_remote_domain_fail_closed():
    """Anything unrecognised under the zone must ask for a password, not be
    mistaken for the LAN and waved through."""
    assert classify_host(f"surprise.{DOMAIN}", NODE_ID, DOMAIN) == OWNER
    assert classify_host(DOMAIN, NODE_ID, DOMAIN) == OWNER


def test_another_nodes_admin_name_is_not_our_admin_name():
    assert classify_host(f"ret9f2b1e44.admin.{DOMAIN}", NODE_ID, DOMAIN) == OWNER


def test_an_unreadable_node_id_does_not_open_the_owner_path():
    """read_node_id() returns 'Unknown' when /data/mender is missing. That must
    not turn the owner hostname into an unauthenticated one."""
    assert classify_host(f"{NODE_ID}.{DOMAIN}", "Unknown", DOMAIN) == OWNER
    assert classify_host(f"{NODE_ID}.admin.{DOMAIN}", "Unknown", DOMAIN) == OWNER


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

    import services as services_module
    importlib.reload(services_module)
    import app as app_module
    importlib.reload(app_module)

    app_module.app.config["TESTING"] = True
    app_module.app.config["WTF_CSRF_ENABLED"] = False
    with app_module.app.test_client() as c:
        c.remote_access = services_module.remote_access
        yield c

    del os.environ["SESSION_COOKIE_SECURE"]
    del os.environ["REMOTE_ACCESS_DOMAIN"]


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


def test_admin_pathway_is_not_challenged(live):
    """Cloudflare Access authenticated them at the edge. Asking again is theatre."""
    assert live.get("/", base_url=ADMIN_URL).status_code == 200


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


def test_the_presence_tier_still_works_for_admins(live):
    r = live.post("/remote-access/toggle", data={"enabled": "0"},
                  base_url=ADMIN_URL)
    assert r.status_code == 200
    assert live.remote_access.is_enabled() is False


def test_the_sign_out_control_only_appears_on_the_owner_pathway(live):
    """It is also the only thing that renders `pathway`, so this is what keeps
    that template variable honest."""
    _sign_in(live)
    assert b"/logout" in live.get("/", base_url=OWNER_URL).data
    assert b"/logout" not in live.get("/", base_url=LAN_URL).data
    assert b"/logout" not in live.get("/", base_url=ADMIN_URL).data


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
    """Otherwise every GUI restart and every OTA signs the owner out."""
    import services as services_module
    first = services_module.secret_key()
    importlib.reload(services_module)
    assert services_module.secret_key() == first
    key_file = os.path.join(temp_dir, "secret-key")
    assert stat.S_IMODE(os.stat(key_file).st_mode) == 0o600
