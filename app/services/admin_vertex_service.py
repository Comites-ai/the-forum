# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Vertex AI lookup for the admin UI's agent detail page.

Calls the Vertex AI REST API with the operator's OAuth access token to
resolve a Reasoning Engine resource name → canonical metadata and a
deep-link URL to the GCP Console. Trusts the API response; no client-side
fallback parsing.
"""
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class AdminVertexService:
    """Resolves Reasoning Engine metadata and Console URLs."""

    def __init__(self, http_client: Optional[httpx.AsyncClient] = None):
        self._http_client = http_client

    async def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=10.0)
        return self._http_client

    @staticmethod
    def _location_from_name(resource_name: str) -> Optional[str]:
        # Resource names: projects/{P}/locations/{L}/reasoningEngines/{ID}
        parts = resource_name.split("/")
        if len(parts) >= 4 and parts[2] == "locations":
            return parts[3]
        return None

    @staticmethod
    def _build_console_url(canonical_name: str) -> Optional[str]:
        parts = canonical_name.split("/")
        if (
            len(parts) != 6
            or parts[0] != "projects"
            or parts[2] != "locations"
            or parts[4] != "reasoningEngines"
        ):
            return None
        project, location, engine_id = parts[1], parts[3], parts[5]
        return (
            f"https://console.cloud.google.com/vertex-ai/agents/locations/"
            f"{location}/agent-engines/{engine_id}?project={project}"
        )

    async def get_reasoning_engine(
        self, access_token: str, resource_name: str
    ) -> Optional[dict]:
        """
        Return the engine metadata plus a Console URL, or None on failure.

        On success the returned dict contains the raw API response under
        `"engine"` and a derived `"console_url"` keyed off the canonical
        `name` the API returns (not the input string), so that the link
        always reflects the resource the API actually resolved.
        """
        location = self._location_from_name(resource_name)
        if not location:
            logger.warning(
                "Could not extract location from resource name %s", resource_name
            )
            return None

        url = f"https://{location}-aiplatform.googleapis.com/v1/{resource_name}"
        try:
            response = await (await self._client()).get(
                url,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as e:
            logger.warning(f"Vertex AI request failed: {e}")
            return None

        if response.status_code != 200:
            logger.warning(
                "Vertex AI get reasoning engine returned %s: %s",
                response.status_code,
                response.text[:300],
            )
            return None

        engine = response.json()
        canonical_name = engine.get("name", resource_name)
        return {
            "engine": engine,
            "console_url": self._build_console_url(canonical_name),
        }

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
