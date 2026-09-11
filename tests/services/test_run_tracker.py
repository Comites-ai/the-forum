# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Tests for in-flight run bookkeeping."""
import logging
from datetime import UTC, datetime, timedelta

from app.services.run_tracker import (
    CUT_OFF_TIMEOUT,
    CUT_OFF_VANISHED,
    CutOffRun,
    active_run_marker,
    describe_cut_off,
    log_run_cut_off,
)

GRACE = 120


def _marker(seconds_ago: float, **extra):
    return {
        "started_at": datetime.now(UTC) - timedelta(seconds=seconds_ago),
        "reason": CUT_OFF_VANISHED,
        **extra,
    }


def test_no_marker_means_nothing_to_report():
    assert describe_cut_off(None, stale_after_seconds=GRACE) is None
    assert describe_cut_off({}, stale_after_seconds=GRACE) is None


def test_a_fresh_marker_is_a_concurrent_run_not_a_cut_off():
    """A second message during a live turn finds the first turn's marker."""
    assert describe_cut_off(_marker(5), stale_after_seconds=GRACE) is None


def test_a_stale_marker_is_a_cut_off():
    run = describe_cut_off(_marker(600), stale_after_seconds=GRACE)
    assert run is not None
    assert run.reason == CUT_OFF_VANISHED
    assert 590 < run.elapsed_seconds < 610


def test_the_marker_carries_a_more_specific_reason_when_one_is_known():
    marker = _marker(600, reason=CUT_OFF_TIMEOUT, last_tool="write_sheet")
    run = describe_cut_off(marker, stale_after_seconds=GRACE)
    assert run.reason == CUT_OFF_TIMEOUT
    assert run.last_tool == "write_sheet"


def test_a_marker_with_no_start_time_is_ignored():
    assert describe_cut_off({"reason": "who knows"}, stale_after_seconds=GRACE) is None


def test_an_unreadable_start_time_is_ignored_not_raised():
    marker = {"started_at": "not a timestamp", "reason": CUT_OFF_VANISHED}
    assert describe_cut_off(marker, stale_after_seconds=GRACE) is None


def test_a_naive_timestamp_is_treated_as_utc():
    """Firestore hands back timestamps in more than one shape."""
    marker = {"started_at": datetime.utcnow() - timedelta(seconds=600)}
    run = describe_cut_off(marker, stale_after_seconds=GRACE)
    assert run is not None
    assert 590 < run.elapsed_seconds < 610


def test_a_new_marker_records_now_and_the_assumed_reason():
    marker = active_run_marker(CUT_OFF_TIMEOUT)
    assert marker["reason"] == CUT_OFF_TIMEOUT
    assert (datetime.now(UTC) - marker["started_at"]).total_seconds() < 5
    # A fresh marker must never read as a cut-off.
    assert describe_cut_off(marker, stale_after_seconds=GRACE) is None


def test_cut_off_log_carries_the_queryable_fields(caplog):
    """These field names are a log-query contract; they should not drift."""
    run = CutOffRun(
        started_at=datetime.now(UTC) - timedelta(seconds=131),
        elapsed_seconds=131.4,
        reason=CUT_OFF_TIMEOUT,
        last_tool="write_sheet",
    )
    with caplog.at_level(logging.WARNING, logger="app.services.run_tracker"):
        log_run_cut_off(
            run, agent_id="engines/1", session_id="abc", platform="slack"
        )

    record = caplog.records[-1]
    assert record.json_fields == {
        "event": "run_cut_off",
        "agent_id": "engines/1",
        "session_id": "abc",
        "platform": "slack",
        "cutoff_reason": CUT_OFF_TIMEOUT,
        "elapsed_seconds": 131.4,
        "last_tool": "write_sheet",
    }
    assert "write_sheet" in record.getMessage()
