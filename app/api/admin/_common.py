# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Shared constants and helpers for admin routes."""
from fastapi import Request
from fastapi.templating import Jinja2Templates


PLATFORMS = ("slack", "google_chat", "telegram")

PLATFORM_LABELS = {
    "slack": "Slack",
    "google_chat": "Google Chat",
    "telegram": "Telegram",
}


def get_templates(request: Request) -> Jinja2Templates:
    """Return the Jinja2Templates instance stored on app.state."""
    return request.app.state.templates
