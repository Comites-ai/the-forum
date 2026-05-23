# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""API v1 routes aggregation."""
from fastapi import APIRouter

from app.api.v1 import (
    discord_events,
    google_chat_events,
    scheduled_jobs,
    slack_events_v2,
    telegram_events,
)

router = APIRouter()

# Include all v1 endpoints
router.include_router(slack_events_v2.router)  # Using v2 (multi-platform architecture)
router.include_router(google_chat_events.router)
router.include_router(telegram_events.router)
router.include_router(discord_events.router)
router.include_router(scheduled_jobs.router)
