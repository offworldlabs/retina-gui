"""Tests for the mode (Radar / Spectrum) toggle endpoint and home page rendering."""
import json
import os
import subprocess
import threading
import time
import pytest
from unittest.mock import patch, MagicMock, call

from restart_lock import is_locked, restart_lock


@pytest.fixture(autouse=True)
def reset_mode_cache():
    """Reset the in-memory mode cache before every test.

    _mode_cache is a module-level variable in routes.mode that survives
    importlib.reload(app) because Python's module cache doesn't re-execute
    already-imported submodules. Without this reset, a test that switches to
    'spectrum' would pollute the next test's default mode.
    """
    import sys
    if 'routes.mode' in sys.modules:
        sys.modules['routes.mode']._mode_cache = 'radar'
    yield


class TestGetMode:

    def test_default_mode_is_radar(self, app_client):
        response = app_client.get('/api/mode')
        assert response.status_code == 200
        assert json.loads(response.data) == {'mode': 'radar'}

    def test_returns_persisted_mode(self, app_client, temp_dir):
        with open(os.path.join(temp_dir, 'mode.txt'), 'w') as f:
            f.write('spectrum')
        response = app_client.get('/api/mode')
        assert json.loads(response.data) == {'mode': 'spectrum'}

    def test_corrupted_mode_file_falls_back_to_radar(self, app_client, temp_dir):
        with open(os.path.join(temp_dir, 'mode.txt'), 'w') as f:
            f.write('garbage')
        response = app_client.get('/api/mode')
        assert json.loads(response.data) == {'mode': 'radar'}


class TestSetMode:

    def test_invalid_mode_returns_400(self, app_client):
        response = app_client.post('/api/mode',
                                   data=json.dumps({'mode': 'invalid'}),
                                   content_type='application/json')
        assert response.status_code == 400
        assert json.loads(response.data)['success'] is False

    def test_missing_mode_returns_400(self, app_client):
        response = app_client.post('/api/mode',
                                   data=json.dumps({}),
                                   content_type='application/json')
        assert response.status_code == 400

    def test_refuses_while_calibration_is_running(self, app_client):
        import app as app_module
        with patch.object(app_module.calibrator, 'is_running', return_value=True):
            response = app_client.post('/api/mode',
                                       data=json.dumps({'mode': 'spectrum'}),
                                       content_type='application/json')
        assert response.status_code == 409
        assert 'calibrat' in json.loads(response.data)['error'].lower()

    def test_refuses_switch_to_radar_while_calibration_is_running(self, app_client):
        # even 'radar' force-recreates the containers, which would yank the
        # SDR out from under an active run just as badly as switching away
        import app as app_module
        with patch.object(app_module.calibrator, 'is_running', return_value=True):
            response = app_client.post('/api/mode',
                                       data=json.dumps({'mode': 'radar'}),
                                       content_type='application/json')
        assert response.status_code == 409

    def test_refuses_via_stale_lock_file_even_if_in_memory_says_not_running(self, app_client):
        # belt-and-suspenders: the file lock (cross-process/crash-recovery
        # signal) still blocks even if this process's calibrator object
        # itself has never run anything (e.g. right after a restart)
        import app as app_module
        assert app_module.device_state.acquire_calibration_lock()
        try:
            response = app_client.post('/api/mode',
                                       data=json.dumps({'mode': 'spectrum'}),
                                       content_type='application/json')
            assert response.status_code == 409
        finally:
            app_module.device_state.release_calibration_lock()

    def test_no_retina_node_still_succeeds(self, app_client_no_retina):
        """Mode switch should succeed (skipping docker) when retina-node is absent."""
        response = app_client_no_retina.post('/api/mode',
                                             data=json.dumps({'mode': 'spectrum'}),
                                             content_type='application/json')
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data['success'] is True
        assert data['mode'] == 'spectrum'

    def test_no_retina_node_persists_mode_in_cache(self, app_client_no_retina):
        """After a no-docker switch the GET endpoint reflects the new mode."""
        app_client_no_retina.post('/api/mode',
                                  data=json.dumps({'mode': 'spectrum'}),
                                  content_type='application/json')
        response = app_client_no_retina.get('/api/mode')
        assert json.loads(response.data)['mode'] == 'spectrum'

    @patch('subprocess.run')
    def test_switch_to_spectrum_calls_correct_docker_commands(self, mock_run, app_client):
        mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')

        response = app_client.post('/api/mode',
                                   data=json.dumps({'mode': 'spectrum'}),
                                   content_type='application/json')

        assert response.status_code == 200
        assert json.loads(response.data)['success'] is True
        assert mock_run.call_count == 2

        stop_args = mock_run.call_args_list[0][0][0]
        assert stop_args[:4] == ['docker', 'compose', '-p', 'retina-node']
        assert 'stop' in stop_args
        for svc in ('blah2', 'blah2_api', 'blah2_web', 'blah2_host'):
            assert svc in stop_args

        up_args = mock_run.call_args_list[1][0][0]
        assert up_args[:4] == ['docker', 'compose', '-p', 'retina-node']
        assert '--profile' in up_args
        assert 'spectrum' in up_args
        assert 'up' in up_args
        assert '-d' in up_args
        assert 'retina-spectrum' in up_args

    @patch('subprocess.run')
    def test_switch_to_radar_calls_correct_docker_commands(self, mock_run, app_client, temp_dir):
        # Pre-set mode to spectrum
        with open(os.path.join(temp_dir, 'mode.txt'), 'w') as f:
            f.write('spectrum')

        mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')

        response = app_client.post('/api/mode',
                                   data=json.dumps({'mode': 'radar'}),
                                   content_type='application/json')

        assert response.status_code == 200
        assert json.loads(response.data)['success'] is True
        # stop retina-spectrum, rm retina-spectrum, restart sdrplay, up the radar stack
        assert mock_run.call_count == 4

        stop_spectrum_args = mock_run.call_args_list[0][0][0]
        assert stop_spectrum_args[:4] == ['docker', 'compose', '-p', 'retina-node']
        assert 'stop' in stop_spectrum_args
        assert 'retina-spectrum' in stop_spectrum_args

        rm_spectrum_args = mock_run.call_args_list[1][0][0]
        assert 'rm' in rm_spectrum_args
        assert 'retina-spectrum' in rm_spectrum_args

        assert mock_run.call_args_list[2][0][0] == ['systemctl', 'restart', 'sdrplay.service']

        up_args = mock_run.call_args_list[3][0][0]
        assert up_args[:4] == ['docker', 'compose', '-p', 'retina-node']
        assert 'up' in up_args
        assert '--force-recreate' in up_args
        # blah2_web/blah2_host read no config but were stopped on the way into
        # spectrum mode, so they must be named here or they stay stopped.
        # retina-tracker bind-mounts the config dir and reads config.yml at
        # startup (--blah2-config), so it must be recreated to pick up any
        # config change made while spectrum mode was active.
        for svc in ('blah2', 'blah2_api', 'blah2_web', 'blah2_host', 'retina-tracker'):
            assert svc in up_args

    @patch('subprocess.run')
    def test_switch_to_spectrum_writes_mode_file(self, mock_run, app_client, temp_dir):
        mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')

        app_client.post('/api/mode',
                        data=json.dumps({'mode': 'spectrum'}),
                        content_type='application/json')

        with open(os.path.join(temp_dir, 'mode.txt')) as f:
            assert f.read().strip() == 'spectrum'

    @patch('subprocess.run')
    def test_docker_stop_failure_returns_500(self, mock_run, app_client):
        mock_run.return_value = MagicMock(returncode=1, stdout='', stderr='permission denied')

        response = app_client.post('/api/mode',
                                   data=json.dumps({'mode': 'spectrum'}),
                                   content_type='application/json')

        assert response.status_code == 500
        data = json.loads(response.data)
        assert data['success'] is False
        assert 'blah2' in data['error']

    @patch('subprocess.run')
    def test_docker_up_failure_returns_500(self, mock_run, app_client):
        # Stop succeeds, up fails
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout='', stderr=''),
            MagicMock(returncode=1, stdout='', stderr='image not found'),
        ]

        response = app_client.post('/api/mode',
                                   data=json.dumps({'mode': 'spectrum'}),
                                   content_type='application/json')

        assert response.status_code == 500
        data = json.loads(response.data)
        assert data['success'] is False
        assert 'retina-spectrum' in data['error']

    @patch('subprocess.run')
    def test_timeout_returns_500(self, mock_run, app_client):
        mock_run.side_effect = subprocess.TimeoutExpired(cmd='docker', timeout=60)

        response = app_client.post('/api/mode',
                                   data=json.dumps({'mode': 'spectrum'}),
                                   content_type='application/json')

        assert response.status_code == 500
        data = json.loads(response.data)
        assert data['success'] is False
        assert 'timed out' in data['error'].lower()

    @patch('subprocess.run')
    def test_spectrum_mode_file_written_before_docker_even_on_failure(
            self, mock_run, app_client, temp_dir):
        """The spectrum transition writes mode.txt *before* touching Docker,
        deliberately (see set_mode): the cron watchdog reads that file, and
        if it were written only after a successful stop it could see blah2
        down mid-transition and fire a spurious stack restart. A failed
        transition therefore still leaves mode.txt == 'spectrum'."""
        mock_run.return_value = MagicMock(returncode=1, stdout='', stderr='error')

        response = app_client.post('/api/mode',
                                   data=json.dumps({'mode': 'spectrum'}),
                                   content_type='application/json')

        assert response.status_code == 500
        assert json.loads(response.data)['success'] is False

        with open(os.path.join(temp_dir, 'mode.txt')) as f:
            assert f.read().strip() == 'spectrum'


class TestHomepageModeRendering:

    def test_radar_mode_shows_services_section(self, app_client):
        response = app_client.get('/')
        assert response.status_code == 200
        assert b'Services' in response.data
        assert b'spectrumFrame' not in response.data

    @patch('subprocess.run')
    def test_spectrum_mode_shows_iframe(self, mock_run, app_client, temp_dir):
        with open(os.path.join(temp_dir, 'mode.txt'), 'w') as f:
            f.write('spectrum')

        response = app_client.get('/')
        assert response.status_code == 200
        assert b'spectrumFrame' in response.data
        assert b'Services' not in response.data

    def test_spectrum_mode_hides_passive_radar_card(self, app_client, temp_dir):
        with open(os.path.join(temp_dir, 'mode.txt'), 'w') as f:
            f.write('spectrum')

        response = app_client.get('/')
        assert b'Passive Radar' not in response.data
        assert b'49152' not in response.data


CONFLICT_OUTPUT = (
    'Error response from daemon: Error when allocating new name: Conflict. '
    'The container name "/tar1090" is already in use by container "56f30a56ce9b".'
)


class TestRepairAfterInterruptedRecreate:
    """A recreate killed between compose's rename and its remove leaves a
    container squatting the name compose next needs. Nothing else clears it,
    so without this repair every later apply fails identically, forever."""

    def _run_apply(self, temp_dir, run_side_effect):
        import routes.mode as mode_module
        with patch('subprocess.run', side_effect=run_side_effect):
            return mode_module.run_config_merger_and_restart(temp_dir)

    def test_recreate_timeout_triggers_a_repair(self, app_client, temp_dir):
        seen = []

        def side_effect(args, **kwargs):
            seen.append(args)
            if '--force-recreate' in args:
                raise subprocess.TimeoutExpired(cmd='docker', timeout=300)
            if args[:3] == ['docker', 'ps', '-a']:
                return MagicMock(returncode=0, stdout='56f30a56ce9b_tar1090\n', stderr='')
            return MagicMock(returncode=0, stdout='', stderr='')

        error = self._run_apply(temp_dir, side_effect)

        assert error is not None and 'timed out' in error.lower()
        assert 'cleaned up 1' in error.lower(), f"no repair reported: {error}"
        assert any(a[:3] == ['docker', 'rm', '-f'] for a in seen), "never removed the stale container"

    def test_name_conflict_triggers_a_repair(self, app_client, temp_dir):
        seen = []

        def side_effect(args, **kwargs):
            seen.append(args)
            if '--force-recreate' in args:
                return MagicMock(returncode=1, stdout='', stderr=CONFLICT_OUTPUT)
            if args[:3] == ['docker', 'ps', '-a']:
                return MagicMock(returncode=0, stdout='56f30a56ce9b_tar1090\n', stderr='')
            return MagicMock(returncode=0, stdout='', stderr='')

        error = self._run_apply(temp_dir, side_effect)

        assert error is not None
        assert 'cleaned up 1' in error.lower(), f"no repair reported: {error}"
        assert any(a[:3] == ['docker', 'rm', '-f'] for a in seen)

    def test_unrelated_failure_does_not_trigger_a_repair(self, app_client, temp_dir):
        """Repair removes containers — it must not fire on every failure."""
        seen = []

        def side_effect(args, **kwargs):
            seen.append(args)
            if '--force-recreate' in args:
                return MagicMock(returncode=1, stdout='', stderr='no such image: blah2:v9')
            return MagicMock(returncode=0, stdout='', stderr='')

        error = self._run_apply(temp_dir, side_effect)

        assert 'no such image' in error
        assert not any(a[:3] == ['docker', 'rm', '-f'] for a in seen), \
            "removed containers for an unrelated failure"

    def test_repair_with_nothing_stale_says_so(self, app_client, temp_dir):
        def side_effect(args, **kwargs):
            if '--force-recreate' in args:
                raise subprocess.TimeoutExpired(cmd='docker', timeout=300)
            if args[:3] == ['docker', 'ps', '-a']:
                return MagicMock(returncode=0, stdout='blah2\ntar1090\n', stderr='')
            return MagicMock(returncode=0, stdout='', stderr='')

        error = self._run_apply(temp_dir, side_effect)
        assert 'no half-created containers' in error.lower()

    def test_repair_failure_is_surfaced_not_swallowed(self, app_client, temp_dir):
        def side_effect(args, **kwargs):
            if '--force-recreate' in args:
                raise subprocess.TimeoutExpired(cmd='docker', timeout=300)
            if args[:3] == ['docker', 'ps', '-a']:
                return MagicMock(returncode=0, stdout='56f30a56ce9b_tar1090\n', stderr='')
            if args[:3] == ['docker', 'rm', '-f']:
                return MagicMock(returncode=1, stdout='', stderr='resource busy')
            return MagicMock(returncode=0, stdout='', stderr='')

        error = self._run_apply(temp_dir, side_effect)
        assert 'automatic repair also failed' in error.lower()

    def test_conflict_detection_matches_the_real_daemon_message(self):
        import routes.mode as mode_module
        assert mode_module._is_name_conflict(CONFLICT_OUTPUT)
        assert not mode_module._is_name_conflict('no such image: blah2:v9')
        assert not mode_module._is_name_conflict('')
        assert not mode_module._is_name_conflict(None)


class TestRestartLockCoverage:
    """Every path that recreates or removes containers must hold the restart
    lock — a `docker compose down` landing inside another caller's
    `up -d --force-recreate` is what renames containers to <hash>_<name> and
    leaves the daemon rejecting the real name as already in use."""

    def test_enforce_radar_mode_takes_the_lock(self, app_client, temp_dir, monkeypatch):
        import routes.mode as mode_module
        import app as app_module
        monkeypatch.setattr(app_module, 'DATA_DIR', temp_dir)

        held = []
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = lambda *a, **k: (
                held.append(is_locked(temp_dir)), MagicMock(returncode=0))[1]
            mode_module.enforce_radar_mode(temp_dir)

        assert held, "enforce_radar_mode ran no commands"
        assert all(held), "ran docker commands without holding the lock"
        assert is_locked(temp_dir) is False, "lock not released"

    def test_enforce_radar_mode_skips_when_lock_is_busy(self, app_client, temp_dir, monkeypatch):
        """Non-fatal by contract: whoever holds the lock is already
        restarting, so this must not raise or hang."""
        import routes.mode as mode_module
        import app as app_module
        import restart_lock as rl
        monkeypatch.setattr(app_module, 'DATA_DIR', temp_dir)
        monkeypatch.setattr(rl, 'DEFAULT_TIMEOUT_SECONDS', 0.1)

        with restart_lock(temp_dir):
            with patch('subprocess.run') as mock_run:
                started = time.monotonic()
                mode_module.enforce_radar_mode(temp_dir)
                elapsed = time.monotonic() - started
        assert mock_run.call_count == 0
        assert elapsed < 5, f"waited {elapsed:.1f}s — timeout not honoured"

    def test_release_spectrum_takes_the_lock(self, app_client, temp_dir, monkeypatch):
        import app as app_module
        monkeypatch.setattr(app_module, 'DATA_DIR', temp_dir)

        held = []
        with patch('subprocess.run') as mock_run:
            mock_run.side_effect = lambda *a, **k: (
                held.append(is_locked(temp_dir)), MagicMock(returncode=0))[1]
            response = app_client.post('/api/mode/release-spectrum')

        assert response.status_code == 204
        assert held and all(held)
        assert is_locked(temp_dir) is False

    def test_release_spectrum_gives_up_quickly_when_busy(self, app_client, temp_dir, monkeypatch):
        """A sendBeacon nobody waits on must not block the request thread."""
        import app as app_module
        import restart_lock as rl
        monkeypatch.setattr(app_module, 'DATA_DIR', temp_dir)
        monkeypatch.setattr(rl, 'OPPORTUNISTIC_TIMEOUT_SECONDS', 0.1)

        with restart_lock(temp_dir):
            with patch('subprocess.run') as mock_run:
                started = time.monotonic()
                response = app_client.post('/api/mode/release-spectrum')
                elapsed = time.monotonic() - started

        assert response.status_code == 204
        assert mock_run.call_count == 0
        assert elapsed < 5, f"blocked for {elapsed:.1f}s on a fire-and-forget beacon"


class TestNoSelfDeadlock:
    """flock is not re-entrant, so a nested acquire from the same process
    blocks against itself forever. These are the two call chains where that
    is a live risk."""

    def test_enforce_radar_mode_does_not_nest_inside_its_own_lock(
            self, app_client, temp_dir, monkeypatch):
        import routes.mode as mode_module
        import app as app_module
        monkeypatch.setattr(app_module, 'DATA_DIR', temp_dir)

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
            done = threading.Event()

            def run():
                mode_module.enforce_radar_mode(temp_dir)
                done.set()

            t = threading.Thread(target=run, daemon=True)
            t.start()
            assert done.wait(timeout=20), "enforce_radar_mode deadlocked on its own lock"

    def test_shared_restart_fn_does_not_nest(self, app_client, temp_dir, monkeypatch):
        import routes.mode as mode_module
        import app as app_module
        monkeypatch.setattr(app_module, 'DATA_DIR', temp_dir)

        with patch('subprocess.run') as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')
            done = threading.Event()
            result = {}

            def run():
                result['error'] = mode_module.run_config_merger_and_restart(temp_dir)
                done.set()

            t = threading.Thread(target=run, daemon=True)
            t.start()
            assert done.wait(timeout=20), "run_config_merger_and_restart deadlocked"
            assert result['error'] is None


class TestRestartSettleTime:
    """run_config_merger_and_restart() is the shared choke point every
    config-applying/mode-switching route funnels through (/api/mode,
    /towers/select, /calibrate/apply, /config/save+apply, wizard
    completion) — testing it here covers all of them at once."""

    @patch('subprocess.run')
    def test_settles_between_sdrplay_restart_and_container_recreate(
            self, mock_run, app_client, temp_dir, monkeypatch):
        """The settle-time fix's whole point (diagnosed live tonight): the
        sdrplay.service restart and the container recreate must not race.

        The settle is slept in ~1s chunks so callers can show a countdown
        (see _settle), so this asserts the *total* time slept and that every
        chunk of it falls between the restart and the recreate — not a
        single flat sleep call, which is an implementation detail.
        """
        import routes.mode as mode_module
        monkeypatch.setattr(mode_module, 'SDRPLAY_RESTART_SETTLE_SECONDS', 30)

        order = []
        clock = {'t': 0.0}
        monkeypatch.setattr(mode_module.time, 'monotonic', lambda: clock['t'])

        def fake_sleep(seconds):
            clock['t'] += seconds
            order.append(('sleep', seconds))
        monkeypatch.setattr(mode_module.time, 'sleep', fake_sleep)

        def record_run(*a, **k):
            order.append(('run', a[0]))
            return MagicMock(returncode=0, stdout='', stderr='')
        mock_run.side_effect = record_run

        error = mode_module.run_config_merger_and_restart(temp_dir)
        assert error is None

        sleep_idx = [i for i, (kind, _) in enumerate(order) if kind == 'sleep']
        assert sleep_idx, "never settled"
        assert sum(order[i][1] for i in sleep_idx) == 30

        restart_idx = next(i for i, (kind, arg) in enumerate(order)
                           if kind == 'run' and 'systemctl' in arg)
        recreate_idx = next(i for i, (kind, arg) in enumerate(order)
                            if kind == 'run' and '--force-recreate' in arg)
        assert restart_idx < min(sleep_idx)
        assert max(sleep_idx) < recreate_idx

    @patch('subprocess.run')
    def test_reports_each_phase_in_order(self, mock_run, app_client, temp_dir):
        """The UI's progress display depends on these phases arriving in
        order — a silent apply is what got clicked twice in the first place."""
        import routes.mode as mode_module
        mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')

        phases = []
        error = mode_module.run_config_merger_and_restart(
            temp_dir, on_phase=lambda phase, detail=None: phases.append(phase))

        assert error is None
        assert phases[0] == 'waiting_for_lock'
        assert [p for p in phases if p != 'settling'] == [
            'waiting_for_lock', 'merging', 'stopping_spectrum',
            'restarting_sdr', 'recreating']
