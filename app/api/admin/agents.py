# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Admin UI agents-list and agent-detail routes."""
import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from app.config import get_settings
from app.core.dependencies import (
    get_admin_logging_service,
    get_admin_vertex_service,
    get_firestore_service,
    get_session_access_token,
    require_admin_user,
)
from app.api.admin._common import PLATFORMS, PLATFORM_LABELS, get_templates
from app.services.admin_logging_service import AdminLoggingService
from app.services.admin_vertex_service import AdminVertexService
from app.services.firestore_service import FirestoreService

LAST_USED_WINDOW_DAYS = 7

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/agents")
async def list_agents(
    request: Request,
    _email: str = Depends(require_admin_user),
    firestore: FirestoreService = Depends(get_firestore_service),
    logging_service: AdminLoggingService = Depends(get_admin_logging_service),
):
    """List all agents with per-platform "last used" from the last 7 days
    of Cloud Logging (one log-query per agent, run in parallel)."""
    settings = get_settings()
    agents = await firestore.list_agents()
    access_token = get_session_access_token(request) or ""

    async def _last_used_for(agent_id: str) -> dict[str, object]:
        if not access_token:
            return {}
        try:
            return await logging_service.get_last_used_per_platform(
                access_token=access_token,
                project_id=settings.gcp_project_id,
                service_name=settings.cloud_run_service_name,
                agent_id=agent_id,
                window_days=LAST_USED_WINDOW_DAYS,
            )
        except Exception as e:
            logger.warning(f"Failed to fetch last-used for agent {agent_id}: {e}")
            return {}

    last_used_list = await asyncio.gather(*[_last_used_for(a.id) for a in agents])

    rows = []
    for agent, last_used in zip(agents, last_used_list):
        configured = [p.platform for p in (agent.platforms or []) if p.enabled]
        rows.append({
            "agent": agent,
            "platforms": configured,
            "last_used": last_used,
        })

    return get_templates(request).TemplateResponse(
        request,
        "admin/agents_list.html",
        {
            "rows": rows,
            "agents": agents,
            "platform_columns": list(PLATFORMS),
            "platform_labels": PLATFORM_LABELS,
            "agents_collection": settings.firestore_agents_collection,
            "last_used_window_days": LAST_USED_WINDOW_DAYS,
        },
    )


@router.get("/agents/{agent_id}")
async def agent_detail(
    agent_id: str,
    request: Request,
    _email: str = Depends(require_admin_user),
    firestore: FirestoreService = Depends(get_firestore_service),
    logging_service: AdminLoggingService = Depends(get_admin_logging_service),
    vertex_service: AdminVertexService = Depends(get_admin_vertex_service),
):
    settings = get_settings()
    agent = await firestore.get_agent_by_id(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    access_token = get_session_access_token(request) or ""
    sessions = await firestore.list_recent_sessions_for_agent(agent_id, limit=10)

    last_error = None
    engine = None
    console_url = None

    if access_token:
        try:
            last_error = await logging_service.get_last_error_for_agent(
                access_token=access_token,
                project_id=settings.gcp_project_id,
                service_name=settings.cloud_run_service_name,
                agent_id=agent_id,
            )
        except Exception as e:
            logger.warning(f"Failed to fetch last error for agent {agent_id}: {e}")

        try:
            engine_result = await vertex_service.get_reasoning_engine(
                access_token=access_token,
                resource_name=agent.vertex_ai_agent_id,
            )
        except Exception as e:
            logger.warning(f"Failed to fetch Vertex AI engine for agent {agent_id}: {e}")
            engine_result = None

        if engine_result:
            engine = engine_result.get("engine")
            console_url = engine_result.get("console_url")

    return get_templates(request).TemplateResponse(
        request,
        "admin/agent_detail.html",
        {
            "agent": agent,
            "sessions": sessions,
            "last_error": last_error,
            "engine": engine,
            "console_url": console_url,
            "platform_labels": PLATFORM_LABELS,
        },
    )
