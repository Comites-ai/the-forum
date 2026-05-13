# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Admin UI agents-list and agent-detail routes."""
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

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/agents")
async def list_agents(
    request: Request,
    _email: str = Depends(require_admin_user),
    firestore: FirestoreService = Depends(get_firestore_service),
):
    """Show all agents and per-platform last-used (from last 10 sessions)."""
    settings = get_settings()
    agents = await firestore.list_agents()

    rows = []
    for agent in agents:
        recent = await firestore.list_recent_sessions_for_agent(agent.id, limit=10)
        last_used: dict[str, object] = {}
        for s in recent:
            if not s.last_activity_at:
                continue
            # Prefer last_active_platform when set; fall back to every
            # platform in platforms_used for older sessions that predate
            # the last_active_platform field being tracked.
            if s.last_active_platform:
                candidates = [s.last_active_platform]
            elif s.platforms_used:
                candidates = list(s.platforms_used)
            else:
                continue
            for platform in candidates:
                existing = last_used.get(platform)
                if existing is None or s.last_activity_at > existing:
                    last_used[platform] = s.last_activity_at
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
