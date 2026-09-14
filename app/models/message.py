# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""One relayed message, as the Forum logs it for the history MCP (PLAT-43).

Stored under ``conversations/{agent_id}__{user_id}/messages``. Putting each
agent-and-user pair in its own subcollection means the history server can
only ever query the caller's own conversation — there is no query that could
reach another agent's or another user's messages — and a time-ordered range
needs nothing beyond Firestore's automatic single-field index.

Retention is a Firestore TTL policy on ``expires_at`` (terraform/firestore.tf).
Nothing in the Forum deletes these documents.
"""
from datetime import datetime, timedelta, UTC
from typing import Literal, Optional

from pydantic import BaseModel, Field

RETENTION = timedelta(days=7)

Direction = Literal["inbound", "outbound"]
Kind = Literal["live", "scheduled"]


def conversation_key(agent_id: str, user_id: str) -> str:
    """Document id of the conversation holding one agent-user pair's messages."""
    return f"{agent_id}__{user_id}"


class LoggedMessage(BaseModel):
    """A message the Forum relayed, verbatim, in either direction."""

    id: Optional[str] = Field(default=None, description="Firestore document id")
    agent_id: str = Field(..., description="Forum agent document id")
    user_id: str = Field(..., description="Forum unified user id, never a platform id")
    platform: str = Field(..., description="slack / google_chat / telegram / discord")
    direction: Direction
    author: str = Field(
        ...,
        description=(
            "The user's name (inbound, live), the scheduled job's name "
            "(inbound, scheduled), or the agent's display name (outbound)."
        ),
    )
    text: str = Field(..., description="Exactly what the user said or the agent sent")
    kind: Kind
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: Optional[datetime] = Field(
        default=None, description="created_at + RETENTION; the Firestore TTL field"
    )
    platform_message_ids: list[str] = Field(
        default_factory=list,
        description="Every platform message this one became (a chunked reply has several)",
    )
    attachments: list[str] = Field(
        default_factory=list,
        description="Placeholders such as '[image: image/jpeg]'; never bytes or URIs",
    )

    def model_post_init(self, __context) -> None:
        if self.expires_at is None:
            self.expires_at = self.created_at + RETENTION

    @property
    def conversation_key(self) -> str:
        return conversation_key(self.agent_id, self.user_id)
