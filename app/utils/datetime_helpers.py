# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Datetime normalization helpers."""
from datetime import datetime, UTC
from typing import Optional


def to_aware_utc(value) -> Optional[datetime]:
    """
    Normalize a Firestore-or-Python datetime to aware UTC.

    Accepts three input shapes:
      - None → returned as-is
      - Firestore Timestamp wrapper (non-datetime with a .timestamp() method)
      - Python datetime, naive or aware

    Returns a timezone-aware datetime in UTC, or None.

    Required because Firestore reads come back as proprietary Timestamp
    wrappers, and pre-migration documents stored naive Python datetimes.
    Funnelling reads through this helper guarantees every comparison and
    arithmetic operation downstream is aware-vs-aware.
    """
    if value is None:
        return None
    if not isinstance(value, datetime):
        return datetime.fromtimestamp(value.timestamp(), tz=UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
