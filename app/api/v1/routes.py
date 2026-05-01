"""API v1 routes aggregation."""
from fastapi import APIRouter

from app.api.v1 import slack_events_v2, google_chat_events, telegram_events, scheduled_jobs

router = APIRouter()

# Include all v1 endpoints
router.include_router(slack_events_v2.router)  # Using v2 (multi-platform architecture)
router.include_router(google_chat_events.router)
router.include_router(telegram_events.router)
router.include_router(scheduled_jobs.router)
