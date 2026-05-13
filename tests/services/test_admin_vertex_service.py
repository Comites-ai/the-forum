# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""AdminVertexService unit tests: location parsing + Console URL building."""
import httpx
import pytest

from app.services.admin_vertex_service import AdminVertexService


def test_location_extracted_from_resource_name():
    name = "projects/p/locations/us-central1/reasoningEngines/12345"
    assert AdminVertexService._location_from_name(name) == "us-central1"


def test_location_none_when_name_does_not_match_pattern():
    assert AdminVertexService._location_from_name("not-a-name") is None


def test_console_url_built_from_canonical_name():
    name = "projects/proj/locations/us-central1/reasoningEngines/9876"
    url = AdminVertexService._build_console_url(name)
    assert url == (
        "https://console.cloud.google.com/vertex-ai/agents/locations/"
        "us-central1/agent-engines/9876?project=proj"
    )


def test_console_url_none_for_unrecognized_name():
    assert AdminVertexService._build_console_url("garbage") is None


@pytest.mark.asyncio
async def test_get_reasoning_engine_returns_engine_and_url():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "name": "projects/proj/locations/us-central1/reasoningEngines/9876",
                "displayName": "engine-friendly",
            },
        )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        service = AdminVertexService(http_client=http)
        result = await service.get_reasoning_engine(
            access_token="tok",
            resource_name="projects/proj/locations/us-central1/reasoningEngines/9876",
        )
        assert result is not None
        assert result["engine"]["displayName"] == "engine-friendly"
        assert "agent-engines/9876" in result["console_url"]


@pytest.mark.asyncio
async def test_get_reasoning_engine_returns_none_on_404():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="not found")
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        service = AdminVertexService(http_client=http)
        result = await service.get_reasoning_engine(
            access_token="tok",
            resource_name="projects/proj/locations/us-central1/reasoningEngines/0",
        )
        assert result is None
