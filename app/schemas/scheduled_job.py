# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Pydantic schemas for Scheduled Jobs API."""
from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field


class ScheduledJobCreate(BaseModel):
    """Request body for creating a scheduled job."""

    name: str = Field(..., min_length=1, max_length=100, description="Human-readable job name")
    prompt: str = Field(..., min_length=1, max_length=4000, description="Prompt to send to agent")
    agent_id: str = Field(..., description="Agent ID from agents collection")
    user_id: str = Field(..., description="User ID from users collection")
    output_platform: str = Field(default="slack", description="Platform to deliver responses to (slack, google_chat)")
    schedule: str = Field(..., description="Cron expression (e.g., '0 9 * * 1-5')")
    timezone: str = Field(default="UTC", description="IANA timezone (e.g., 'America/New_York')")
    enabled: bool = Field(default=True, description="Whether job is active")


class ScheduledJobUpdate(BaseModel):
    """Request body for updating a scheduled job (all fields optional)."""

    name: Optional[str] = Field(None, min_length=1, max_length=100)
    prompt: Optional[str] = Field(None, min_length=1, max_length=4000)
    schedule: Optional[str] = Field(None, description="Cron expression")
    timezone: Optional[str] = Field(None, description="IANA timezone")
    enabled: Optional[bool] = Field(None, description="Whether job is active")


class ScheduledJobResponse(BaseModel):
    """Response body for scheduled job operations."""

    id: str
    name: str
    prompt: str
    agent_id: str
    user_id: str
    output_platform: str
    schedule: str
    timezone: str
    enabled: bool
    last_execution_at: Optional[datetime] = None
    last_error: Optional[str] = None
    consecutive_failures: int = 0
    created_at: datetime
    updated_at: datetime


class ScheduledJobListResponse(BaseModel):
    """Response body for listing scheduled jobs."""

    jobs: List[ScheduledJobResponse]
    total: int


class ExecuteJobRequest(BaseModel):
    """Request body for Cloud Scheduler webhook."""

    execution_id: str = Field(..., description="Unique execution ID for idempotency")


class ExecuteJobResponse(BaseModel):
    """Response body for job execution."""

    success: bool
    job_id: str
    message: Optional[str] = None
