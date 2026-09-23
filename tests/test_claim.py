"""POST /set-up/claim, and the file it writes.

The address that owns the node, which is a different question from the contact
details and carries a worse failure: the server mails this address a link, and
opening it binds the node to the account behind it. A wrong value here mails a
stranger a link that hands them somebody's node.

Most of what is worth pinning is that every submission is an ask for a link.
There used to be a Save beside Send again, and it was a trap: saving an address
the node already held wrote an identical file, so retina-telemetry could not see
the press and nothing was mailed. A released node showed its owner exactly that.
"""
import json

import pytest


def post(client, payload):
    return client.post('/set-up/claim',
                       data=json.dumps(payload),
                       content_type='application/json')


def stored(app_module):
    return app_module.device_state.get_telemetry_claim()


ADDRESS = "owner@example.com"


class TestStoringTheAddress:

    def test_an_address_round_trips(self, app_client):
        import app as app_module

        assert json.loads(post(app_client, {"email": ADDRESS}).data)['success'] is True
        assert stored(app_module)['email'] == ADDRESS

    def test_sending_asks_for_a_link(self, app_client):
        """The timestamp is how the ask reaches a service that binds no ports
        and cannot be called."""
        import app as app_module
        post(app_client, {"email": ADDRESS})

        assert stored(app_module)['send_requested_at'].endswith('Z')

    def test_the_address_is_trimmed(self, app_client):
        import app as app_module
        post(app_client, {"email": f"  {ADDRESS}  "})

        assert stored(app_module)['email'] == ADDRESS

    def test_clearing_the_box_removes_the_record(self, app_client):
        """It does not unclaim the node, which only its owner can do from the
        dashboard. It removes the address this node was told to offer."""
        import app as app_module
        post(app_client, {"email": ADDRESS})

        assert json.loads(post(app_client, {"email": ""}).data)['stored'] is False
        assert stored(app_module) == {}

    def test_it_is_kept_apart_from_the_contact_details(self, app_client):
        """Different questions, different files, and nothing copies between
        them. An owner may give the same address twice; that is their answer to
        both rather than our licence to infer one from the other."""
        import app as app_module
        post(app_client, {"email": ADDRESS})

        assert app_module.device_state.get_telemetry_contact() == {}
        assert app_module.device_state.telemetry_claim_file != \
            app_module.device_state.telemetry_contact_file


class TestAskingForAnotherLink:

    def test_the_same_address_again_is_a_fresh_ask(self, app_client):
        """The case the old Save could not express. Unchanged address, and the
        press still has to reach the node, so the stamp moves and the file is
        no longer identical to the one before it."""
        import app as app_module
        post(app_client, {"email": ADDRESS})
        path = app_module.device_state.telemetry_claim_file
        with open(path, 'w') as f:
            json.dump({"email": ADDRESS, "send_requested_at": "2026-01-01T00:00:00Z"}, f)

        post(app_client, {"email": ADDRESS})

        assert stored(app_module)['send_requested_at'] != "2026-01-01T00:00:00Z"
        assert stored(app_module)['email'] == ADDRESS


class TestRefusingABadAddress:

    @pytest.mark.parametrize("bad", ["nope", "a b@example.com", "two@@example.com", "@example.com"])
    def test_a_malformed_address_is_refused(self, app_client, bad):
        """Checked here rather than left to the server, which would refuse it
        thirty seconds later with nothing on screen to explain why."""
        response = post(app_client, {"email": bad})

        assert response.status_code == 400
        assert 'email' in json.loads(response.data)['errors']

    def test_a_refused_address_is_not_stored(self, app_client):
        import app as app_module
        post(app_client, {"email": ADDRESS})

        post(app_client, {"email": "nope"})

        assert stored(app_module)['email'] == ADDRESS

    def test_an_address_past_the_specs_cap_is_refused(self, app_client):
        """255 is the node-ingest spec's bound on NodeClaimRequest.email. Past
        it retina-telemetry cannot build the payload at all."""
        response = post(app_client, {"email": "a" * 250 + "@example.com"})

        assert response.status_code == 400

    def test_a_missing_body_is_refused(self, app_client):
        response = app_client.post('/set-up/claim', data='', content_type='application/json')

        assert response.status_code == 400


class TestWhatTheSectionShows:

    def _with_claim(self, app_client, monkeypatch, claim):
        import app as app_module
        monkeypatch.setattr(app_module.telemetry_status, 'read',
                            lambda: {'stale': False, 'claim': claim})
        return app_client.get('/config').data.decode()

    def test_the_section_is_on_the_config_page(self, app_client):
        page = app_client.get('/config').data.decode()

        assert 'id="claim"' in page
        assert 'Node claim' in page

    def test_the_stored_address_is_prefilled(self, app_client):
        post(app_client, {"email": ADDRESS})

        assert ADDRESS in app_client.get('/config').data.decode()

    def test_it_admits_when_telemetry_has_not_reported(self, app_client):
        """No status document at all, which is what a node whose telemetry is
        not running looks like. Showing "not claimed" there would be a claim
        this page is in no position to make."""
        assert 'so we cannot say' in app_client.get('/config').data.decode()

    def test_an_owned_node_names_its_owner(self, app_client, monkeypatch):
        page = self._with_claim(
            app_client, monkeypatch,
            {'state': 'owned', 'email': ADDRESS, 'undeliverable': False})

        assert 'Claimed' in page
        assert ADDRESS in page

    def test_a_declined_link_says_how_to_get_another(self, app_client, monkeypatch):
        """A decline leaves the address on file and nothing more arrives
        unless asked, so the page has to say which button asks."""
        page = self._with_claim(
            app_client, monkeypatch,
            {'state': 'unclaimed', 'email': ADDRESS, 'undeliverable': False})

        assert 'No link is waiting' in page
        assert 'Send link' in page

    def test_an_owned_node_cannot_be_sent_a_link(self, app_client, monkeypatch):
        """A resend is refused on an owned node, and a new address would be
        offering somebody else's node. Releasing is the dashboard's."""
        page = self._with_claim(
            app_client, monkeypatch,
            {'state': 'owned', 'email': ADDRESS, 'undeliverable': False})

        button = page[page.index('id="claimSendBtn"'):]
        assert button[:button.index('>')].rstrip().endswith('disabled')
        assert 'Release it from your dashboard' in page

    def test_an_unclaimed_node_can_be_sent_a_link(self, app_client, monkeypatch):
        page = self._with_claim(
            app_client, monkeypatch,
            {'state': 'unclaimed', 'email': None, 'undeliverable': False})

        button = page[page.index('id="claimSendBtn"'):]
        assert 'disabled' not in button[:button.index('>')]

    def test_a_bounced_address_is_called_out(self, app_client, monkeypatch):
        """Sending again cannot help until the address changes, so an owner
        pressing it repeatedly needs telling why nothing arrives."""
        page = self._with_claim(
            app_client, monkeypatch,
            {'state': 'pending', 'email': ADDRESS, 'undeliverable': True})

        assert 'bounced' in page

    def test_an_unrecognised_state_is_shown_rather_than_hidden(self, app_client, monkeypatch):
        """The same discipline retina-telemetry applies when it passes the
        string through: a value from a later server reaches the owner as
        itself rather than disappearing."""
        page = self._with_claim(
            app_client, monkeypatch,
            {'state': 'disputed', 'email': ADDRESS, 'undeliverable': False})

        assert 'disputed' in page
