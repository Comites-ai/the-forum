# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""AdminLoggingService unit tests: filter assembly + response handling."""
import httpx
import pytest

from app.services.admin_logging_service import AdminLoggingService


def test_filter_includes_service_name_severity_and_agent_id():
    f = AdminLoggingService._build_filter("the-forum", "agent-42")
    assert 'resource.type="cloud_run_revision"' in f
    assert 'resource.labels.service_name="the-forum"' in f
    assert "severity>=ERROR" in f
    assert 'jsonPayload.agent_id="agent-42"' in f
    assert 'jsonPayload.message:"agent-42"' in f
    assert 'textPayload:"agent-42"' in f


def test_filter_escapes_quotes_in_agent_id():
    f = AdminLoggingService._build_filter("the-forum", 'evil"id')
    assert 'evil\\"id' in f


@pytest.mark.asyncio
async def test_returns_first_entry_when_entries_present():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"entries": [
                {"severity": "ERROR", "textPayload": "boom"},
                {"severity": "ERROR", "textPayload": "second"},
            ]},
        )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        service = AdminLoggingService(http_client=http)
        result = await service.get_last_error_for_agent(
            access_token="tok",
            project_id="p",
            service_name="svc",
            agent_id="a",
        )
        assert result == {"severity": "ERROR", "textPayload": "boom"}


@pytest.mark.asyncio
async def test_returns_none_when_no_entries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        service = AdminLoggingService(http_client=http)
        result = await service.get_last_error_for_agent(
            access_token="tok",
            project_id="p",
            service_name="svc",
            agent_id="a",
        )
        assert result is None


@pytest.mark.asyncio
async def test_returns_none_on_non_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        service = AdminLoggingService(http_client=http)
        result = await service.get_last_error_for_agent(
            access_token="tok",
            project_id="p",
            service_name="svc",
            agent_id="a",
        )
        assert result is None


# ---- last-used per platform ----


def test_last_used_filter_scopes_to_message_processed_event_and_window():
    f = AdminLoggingService._build_last_used_filter("the-forum", "agent-42", 7)
    assert 'jsonPayload.event="message_processed"' in f
    assert 'jsonPayload.agent_id="agent-42"' in f
    assert 'resource.labels.service_name="the-forum"' in f
    assert "timestamp>=" in f


@pytest.mark.asyncio
async def test_last_used_picks_first_per_platform_in_desc_order():
    # Cloud Logging returns entries in timestamp-desc order. Our method must
    # take the FIRST occurrence per platform (newest), not the last.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"entries": [
                {"timestamp": "2026-05-13T11:30:00Z", "jsonPayload": {"platform": "telegram"}},
                {"timestamp": "2026-05-13T11:00:00Z", "jsonPayload": {"platform": "slack"}},
                {"timestamp": "2026-05-12T18:00:00Z", "jsonPayload": {"platform": "slack"}},
                {"timestamp": "2026-05-11T09:00:00Z", "jsonPayload": {"platform": "google_chat"}},
            ]},
        )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        service = AdminLoggingService(http_client=http)
        result = await service.get_last_used_per_platform(
            access_token="tok",
            project_id="p",
            service_name="svc",
            agent_id="a",
        )
        assert set(result.keys()) == {"telegram", "slack", "google_chat"}
        # Slack must be the 11:00 entry, not the 18:00 one from the day before.
        assert result["slack"].isoformat() == "2026-05-13T11:00:00+00:00"
        assert result["telegram"].isoformat() == "2026-05-13T11:30:00+00:00"


@pytest.mark.asyncio
async def test_last_used_returns_empty_on_non_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        service = AdminLoggingService(http_client=http)
        result = await service.get_last_used_per_platform(
            access_token="tok",
            project_id="p",
            service_name="svc",
            agent_id="a",
        )
        assert result == {}


@pytest.mark.asyncio
async def test_last_used_skips_entries_without_platform_or_timestamp():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"entries": [
                {"jsonPayload": {"platform": "slack"}},  # no timestamp
                {"timestamp": "2026-05-13T11:30:00Z", "jsonPayload": {}},  # no platform
                {"timestamp": "2026-05-13T11:00:00Z", "jsonPayload": {"platform": "telegram"}},
            ]},
        )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        service = AdminLoggingService(http_client=http)
        result = await service.get_last_used_per_platform(
            access_token="tok",
            project_id="p",
            service_name="svc",
            agent_id="a",
        )
        assert result == {"telegram": result["telegram"]}
        assert result["telegram"].isoformat() == "2026-05-13T11:00:00+00:00"
