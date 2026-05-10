# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Pydantic schemas for Slack Events API."""
from typing import Any, Dict
from pydantic import BaseModel, Field


class SlackChallenge(BaseModel):
    """
    Slack URL verification challenge.

    Sent by Slack when configuring the Events API Request URL.
    Must respond with the challenge value.
    """

    token: str = Field(..., description="Verification token")
    challenge: str = Field(..., description="Challenge string to echo back")
    type: str = Field(..., description="Request type (url_verification)")


class SlackEvent(BaseModel):
    """
    Slack event callback.

    Received when a subscribed event occurs (e.g., message.im).
    """

    token: str = Field(..., description="Verification token")
    team_id: str = Field(..., description="Slack workspace ID")
    api_app_id: str = Field(..., description="Slack app ID")
    event: Dict[str, Any] = Field(..., description="Event payload")
    type: str = Field(..., description="Request type (event_callback)")
    event_id: str = Field(..., description="Unique event ID")
    event_time: int = Field(..., description="Event timestamp (Unix epoch)")
    authorizations: list = Field(default_factory=list, description="Authorization info")
    is_ext_shared_channel: bool = Field(default=False)
    event_context: str = Field(default="")
