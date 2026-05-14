# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""In-memory AdminLoggingService stand-in for tests."""
from typing import Optional


class FakeLoggingService:
    """Returns canned responses keyed by agent id for the two read methods."""

    def __init__(
        self,
        errors: Optional[dict[str, dict]] = None,
        last_used: Optional[dict[str, dict]] = None,
    ):
        self.errors = dict(errors or {})
        # last_used: agent_id -> { platform: datetime }
        self.last_used = dict(last_used or {})
        self.calls: list[dict] = []

    async def get_last_error_for_agent(
        self,
        access_token: str,
        project_id: str,
        service_name: str,
        agent_id: str,
    ) -> Optional[dict]:
        self.calls.append({
            "method": "last_error",
            "access_token": access_token,
            "project_id": project_id,
            "service_name": service_name,
            "agent_id": agent_id,
        })
        return self.errors.get(agent_id)

    async def get_last_used_per_platform(
        self,
        access_token: str,
        project_id: str,
        service_name: str,
        agent_id: str,
        window_days: int = 7,
        page_size: int = 200,
    ) -> dict:
        self.calls.append({
            "method": "last_used",
            "access_token": access_token,
            "project_id": project_id,
            "service_name": service_name,
            "agent_id": agent_id,
            "window_days": window_days,
        })
        return dict(self.last_used.get(agent_id, {}))

    async def aclose(self) -> None:  # pragma: no cover
        return None
