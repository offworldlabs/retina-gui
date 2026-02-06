"""Tests for mender.py - version parsing and artifact selection."""
import pytest
from mender import parse_version, find_latest_stable


class TestParseVersion:
    """Version parsing - only stable releases match."""

    def test_stable_version(self):
        assert parse_version("retina-node-v0.3.5") == (0, 3, 5)

    def test_stable_version_v1(self):
        assert parse_version("retina-node-v1.0.0") == (1, 0, 0)

    def test_stable_version_large(self):
        assert parse_version("retina-node-v10.20.30") == (10, 20, 30)

    def test_rc_excluded(self):
        assert parse_version("retina-node-v0.3.5-rc1") is None

    def test_rc2_excluded(self):
        assert parse_version("retina-node-v0.3.5-rc2") is None

    def test_dev_excluded(self):
        assert parse_version("retina-node-v0.3.5-dev") is None

    def test_dev_only_excluded(self):
        assert parse_version("retina-node-dev") is None

    def test_beta_excluded(self):
        assert parse_version("retina-node-v0.3.5-beta") is None

    def test_beta1_excluded(self):
        assert parse_version("retina-node-v0.3.5-beta1") is None

    def test_other_artifact_excluded(self):
        assert parse_version("other-artifact-v1.0.0") is None

    def test_missing_v_excluded(self):
        assert parse_version("retina-node-1.0.0") is None

    def test_empty_excluded(self):
        assert parse_version("") is None


class TestFindLatestStable:
    """Finding latest stable from artifact list."""

    def test_finds_latest(self):
        artifacts = [
            {"artifact_name": "retina-node-v0.3.2", "id": "a"},
            {"artifact_name": "retina-node-v0.3.5", "id": "b"},
            {"artifact_name": "retina-node-v0.3.3", "id": "c"},
        ]
        result = find_latest_stable(artifacts)
        assert result["id"] == "b"
        assert result["artifact_name"] == "retina-node-v0.3.5"

    def test_excludes_rc(self):
        artifacts = [
            {"artifact_name": "retina-node-v0.3.2", "id": "a"},
            {"artifact_name": "retina-node-v0.3.6-rc1", "id": "b"},
        ]
        result = find_latest_stable(artifacts)
        assert result["id"] == "a"

    def test_excludes_dev(self):
        artifacts = [
            {"artifact_name": "retina-node-v0.3.2", "id": "a"},
            {"artifact_name": "retina-node-v0.4.0-dev", "id": "b"},
        ]
        result = find_latest_stable(artifacts)
        assert result["id"] == "a"

    def test_empty_list(self):
        assert find_latest_stable([]) is None

    def test_no_stable(self):
        artifacts = [
            {"artifact_name": "retina-node-v0.3.6-rc1", "id": "a"},
            {"artifact_name": "retina-node-dev", "id": "b"},
        ]
        assert find_latest_stable(artifacts) is None

    def test_major_version_wins(self):
        artifacts = [
            {"artifact_name": "retina-node-v0.9.9", "id": "a"},
            {"artifact_name": "retina-node-v1.0.0", "id": "b"},
        ]
        result = find_latest_stable(artifacts)
        assert result["id"] == "b"

    def test_minor_version_wins(self):
        artifacts = [
            {"artifact_name": "retina-node-v1.0.9", "id": "a"},
            {"artifact_name": "retina-node-v1.1.0", "id": "b"},
        ]
        result = find_latest_stable(artifacts)
        assert result["id"] == "b"

    def test_patch_version_wins(self):
        artifacts = [
            {"artifact_name": "retina-node-v1.1.0", "id": "a"},
            {"artifact_name": "retina-node-v1.1.1", "id": "b"},
        ]
        result = find_latest_stable(artifacts)
        assert result["id"] == "b"
