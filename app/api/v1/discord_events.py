# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Discord Events API endpoint.

This endpoint does not receive events directly from Discord. Instead, it
receives normalized events forwarded by the discord-worker, which holds
the long-lived Discord Gateway WebSocket connection (see
docs/DISCORD_WORKER.md for the full architecture).

Authentication: the worker authenticates with a Google-issued OIDC token,
minted by its VM service account with the forum's Cloud Run URL as the
audience. We verify the token signature, audience, and expiry, then check
the verified email against the agent's configured
discord_worker_service_account to bind a given worker to a specific agent.
"""
import logging
import os
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from google.auth.transport import requests as google_auth_requests
from google.oauth2 import id_token

from app.core.dependencies import get_firestore_service, get_message_processor_v2
from app.services.firestore_service import FirestoreService
from app.services.message_processor_v2 import MessageProcessorV2
from app.services.platforms.discord_connector import DiscordConnector

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/discord", tags=["discord"])


# Audience the worker must mint its OIDC token for. Defaults to the forum's
# own public URL at runtime. Can be overridden in tests or non-Cloud-Run
# deployments by setting DISCORD_WORKER_OIDC_AUDIENCE explicitly.
def _expected_audience() -> Optional[str]:
    return os.environ.get("DISCORD_WORKER_OIDC_AUDIENCE") or os.environ.get("SERVICE_URL")


def _verify_worker_token(authorization_header: str, expected_email: Optional[str]) -> str:
    """
    Verify the OIDC bearer token presented by the discord-worker.

    Returns the verified email on success, raises HTTPException(401) on any failure.

    We rely on google.oauth2.id_token.verify_oauth2_token for signature,
    issuer, and expiry checks. If an expected_email is configured on the
    agent, we additionally require an exact match against the token's
    email claim — that's the binding between a worker VM and an agent.
    """
    if not authorization_header or not authorization_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing or malformed bearer token")

    token = authorization_header.split(" ", 1)[1].strip()
    audience = _expected_audience()

    try:
        claims = id_token.verify_oauth2_token(
            token, google_auth_requests.Request(), audience=audience
        )
    except ValueError as e:
        logger.warning(f"Discord worker token verification failed: {e}")
        raise HTTPException(status_code=401, detail="Invalid OIDC token")

    email = claims.get("email")
    if not email or not claims.get("email_verified", False):
        raise HTTPException(status_code=401, detail="Token missing verified email claim")

    if expected_email and email != expected_email:
        logger.warning(
            f"Discord worker token email mismatch: got {email}, expected {expected_email}"
        )
        raise HTTPException(status_code=403, detail="Caller not authorized for this agent")

    return email


@router.post("/events/{agent_id}")
async def discord_events(
    agent_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    message_processor: MessageProcessorV2 = Depends(get_message_processor_v2),
    firestore: FirestoreService = Depends(get_firestore_service),
):
    """
    Receive a normalized Discord event from the discord-worker.

    Returns 200 immediately and processes the event in the background,
    matching the pattern used by Slack, Google Chat, and Telegram. Errors
    after authentication are logged but still acknowledged so the worker
    does not enter a retry loop on a deterministically-bad payload.
    """
    # Look up the agent first so we know which worker SA to expect.
    agent = await firestore.get_agent_by_id(agent_id)
    if not agent:
        logger.error(f"Discord events POST for unknown agent_id: {agent_id}")
        raise HTTPException(status_code=404, detail="Unknown agent")

    discord_config = agent.get_discord_config()
    if not discord_config or not discord_config.enabled:
        logger.error(f"Agent {agent.id} does not have Discord enabled")
        raise HTTPException(status_code=404, detail="Discord not enabled for this agent")

    has_direct_token = discord_config.discord_bot_token is not None
    has_secret_config = (
        discord_config.discord_bot_token_secret is not None
        and discord_config.discord_bot_token_project_id is not None
    )
    if not has_direct_token and not has_secret_config:
        logger.error(
            f"Agent {agent.id} Discord config missing bot token. "
            f"Need either discord_bot_token OR "
            f"(discord_bot_token_secret + discord_bot_token_project_id)"
        )
        raise HTTPException(status_code=500, detail="Agent Discord config incomplete")

    # Verify the worker's OIDC token. We do this *after* loading the agent
    # so that the expected_email check has something to compare against.
    _verify_worker_token(
        request.headers.get("Authorization", ""),
        discord_config.discord_worker_service_account,
    )

    try:
        data = await request.json()
    except Exception as e:
        logger.error(f"Discord events: invalid JSON body: {e}")
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # Currently only DM messages are supported. Other event_type values
    # (reactions, edits, etc.) are accepted-and-ignored so the worker can
    # be evolved independently.
    event_type = data.get("event_type")
    if event_type != "dm_message":
        logger.info(f"Ignoring non-dm Discord event_type: {event_type!r}")
        return JSONResponse(content={"ok": True})

    logger.info(
        f"Received Discord DM for agent {agent_id} "
        f"from user {data.get('user_id')}: {data.get('text', '<no text>')!r}"
    )

    connector = DiscordConnector(
        bot_token=discord_config.discord_bot_token if has_direct_token else None,
        bot_token_secret=discord_config.discord_bot_token_secret if has_secret_config else None,
        bot_token_project_id=(
            discord_config.discord_bot_token_project_id if has_secret_config else None
        ),
    )

    try:
        platform_event = connector.parse_event(data)
    except ValueError as e:
        logger.error(f"Discord event parse error: {e}")
        # Bad payload from worker — surface so the worker can be fixed
        # rather than silently swallowed.
        raise HTTPException(status_code=400, detail=str(e))

    background_tasks.add_task(
        message_processor.process_platform_event,
        platform_event,
        connector,
        agent.id,
    )

    return JSONResponse(content={"ok": True})
