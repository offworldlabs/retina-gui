"""stack_reconcile: clearing containers left half-created by an interrupted
`docker compose up --force-recreate`.

Compose renames the old container to <id-prefix>_<name> before creating the
replacement. Killed in between, it leaves that container squatting the name
compose next needs — and nothing clears it, so every later apply fails the
same way, across reboots.
"""
from unittest.mock import MagicMock, patch

from stack_reconcile import (
    PROJECT, find_stale_containers, reconcile,
)

# Real shape of the failure, from a node that hit it.
CONFLICT_OUTPUT = (
    'Error response from daemon: Error when allocating new name: Conflict. '
    'The container name "/tar1090" is already in use by container '
    '"56f30a56ce9ba3fa3df56ed9dea871dcdd6490bc40e92fcb87ab198c18d70310".'
)


def ok(stdout=''):
    return MagicMock(returncode=0, stdout=stdout, stderr='')


class TestFindStaleContainers:

    def test_picks_out_renamed_containers_only(self):
        listing = ok('blah2\n56f30a56ce9b_tar1090\ntar1090\nb37c58b1ec89_retina-tracker\n')
        with patch('subprocess.run', return_value=listing):
            assert find_stale_containers() == [
                '56f30a56ce9b_tar1090', 'b37c58b1ec89_retina-tracker']

    def test_healthy_project_has_none(self):
        with patch('subprocess.run', return_value=ok('blah2\ntar1090\nadsb2dd\n')):
            assert find_stale_containers() == []

    def test_scopes_the_query_to_the_compose_project(self):
        """A bare name regex over `docker ps -a` would also match containers
        from other projects on this host — and reconcile removes what it
        finds."""
        with patch('subprocess.run', return_value=ok('')) as mock_run:
            find_stale_containers()
        args = mock_run.call_args[0][0]
        assert '--filter' in args
        assert f'label=com.docker.compose.project={PROJECT}' in args

    def test_underscore_names_that_are_not_compose_renames_are_left_alone(self):
        """Only a 12-hex-char prefix is compose's rename form."""
        with patch('subprocess.run', return_value=ok('my_service\nnot_hex_prefix_thing\n')):
            assert find_stale_containers() == []

    def test_docker_failure_reports_nothing_rather_than_raising(self):
        with patch('subprocess.run', return_value=MagicMock(returncode=1, stdout='', stderr='boom')):
            assert find_stale_containers() == []

    def test_docker_missing_reports_nothing_rather_than_raising(self):
        with patch('subprocess.run', side_effect=FileNotFoundError()):
            assert find_stale_containers() == []


class TestReconcile:

    def test_no_stale_containers_is_a_no_op(self):
        with patch('subprocess.run', return_value=ok('blah2\ntar1090\n')) as mock_run:
            removed, error = reconcile('/manifests')
        assert removed == []
        assert error is None
        assert mock_run.call_count == 1, "should not have run rm or up"

    def test_removes_stale_then_restores_the_stack(self):
        calls = []

        def record(args, **kwargs):
            calls.append(args)
            if args[:3] == ['docker', 'ps', '-a']:
                return ok('56f30a56ce9b_tar1090\nblah2\n')
            return ok()

        with patch('subprocess.run', side_effect=record):
            removed, error = reconcile('/manifests')

        assert removed == ['56f30a56ce9b_tar1090']
        assert error is None
        assert calls[1][:3] == ['docker', 'rm', '-f']
        assert '56f30a56ce9b_tar1090' in calls[1]
        assert 'up' in calls[2] and '-d' in calls[2]
        assert '--force-recreate' not in calls[2], \
            "a repair should restore what is missing, not bounce healthy containers"

    def test_bring_up_false_removes_without_starting_anything(self):
        """Spectrum/sdrconnect mode deliberately keeps blah2 stopped."""
        calls = []

        def record(args, **kwargs):
            calls.append(args)
            if args[:3] == ['docker', 'ps', '-a']:
                return ok('56f30a56ce9b_tar1090\n')
            return ok()

        with patch('subprocess.run', side_effect=record):
            removed, error = reconcile('/manifests', bring_up=False)

        assert removed == ['56f30a56ce9b_tar1090']
        assert error is None
        assert not any('up' in c for c in calls)

    def test_failed_removal_is_reported(self):
        def record(args, **kwargs):
            if args[:3] == ['docker', 'ps', '-a']:
                return ok('56f30a56ce9b_tar1090\n')
            return MagicMock(returncode=1, stdout='', stderr='device or resource busy')

        with patch('subprocess.run', side_effect=record):
            removed, error = reconcile('/manifests')

        assert removed == []
        assert 'could not remove' in error
        assert '56f30a56ce9b_tar1090' in error

    def test_failed_restore_still_reports_what_was_removed(self):
        def record(args, **kwargs):
            if args[:3] == ['docker', 'ps', '-a']:
                return ok('56f30a56ce9b_tar1090\n')
            if args[:3] == ['docker', 'rm', '-f']:
                return ok()
            return MagicMock(returncode=1, stdout='', stderr='no such image')

        with patch('subprocess.run', side_effect=record):
            removed, error = reconcile('/manifests')

        assert removed == ['56f30a56ce9b_tar1090']
        assert 'repair restart failed' in error

    def test_never_raises_on_unexpected_failure(self):
        def record(args, **kwargs):
            if args[:3] == ['docker', 'ps', '-a']:
                return ok('56f30a56ce9b_tar1090\n')
            raise OSError('docker daemon gone')

        with patch('subprocess.run', side_effect=record):
            removed, error = reconcile('/manifests')

        assert removed == []
        assert 'could not remove' in error
