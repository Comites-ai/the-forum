# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Agents (A2A) MCP tool-handler tests.

Exercise the handlers directly by setting the per-request ContextVar (the
ASGI layer normally sets it after authenticating the X-API-Key). Cover:
listing excludes the caller, inquiry lookup, and query_agent's attribution
prefix / per-(caller,target,user) session reuse / validation errors.
"""
import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from app.api.v1 import agents_mcp
from app.models.agent import Agent, AgentInquiry
from app.models.user import PlatformIdentity, User
from app.services.run_tracker import CUT_OFF_TIMEOUT
from app.services.session_healer import HealOutcome

from tests.fakes.fake_vertex_ai import FakeVertexAIService


CALLER_VERTEX_ID = "re-caller"
TARGET_VERTEX_ID = "re-target"


@pytest.fixture
def caller(fake_firestore) -> Agent:
    a = Agent(
        vertex_ai_agent_id=CALLER_VERTEX_ID,
        display_name="Nora the Nutritionist",
        id="agent-nora",
    )
    fake_firestore.add_agent(a, agent_id="agent-nora")
    return a


@pytest.fixture
def target(fake_firestore) -> Agent:
    a = Agent(
        vertex_ai_agent_id=TARGET_VERTEX_ID,
        display_name="Mickey Marathon",
        description="Marathon coach.",
        inquiries=[
            AgentInquiry(
                name="planned_workouts_today",
                description="Today's planned workout with purpose and estimated calories.",
                request_format="AGENT_QUERY: planned_workouts_today",
                response_format="PLANNED_WORKOUTS <date>: ...",
            ),
        ],
        id="agent-mickey",
    )
    fake_firestore.add_agent(a, agent_id="agent-mickey")
    return a


@pytest.fixture
def fake_vertex() -> FakeVertexAIService:
    return FakeVertexAIService()


@pytest.fixture
def request_ctx(fake_firestore, fake_vertex, caller, target):
    """Bind the authenticated request context the handlers read from."""
    token = agents_mcp._request_ctx.set(
        {"agent": caller, "firestore": fake_firestore, "vertex_ai": fake_vertex}
    )
    yield
    agents_mcp._request_ctx.reset(token)


async def _seed_user(fake_firestore) -> str:
    return await fake_firestore.create_user(
        User(
            primary_name="Jonathan Cavell",
            identities=[
                PlatformIdentity(
                    platform="discord", platform_user_id="D_001", display_name="jonathan"
                )
            ],
        )
    )


# ---- list_agents ----


async def test_list_agents_excludes_caller(fake_firestore, request_ctx):
    listed = json.loads(await agents_mcp._handle_list_agents({}))
    assert [a["display_name"] for a in listed] == ["Mickey Marathon"]
    assert listed[0]["inquiries"] == ["planned_workouts_today"]


# ---- get_agent_inquiries ----


async def test_get_inquiries_returns_full_records(fake_firestore, request_ctx):
    result = json.loads(
        await agents_mcp._handle_get_inquiries({"agent_name": "Mickey Marathon"})
    )
    assert result["agent"] == "Mickey Marathon"
    assert result["inquiries"][0]["request_format"] == "AGENT_QUERY: planned_workouts_today"


async def test_get_inquiries_unknown_agent_raises(fake_firestore, request_ctx):
    with pytest.raises(ValueError, match="No agent found"):
        await agents_mcp._handle_get_inquiries({"agent_name": "Ghost"})


async def test_get_inquiries_is_case_insensitive(fake_firestore, request_ctx):
    result = json.loads(
        await agents_mcp._handle_get_inquiries({"agent_name": "mickey marathon"})
    )
    assert result["agent"] == "Mickey Marathon"


# ---- query_agent ----


async def test_query_agent_prefixes_attribution(fake_firestore, fake_vertex, request_ctx):
    await _seed_user(fake_firestore)
    fake_vertex.set_text_response(TARGET_VERTEX_ID, "PLANNED_WORKOUTS 2026-07-12: rest day")

    result = json.loads(
        await agents_mcp._handle_query_agent({
            "agent_name": "Mickey Marathon",
            "message": "AGENT_QUERY: planned_workouts_today",
            "on_behalf_of": "Jonathan Cavell",
        })
    )

    assert result["reply"] == "PLANNED_WORKOUTS 2026-07-12: rest day"
    assert result["on_behalf_of"] == "Jonathan Cavell"
    sent = fake_vertex.messages_sent[-1]
    assert sent["message"].startswith(
        "[From Agent: Nora the Nutritionist | On Behalf Of: Jonathan Cavell] "
    )
    assert sent["agent_id"] == TARGET_VERTEX_ID


async def test_query_agent_reuses_session_per_user(fake_firestore, fake_vertex, request_ctx):
    await _seed_user(fake_firestore)
    fake_vertex.set_text_response(TARGET_VERTEX_ID, "ok")

    args = {
        "agent_name": "Mickey Marathon",
        "message": "hello",
        "on_behalf_of": "Jonathan Cavell",
    }
    await agents_mcp._handle_query_agent(args)
    await agents_mcp._handle_query_agent(args)

    assert len(fake_vertex.sessions_created) == 1
    assert (
        fake_vertex.messages_sent[0]["session_id"]
        == fake_vertex.messages_sent[1]["session_id"]
    )


async def test_query_agent_separate_sessions_per_user(fake_firestore, fake_vertex, request_ctx):
    await _seed_user(fake_firestore)
    await fake_firestore.create_user(User(primary_name="Nicole Cavell", identities=[]))
    fake_vertex.set_text_response(TARGET_VERTEX_ID, "ok")

    await agents_mcp._handle_query_agent({
        "agent_name": "Mickey Marathon",
        "message": "hello",
        "on_behalf_of": "Jonathan Cavell",
    })
    await agents_mcp._handle_query_agent({
        "agent_name": "Mickey Marathon",
        "message": "hello",
        "on_behalf_of": "Nicole Cavell",
    })

    assert len(fake_vertex.sessions_created) == 2
    assert (
        fake_vertex.messages_sent[0]["session_id"]
        != fake_vertex.messages_sent[1]["session_id"]
    )


async def test_query_agent_recreates_session_when_engine_changed(
    fake_firestore, fake_vertex, request_ctx
):
    """A cached session from before the target's redeploy is dead; the
    lookup must detect the engine change and mint a fresh session (#18)."""
    user_id = await _seed_user(fake_firestore)
    fake_vertex.set_text_response(TARGET_VERTEX_ID, "ok")
    key = agents_mcp._a2a_session_key("agent-nora", "agent-mickey", user_id)
    fake_firestore.a2a_sessions[key] = {
        "vertex_ai_session_id": "u:dead-session-on-old-engine",
        "engine_id": "re-target-OLD",
    }

    await agents_mcp._handle_query_agent({
        "agent_name": "Mickey Marathon",
        "message": "hello",
        "on_behalf_of": "Jonathan Cavell",
    })

    assert len(fake_vertex.sessions_created) == 1
    assert fake_vertex.messages_sent[0]["session_id"] != "u:dead-session-on-old-engine"
    assert fake_firestore.a2a_sessions[key]["engine_id"] == TARGET_VERTEX_ID


async def test_query_agent_treats_legacy_entry_without_engine_as_stale(
    fake_firestore, fake_vertex, request_ctx
):
    user_id = await _seed_user(fake_firestore)
    fake_vertex.set_text_response(TARGET_VERTEX_ID, "ok")
    key = agents_mcp._a2a_session_key("agent-nora", "agent-mickey", user_id)
    fake_firestore.a2a_sessions[key] = {"vertex_ai_session_id": "u:legacy-session"}

    await agents_mcp._handle_query_agent({
        "agent_name": "Mickey Marathon",
        "message": "hello",
        "on_behalf_of": "Jonathan Cavell",
    })

    assert len(fake_vertex.sessions_created) == 1
    assert fake_vertex.messages_sent[0]["session_id"] != "u:legacy-session"


async def test_query_agent_retries_once_on_empty_reply_from_cached_session(
    fake_firestore, fake_vertex, request_ctx
):
    """An empty reply on a cached session is what a server-side-dead session
    looks like (SessionNotFound dies mid-stream as 0 chunks). The handler
    must drop the session and retry once on a fresh one (#18)."""
    user_id = await _seed_user(fake_firestore)
    from app.services.vertex_ai_service import VertexAIResponse

    key = agents_mcp._a2a_session_key("agent-nora", "agent-mickey", user_id)
    fake_firestore.a2a_sessions[key] = {
        "vertex_ai_session_id": "u:dead-but-engine-matches",
        "engine_id": TARGET_VERTEX_ID,
    }
    fake_vertex.queue_response(TARGET_VERTEX_ID, VertexAIResponse(text="", chunk_count=0))
    fake_vertex.queue_response(TARGET_VERTEX_ID, VertexAIResponse(text="recovered", chunk_count=1))

    result = json.loads(
        await agents_mcp._handle_query_agent({
            "agent_name": "Mickey Marathon",
            "message": "hello",
            "on_behalf_of": "Jonathan Cavell",
        })
    )

    assert result["reply"] == "recovered"
    assert len(fake_vertex.messages_sent) == 2
    assert fake_vertex.messages_sent[0]["session_id"] == "u:dead-but-engine-matches"
    assert fake_vertex.messages_sent[1]["session_id"] != "u:dead-but-engine-matches"
    assert len(fake_vertex.sessions_created) == 1
    assert (
        fake_firestore.a2a_sessions[key]["vertex_ai_session_id"]
        == fake_vertex.sessions_created[0]["session_id"]
    )


async def test_query_agent_empty_reply_on_fresh_session_does_not_retry(
    fake_firestore, fake_vertex, request_ctx
):
    """No cached session -> the empty reply is not a dead-session symptom;
    fail loudly without a second call (no infinite fresh-session loops)."""
    await _seed_user(fake_firestore)
    from app.services.vertex_ai_service import VertexAIResponse

    fake_vertex.set_response(TARGET_VERTEX_ID, VertexAIResponse(text="", chunk_count=0))

    with pytest.raises(ValueError, match="empty reply"):
        await agents_mcp._handle_query_agent({
            "agent_name": "Mickey Marathon",
            "message": "hello",
            "on_behalf_of": "Jonathan Cavell",
        })

    assert len(fake_vertex.messages_sent) == 1


async def test_query_agent_requires_known_user(fake_firestore, request_ctx):
    with pytest.raises(ValueError, match="No user found"):
        await agents_mcp._handle_query_agent({
            "agent_name": "Mickey Marathon",
            "message": "hello",
            "on_behalf_of": "Nobody",
        })


async def test_query_agent_requires_on_behalf_of(fake_firestore, request_ctx):
    with pytest.raises(ValueError, match="on_behalf_of is required"):
        await agents_mcp._handle_query_agent({
            "agent_name": "Mickey Marathon",
            "message": "hello",
        })


async def test_query_agent_rejects_self(fake_firestore, request_ctx):
    await _seed_user(fake_firestore)
    with pytest.raises(ValueError, match="cannot query yourself"):
        await agents_mcp._handle_query_agent({
            "agent_name": "Nora the Nutritionist",
            "message": "hello",
            "on_behalf_of": "Jonathan Cavell",
        })


async def test_query_agent_empty_reply_raises(fake_firestore, fake_vertex, request_ctx):
    await _seed_user(fake_firestore)
    from app.services.vertex_ai_service import VertexAIResponse
    fake_vertex.set_response(TARGET_VERTEX_ID, VertexAIResponse(text="", chunk_count=0))

    with pytest.raises(ValueError, match="empty reply"):
        await agents_mcp._handle_query_agent({
            "agent_name": "Mickey Marathon",
            "message": "hello",
            "on_behalf_of": "Jonathan Cavell",
        })


# ---------------------------------------------------------------------------
# Cut-off runs (PLAT-42)
# ---------------------------------------------------------------------------


async def test_query_agent_marks_and_clears_the_run(
    fake_firestore, fake_vertex, request_ctx
):
    await _seed_user(fake_firestore)
    fake_vertex.set_text_response(TARGET_VERTEX_ID, "ok")

    await agents_mcp._handle_query_agent({
        "agent_name": "Mickey Marathon",
        "message": "hello",
        "on_behalf_of": "Jonathan Cavell",
    })

    entry = next(iter(fake_firestore.a2a_sessions.values()))
    assert entry["active_run"] is None


async def test_query_agent_timeout_leaves_a_marker_naming_the_timeout(
    monkeypatch, fake_firestore, fake_vertex, request_ctx
):
    """The Forum stopped listening; the engine may still be mid-tool."""
    await _seed_user(fake_firestore)

    async def _never_answers(*args, **kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(fake_vertex, "send_message", _never_answers)

    with pytest.raises(ValueError, match="did not reply within"):
        await agents_mcp._handle_query_agent({
            "agent_name": "Mickey Marathon",
            "message": "hello",
            "on_behalf_of": "Jonathan Cavell",
        })

    entry = next(iter(fake_firestore.a2a_sessions.values()))
    assert entry["active_run"]["reason"] == CUT_OFF_TIMEOUT


async def test_query_agent_heals_a_session_a_cut_off_run_left_behind(
    monkeypatch, fake_firestore, fake_vertex, request_ctx
):
    await _seed_user(fake_firestore)
    fake_vertex.set_text_response(TARGET_VERTEX_ID, "ok")

    args = {
        "agent_name": "Mickey Marathon",
        "message": "hello",
        "on_behalf_of": "Jonathan Cavell",
    }
    await agents_mcp._handle_query_agent(args)

    # Strand the session: an in-flight marker, aged past the grace period.
    key, entry = next(iter(fake_firestore.a2a_sessions.items()))
    entry["active_run"] = {
        "started_at": datetime.now(UTC) - timedelta(seconds=600),
        "reason": CUT_OFF_TIMEOUT,
    }

    calls = []

    class _Healer:
        async def heal(self, **kwargs):
            calls.append(kwargs)
            return HealOutcome(healed=True, reason="healed")

    monkeypatch.setattr(agents_mcp, "get_session_healer", lambda: _Healer())

    await agents_mcp._handle_query_agent(args)

    assert len(calls) == 1
    assert calls[0]["agent_id"] == TARGET_VERTEX_ID
    assert calls[0]["session_id"] == entry["vertex_ai_session_id"]
    assert calls[0]["reason"] == CUT_OFF_TIMEOUT
