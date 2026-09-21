# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""In-memory VertexAIService stand-in for tests.

Records every send_message call and returns canned VertexAIResponse objects.
Tests can preload a response by agent_id; the default is a simple echo.
"""
import uuid
from typing import Callable, Optional

from app.services.vertex_ai_service import VertexAIResponse


class FakeVertexAIService:
    def __init__(self, default_response_text: str = "Echo response"):
        self.default_response_text = default_response_text
        self.canned_responses: dict[str, VertexAIResponse] = {}
        self.response_queues: dict[str, list[VertexAIResponse]] = {}
        self.errors: dict[str, Exception] = {}
        self.sessions_created: list[dict] = []
        self.messages_sent: list[dict] = []

    def set_response(self, agent_id: str, response: VertexAIResponse) -> None:
        """Preload a response for the next send_message call against agent_id."""
        self.canned_responses[agent_id] = response

    def set_error(self, agent_id: str, error: Exception) -> None:
        """Make send_message raise instead of answering — a broken stream,
        a rate limit, whatever the caller needs to handle."""
        self.errors[agent_id] = error

    def set_text_response(self, agent_id: str, text: str) -> None:
        self.canned_responses[agent_id] = VertexAIResponse(text=text, chunk_count=1)

    def queue_response(self, agent_id: str, response: VertexAIResponse) -> None:
        """Queue a one-shot response; queued responses are served (in order)
        before any canned response, letting tests script a sequence like
        empty-then-success."""
        self.response_queues.setdefault(agent_id, []).append(response)

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
        self,
        agent_id: str,
        session_id: str,
        message: str,
        on_first_chunk: Optional[Callable[[], None]] = None,
    ) -> VertexAIResponse:
        self.messages_sent.append(
            {"agent_id": agent_id, "session_id": session_id, "message": message}
        )
        if agent_id in self.errors:
            raise self.errors[agent_id]
        queue = self.response_queues.get(agent_id)
        if queue:
            response = queue.pop(0)
        elif agent_id in self.canned_responses:
            response = self.canned_responses[agent_id]
        else:
            response = VertexAIResponse(text=self.default_response_text, chunk_count=1)
        # Like the real service: the callback fires only if the engine sent
        # something back, and before the reply is returned.
        if on_first_chunk is not None and response.chunk_count > 0:
            on_first_chunk()
        return response
