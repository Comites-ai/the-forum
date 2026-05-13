# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""In-memory AdminLoggingService stand-in for tests."""
from typing import Optional


class FakeLoggingService:
    """Returns a single canned response keyed by agent id."""

    def __init__(self, errors: Optional[dict[str, dict]] = None):
        self.errors = dict(errors or {})
        self.calls: list[dict] = []

    async def get_last_error_for_agent(
        self,
        access_token: str,
        project_id: str,
        service_name: str,
        agent_id: str,
    ) -> Optional[dict]:
        self.calls.append({
            "access_token": access_token,
            "project_id": project_id,
            "service_name": service_name,
            "agent_id": agent_id,
        })
        return self.errors.get(agent_id)

    async def aclose(self) -> None:  # pragma: no cover
        return None
