"""The setup wizard's order and the steps this round added or reshaped.

See docs/features/setup-wizard.md#step-order.
"""
import json
import os
import re
from unittest.mock import MagicMock, patch

import pytest
import yaml

HERE = os.path.dirname(__file__)
ROOT = os.path.join(HERE, '..')


def page(client, **args):
    return client.get('/set-up', query_string=args).data.decode()


def panel(html, name):
    """One step's markup: from its data-step to the next step's."""
    start = html.index(f'data-step="{name}"')
    following = [m.start() for m in re.finditer(r'data-step="', html) if m.start() > start]
    return html[start:following[0]] if following else html[start:]


def step_order(html):
    return re.findall(r'data-step="([a-z]+)"', html)


def setup_js():
    with open(os.path.join(ROOT, 'static', 'setup.js')) as f:
        return f.read()


@pytest.fixture
def legal_dir(tmp_path, monkeypatch):
    """A legal directory with a (fake) PDF for each document, so the serving
    paths are tested whether or not the real files are in the checkout."""
    import routes.home as home
    for filename, _name, _title in home.LEGAL_DOCUMENTS.values():
        (tmp_path / filename).write_bytes(b'%PDF-1.4\n%fake\n')
    monkeypatch.setattr(home, 'LEGAL_DIR', str(tmp_path))
    return tmp_path


# ── Order ──────────────────────────────────────────────────────


class TestStepOrder:
    def test_the_steps_run_in_this_order(self, app_client):
        """Agreements and contact first, while the owner is still answering
        about themselves; then the node is brought up to date, sited and
        tuned; then named; and connecting it to an account is the last thing,
        so the link binds a node that is already set up."""
        assert step_order(page(app_client)) == [
            'agreements', 'contact', 'system', 'radar', 'location', 'towers',
            'calibrate', 'name', 'account', 'complete',
        ]

    def test_setup_js_names_every_step(self, app_client):
        js = setup_js()
        names = js[js.index('var stepNames = {'):js.index('};', js.index('var stepNames = {'))]
        for step in step_order(page(app_client)):
            assert f'{step}:' in names, step

    def test_every_step_has_its_footer_buttons(self, app_client):
        html = page(app_client)
        for step in step_order(html):
            assert f'id="stepBtns-{step}"' in html, step


class TestResumingFromAnOlderBuild:
    """The saved resume point is only a step name, and the build that reads
    it can be newer than the one that wrote it: the wizard ships inside
    OWL-OS and updates itself at the System Update step, so every node
    flashed before this release starts the wizard on the old build and
    finishes it on this one. The old order had the claim third; it is ninth
    now."""

    def test_the_old_claim_key_is_not_reused(self, app_client):
        """node-infra pushes the OS update as soon as the agreements step
        turns cloud services on, and the old build's next two steps were
        contact and claim. A saved `claim` must not match the ninth step."""
        assert 'claim' not in step_order(page(app_client))
        assert 'data-step="account"' in panel(page(app_client), 'account')

    def test_a_node_on_the_old_claim_resumes_at_the_update_step(self, app_client, temp_dir):
        """Seeded as the old build saves it while the owner is on its claim
        step. Before the fix this rendered initSetupWizard("claim"), which
        opened at step 9 of 10 with completion the next click, skipping
        System Update, Packages, location, towers, tuning and naming."""
        from datetime import datetime
        with open(os.path.join(temp_dir, 'setup-wizard.json'), 'w') as f:
            json.dump({'started_at': datetime.now().isoformat(), 'step': 'claim'}, f)
        assert 'initSetupWizard("system"' in page(app_client)

    def test_every_other_saved_step_resumes_where_it_was(self):
        """The rest of the old order is the new order with steps inserted,
        so those names still mean the same place."""
        from routes.setup import resume_point
        for step in ('agreements', 'contact', 'system', 'radar', 'location', 'towers', 'calibrate'):
            assert resume_point(step) == step, step
        assert resume_point(None) is None

    def test_a_step_saved_by_this_build_resumes_there(self, app_client):
        app_client.post('/set-up/save-step', json={'step': 'account'})
        assert 'initSetupWizard("account"' in page(app_client)


# ── Agreements ─────────────────────────────────────────────────


class TestAgreements:
    def test_the_two_documents_have_a_box_each_and_cloud_services_a_third(self, app_client):
        agreements = panel(page(app_client), 'agreements')
        assert 'id="eulaCheck"' in agreements
        assert 'id="privacyCheck"' in agreements
        assert 'id="cloudCheck"' in agreements
        js = setup_js()
        assert "['eulaCheck', 'privacyCheck', 'cloudCheck']" in js

    def test_the_two_sections_are_titled(self, app_client):
        agreements = panel(page(app_client), 'agreements')
        assert agreements.index('>Agreements<') < agreements.index('>Cloud Services<')
        assert '<h1>Agreements</h1>' in agreements

    def test_each_document_can_be_read_and_downloaded(self, app_client):
        agreements = panel(page(app_client), 'agreements')
        assert 'href="/eula"' in agreements
        assert 'href="/privacy-policy"' in agreements
        assert 'href="/eula?download=1"' in agreements
        assert 'href="/privacy-policy?download=1"' in agreements

    def test_the_read_links_lead_to_the_pdfs(self, app_client, legal_dir):
        resp = app_client.get('/eula')
        assert resp.status_code == 302
        assert resp.headers['Location'].endswith('/static/legal/eula.pdf')
        resp = app_client.get('/privacy-policy')
        assert resp.status_code == 302
        assert resp.headers['Location'].endswith('/static/legal/privacy-policy.pdf')

    def test_the_download_links_send_the_pdf_as_a_named_attachment(self, app_client, legal_dir):
        """The download attribute on a link is advice some browsers ignore, so
        the name travels in Content-Disposition instead."""
        for path, name in (('/eula', 'RETINA-EULA.pdf'),
                           ('/privacy-policy', 'Offworld-Labs-Privacy-Policy.pdf')):
            resp = app_client.get(path + '?download=1')
            assert resp.status_code == 200
            assert resp.mimetype == 'application/pdf'
            assert f'attachment; filename={name}' in resp.headers['Content-Disposition']
            assert resp.data[:5] == b'%PDF-'

    def test_without_the_file_both_links_say_the_document_is_coming(self, app_client, tmp_path, monkeypatch):
        """The PDFs ship separately from the flow that links to them, and a
        node can carry this flow before them. Neither link may 404."""
        import routes.home as home
        monkeypatch.setattr(home, 'LEGAL_DIR', str(tmp_path))
        for path, title in (('/eula', 'End User Licence Agreement'),
                            ('/privacy-policy', 'Privacy Policy')):
            for suffix in ('', '?download=1'):
                resp = app_client.get(path + suffix)
                assert resp.status_code == 200, path + suffix
                assert resp.mimetype == 'text/html'
                body = resp.data.decode()
                assert title in body and 'still being finalised' in body, path + suffix

    def test_the_consent_version_moved_with_the_terms(self):
        """The agreements screen changed (a privacy policy with its own box,
        new wording), so what an owner accepts today is not what the old
        version named."""
        from device_state import TELEMETRY_CONSENT_VERSION
        assert TELEMETRY_CONSENT_VERSION > "2026-08-15"

    def test_the_publication_disclosure_still_says_public(self, app_client):
        """TELEMETRY_PUBLICATION_CHOICE is "public" only while this text says so."""
        agreements = panel(page(app_client), 'agreements')
        assert 'published in a public archive' in agreements


# ── Contact and support ────────────────────────────────────────


class TestContactAndSupport:
    def test_the_step_has_a_contact_section_and_a_support_section(self, app_client):
        contact = panel(page(app_client), 'contact')
        assert contact.index('>Contact<') < contact.index('>Support<')

    def test_the_support_switches_are_the_configuration_page_s(self, app_client):
        """Same two choices, same routes, so the two surfaces cannot store
        different things."""
        contact = panel(page(app_client), 'contact')
        assert 'id="wizRemoteToggle"' in contact
        assert 'id="wizShellToggle"' in contact
        assert 'Let support open this node' in contact
        assert 'Let support open a command line' in contact
        js = setup_js()
        assert "wireSwitch('wizRemoteToggle', '/remote-access/toggle', 'enabled'" in js
        assert "wireSwitch('wizShellToggle', '/remote-access/shell', 'allowed'" in js

    def test_the_switches_show_the_node_s_current_state(self, app_client, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module.remote_access, 'status', lambda: {
            'enabled': False, 'has_password': False, 'shell_allowed': True, 'updated_at': None})
        contact = panel(page(app_client), 'contact')
        remote = contact[contact.index('id="wizRemoteToggle"'):contact.index('>', contact.index('id="wizRemoteToggle"'))]
        shell = contact[contact.index('id="wizShellToggle"'):contact.index('>', contact.index('id="wizShellToggle"'))]
        assert 'checked' not in remote
        assert 'checked' in shell


# ── System update ──────────────────────────────────────────────


class TestSystemUpdate:
    def test_the_step_says_it_updates_the_setup_flow_too(self, app_client):
        """The wizard ships inside OWL-OS: this step is where the flow itself
        is brought up to date, and it sits as early as cloud services allow."""
        system = panel(page(app_client), 'system')
        assert 'this setup flow' in system
        order = step_order(page(app_client))
        assert order.index('system') == order.index('agreements') + 2


# ── Name your node ─────────────────────────────────────────────


class TestNameYourNode:
    def test_the_step_prefills_the_current_name(self, app_client):
        app_client.post("/node-name", data={"name": "Sample Rooftop"})
        name = panel(page(app_client), 'name')
        assert 'id="wizNodeName" value="Sample Rooftop"' in name

    def test_the_rename_route_is_reachable_mid_wizard(self, app_client):
        """The config blueprint bounces everything to /set-up while the
        wizard is in progress; the naming step posts to its rename route."""
        import app as app_module
        app_module.device_state.save_setup_wizard_step('name')
        assert app_client.get('/config').status_code == 302

        resp = app_client.post("/node-name", data={"name": "Sample Rooftop"})
        assert resp.status_code == 200
        assert app_module.node_name.get() == "Sample Rooftop"

    def test_completion_writes_the_name_into_the_receiver_config(self, app_client, config_files):
        """The tower step runs before the naming step, so it wrote the node's
        id as the receiver name. Completion refreshes it from the one rule."""
        user_path, _ = config_files
        app_client.post('/towers/select', data=json.dumps({
            "rx_latitude": 40.0, "rx_longitude": -75.0, "rx_altitude": 30.0,
            "tx_latitude": 40.5, "tx_longitude": -75.5, "tx_altitude": 250.0,
            "tx_callsign": "KTST-TV",
        }), content_type='application/json')
        with open(user_path) as f:
            assert yaml.safe_load(f)['location']['rx']['name'] == 'ret7a000001'

        app_client.post("/node-name", data={"name": "Sample Rooftop"})

        # The restart's `up` is what merges the name (config-merger is a
        # compose dependency), so the name must be on disk before it runs.
        names_at_restart = []

        def restart(_path):
            with open(user_path) as f:
                names_at_restart.append(yaml.safe_load(f)['location']['rx']['name'])

        with patch('routes.sdr.enforce_radar_mode', side_effect=restart):
            resp = app_client.post('/set-up/complete')
        assert resp.get_json()['success'] is True
        assert names_at_restart == ['Sample Rooftop']

    def test_completion_leaves_an_unsited_node_alone(self, app_client, config_files):
        user_path, _ = config_files
        with open(user_path) as f:
            unsited = yaml.safe_load(f)
        unsited.pop('location', None)
        with open(user_path, 'w') as f:
            yaml.dump(unsited, f)

        app_client.post("/node-name", data={"name": "Sample Rooftop"})
        with patch('routes.sdr.enforce_radar_mode'):
            app_client.post('/set-up/complete')
        with open(user_path) as f:
            assert yaml.safe_load(f) == unsited


class TestCompletionSaysWhenItFallsShort:
    """Nothing on the page waits on /set-up/complete, so a restart that is
    skipped or fails, or a name that is not written, is invisible unless
    it is logged. Found on a test node with the restart lock held for the
    watchdog's 130 s: completion answered success after 100 s, the name
    was in user.yml but not config.yml, no container had restarted, and
    the journal held only the access log."""

    def test_a_failed_up_is_logged_with_its_exit_code_and_stderr(self, app_client, temp_dir, monkeypatch, caplog):
        """The restart's `up` runs config-merger as a dependency, so a failed
        merge stops the whole stack starting, and this is the only sign."""
        import app as app_module
        from routes.sdr import enforce_radar_mode
        monkeypatch.setattr(app_module, 'DATA_DIR', temp_dir)
        failed = MagicMock(returncode=1, stdout='',
                           stderr="service \"config-merger\" didn't complete successfully: exit 2\n")
        with patch('subprocess.run', return_value=failed), \
                patch('routes.sdr._stop_blah2'), patch('routes.sdr.restart_sdrplay_service'):
            with caplog.at_level('ERROR', logger=app_module.app.logger.name):
                enforce_radar_mode(temp_dir)
        messages = [r.message for r in caplog.records]
        assert any('up exited 1' in m and 'config-merger' in m for m in messages), messages

    def test_a_timed_out_docker_command_is_logged(self, app_client, temp_dir, monkeypatch, caplog):
        import subprocess as sp

        import app as app_module
        from routes.sdr import enforce_radar_mode
        monkeypatch.setattr(app_module, 'DATA_DIR', temp_dir)
        with patch('subprocess.run', side_effect=sp.TimeoutExpired('docker', 60)):
            with caplog.at_level('ERROR', logger=app_module.app.logger.name):
                enforce_radar_mode(temp_dir)   # must not raise
        assert any('Return to radar failed' in r.message for r in caplog.records)

    def test_an_unwritable_name_is_logged(self, app_client, caplog):
        import app as app_module
        from routes.setup import _refresh_receiver_name
        config_mgr = MagicMock()
        config_mgr.load_user_config.return_value = {'location': {'rx': {'name': 'old', 'latitude': 1}}}
        config_mgr.save_user_config.side_effect = OSError('read-only file system')
        app_client.post("/node-name", data={"name": "Sample Rooftop"})
        with caplog.at_level('ERROR', logger=app_module.app.logger.name):
            assert _refresh_receiver_name(config_mgr) is False
        assert any('receiver name' in r.message and 'read-only' in r.message for r in caplog.records)

    def test_a_busy_lock_on_the_radar_return_is_logged(self, app_client, temp_dir, monkeypatch, caplog):
        """Older than the naming step, and the other half of the same silence:
        the restart after the name is written is skipped just as quietly."""
        import app as app_module
        import restart_lock as rl
        from restart_lock import restart_lock
        from routes.sdr import enforce_radar_mode
        monkeypatch.setattr(app_module, 'DATA_DIR', temp_dir)
        monkeypatch.setattr(rl, 'DEFAULT_TIMEOUT_SECONDS', 0.1)
        with restart_lock(temp_dir), patch('subprocess.run') as run:
            with caplog.at_level('WARNING', logger=app_module.app.logger.name):
                enforce_radar_mode(temp_dir)
        run.assert_not_called()
        assert any('Return to radar skipped' in r.message for r in caplog.records)


# ── Choosing what to cache ─────────────────────────────────────


def tower(callsign, rank, freq=500.0 + 0.0):
    return {"callsign": callsign, "name": callsign, "rank": rank,
            "frequency_mhz": freq, "latitude": 40.0 + rank / 100,
            "longitude": -75.0, "altitude_m": 100, "distance_km": 10.0 * rank}


class TestChoosingWhatToCache:
    def test_the_table_has_a_keep_column_and_says_what_it_means(self, app_client):
        towers = panel(page(app_client), 'towers')
        assert '<th title="Save this tower on the node as a preset">Keep</th>' in towers
        assert 'Cached Towers on the Configuration page' in towers
        js = setup_js()
        assert "postJSON('/towers/cache/set'" in js

    def test_setting_the_cache_replaces_it_with_the_ticked_towers(self, app_client):
        import app as app_module
        app_module.device_state.save_towers_cache(40.0, -75.0, [tower("KOLD-TV", 1)])

        kept = [tower("KTST-TV", 2), tower("KNEW-TV", 5)]
        resp = app_client.post('/towers/cache/set',
                               data=json.dumps({"lat": 40.0, "lon": -75.0, "towers": kept}),
                               content_type='application/json')
        assert resp.status_code == 200
        cache = app_module.device_state.get_towers_cache()
        assert [t["callsign"] for t in cache["towers"]] == ["KTST-TV", "KNEW-TV"]
        assert cache["lat"] == 40.0

    def test_the_towers_are_screened_as_the_search_screens_them(self, app_client):
        import app as app_module
        bad = dict(tower("KBAD-TV", 3), latitude="north")
        resp = app_client.post('/towers/cache/set',
                               data=json.dumps({"lat": 40.0, "lon": -75.0,
                                                "towers": [tower("KTST-TV", 1), bad]}),
                               content_type='application/json')
        assert resp.status_code == 200
        assert [t["callsign"] for t in app_module.device_state.get_towers_cache()["towers"]] == ["KTST-TV"]

    def test_there_is_a_ceiling_by_hand(self, app_client):
        from routes.towers import MAX_CACHED_TOWERS_BY_HAND
        too_many = [tower(f"K{i:03d}", i) for i in range(MAX_CACHED_TOWERS_BY_HAND + 1)]
        resp = app_client.post('/towers/cache/set',
                               data=json.dumps({"lat": 40.0, "lon": -75.0, "towers": too_many}),
                               content_type='application/json')
        assert resp.status_code == 400

    def test_a_bad_body_is_refused(self, app_client):
        for body in ({}, {"lat": 40.0}, {"lat": 40.0, "lon": -75.0, "towers": "KTST-TV"}):
            resp = app_client.post('/towers/cache/set', data=json.dumps(body),
                                   content_type='application/json')
            assert resp.status_code == 400, body

    def test_towers_added_on_the_configuration_page_do_not_count_toward_the_ceiling(self, app_client):
        """That page's add has no cap, so an owner can hold more hand-added
        towers than the wizard's ceiling. Counting them would refuse every
        save from the wizard."""
        import app as app_module
        from routes.towers import MAX_CACHED_TOWERS_BY_HAND
        picked = [tower(f"K{i:03d}", i + 1) for i in range(MAX_CACHED_TOWERS_BY_HAND)]
        manual = [dict(tower(f"KHND{i}", 1), source="manual") for i in range(3)]
        for t in manual:
            t.pop("rank", None)
        resp = app_client.post('/towers/cache/set',
                               data=json.dumps({"lat": 40.0, "lon": -75.0, "towers": picked + manual}),
                               content_type='application/json')
        assert resp.status_code == 200
        saved = app_module.device_state.get_towers_cache()["towers"]
        assert len(saved) == MAX_CACHED_TOWERS_BY_HAND + 3
        assert [t["callsign"] for t in saved[-3:]] == ["KHND0", "KHND1", "KHND2"]

    @patch('routes.towers.http_requests.get')
    def test_the_wizard_search_does_not_replace_the_kept_list(self, mock_get, app_client):
        """Every search used to cache the finder's best five, which threw away
        the owner's picks on "Show every tower", going back, and a reload.
        Found on a test node. The wizard now saves its list itself."""
        import app as app_module
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"towers": [tower("KNEW-TV", 1)]}
        mock_get.return_value = mock_resp
        app_module.device_state.save_towers_cache(40.0, -75.0, [tower("KPICK-TV", 4)])

        app_client.post('/towers/search', data=json.dumps({"lat": 40.0, "lon": -75.0, "cache": False}),
                        content_type='application/json')
        assert [t["callsign"] for t in app_module.device_state.get_towers_cache()["towers"]] == ["KPICK-TV"]

        # A caller that does not say so still caches, as before.
        app_client.post('/towers/search', data=json.dumps({"lat": 40.0, "lon": -75.0}),
                        content_type='application/json')
        assert [t["callsign"] for t in app_module.device_state.get_towers_cache()["towers"]] == ["KNEW-TV"]

    def test_the_page_owns_the_list_across_searches_and_reloads(self, app_client):
        js = setup_js()
        start = js.index('function search(useSweep)')
        assert 'cache: false' in js[start:js.index('search(true);', start)]
        # Matched by callsign and frequency: the two lists hold different copies.
        assert 'function towerKey(t)' in js
        assert "(keptTowers || []).some(function(k) { return towerKey(k) === key; })" in js
        # A reload, or a re-run at the same point, starts from the saved list.
        assert 'window._cachedTowers' in js
        assert 'saved.lat === params.lat && saved.lon === params.lon' in js
        # Hand-added towers carry over anywhere.
        assert 'manualCarry = savedTowers.filter(isManual)' in js
        page_html = page(app_client)
        assert 'window._cachedTowers = ' in page_html
        assert 'window._maxKeptTowers = ' in page_html

    def test_a_configuration_tab_left_open_cannot_edit_the_list_mid_wizard(self, app_client):
        """The Configuration page redirects to the wizard, but a tab opened
        before the wizard started could still post. The wizard replaces the
        list whole, so that add would be undone, and a remove by position
        could hit the wrong tower after the wizard re-sorts."""
        import app as app_module
        app_module.device_state.save_towers_cache(40.0, -75.0, [tower("KTST-TV", 1)])
        app_module.device_state.save_setup_wizard_step('towers')

        add = {"callsign": "KHND-TV", "frequency_mhz": 600.0, "latitude": 40.1, "longitude": -75.1}
        resp = app_client.post('/towers/cache/add', data=json.dumps(add), content_type='application/json')
        assert resp.status_code == 409
        assert 'setup wizard' in resp.get_json()['error']
        resp = app_client.post('/towers/cache/remove', data=json.dumps({"index": 0}),
                               content_type='application/json')
        assert resp.status_code == 409
        assert [t["callsign"] for t in app_module.device_state.get_towers_cache()["towers"]] == ["KTST-TV"]

        # The wizard's own save still works.
        resp = app_client.post('/towers/cache/set',
                               data=json.dumps({"lat": 40.0, "lon": -75.0, "towers": [tower("KNEW-TV", 2)]}),
                               content_type='application/json')
        assert resp.status_code == 200

        # Once the wizard is over, the Configuration page edits again.
        app_client.post('/set-up/save-step', data=json.dumps({"step": "complete"}),
                        content_type='application/json')
        resp = app_client.post('/towers/cache/add', data=json.dumps(add), content_type='application/json')
        assert resp.status_code == 200

    def test_a_failed_save_puts_the_ticks_back(self):
        """The page said "Could not save the list" but left the box ticked, so
        it showed a list the node did not hold. Found on a test node."""
        js = setup_js()
        start = js.index('function syncCache(now)')
        body = js[start:js.index('}, now ? 0 : 600);', start)]
        assert 'savedKept = sending' in body
        assert 'keptTowers = savedKept.slice()' in body
        assert 'refreshKeepBoxes()' in body

    def test_the_list_is_saved_best_first_with_hand_added_towers_last(self):
        """Auto-Calibrate tries the first cached towers as its alternates."""
        js = setup_js()
        start = js.index('function bestFirst(list)')
        body = js[start:js.index('function keptSummary', start)]
        assert 'Number(a.rank)' in body
        assert 'picked.concat(list.filter(isManual))' in body

    def test_the_page_offers_the_location_only_search(self, app_client):
        """The finder drops every tower the sweep did not measure, so a strong
        tower the scan missed is simply absent. The way to see it is a search
        by location alone."""
        towers = panel(page(app_client), 'towers')
        assert 'id="towerScope"' in towers
        js = setup_js()
        assert 'Show every tower near you' in js
        assert 'search(false)' in js


# ── Coordinates ────────────────────────────────────────────────


class TestCoordinates:
    def test_the_boxes_keep_what_was_pasted(self, app_client):
        """A pair from a map carries a dozen decimals. Nothing rounds it: the
        boxes are text, and the finder's fix is written back as returned."""
        location = panel(page(app_client), 'location')
        assert 'id="rxLat" inputmode="decimal"' in location
        assert 'id="rxLon" inputmode="decimal"' in location
        js = setup_js()
        assert '.toFixed(6)' not in js
        assert 'rxLat.value = String(d.latitude)' in js

    def test_a_pasted_pair_is_split_across_the_boxes(self):
        js = setup_js()
        assert 'function splitPair(box)' in js
        start = js.index('function splitPair(box)')
        body = js[start:js.index('function onCoordInput', start)]
        assert 'rxLat.value = m[1]' in body
        assert 'rxLon.value = m[2]' in body

    def test_the_map_lets_the_owner_place_the_pin(self, app_client):
        location = panel(page(app_client), 'location')
        assert 'id="rxMap"' in location
        assert 'drag the pin' in location
        js = setup_js()
        assert "rxPin.on('dragend'" in js
        assert "rxMap.on('click'" in js

    def test_a_zip_the_match_does_not_carry_is_pointed_out(self):
        js = setup_js()
        assert 'typedZip !== matchedZip' in js
