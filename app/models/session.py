# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Session mapping model."""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class Session(BaseModel):
    """
    Session mapping stored in Firestore.

    Maps a (unified user + agent) combination to a Vertex AI session.
    Supports cross-platform session continuity - the same user can message
    from Slack or Google Chat and maintain conversation history.

    Document ID format: {user_id}_{agent_id}
    """

    id: Optional[str] = Field(default=None, description="Firestore document ID")
    user_id: str = Field(..., description="Unified user ID from users collection")
    agent_id: str = Field(..., description="Agent ID from agents collection")
    vertex_ai_session_id: str = Field(..., description="Vertex AI session ID")
    platforms_used: list[str] = Field(
        default_factory=list,
        description="Platforms this session has been accessed from (slack, google_chat)"
    )
    last_active_platform: Optional[str] = Field(
        default=None,
        description="Platform of the most recent message; used to default scheduled-reminder delivery target"
    )
    created_at: datetime = Field(..., description="Session creation timestamp")
    last_activity_at: datetime = Field(..., description="Last message timestamp")

    model_config = {"frozen": False}  # Mutable to update platforms_used and last_activity_at
