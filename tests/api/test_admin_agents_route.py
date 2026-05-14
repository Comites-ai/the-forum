# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Admin agents list + detail pages."""
from datetime import datetime, UTC, timedelta

from app.models.agent import Agent, AgentPlatformConfig


def _login(client):
    client.get("/admin/auth/callback?code=t&state=t", follow_redirects=False)


def _seed_agent(fake_firestore, agent_id="agent-1", display_name="Imperator") -> str:
    agent = Agent(
        vertex_ai_agent_id="projects/test-project/locations/us-central1/reasoningEngines/12345",
        display_name=display_name,
        platforms=[
            AgentPlatformConfig(platform="slack", enabled=True, slack_bot_id="B1"),
            AgentPlatformConfig(platform="telegram", enabled=True),
        ],
    )
    return fake_firestore.add_agent(agent, agent_id=agent_id)


def _add_session(fake_firestore, agent_id, platform, when):
    fake_firestore.add_session({
        "user_id": f"u-{platform}",
        "agent_id": agent_id,
        "vertex_ai_session_id": f"vsess-{platform}",
        "platforms_used": [platform],
        "last_active_platform": platform,
        "created_at": when - timedelta(minutes=1),
        "last_activity_at": when,
    })


def test_agents_list_shows_seeded_agent(admin_client, fake_firestore):
    _seed_agent(fake_firestore)
    _login(admin_client)
    response = admin_client.get("/admin/agents")
    assert response.status_code == 200
    body = response.text
    assert "Imperator" in body
    assert "agent-1" in body


def test_agents_list_shows_per_platform_last_used(
    admin_client, fake_firestore, fake_admin_logging
):
    _seed_agent(fake_firestore)
    now = datetime.now(UTC)
    fake_admin_logging.last_used["agent-1"] = {
        "slack": now - timedelta(hours=2),
        "telegram": now - timedelta(minutes=30),
    }
    _login(admin_client)
    response = admin_client.get("/admin/agents")
    body = response.text
    # Both platforms appear with a timestamp; the iso strings render in
    # the <time datetime="..."> attribute even before JS reformats them.
    assert (now - timedelta(hours=2)).isoformat() in body
    assert (now - timedelta(minutes=30)).isoformat() in body
    # google_chat has no data → its cell shows the em-dash placeholder.
    assert "—" in body


def test_agent_detail_shows_recent_sessions_and_console_link(
    admin_client, fake_firestore, fake_admin_vertex, fake_admin_logging
):
    _seed_agent(fake_firestore)
    now = datetime.now(UTC)
    _add_session(fake_firestore, "agent-1", "slack", now)
    fake_admin_vertex.engines[
        "projects/test-project/locations/us-central1/reasoningEngines/12345"
    ] = {
        "engine": {"displayName": "engine-friendly", "name":
            "projects/test-project/locations/us-central1/reasoningEngines/12345"},
        "console_url":
            "https://console.cloud.google.com/vertex-ai/agents/locations/us-central1/agent-engines/12345?project=test-project",
    }
    fake_admin_logging.errors["agent-1"] = {
        "severity": "ERROR",
        "timestamp": "2025-01-01T00:00:00Z",
        "textPayload": "boom: agent-1 failed",
    }
    _login(admin_client)
    response = admin_client.get("/admin/agents/agent-1")
    assert response.status_code == 200
    body = response.text
    assert "vsess-slack" in body
    assert "engine-friendly" in body
    assert "console.cloud.google.com/vertex-ai/agents/locations/us-central1/agent-engines/12345" in body
    assert "boom: agent-1 failed" in body


def test_agent_detail_unknown_agent_404(admin_client):
    _login(admin_client)
    response = admin_client.get("/admin/agents/ghost")
    assert response.status_code == 404


def test_agent_detail_handles_missing_vertex_and_no_error_gracefully(
    admin_client, fake_firestore
):
    _seed_agent(fake_firestore)
    _login(admin_client)
    response = admin_client.get("/admin/agents/agent-1")
    assert response.status_code == 200
    body = response.text
    assert "No recent errors" in body
    assert "Could not resolve Reasoning Engine" in body
