"""Tests for the two deadlines bound to how long the setup wizard stays open.

The wizard is one page load. It reads a CSRF token once, holds it for the
whole run, and deliberately survives an OS update and the reboot that update
triggers. Two separate clocks used to kill it from underneath the owner:

  * Flask-WTF expires a token 3600s after issue, and a real run passes the
    hour easily (agreements, OS update, ~600 MB of packages, calibration).
  * SECRET_KEY is `os.urandom` per process, so the reboot the wizard is
    designed to survive invalidates every token already held. That half is
    fixed by services.secret_key() on 20260828-remote-access and is covered
    by that branch's tests, not here.

These surfaced as `Failed to save: Unexpected token '<'` when a caller parsed
the HTML error page, or as a step wedged mid-operation when nobody did.
"""
from unittest.mock import patch

import pytest
from flask_wtf.csrf import generate_csrf, validate_csrf
from itsdangerous.timed import TimestampSigner
from wtforms.validators import ValidationError


def _issued_hours_ago(hours):
    """Patch the signer so a token minted inside the block looks that old."""
    real = TimestampSigner.get_timestamp
    return patch.object(
        TimestampSigner, 'get_timestamp',
        lambda self: real(self) - int(hours * 3600),
    )


@pytest.fixture
def csrf_app(app_client):
    """The app app_client built, with CSRF validation switched back on.

    app_client disables it so route tests need not carry tokens. These tests
    are about the validation itself, so they need the real thing.
    """
    import app as app_module

    app_module.app.config['WTF_CSRF_ENABLED'] = True
    yield app_module.app
    app_module.app.config['WTF_CSRF_ENABLED'] = False


class TestCsrfOutlivesTheWizard:
    def test_time_limit_is_disabled(self, csrf_app):
        assert csrf_app.config['WTF_CSRF_TIME_LIMIT'] is None, (
            "Flask-WTF's 3600s default expires the wizard's token mid-run"
        )

    def test_token_still_valid_after_three_hours(self, csrf_app):
        with csrf_app.test_request_context():
            with _issued_hours_ago(3):
                token = generate_csrf()
            validate_csrf(token)  # must not raise

    def test_the_same_token_would_have_failed_on_the_old_default(self, csrf_app):
        """Control: proves the backdating above is real, not a no-op.

        Without this, test_token_still_valid_after_three_hours would keep
        passing if the patch stopped taking effect.
        """
        with csrf_app.test_request_context():
            with _issued_hours_ago(3):
                token = generate_csrf()
            with pytest.raises(ValidationError, match='expired'):
                validate_csrf(token, time_limit=3600)


class TestCsrfErrorIsAnsweredInJson:
    """`Failed to save: Unexpected token '<'` was Flask-WTF's HTML error page
    reaching r.json(). A rejected token has to answer JSON callers in JSON."""

    def test_rejected_token_returns_json_not_html(self, csrf_app):
        with csrf_app.test_client() as client:
            resp = client.post('/set-up/consent', json={},
                               headers={'X-CSRFToken': 'not-a-real-token'})
        assert resp.status_code == 400
        assert resp.is_json, f"expected JSON, got {resp.content_type}"
        assert resp.get_json()['session_expired'] is True

    def test_message_tells_the_owner_what_to_do(self, csrf_app):
        with csrf_app.test_client() as client:
            resp = client.post('/set-up/consent', json={},
                               headers={'X-CSRFToken': 'not-a-real-token'})
        assert 'Reload' in resp.get_json()['error']
