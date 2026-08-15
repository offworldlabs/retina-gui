"""Tests for the retina-telemetry status document reader.

The document is written by another container and is the only channel out of a
service that binds no ports, so the cases that matter here are the ones where
it is absent, stale, or half-understood — none of which may raise.
"""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from telemetry_status import TelemetryStatus


def written_at(age=timedelta(0)):
    """A timestamp in the format retina-telemetry actually writes."""
    return (datetime.now(timezone.utc) - age).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def reader(temp_dir):
    return TelemetryStatus(
        status_path=os.path.join(temp_dir, "status.json"),
        node_ref_cache_path=os.path.join(temp_dir, "telemetry-node-ref"),
    )


def write_status(reader, **fields):
    document = {"schema": 1, "written_at": written_at(), **fields}
    with open(reader.status_path, "w") as f:
        json.dump(document, f)


class TestMissingOrUnreadable:
    """Absent means the telemetry package isn't installed — not a fault."""

    def test_absent_file_reads_as_none(self, reader):
        assert reader.read() is None

    def test_malformed_json_reads_as_none(self, reader):
        with open(reader.status_path, "w") as f:
            f.write("{not json")
        assert reader.read() is None

    def test_non_object_document_reads_as_none(self, reader):
        with open(reader.status_path, "w") as f:
            json.dump(["not", "an", "object"], f)
        assert reader.read() is None


class TestDetailSuppression:
    """Prose is hidden for normal transients and shown for everything else.

    The list is deliberately of transients rather than of faults, so a state
    added to retina-telemetry later surfaces instead of being silently hidden.
    """

    @pytest.mark.parametrize("state", ["registering", "awaiting_config", "starting"])
    def test_transient_states_suppress_detail(self, reader, state):
        write_status(reader, state=state, detail="this is normal and will pass")
        assert reader.read()["detail"] is None

    @pytest.mark.parametrize("state", ["no_identity", "no_agreement", "revoked", "no_detections"])
    def test_actionable_states_show_detail(self, reader, state):
        write_status(reader, state=state, detail="something needs doing")
        assert reader.read()["detail"] == "Something needs doing"

    def test_unrecognised_state_shows_detail(self, reader):
        """A state we've never heard of must surface, not hide.

        This is the whole reason the list is of transients: a fault added
        upstream reaches the operator without retina-gui being changed first.
        """
        write_status(reader, state="some_future_fault", detail="a new way to fail")
        assert reader.read()["detail"] == "A new way to fail"

    def test_state_is_passed_through_unmapped(self, reader):
        write_status(reader, state="streaming", detail=None)
        assert reader.read()["state"] == "streaming"

    def test_detail_casing_is_otherwise_untouched(self, reader):
        """Only the first character moves.

        These strings are shown verbatim, and they carry casing that matters —
        "Mender", "MAC-based", "blah2-api". Jinja's `capitalize` would lowercase
        all of it, which is why this happens in Python instead.
        """
        write_status(
            reader, state="no_identity",
            detail="/data/mender/node_id is missing. It has not enrolled with "
                   "Mender, or has fallen back to a MAC-based one.",
        )
        assert reader.read()["detail"] == (
            "/data/mender/node_id is missing. It has not enrolled with "
            "Mender, or has fallen back to a MAC-based one."
        )

    def test_lowercase_detail_gets_a_capital(self, reader):
        write_status(reader, state="revoked", detail="the server rejected this node's token.")
        assert reader.read()["detail"].startswith("The server rejected")


class TestStaleness:
    """A stale document means the container died; no state value can say so."""

    def test_fresh_document_is_not_stale(self, reader):
        write_status(reader, state="streaming")
        assert reader.read()["stale"] is False

    def test_old_document_is_stale(self, reader):
        with open(reader.status_path, "w") as f:
            json.dump({"state": "streaming", "written_at": written_at(timedelta(minutes=10))}, f)
        assert reader.read()["stale"] is True

    def test_missing_written_at_is_stale(self, reader):
        """retina-telemetry writes it on every write, so its absence means
        this is not a document we understand."""
        with open(reader.status_path, "w") as f:
            json.dump({"state": "streaming"}, f)
        assert reader.read()["stale"] is True

    def test_unparseable_written_at_is_stale(self, reader):
        with open(reader.status_path, "w") as f:
            json.dump({"state": "streaming", "written_at": "not a timestamp"}, f)
        assert reader.read()["stale"] is True

    def test_naive_written_at_is_treated_as_utc(self, reader):
        """Defensive: the writer always emits Z, but a naive timestamp read as
        local time could look hours stale on a node in the wrong timezone."""
        naive = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
        with open(reader.status_path, "w") as f:
            json.dump({"state": "streaming", "written_at": naive}, f)
        assert reader.read()["stale"] is False


class TestNodeRefCache:
    """node_ref is null for up to a heartbeat interval after a restart.

    The cache exists only to cover that window — telemetry itself is the source
    of truth and already handles rotation.
    """

    def test_live_value_is_returned_and_remembered(self, reader):
        write_status(reader, state="streaming", node_ref="nde4f2k9xq7m3b8")
        assert reader.read()["node_ref"] == "nde4f2k9xq7m3b8"
        with open(reader.node_ref_cache_path) as f:
            assert f.read().strip() == "nde4f2k9xq7m3b8"

    def test_null_falls_back_to_last_known_good(self, reader):
        write_status(reader, state="streaming", node_ref="nde4f2k9xq7m3b8")
        reader.read()

        # What the document looks like for ~60s after telemetry restarts.
        write_status(reader, state="awaiting_config", node_ref=None)
        assert reader.read()["node_ref"] == "nde4f2k9xq7m3b8"

    def test_null_with_no_cache_returns_none(self, reader):
        write_status(reader, state="unregistered", node_ref=None)
        assert reader.read()["node_ref"] is None

    def test_rotation_replaces_the_cached_value(self, reader):
        write_status(reader, state="streaming", node_ref="nde4f2k9xq7m3b8")
        reader.read()

        write_status(reader, state="streaming", node_ref="ndeaaaaaaaaaaaa")
        assert reader.read()["node_ref"] == "ndeaaaaaaaaaaaa"

        write_status(reader, state="streaming", node_ref=None)
        assert reader.read()["node_ref"] == "ndeaaaaaaaaaaaa"

    def test_unwritable_cache_does_not_break_the_read(self, reader):
        """The cache is a display convenience; losing it must not cost us the
        value we were just handed."""
        reader.node_ref_cache_path = "/proc/nonexistent/telemetry-node-ref"
        write_status(reader, state="streaming", node_ref="nde4f2k9xq7m3b8")
        assert reader.read()["node_ref"] == "nde4f2k9xq7m3b8"


class TestHomePageCard:
    """The card is rendered server-side on page load."""

    def _point_at(self, app_module, temp_dir):
        reader = TelemetryStatus(
            status_path=os.path.join(temp_dir, "status.json"),
            node_ref_cache_path=os.path.join(temp_dir, "telemetry-node-ref"),
        )
        app_module.telemetry_status = reader
        return reader

    def test_no_card_when_telemetry_is_not_installed(self, app_client, temp_dir):
        import app as app_module
        self._point_at(app_module, temp_dir)

        assert b"Node identifier" not in app_client.get("/").data

    def test_node_ref_is_shown(self, app_client, temp_dir):
        import app as app_module
        reader = self._point_at(app_module, temp_dir)
        write_status(reader, state="streaming", node_ref="nde4f2k9xq7m3b8", detail=None)

        body = app_client.get("/").data
        assert b"nde4f2k9xq7m3b8" in body

    def test_actionable_detail_reaches_the_page(self, app_client, temp_dir):
        import app as app_module
        reader = self._point_at(app_module, temp_dir)
        write_status(reader, state="no_agreement", node_ref=None,
                     detail="no record of the terms being accepted")

        assert b"No record of the terms being accepted" in app_client.get("/").data

    def test_stale_document_reports_the_service_as_down(self, app_client, temp_dir):
        import app as app_module
        reader = self._point_at(app_module, temp_dir)
        with open(reader.status_path, "w") as f:
            json.dump({"state": "streaming", "node_ref": "nde4f2k9xq7m3b8",
                       "written_at": written_at(timedelta(hours=1))}, f)

        body = app_client.get("/").data
        assert b"Not running" in body
        # The identifier still shows: it stays valid, and someone contacting
        # support about a dead service is exactly who needs to quote it.
        assert b"nde4f2k9xq7m3b8" in body
