# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""In-memory AdminVertexService stand-in for tests."""
from typing import Optional


class FakeAdminVertexService:
    """Returns a canned engine response keyed by resource name."""

    def __init__(self, engines: Optional[dict[str, dict]] = None):
        self.engines = dict(engines or {})
        self.calls: list[tuple[str, str]] = []

    async def get_reasoning_engine(
        self, access_token: str, resource_name: str
    ) -> Optional[dict]:
        self.calls.append((access_token, resource_name))
        return self.engines.get(resource_name)

    async def aclose(self) -> None:  # pragma: no cover
        return None
