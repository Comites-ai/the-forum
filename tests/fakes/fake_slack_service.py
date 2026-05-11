# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""In-memory SlackService stand-in for tests."""


class FakeSlackService:
    def __init__(self):
        self.calls: list[dict] = []

    async def open_conversation(self, token: str, user_id: str) -> str:
        self.calls.append({"method": "open_conversation", "token": token, "user_id": user_id})
        return f"D{user_id}"

    async def post_message(self, token: str, channel: str, text: str) -> dict:
        self.calls.append(
            {"method": "post_message", "token": token, "channel": channel, "text": text}
        )
        return {"ok": True, "channel": channel, "ts": "1234567890.000001"}

    async def get_user_info(self, token: str, user_id: str) -> dict:
        self.calls.append({"method": "get_user_info", "token": token, "user_id": user_id})
        return {"display_name": f"Test User {user_id}", "real_name": "Test User", "email": None}

    async def get_conversation_info(self, token: str, channel: str) -> dict:
        self.calls.append({"method": "get_conversation_info", "token": token, "channel": channel})
        return {"id": channel, "is_im": True}

    async def download_file(self, token: str, url: str) -> bytes:
        self.calls.append({"method": "download_file", "token": token, "url": url})
        return b"fake-file-bytes"
