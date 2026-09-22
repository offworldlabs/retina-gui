"""POST /set-up/claim, and the file it writes.

The address that owns the node, which is a different question from the contact
details and carries a worse failure: the server mails this address a link, and
opening it binds the node to the account behind it. A wrong value here mails a
stranger a link that hands them somebody's node.

Most of what is worth pinning is the difference between saving an address and
asking for another link, because the two are not interchangeable and the reason
is not obvious. Re-offering an address the node already holds is accepted,
changes nothing and mails nothing, so after a declined link the node sits
unclaimed with the address still on file and no amount of saving will move it.
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

    def test_saving_does_not_ask_for_a_link(self, app_client):
        """A changed address is what makes retina-telemetry offer it, and that
        offer is the call that mails. Saving is not a second way to ask."""
        import app as app_module
        post(app_client, {"email": ADDRESS})

        assert 'send_requested_at' not in stored(app_module)

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

    def test_a_resend_stamps_the_document(self, app_client):
        """The timestamp is how the ask reaches a service that binds no ports
        and cannot be called."""
        import app as app_module
        post(app_client, {"email": ADDRESS})

        assert json.loads(post(app_client, {"email": ADDRESS, "resend": True}).data)['requested']
        assert stored(app_module)['send_requested_at'].endswith('Z')

    def test_an_ordinary_save_keeps_the_last_ask(self, app_client):
        """Saving a typo fix must not read as a fresh ask, and must not lose
        the record of the one before it either."""
        import app as app_module
        post(app_client, {"email": ADDRESS, "resend": True})
        asked = stored(app_module)['send_requested_at']

        post(app_client, {"email": ADDRESS})

        assert stored(app_module)['send_requested_at'] == asked

    def test_a_resend_with_no_address_is_refused(self, app_client):
        """There is nothing to send to, and the complaint is attached to the
        box so the page can mark it."""
        response = post(app_client, {"email": "", "resend": True})
        body = json.loads(response.data)

        assert response.status_code == 400
        assert body['success'] is False
        assert 'email' in body['errors']


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

    def test_a_declined_link_points_at_send_again(self, app_client, monkeypatch):
        """The trap the live run found. A decline leaves the address on file,
        so saving it again is a no-op that mails nothing, and Send again is the
        only thing that produces another link. The page has to say so."""
        page = self._with_claim(
            app_client, monkeypatch,
            {'state': 'unclaimed', 'email': ADDRESS, 'undeliverable': False})

        assert 'No link is waiting' in page

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
