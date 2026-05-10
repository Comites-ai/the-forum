# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""FastAPI dependencies for dependency injection."""
from typing import Optional

from fastapi import Request

from app.services.message_processor_v2 import MessageProcessorV2
from app.services.firestore_service import FirestoreService
from app.services.vertex_ai_service import VertexAIService
from app.services.slack_service import SlackService
from app.services.identity_service import IdentityService
from app.services.scheduled_job_service import ScheduledJobService
from app.services.scheduled_job_executor_v2 import ScheduledJobExecutorV2
from app.services.gcs_service import GCSService


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
