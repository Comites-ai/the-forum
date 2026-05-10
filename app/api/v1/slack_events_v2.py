# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Slack Events API endpoint (v2 - multi-platform architecture)."""
import logging
from fastapi import APIRouter, Request, BackgroundTasks, HTTPException, Depends
from fastapi.responses import JSONResponse

from app.schemas.slack import SlackEvent, SlackChallenge
from app.services.message_processor_v2 import MessageProcessorV2
from app.services.platforms.slack_connector import SlackConnector
from app.core.dependencies import get_message_processor_v2, get_firestore_service
from app.services.firestore_service import FirestoreService
from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/slack", tags=["slack"])


@router.post("/events")
async def slack_events_v2(
    request: Request,
    background_tasks: BackgroundTasks,
    message_processor: MessageProcessorV2 = Depends(get_message_processor_v2),
    firestore: FirestoreService = Depends(get_firestore_service),
):
    """
    Slack Events API endpoint (v2 - multi-platform).

    Handles:
    1. URL verification challenge (when configuring Request URL)
    2. Event callbacks (messages, etc.)

    Returns 200 within 3 seconds. Processes events in background.

    Args:
        request: FastAPI request object
        background_tasks: FastAPI background tasks
        message_processor: Message processor service (v2)
        firestore: Firestore service

    Returns:
        JSON response with challenge (for verification) or ok status
    """
    settings = get_settings()

    # Acknowledge Slack retries immediately
    retry_num = request.headers.get("X-Slack-Retry-Num")
    if retry_num is not None:
        retry_reason = request.headers.get("X-Slack-Retry-Reason", "unknown")
        logger.info(f"Acknowledging Slack retry #{retry_num} (reason: {retry_reason})")
        return JSONResponse(content={"ok": True})

    # Parse JSON
    data = await request.json()

    # Handle URL verification challenge
    if data.get("type") == "url_verification":
        try:
            challenge = SlackChallenge(**data)
            logger.info("Slack URL verification challenge received")
            return JSONResponse(content={"challenge": challenge.challenge})
        except Exception as e:
            logger.error(f"Error parsing Slack challenge: {e}")
            raise HTTPException(status_code=400, detail="Invalid challenge format")

    # Handle event callback
    if data.get("type") == "event_callback":
        try:
            event = SlackEvent(**data)

            # Ignore bot messages to prevent loops
            if event.event.get("bot_id"):
                logger.debug("Ignoring bot message to prevent loops")
                return JSONResponse(content={"ok": True})

            # Ignore message edits and deletions
            if event.event.get("subtype") in ["message_changed", "message_deleted"]:
                logger.debug(f"Ignoring message subtype: {event.event.get('subtype')}")
                return JSONResponse(content={"ok": True})

            logger.info(
                f"Received event: {event.event.get('type')} "
                f"from user {event.event.get('user')}"
            )

            # Step 1: Identify which agent this message is for
            bot_id = None
            if event.authorizations and len(event.authorizations) > 0:
                bot_id = event.authorizations[0].get("user_id")

            if not bot_id:
                bot_id = event.event.get("bot_id")

            if not bot_id:
                logger.error(f"No bot_id found in event")
                return JSONResponse(content={"ok": True})

            # Step 2: Get agent configuration
            agent = await firestore.get_agent_by_bot_id(bot_id)
            if not agent:
                logger.error(f"No agent found for bot_id: {bot_id}")
                return JSONResponse(content={"ok": True})

            # Step 3: Get Slack platform config from agent
            slack_config = agent.get_slack_config()
            if not slack_config:
                logger.error(f"Agent {agent.id} has no Slack configuration")
                return JSONResponse(content={"ok": True})

            # Validate that we have either direct token or Secret Manager config
            has_direct_token = slack_config.slack_bot_token is not None
            has_secret_config = (
                slack_config.slack_bot_token_secret is not None and
                slack_config.slack_bot_token_project_id is not None
            )

            if not has_direct_token and not has_secret_config:
                logger.error(
                    f"Agent {agent.id} Slack config missing bot token. "
                    f"Need either slack_bot_token OR (slack_bot_token_secret + slack_bot_token_project_id)"
                )
                return JSONResponse(content={"ok": True})

            # Step 4: Verify request signature against all configured signing secrets
            # (each Slack app has its own signing secret)
            signature_valid = False
            for signing_secret in settings.slack_signing_secrets:
                connector_check = SlackConnector(
                    bot_token=slack_config.slack_bot_token if has_direct_token else None,
                    bot_token_secret=slack_config.slack_bot_token_secret if has_secret_config else None,
                    bot_token_project_id=slack_config.slack_bot_token_project_id if has_secret_config else None,
                    signing_secret=signing_secret
                )
                if await connector_check.verify_request(request):
                    signature_valid = True
                    break

            if not signature_valid:
                logger.warning("Invalid Slack signature for all configured secrets")
                raise HTTPException(status_code=401, detail="Invalid signature")

            # Step 5: Create Slack connector with agent's credentials (no signing secret needed for sending)
            connector = SlackConnector(
                bot_token=slack_config.slack_bot_token if has_direct_token else None,
                bot_token_secret=slack_config.slack_bot_token_secret if has_secret_config else None,
                bot_token_project_id=slack_config.slack_bot_token_project_id if has_secret_config else None,
                signing_secret=None  # Not needed for sending messages
            )

            # Step 6: Parse Slack event into platform event
            platform_event = connector.parse_event(data)

            # Step 7: Process event in background
            background_tasks.add_task(
                message_processor.process_platform_event,
                platform_event,
                connector,
                agent.id
            )

            # Return immediately
            return JSONResponse(content={"ok": True})

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Error processing Slack event: {e}")
            # Still return 200 to acknowledge receipt
            return JSONResponse(content={"ok": True})

    # Unknown event type
    logger.warning(f"Unknown Slack event type: {data.get('type')}")
    return JSONResponse(content={"ok": True})
