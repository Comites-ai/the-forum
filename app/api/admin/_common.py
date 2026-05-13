# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Shared constants and helpers for admin routes."""
from zoneinfo import available_timezones

from fastapi import Request
from fastapi.templating import Jinja2Templates


PLATFORMS = ("slack", "google_chat", "telegram")

PLATFORM_LABELS = {
    "slack": "Slack",
    "google_chat": "Google Chat",
    "telegram": "Telegram",
}

# Sorted IANA timezone names for the scheduled-job timezone dropdown.
# Computed once at import; ~600 entries, fine in a native <select>.
# Excludes pure aliases like "GMT+5" because the underlying scheduler
# (pytz / croniter) expects canonical IANA names.
TIMEZONES = sorted(available_timezones())


def get_templates(request: Request) -> Jinja2Templates:
    """Return the Jinja2Templates instance stored on app.state."""
    return request.app.state.templates
