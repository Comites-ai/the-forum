# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Generic fake PlatformConnector for testing.

Subclasses the real ABC so it can stand in for any concrete connector when
testing platform-agnostic services like MessageProcessorV2.
"""
from typing import Optional

from fastapi import Request

from app.schemas.platform_event import PlatformEvent
from app.services.platforms.base import PlatformConnector


class FakePlatformConnector(PlatformConnector):
    def __init__(
        self,
        platform: str = "slack",
        verify_result: bool = True,
        user_info: Optional[dict] = None,
    ):
        self.platform = platform
        self._verify_result = verify_result
        self._user_info = user_info or {
            "display_name": "Test User",
            "email": "test@example.com",
        }
        self.sent_messages: list[dict] = []
        self.opened_conversations: list[dict] = []
        self.file_responses: dict[str, bytes] = {}
        self.parse_calls: list[dict] = []

    def set_file_response(self, download_ref: str, data: bytes) -> None:
        self.file_responses[download_ref] = data

    def set_user_info(self, info: dict) -> None:
        self._user_info = info

    async def send_message(self, recipient_id: str, text: str) -> dict:
        self.sent_messages.append({"recipient_id": recipient_id, "text": text})
        return {"ok": True, "recipient_id": recipient_id}

    async def download_file(self, download_ref: str) -> bytes:
        return self.file_responses.get(download_ref, b"fake-image-bytes")

    async def get_user_info(self, user_id: str) -> dict:
        return dict(self._user_info)

    async def open_conversation(self, user_id: str, space_id: Optional[str] = None) -> str:
        self.opened_conversations.append({"user_id": user_id, "space_id": space_id})
        return space_id or f"conversation-{user_id}"

    async def verify_request(self, request: Request) -> bool:
        return self._verify_result

    def parse_event(self, data: dict) -> PlatformEvent:
        self.parse_calls.append(data)
        return PlatformEvent(
            platform=self.platform,
            user_id=data.get("user_id", "U_FAKE"),
            message_text=data.get("text", ""),
            space_id=data.get("space_id", "C_FAKE"),
            files=[],
            raw_event=data,
        )
