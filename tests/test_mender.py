"""Tests for mender.py - version parsing and artifact selection."""
from unittest.mock import patch, Mock
import pytest
from mender import (
    parse_version, get_latest_stable_from_github,
    parse_os_version, get_latest_owl_os_from_github,
)


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


class TestGetLatestStableFromGitHub:
    """GitHub releases version discovery."""

    @patch("mender.requests.get")
    def test_finds_latest_stable(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"tag_name": "v0.3.5"},
            {"tag_name": "v0.3.2"},
            {"tag_name": "v0.3.6-rc1"},
        ]
        mock_get.return_value = mock_response

        version, error = get_latest_stable_from_github()
        assert version == "v0.3.5"
        assert error is None

    @patch("mender.requests.get")
    def test_excludes_rc_and_dev(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"tag_name": "v0.4.0-rc1"},
            {"tag_name": "v0.4.0-dev"},
            {"tag_name": "v0.3.5"},
        ]
        mock_get.return_value = mock_response

        version, error = get_latest_stable_from_github()
        assert version == "v0.3.5"
        assert error is None

    @patch("mender.requests.get")
    def test_no_stable_releases(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"tag_name": "v0.4.0-rc1"},
            {"tag_name": "dev"},
        ]
        mock_get.return_value = mock_response

        version, error = get_latest_stable_from_github()
        assert version is None
        assert error == "No stable releases found"

    @patch("mender.requests.get")
    def test_api_error(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 403
        mock_get.return_value = mock_response

        version, error = get_latest_stable_from_github()
        assert version is None
        assert "GitHub API error: 403" in error

    @patch("mender.requests.get")
    def test_semver_sorting(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"tag_name": "v0.9.9"},
            {"tag_name": "v1.0.0"},
            {"tag_name": "v0.10.0"},
        ]
        mock_get.return_value = mock_response

        version, error = get_latest_stable_from_github()
        assert version == "v1.0.0"
        assert error is None


class TestParseOsVersion:
    """owl-os version parsing — accepts os-v*, v*, and bare versions."""

    def test_full_tag(self):
        assert parse_os_version("os-v0.1.0") == (0, 1, 0)

    def test_v_prefix(self):
        assert parse_os_version("v0.2.0") == (0, 2, 0)

    def test_bare_version(self):
        assert parse_os_version("0.3.0") == (0, 3, 0)

    def test_large_numbers(self):
        assert parse_os_version("os-v10.20.30") == (10, 20, 30)

    def test_rc_excluded(self):
        assert parse_os_version("os-v0.1.0-rc1") is None

    def test_dev_excluded(self):
        assert parse_os_version("os-v0.1.0-dev") is None

    def test_empty(self):
        assert parse_os_version("") is None

    def test_non_os_tag(self):
        assert parse_os_version("retina-node-v0.3.5") is None


class TestGetLatestOwlOsFromGitHub:
    """owl-os GitHub releases version discovery."""

    @patch("mender.requests.get")
    def test_finds_latest_stable(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"tag_name": "os-v0.2.0"},
            {"tag_name": "os-v0.1.0"},
            {"tag_name": "os-v0.3.0-rc1"},
        ]
        mock_get.return_value = mock_response

        version, error = get_latest_owl_os_from_github()
        assert version == "os-v0.2.0"
        assert error is None

    @patch("mender.requests.get")
    def test_excludes_non_os_tags(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"tag_name": "v0.9.0"},
            {"tag_name": "os-v0.1.0"},
        ]
        mock_get.return_value = mock_response

        version, error = get_latest_owl_os_from_github()
        assert version == "os-v0.1.0"
        assert error is None

    @patch("mender.requests.get")
    def test_no_stable_releases(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"tag_name": "os-v0.1.0-rc1"},
            {"tag_name": "v1.0.0"},
        ]
        mock_get.return_value = mock_response

        version, error = get_latest_owl_os_from_github()
        assert version is None
        assert "No stable owl-os releases found" in error

    @patch("mender.requests.get")
    def test_api_error(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 403
        mock_get.return_value = mock_response

        version, error = get_latest_owl_os_from_github()
        assert version is None
        assert "GitHub API error: 403" in error

    @patch("mender.requests.get")
    def test_semver_sorting(self, mock_get):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"tag_name": "os-v0.9.0"},
            {"tag_name": "os-v1.0.0"},
            {"tag_name": "os-v0.10.0"},
        ]
        mock_get.return_value = mock_response

        version, error = get_latest_owl_os_from_github()
        assert version == "os-v1.0.0"
        assert error is None
