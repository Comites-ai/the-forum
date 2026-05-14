# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""FastAPI dependencies for dependency injection."""
from typing import Optional

from fastapi import Request


class AdminAuthRequired(Exception):
    """Raised by require_admin_user when the request is not authenticated.

    Handled by a global exception handler registered in create_app() that
    issues a 303 redirect to /admin/login — keeping unauthenticated browser
    GETs out of FastAPI's default JSON error response.
    """

from app.services.message_processor_v2 import MessageProcessorV2
from app.services.firestore_service import FirestoreService
from app.services.vertex_ai_service import VertexAIService
from app.services.slack_service import SlackService
from app.services.identity_service import IdentityService
from app.services.scheduled_job_service import ScheduledJobService
from app.services.scheduled_job_executor_v2 import ScheduledJobExecutorV2
from app.services.gcs_service import GCSService
from app.services.admin_auth_service import AdminAuthService
from app.services.admin_logging_service import AdminLoggingService
from app.services.admin_vertex_service import AdminVertexService


def get_gcs_service(request: Request) -> Optional[GCSService]:
    """
    Get GCSService instance from app state.

    Args:
        request: FastAPI request object

    Returns:
        GCSService instance, or None if GCS is not configured
    """
    return getattr(request.app.state, "gcs", None)


def get_firestore_service(request: Request) -> FirestoreService:
    """
    Get FirestoreService instance from app state.

    Args:
        request: FastAPI request object

    Returns:
        FirestoreService instance
    """
    return request.app.state.firestore


def get_vertex_ai_service(request: Request) -> VertexAIService:
    """
    Get VertexAIService instance from app state.

    Args:
        request: FastAPI request object

    Returns:
        VertexAIService instance
    """
    return request.app.state.vertex_ai


def get_slack_service(request: Request) -> SlackService:
    """
    Get SlackService instance from app state.

    Args:
        request: FastAPI request object

    Returns:
        SlackService instance
    """
    return request.app.state.slack


def get_scheduled_job_service(request: Request) -> ScheduledJobService:
    """
    Get ScheduledJobService instance from app state.

    Args:
        request: FastAPI request object

    Returns:
        ScheduledJobService instance
    """
    return request.app.state.scheduled_job_service


def get_identity_service(request: Request) -> IdentityService:
    """
    Get IdentityService instance.

    Args:
        request: FastAPI request object

    Returns:
        IdentityService instance
    """
    return IdentityService(firestore_service=request.app.state.firestore)


def get_message_processor_v2(request: Request) -> MessageProcessorV2:
    """
    Get MessageProcessorV2 instance (multi-platform version).

    Args:
        request: FastAPI request object

    Returns:
        MessageProcessorV2 instance
    """
    identity = get_identity_service(request)
    return MessageProcessorV2(
        firestore=request.app.state.firestore,
        vertex_ai=request.app.state.vertex_ai,
        identity=identity,
        gcs=getattr(request.app.state, "gcs", None),
    )


def get_admin_auth_service(request: Request) -> AdminAuthService:
    """Get the AdminAuthService from app.state (admin UI only)."""
    return request.app.state.admin_auth


def get_admin_logging_service(request: Request) -> AdminLoggingService:
    """Get the AdminLoggingService from app.state (admin UI only)."""
    return request.app.state.admin_logging


def get_admin_vertex_service(request: Request) -> AdminVertexService:
    """Get the AdminVertexService from app.state (admin UI only)."""
    return request.app.state.admin_vertex


def require_admin_user(request: Request) -> str:
    """
    FastAPI dependency that enforces an authenticated, IAM-verified admin.

    Returns the operator's email on success. Raises a 303 redirect to
    /admin/login when the request is not authenticated, so unauthenticated
    browser GETs land cleanly on the login page rather than seeing JSON
    error bodies.
    """
    session = request.scope.get("session") or {}
    if not session.get("authorized") or not session.get("email"):
        raise AdminAuthRequired()
    return session["email"]


def get_session_access_token(request: Request) -> Optional[str]:
    """Return the OAuth access token stored in the admin session, if any."""
    session = request.scope.get("session") or {}
    return session.get("access_token")


def get_scheduled_job_executor_v2(request: Request) -> ScheduledJobExecutorV2:
    """
    Get ScheduledJobExecutorV2 instance (multi-platform version).

    Args:
        request: FastAPI request object

    Returns:
        ScheduledJobExecutorV2 instance
    """
    identity = get_identity_service(request)
    return ScheduledJobExecutorV2(
        firestore=request.app.state.firestore,
        vertex_ai=request.app.state.vertex_ai,
        identity=identity,
    )
