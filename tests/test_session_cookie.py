"""The session cookie's flags, which differ per pathway.

These run with SESSION_COOKIE_SECURE on, as a node does, because that is the
only configuration where the bug they exist for can appear. The rest of the
suite turns it off so the test client can hold a cookie at all, which is exactly
why nothing caught this: a Secure cookie is silently dropped by the client that
would have noticed.

The bug: the cookie was Secure and `__Host-` prefixed on every response,
including the LAN's plain HTTP. Browsers discard a Secure cookie delivered over
HTTP, so the LAN held no session, and CSRFProtect keeps its token in the
session. Every POST an owner made from their own network was refused with 400.
"""

import importlib
import json
import os

import pytest

NODE_ID = "ret7dd2cb0d"
DOMAIN = "retnode.com"
LAN_URL = "http://owl.local"
OWNER_URL = f"http://{NODE_ID}.{DOMAIN}"
PORT_FORWARD_URL = "http://localhost:8080"


@pytest.fixture
def client(temp_dir, config_files, test_manifests_dir):
    """A node-like app: Secure cookies permitted, CSRF enforced."""
    user_path, merged_path = config_files

    mender_dir = os.path.join(temp_dir, "mender")
    os.makedirs(mender_dir, exist_ok=True)
    node_id_file = os.path.join(mender_dir, "node_id")
    with open(node_id_file, "w") as f:
        f.write(NODE_ID)

    saved = {k: os.environ.get(k) for k in
             ("DATA_DIR", "USER_CONFIG_PATH", "MERGED_CONFIG_PATH",
              "RETINA_NODE_PATH", "NODE_ID_FILE", "REMOTE_ACCESS_DOMAIN",
              "SESSION_COOKIE_SECURE", "MENDER_CONNECT_CONF",
              "ACCESS_CONFIG_PATH")}

    os.environ.update({
        "DATA_DIR": temp_dir,
        "USER_CONFIG_PATH": user_path,
        "MERGED_CONFIG_PATH": merged_path,
        "RETINA_NODE_PATH": test_manifests_dir,
        "NODE_ID_FILE": node_id_file,
        "REMOTE_ACCESS_DOMAIN": DOMAIN,
        # The whole point: as a node runs.
        "SESSION_COOKIE_SECURE": "1",
        "MENDER_CONNECT_CONF": os.path.join(temp_dir, "mender-connect.conf"),
        "ACCESS_CONFIG_PATH": os.path.join(temp_dir, "access.json"),
    })

    import services as services_module
    importlib.reload(services_module)
    import app as app_module
    importlib.reload(app_module)

    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        c.remote_access = services_module.remote_access
        yield c

    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _set_cookie(response):
    return " ".join(response.headers.getlist("Set-Cookie"))


def _drop_secure_cookies(client, response):
    """Discard cookies a browser would have refused over plain HTTP.

    The test client, like curl, happily stores a Secure cookie delivered over
    http and sends it back. A browser does not. That difference is precisely
    what hid this bug: every automated check passed while every real browser
    was left without a session.

    So the rule is applied here rather than assumed away.
    """
    for header in response.headers.getlist("Set-Cookie"):
        if "secure" not in header.lower():
            continue
        name = header.split("=", 1)[0].strip()
        try:
            client.delete_cookie(name, domain="owl.local")
            client.delete_cookie(name, domain="localhost")
        except TypeError:  # older werkzeug signature
            client.delete_cookie("owl.local", name)


# ── the flags ────────────────────────────────────────────────────

def test_the_lan_cookie_is_not_secure(client):
    """The regression. A Secure cookie over plain HTTP is discarded by the
    browser, leaving the LAN with no session to keep a CSRF token in."""
    header = _set_cookie(client.get("/", base_url=LAN_URL))
    assert "Secure" not in header
    assert "__Host-" not in header


def test_a_port_forward_is_treated_as_lan(client):
    """Mender port-forwards arrive as localhost over plain HTTP too."""
    header = _set_cookie(client.get("/", base_url=PORT_FORWARD_URL))
    assert "Secure" not in header


def test_the_remote_cookie_keeps_secure_and_the_host_prefix(client):
    """Every node is a sibling under one registrable domain, so without the
    prefix one node could set a `.retnode.com` cookie shadowing another's."""
    client.remote_access.set_enabled(True)
    header = _set_cookie(client.get("/login", base_url=OWNER_URL))
    if header:
        assert "Secure" in header
        assert "__Host-session" in header


# ── what the flags were breaking ─────────────────────────────────

def test_an_owner_can_post_from_the_lan(client):
    """The failure as it actually presented: every POST from an owner's own
    network refused with 400, so no toggle, no config change, no setup wizard.

    Driven through CSRF rather than around it, because the cookie was only ever
    a problem via the token CSRFProtect keeps inside the session.
    """
    page = client.get("/config", base_url=LAN_URL)
    assert page.status_code == 200
    # As a browser would: anything Secure never made it into the jar.
    _drop_secure_cookies(client, page)

    body = page.get_data(as_text=True)
    marker = 'name="csrf-token" content="'
    assert marker in body, "config page carries no CSRF token to test with"
    token = body.split(marker, 1)[1].split('"', 1)[0]

    r = client.post("/remote-access/toggle", data={"enabled": "1"},
                    headers={"X-CSRFToken": token}, base_url=LAN_URL)
    assert r.status_code == 200, f"LAN POST refused with {r.status_code}"
    assert json.loads(r.get_data(as_text=True))["ok"] is True


def test_the_session_survives_across_requests_on_the_lan(client):
    """A cookie the client declines to store shows up as a session that resets
    every request, which is the shape the CSRF failure actually took."""
    client.get("/config", base_url=LAN_URL)
    header = _set_cookie(client.get("/config", base_url=LAN_URL))
    # Either the cookie was kept (nothing re-set) or it was re-set unsecured,
    # but it must never be re-set with Secure on a plain HTTP pathway.
    assert "Secure" not in header
