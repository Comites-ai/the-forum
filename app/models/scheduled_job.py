# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Scheduled job configuration model."""
from datetime import datetime, UTC
from typing import Optional
from pydantic import BaseModel, Field, model_validator


class ScheduledJob(BaseModel):
    """
    Scheduled job configuration stored in Firestore.

    Represents a recurring job that sends a prompt to a Vertex AI agent
    and delivers the response to a user on any platform (Slack, Google Chat, Telegram).
    """

    id: Optional[str] = Field(default=None, description="Firestore document ID")
    name: str = Field(..., description="Human-readable job name")
    prompt: str = Field(..., description="Prompt to send to the agent")
    agent_id: str = Field(..., description="Agent ID from agents collection")

    # Multi-platform fields
    user_id: str = Field(..., description="Unified user ID from users collection")
    output_platform: str = Field(
        default="slack",
        description="Platform to deliver responses to (slack, google_chat, telegram)"
    )

    schedule: str = Field(..., description="Cron expression (e.g., '0 9 * * 1-5')")
    timezone: str = Field(default="UTC", description="IANA timezone (e.g., 'America/New_York')")

    enabled: bool = Field(default=True, description="Whether job is active")
    cloud_scheduler_job_name: Optional[str] = Field(
        default=None, description="Full Cloud Scheduler job resource name"
    )

    last_execution_at: Optional[datetime] = Field(
        default=None, description="Last successful execution timestamp"
    )
    last_execution_id: Optional[str] = Field(
        default=None, description="Unique ID of last execution attempt"
    )
    execution_started_at: Optional[datetime] = Field(
        default=None, description="Execution lock timestamp (set when job starts)"
    )
    last_error: Optional[str] = Field(
        default=None, description="Last error message if execution failed"
    )
    consecutive_failures: int = Field(
        default=0, description="Number of consecutive failed executions"
    )

    retry_at: Optional[datetime] = Field(
        default=None, description="One-time retry scheduled for this datetime"
    )
    retry_reason: Optional[str] = Field(
        default=None, description="Reason for scheduling a retry (e.g., 'rate_limit_429')"
    )

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC), description="Creation timestamp")
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC), description="Last update timestamp")

    model_config = {"frozen": False}  # Mutable for updates
