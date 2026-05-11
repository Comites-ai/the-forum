# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""In-memory VertexAIService stand-in for tests.

Records every send_message call and returns canned VertexAIResponse objects.
Tests can preload a response by agent_id; the default is a simple echo.
"""
import uuid
from typing import Optional

from app.services.vertex_ai_service import VertexAIResponse


class FakeVertexAIService:
    def __init__(self, default_response_text: str = "Echo response"):
        self.default_response_text = default_response_text
        self.canned_responses: dict[str, VertexAIResponse] = {}
        self.sessions_created: list[dict] = []
        self.messages_sent: list[dict] = []

    def set_response(self, agent_id: str, response: VertexAIResponse) -> None:
        """Preload a response for the next send_message call against agent_id."""
        self.canned_responses[agent_id] = response

    def set_text_response(self, agent_id: str, text: str) -> None:
        self.canned_responses[agent_id] = VertexAIResponse(text=text, chunk_count=1)

    async def create_session(
        self, agent_id: str, user_name: Optional[str] = None
    ) -> str:
        user_id = user_name or f"user-{uuid.uuid4().hex[:12]}"
        session_id = f"session-{uuid.uuid4().hex[:16]}"
        combined_id = f"{user_id}:{session_id}"
        self.sessions_created.append(
            {"agent_id": agent_id, "user_name": user_name, "session_id": combined_id}
        )
        return combined_id

    async def send_message(
        self, agent_id: str, session_id: str, message: str
    ) -> VertexAIResponse:
        self.messages_sent.append(
            {"agent_id": agent_id, "session_id": session_id, "message": message}
        )
        if agent_id in self.canned_responses:
            return self.canned_responses[agent_id]
        return VertexAIResponse(text=self.default_response_text, chunk_count=1)
