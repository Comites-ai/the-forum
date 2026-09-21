# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""One query_agent call, as the Forum records it: the delivery receipt (PLAT-51).

A caller whose query_agent call ended in an error, a timeout, or a dropped
connection cannot tell from the error alone whether its message reached the
target. This record answers that. It is written the moment the Forum hands
the message to the target's engine, marked delivered when the engine sends
its first chunk back, and closed with the reply or with what went wrong.
The status tool (get_query_status) reads it back.

Stored under ``a2a_sessions/{caller}__{target}__{user}/a2a_queries``, the
same document the A2A conversation itself hangs off. A subcollection per
(caller, target, user) means the status tool can only ever reach the
caller's own queries, and "newest first" needs nothing beyond Firestore's
automatic single-field index. Subcollections survive the parent document
being deleted and recreated, which the session-recovery paths do.

Retention is a Firestore TTL policy on ``expires_at`` (terraform/firestore.tf).
"""
import base64
from datetime import datetime, timedelta, UTC
from typing import Any, Optional

from pydantic import BaseModel, Field

RETENTION = timedelta(days=7)

# How a query_agent call to the engine ended. The same values are the
# `outcome` field of the `a2a_query` log row, so they are a log-query
# contract as well as a stored status — keep them stable.
QUERY_SENT = "sent"  # handed to the engine; nothing heard back yet
QUERY_REPLIED = "replied"
QUERY_EMPTY_REPLY = "empty_reply"
QUERY_TIMED_OUT = "timed_out"
QUERY_FAILED = "failed"


def a2a_session_key(caller_id: str, target_id: str, user_id: str) -> str:
    """Document id of the A2A conversation for one (caller, target, user)."""
    return f"{caller_id}__{target_id}__{user_id}"


def encode_query_id(caller_id: str, target_id: str, user_id: str, doc_id: str) -> str:
    """The receipt handed to the caller: an opaque token naming the record."""
    raw = f"{caller_id}|{target_id}|{user_id}|{doc_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_query_id(token: Any) -> tuple[str, str, str, str]:
    """Inverse of encode_query_id. Raises ValueError for anything else."""
    if not isinstance(token, str) or not token.strip():
        raise ValueError(
            "query_id is required. Use the one query_agent returned, or pass "
            "agent_name and on_behalf_of to list your recent queries."
        )
    stripped = token.strip()
    padded = stripped + "=" * (-len(stripped) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded).decode("utf-8")
        caller_id, target_id, user_id, doc_id = raw.split("|", 3)
    except Exception:
        raise ValueError(f"query_id {token!r} is not a query id from this server.")
    if not (caller_id and target_id and user_id and doc_id):
        raise ValueError(f"query_id {token!r} is not a query id from this server.")
    return caller_id, target_id, user_id, doc_id


class A2AQuery(BaseModel):
    """The Forum's record of one query_agent call."""

    id: Optional[str] = Field(default=None, description="Firestore document id")
    caller_agent_id: str = Field(..., description="Forum agent document id of the caller")
    target_agent_id: str = Field(..., description="Forum agent document id of the target")
    user_id: str = Field(..., description="Forum unified user id the query was on behalf of")
    caller: str = Field(..., description="Caller's display name")
    target: str = Field(..., description="Target's display name")
    on_behalf_of: str = Field(..., description="The user's primary name")
    message: str = Field(..., description="The message as the caller wrote it, without the attribution prefix")
    status: str = Field(default=QUERY_SENT, description="One of the QUERY_* values")
    attempts: int = Field(default=1, description="Sends of this message to the engine (2 after a dead-session retry)")
    sent_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    delivered_at: Optional[datetime] = Field(
        default=None, description="When the engine sent its first chunk back: proof it has the message"
    )
    finished_at: Optional[datetime] = Field(
        default=None, description="When the Forum recorded the outcome"
    )
    reply: Optional[str] = Field(default=None, description="The target's reply, once there is one")
    error: Optional[str] = Field(default=None, description="What went wrong, for failed calls")
    timeout_seconds: Optional[float] = Field(
        default=None, description="How long the Forum was prepared to wait on this attempt"
    )
    session_id: Optional[str] = Field(default=None, description="The Vertex session the attempt ran on")
    expires_at: Optional[datetime] = Field(
        default=None, description="sent_at + RETENTION; the Firestore TTL field"
    )

    def model_post_init(self, __context) -> None:
        if self.expires_at is None:
            self.expires_at = self.sent_at + RETENTION

    @property
    def delivered(self) -> bool:
        return self.delivered_at is not None

    @property
    def query_id(self) -> str:
        return encode_query_id(
            self.caller_agent_id, self.target_agent_id, self.user_id, self.id or ""
        )
