# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Unit tests for app.utils.datetime_helpers.to_aware_utc."""
from datetime import datetime, timezone, timedelta

from app.utils.datetime_helpers import to_aware_utc


def test_to_aware_utc_returns_none_for_none():
    assert to_aware_utc(None) is None


def test_to_aware_utc_makes_naive_datetime_aware():
    naive = datetime(2026, 5, 10, 12, 0, 0)
    result = to_aware_utc(naive)
    assert result.tzinfo is not None
    assert result.tzinfo.utcoffset(result) == timedelta(0)
    assert result.year == 2026 and result.hour == 12


def test_to_aware_utc_converts_non_utc_aware_to_utc():
    est = timezone(timedelta(hours=-5))
    aware_est = datetime(2026, 5, 10, 12, 0, 0, tzinfo=est)
    result = to_aware_utc(aware_est)
    assert result.tzinfo.utcoffset(result) == timedelta(0)
    assert result.hour == 17  # 12 EST → 17 UTC


def test_to_aware_utc_passes_through_utc_aware_datetime():
    aware_utc = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    result = to_aware_utc(aware_utc)
    assert result == aware_utc
    assert result.tzinfo.utcoffset(result) == timedelta(0)


def test_to_aware_utc_unwraps_firestore_timestamp_wrapper():
    """Firestore returns a wrapper object with a .timestamp() method."""

    class FakeFirestoreTimestamp:
        def __init__(self, ts: float):
            self._ts = ts

        def timestamp(self) -> float:
            return self._ts

    epoch = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc).timestamp()
    result = to_aware_utc(FakeFirestoreTimestamp(epoch))
    assert result.tzinfo is not None
    assert result.tzinfo.utcoffset(result) == timedelta(0)
    assert result == datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)


def test_result_is_comparable_to_datetime_now_utc():
    """The whole reason this helper exists: aware-vs-aware comparisons."""
    from datetime import UTC

    naive_legacy = datetime(2020, 1, 1)
    aware_now = datetime.now(UTC)
    normalized = to_aware_utc(naive_legacy)
    assert normalized < aware_now
