# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Sanity tests for FakeFirestoreService.

These exist so future tests can trust the fake. If something here breaks,
the fake's behavior has drifted from the real FirestoreService and the
service-layer tests are no longer meaningful.
"""
from app.models.agent import Agent, AgentPlatformConfig
from app.models.user import PlatformIdentity, User


# ---- Agent ----


async def test_get_agent_by_id_roundtrip(fake_firestore):
    agent = Agent(
        vertex_ai_agent_id="projects/x/locations/us-central1/reasoningEngines/abc",
        display_name="Test Agent",
    )
    agent_id = fake_firestore.add_agent(agent, agent_id="agent-123")
    fetched = await fake_firestore.get_agent_by_id(agent_id)
    assert fetched is not None
    assert fetched.id == "agent-123"
    assert fetched.display_name == "Test Agent"


async def test_get_agent_by_id_returns_none_when_missing(fake_firestore):
    assert await fake_firestore.get_agent_by_id("does-not-exist") is None


async def test_get_agent_by_bot_id_uses_platforms_list(fake_firestore):
    agent = Agent(
        vertex_ai_agent_id="re-1",
        display_name="Slack Agent",
        platforms=[
            AgentPlatformConfig(
                platform="slack",
                slack_bot_id="U_BOT_001",
                slack_bot_token="xoxb-test",
            )
        ],
    )
    fake_firestore.add_agent(agent, agent_id="agent-slack")
    fetched = await fake_firestore.get_agent_by_bot_id("U_BOT_001")
    assert fetched is not None
    assert fetched.id == "agent-slack"


# ---- User identity ----


async def test_create_user_then_lookup_by_identity(fake_firestore):
    user = User(
        primary_name="Alice",
        identities=[PlatformIdentity(platform="slack", platform_user_id="U_001")],
    )
    user_id = await fake_firestore.create_user(user)
    fetched = await fake_firestore.get_user_by_identity("slack", "U_001")
    assert fetched is not None
    assert fetched.id == user_id
    assert fetched.primary_name == "Alice"


async def test_get_user_by_identity_returns_none_for_unknown_platform(fake_firestore):
    user = User(
        primary_name="Alice",
        identities=[PlatformIdentity(platform="slack", platform_user_id="U_001")],
    )
    await fake_firestore.create_user(user)
    assert await fake_firestore.get_user_by_identity("google_chat", "U_001") is None


async def test_add_user_identity_appends(fake_firestore):
    user = User(
        primary_name="Alice",
        identities=[PlatformIdentity(platform="slack", platform_user_id="U_001")],
    )
    user_id = await fake_firestore.create_user(user)
    await fake_firestore.add_user_identity(
        user_id, PlatformIdentity(platform="google_chat", platform_user_id="users/abc")
    )
    fetched = await fake_firestore.get_user_by_id(user_id)
    assert fetched.has_platform("slack")
    assert fetched.has_platform("google_chat")


# ---- Sessions ----


async def test_session_roundtrip(fake_firestore):
    created = await fake_firestore.create_session_for_user(
        user_id="user-1", agent_id="agent-1", vertex_ai_session_id="vs-1", platform="slack"
    )
    assert created.id == "user-1_agent-1"
    fetched = await fake_firestore.get_session_by_user("user-1", "agent-1")
    assert fetched is not None
    assert fetched.vertex_ai_session_id == "vs-1"
    assert "slack" in fetched.platforms_used


async def test_update_session_platforms_adds_to_set(fake_firestore):
    await fake_firestore.create_session_for_user(
        user_id="user-1", agent_id="agent-1", vertex_ai_session_id="vs-1", platform="slack"
    )
    await fake_firestore.update_session_platforms("user-1_agent-1", "google_chat")
    fetched = await fake_firestore.get_session_by_user("user-1", "agent-1")
    assert set(fetched.platforms_used) == {"slack", "google_chat"}


async def test_session_expiry_comparison_does_not_raise(fake_firestore):
    """
    Regression guard for the utcnow→now(UTC) migration: the session
    freshness check (`datetime.now(UTC) > expiry_time`) must not raise
    TypeError, which it would if one side were naive and the other aware.
    """
    await fake_firestore.create_session_for_user(
        user_id="user-1", agent_id="agent-1", vertex_ai_session_id="vs-1", platform="slack"
    )
    fetched = await fake_firestore.get_session_by_user("user-1", "agent-1")
    assert fetched is not None
    assert fetched.vertex_ai_session_id == "vs-1"


# ---- Scheduled jobs ----


async def test_scheduled_job_create_and_get(fake_firestore):
    created = await fake_firestore.create_scheduled_job(
        {
            "name": "morning brief",
            "prompt": "summarize my calendar",
            "agent_id": "agent-1",
            "user_id": "user-1",
            "schedule": "0 9 * * 1-5",
            "timezone": "UTC",
            "output_platform": "slack",
            "enabled": True,
        }
    )
    assert created.id is not None
    fetched = await fake_firestore.get_scheduled_job(created.id)
    assert fetched.name == "morning brief"


async def test_list_scheduled_jobs_filters(fake_firestore):
    base = {
        "prompt": "x",
        "schedule": "* * * * *",
        "timezone": "UTC",
        "output_platform": "slack",
    }
    await fake_firestore.create_scheduled_job(
        {**base, "name": "a", "agent_id": "agent-1", "user_id": "user-1", "enabled": True}
    )
    await fake_firestore.create_scheduled_job(
        {**base, "name": "b", "agent_id": "agent-2", "user_id": "user-1", "enabled": False}
    )
    enabled = await fake_firestore.list_scheduled_jobs(enabled_only=True)
    assert len(enabled) == 1
    assert enabled[0].name == "a"
    by_user = await fake_firestore.list_scheduled_jobs(user_id="user-1")
    assert len(by_user) == 2


async def test_acquire_lock_blocks_second_caller(fake_firestore):
    job = await fake_firestore.create_scheduled_job(
        {
            "name": "x",
            "prompt": "p",
            "agent_id": "agent-1",
            "user_id": "user-1",
            "schedule": "* * * * *",
            "timezone": "UTC",
            "output_platform": "slack",
            "enabled": True,
        }
    )
    first = await fake_firestore.acquire_job_execution_lock(job.id, "exec-1")
    second = await fake_firestore.acquire_job_execution_lock(job.id, "exec-2")
    assert first is True
    assert second is False


async def test_release_lock_clears_started_at(fake_firestore):
    job = await fake_firestore.create_scheduled_job(
        {
            "name": "x",
            "prompt": "p",
            "agent_id": "agent-1",
            "user_id": "user-1",
            "schedule": "* * * * *",
            "timezone": "UTC",
            "output_platform": "slack",
            "enabled": True,
        }
    )
    await fake_firestore.acquire_job_execution_lock(job.id, "exec-1")
    await fake_firestore.release_job_execution_lock(job.id, success=True)
    refetched = await fake_firestore.get_scheduled_job(job.id)
    assert refetched.execution_started_at is None
    assert refetched.consecutive_failures == 0
