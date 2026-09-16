"""POST /set-up/contact, and the file it writes.

Whom to contact about this node. Optional everywhere: the node-ingest spec says
a node with nothing to report never calls the contact endpoint at all, so an
absent file is a legitimate steady state rather than a gap, and most of what is
worth pinning here is what happens when an owner gives us nothing.
"""
import json
import os

import pytest


def post(client, payload):
    return client.post('/set-up/contact',
                       data=json.dumps(payload),
                       content_type='application/json')


def stored(app_module):
    return app_module.device_state.get_telemetry_contact()


FULL = {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.com",
    "phone": "+441234567890",
    "country": "GB",
}


class TestStoringContactDetails:

    def test_a_full_document_round_trips(self, app_client):
        import app as app_module

        assert json.loads(post(app_client, FULL).data)['success'] is True
        assert stored(app_module) == FULL

    def test_the_shape_mirrors_the_wire(self, app_client):
        """One-for-one with NodeContact, so retina-telemetry translates
        nothing between what the owner typed and what the server is told."""
        import app as app_module
        post(app_client, FULL)

        assert set(stored(app_module)) <= {
            "first_name", "last_name", "email", "phone", "country"
        }

    def test_a_partial_document_stores_only_what_was_given(self, app_client):
        """Every field is independently optional. An owner who gives an email
        and nothing else is a normal owner, not a half-filled form."""
        import app as app_module

        post(app_client, {"email": "ada@example.com"})

        assert stored(app_module) == {"email": "ada@example.com"}

    def test_a_later_save_replaces_rather_than_merges(self, app_client):
        """The endpoint replaces the document wholesale, so this end has to as
        well. Merging here would leave the node holding a detail the owner had
        removed and the server no longer has."""
        import app as app_module
        post(app_client, FULL)

        post(app_client, {"email": "grace@example.com"})

        assert stored(app_module) == {"email": "grace@example.com"}


class TestGivingUsNothing:
    """Skipping the step, and clearing the details later, are the same thing."""

    def test_an_empty_document_writes_no_file(self, app_client):
        import app as app_module

        assert json.loads(post(app_client, {}).data)['success'] is True
        assert stored(app_module) == {}
        assert not os.path.exists(app_module.device_state.telemetry_contact_file)

    def test_clearing_every_box_removes_the_file(self, app_client):
        """Not a no-op: an owner emptying every box wants the details gone,
        and an empty document left behind would read as "still stored"."""
        import app as app_module
        post(app_client, FULL)
        assert os.path.exists(app_module.device_state.telemetry_contact_file)

        post(app_client, dict.fromkeys(FULL, ""))

        assert stored(app_module) == {}
        assert not os.path.exists(app_module.device_state.telemetry_contact_file)

    def test_blank_values_are_dropped_rather_than_stored_as_null(self, app_client):
        import app as app_module

        post(app_client, {**FULL, "phone": "", "country": None})

        assert "phone" not in stored(app_module)
        assert "country" not in stored(app_module)

    def test_whitespace_is_not_a_detail(self, app_client):
        """A space in the email box would otherwise be stored, sent, and shown
        back to the owner as though it were something."""
        import app as app_module

        post(app_client, {"email": "   ", "first_name": "  Ada  "})

        assert stored(app_module) == {"first_name": "Ada"}

    def test_reading_an_absent_file_is_empty_not_an_error(self, app_client):
        import app as app_module

        assert stored(app_module) == {}


class TestRefusals:

    def test_a_country_that_is_not_two_letters_is_refused(self, app_client):
        import app as app_module

        response = post(app_client, {**FULL, "country": "GBR"})
        body = json.loads(response.data)

        assert response.status_code == 400
        assert "country" in body["errors"]
        assert stored(app_module) == {}, "a refused document must store nothing"

    def test_a_country_is_stored_uppercase(self, app_client):
        """ISO 3166-1 alpha-2 is uppercase, and an owner typing 'gb' means GB."""
        import app as app_module

        post(app_client, {**FULL, "country": "gb"})

        assert stored(app_module)["country"] == "GB"

    @pytest.mark.parametrize("field,length", [
        ("first_name", 65), ("last_name", 65), ("email", 256), ("phone", 33),
    ])
    def test_a_value_past_the_wire_cap_is_refused(self, app_client, field, length):
        """The caps are the spec's. A value past one of them means
        retina-telemetry cannot build the payload, so the whole document is
        refused and the owner is never reachable: better to say so here."""
        import app as app_module

        response = post(app_client, {field: "x" * length})

        assert response.status_code == 400
        assert stored(app_module) == {}

    def test_a_value_at_the_cap_is_accepted(self, app_client):
        import app as app_module

        post(app_client, {"first_name": "x" * 64})

        assert stored(app_module) == {"first_name": "x" * 64}

    def test_a_missing_body_is_refused(self, app_client):
        response = app_client.post('/set-up/contact',
                                   data="not json",
                                   content_type='application/json')

        assert response.status_code == 400


class TestIndependenceFromTheRestOfSetup:

    def test_contact_does_not_touch_the_consent_records(self, app_client):
        """Separate files on purpose. retina-telemetry refuses to register
        without all three consent records, so a contact document sharing that
        file could stop a node registering."""
        import app as app_module
        app_client.post('/set-up/consent', data=json.dumps({}),
                        content_type='application/json')

        post(app_client, FULL)

        consent = app_module.device_state.get_telemetry_consent()
        assert set(consent) == {"licence", "remote_management", "publication"}

    def test_contact_needs_no_consent_and_no_completed_wizard(self, app_client):
        """It is reachable from Settings long after setup, and skippable
        during it. Neither gate belongs on a diagnostic field."""
        import app as app_module

        assert json.loads(post(app_client, FULL).data)['success'] is True
        assert stored(app_module) == FULL


class TestBothSurfacesRender:
    """A Jinja slip in the prefill would 500 the page rather than fail a
    check, and neither page is covered by the route tests above."""

    def test_the_wizard_shows_the_contact_step(self, app_client):
        page = app_client.get('/set-up').data.decode()

        assert 'data-step="contact"' in page
        assert 'How can we reach you?' in page
        assert 'contactSkipBtn' in page, "skipping must be offered, not just possible"

    def test_the_wizard_prefills_what_is_stored(self, app_client):
        post(app_client, FULL)

        page = app_client.get('/set-up').data.decode()

        assert 'value="Ada"' in page
        assert 'value="ada@example.com"' in page

    def test_the_config_page_shows_it_under_remote_support(self, app_client):
        page = app_client.get('/config').data.decode()

        assert 'How we reach you' in page
        assert 'contactSaveBtn' in page

    def test_the_config_page_prefills_what_is_stored(self, app_client):
        post(app_client, FULL)

        page = app_client.get('/config').data.decode()

        assert 'value="Lovelace"' in page
        assert 'value="GB"' in page

    def test_both_pages_render_with_nothing_stored(self, app_client):
        """The ordinary case, and the one where `contact` is an empty dict."""
        assert app_client.get('/set-up').status_code == 200
        assert app_client.get('/config').status_code == 200
