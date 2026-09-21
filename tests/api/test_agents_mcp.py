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
import logging
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
        "started_at": datetime.now(UTC) - timedelta(seconds=900),
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


# ---------------------------------------------------------------------------
# The query ceiling and its instrumentation (#26)
# ---------------------------------------------------------------------------


def _a2a_query_rows(caplog) -> list[dict]:
    return [
        r.json_fields
        for r in caplog.records
        if getattr(r, "json_fields", {}).get("event") == "a2a_query"
    ]


def test_query_timeout_stays_under_cloud_runs_request_timeout():
    """query_agent runs inside an inbound Cloud Run request. Past Cloud Run's
    own 300s the request is killed outright, the TimeoutError branch never
    runs, and the caller gets nothing it can act on — so the ceiling must
    leave real margin under it."""
    assert agents_mcp.QUERY_TIMEOUT_SECONDS <= 240


async def test_query_agent_logs_duration_and_outcome(
    caplog, fake_firestore, fake_vertex, request_ctx
):
    """The `a2a_query` field names are a log-query contract; they should not
    drift. This is the only place a real call duration is recorded."""
    await _seed_user(fake_firestore)
    fake_vertex.set_text_response(TARGET_VERTEX_ID, "ok")

    with caplog.at_level(logging.INFO, logger="app.api.v1.agents_mcp"):
        await agents_mcp._handle_query_agent({
            "agent_name": "Mickey Marathon",
            "message": "hello",
            "on_behalf_of": "Jonathan Cavell",
        })

    rows = _a2a_query_rows(caplog)
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == agents_mcp.QUERY_REPLIED
    assert row["agent_id"] == TARGET_VERTEX_ID
    assert row["caller_agent_id"] == CALLER_VERTEX_ID
    assert row["caller"] == "Nora the Nutritionist"
    assert row["target"] == "Mickey Marathon"
    assert row["platform"] == "a2a"
    assert row["chunk_count"] == 1
    assert isinstance(row["duration_seconds"], float)
    # Logged on every row: a sample of completed calls is censored at
    # whatever ceiling was in force, so the two only mean anything together.
    assert row["timeout_seconds"] == agents_mcp.QUERY_TIMEOUT_SECONDS


async def test_query_agent_logs_a_timed_out_outcome(
    caplog, monkeypatch, fake_firestore, fake_vertex, request_ctx
):
    await _seed_user(fake_firestore)

    async def _never_answers(*args, **kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(fake_vertex, "send_message", _never_answers)

    with caplog.at_level(logging.INFO, logger="app.api.v1.agents_mcp"):
        with pytest.raises(ValueError):
            await agents_mcp._handle_query_agent({
                "agent_name": "Mickey Marathon",
                "message": "hello",
                "on_behalf_of": "Jonathan Cavell",
            })

    rows = _a2a_query_rows(caplog)
    assert [r["outcome"] for r in rows] == [agents_mcp.QUERY_TIMED_OUT]


async def test_query_agent_logs_every_engine_call_not_just_the_last(
    caplog, fake_firestore, fake_vertex, request_ctx
):
    """The empty-reply retry makes two engine calls. Both get a row, so the
    timed-out share is a fraction of every call rather than of turns."""
    user_id = await _seed_user(fake_firestore)
    from app.services.vertex_ai_service import VertexAIResponse

    key = agents_mcp._a2a_session_key("agent-nora", "agent-mickey", user_id)
    fake_firestore.a2a_sessions[key] = {
        "vertex_ai_session_id": "u:dead-but-engine-matches",
        "engine_id": TARGET_VERTEX_ID,
    }
    fake_vertex.queue_response(TARGET_VERTEX_ID, VertexAIResponse(text="", chunk_count=0))
    fake_vertex.queue_response(TARGET_VERTEX_ID, VertexAIResponse(text="recovered", chunk_count=1))

    with caplog.at_level(logging.INFO, logger="app.api.v1.agents_mcp"):
        await agents_mcp._handle_query_agent({
            "agent_name": "Mickey Marathon",
            "message": "hello",
            "on_behalf_of": "Jonathan Cavell",
        })

    rows = _a2a_query_rows(caplog)
    assert [r["outcome"] for r in rows] == [
        agents_mcp.QUERY_EMPTY_REPLY,
        agents_mcp.QUERY_REPLIED,
    ]


async def test_query_agent_logs_a_failed_outcome(
    caplog, monkeypatch, fake_firestore, fake_vertex, request_ctx
):
    await _seed_user(fake_firestore)

    async def _breaks(*args, **kwargs):
        raise RuntimeError("stream broke")

    monkeypatch.setattr(fake_vertex, "send_message", _breaks)

    with caplog.at_level(logging.INFO, logger="app.api.v1.agents_mcp"):
        with pytest.raises(RuntimeError):
            await agents_mcp._handle_query_agent({
                "agent_name": "Mickey Marathon",
                "message": "hello",
                "on_behalf_of": "Jonathan Cavell",
            })

    rows = _a2a_query_rows(caplog)
    assert [r["outcome"] for r in rows] == [agents_mcp.QUERY_FAILED]


async def test_timeout_message_warns_the_work_may_already_have_landed(
    monkeypatch, fake_firestore, fake_vertex, request_ctx
):
    """This text lands in a calling agent's conversation history. "Try again
    later" invited a blind retry of a request whose side effects may already
    have committed — which is how Linear issues got moved twice."""
    await _seed_user(fake_firestore)

    async def _never_answers(*args, **kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(fake_vertex, "send_message", _never_answers)

    with pytest.raises(ValueError) as excinfo:
        await agents_mcp._handle_query_agent({
            "agent_name": "Mickey Marathon",
            "message": "hello",
            "on_behalf_of": "Jonathan Cavell",
        })

    text = str(excinfo.value)
    assert "Try again later" not in text
    assert "may already have happened" in text
    assert str(agents_mcp.QUERY_TIMEOUT_SECONDS) in text


# ---- delivery receipts (PLAT-51 / #28) ----


def _receipt_key(user_id: str) -> str:
    return agents_mcp._a2a_session_key("agent-nora", "agent-mickey", user_id)


async def _drain_background() -> None:
    """Let the receipt's fire-and-forget writes land."""
    while agents_mcp._background_tasks:
        await asyncio.gather(*list(agents_mcp._background_tasks), return_exceptions=True)


async def _query(message: str = "hello") -> dict:
    return json.loads(
        await agents_mcp._handle_query_agent({
            "agent_name": "Mickey Marathon",
            "message": message,
            "on_behalf_of": "Jonathan Cavell",
        })
    )


async def test_query_agent_returns_a_receipt_and_records_the_reply(
    fake_firestore, fake_vertex, request_ctx
):
    from app.models.a2a_query import decode_query_id

    user_id = await _seed_user(fake_firestore)
    fake_vertex.set_text_response(TARGET_VERTEX_ID, "PLANNED_WORKOUTS: rest day")

    result = await _query("AGENT_QUERY: planned_workouts_today")
    await _drain_background()

    records = fake_firestore.a2a_query_records(_receipt_key(user_id))
    assert len(records) == 1
    record = records[0]
    assert decode_query_id(result["query_id"]) == (
        "agent-nora", "agent-mickey", user_id, record.id
    )
    assert record.status == "replied"
    assert record.reply == "PLANNED_WORKOUTS: rest day"
    assert record.attempts == 1
    # The message as the caller wrote it, not the prefixed form the engine saw.
    assert record.message == "AGENT_QUERY: planned_workouts_today"
    assert record.caller == "Nora the Nutritionist"
    assert record.on_behalf_of == "Jonathan Cavell"
    # The engine's first chunk is the proof of delivery.
    assert record.delivered_at is not None
    assert record.finished_at is not None
    assert record.expires_at == record.sent_at + timedelta(days=7)


async def test_get_query_status_by_id_returns_the_reply(
    fake_firestore, fake_vertex, request_ctx
):
    await _seed_user(fake_firestore)
    fake_vertex.set_text_response(TARGET_VERTEX_ID, "done")
    result = await _query()
    await _drain_background()

    status = json.loads(
        await agents_mcp._handle_get_query_status({"query_id": result["query_id"]})
    )["query"]

    assert status["query_id"] == result["query_id"]
    assert status["status"] == "replied"
    assert status["delivered"] is True
    assert status["reply"] == "done"
    assert status["agent"] == "Mickey Marathon"
    assert status["on_behalf_of"] == "Jonathan Cavell"
    assert "answered" in status["meaning"]


async def test_get_query_status_lists_recent_queries_newest_first(
    fake_firestore, fake_vertex, request_ctx
):
    """The caller whose connection dropped has no id in hand. Listing by
    target and user is how it finds the call it made."""
    await _seed_user(fake_firestore)
    fake_vertex.set_text_response(TARGET_VERTEX_ID, "ok")
    await _query("first")
    await _query("second")
    await _drain_background()

    listing = json.loads(
        await agents_mcp._handle_get_query_status({
            "agent_name": "Mickey Marathon",
            "on_behalf_of": "Jonathan Cavell",
        })
    )

    assert [q["message"] for q in listing["queries"]] == ["second", "first"]
    assert all(q["status"] == "replied" for q in listing["queries"])
    assert "never reached" in listing["note"]

    limited = json.loads(
        await agents_mcp._handle_get_query_status({
            "agent_name": "Mickey Marathon",
            "on_behalf_of": "Jonathan Cavell",
            "limit": 1,
        })
    )
    assert [q["message"] for q in limited["queries"]] == ["second"]


async def test_get_query_status_with_nothing_recorded_says_nothing_was_delivered(
    fake_firestore, request_ctx
):
    await _seed_user(fake_firestore)

    listing = json.loads(
        await agents_mcp._handle_get_query_status({
            "agent_name": "Mickey Marathon",
            "on_behalf_of": "Jonathan Cavell",
        })
    )

    assert listing["queries"] == []
    assert "nothing was delivered" in listing["note"]


async def test_get_query_status_rejects_another_agents_receipt(
    fake_firestore, request_ctx
):
    from app.models.a2a_query import encode_query_id

    user_id = await _seed_user(fake_firestore)
    foreign = encode_query_id("agent-someone-else", "agent-mickey", user_id, "q-1")

    with pytest.raises(ValueError, match="not one of your queries"):
        await agents_mcp._handle_get_query_status({"query_id": foreign})


async def test_get_query_status_unknown_id_means_not_delivered(
    fake_firestore, request_ctx
):
    from app.models.a2a_query import encode_query_id

    user_id = await _seed_user(fake_firestore)
    unknown = encode_query_id("agent-nora", "agent-mickey", user_id, "q-never")

    with pytest.raises(ValueError, match="not delivered"):
        await agents_mcp._handle_get_query_status({"query_id": unknown})


async def test_get_query_status_rejects_garbage_and_missing_arguments(
    fake_firestore, request_ctx
):
    with pytest.raises(ValueError, match="not a query id"):
        await agents_mcp._handle_get_query_status({"query_id": "nonsense"})
    with pytest.raises(ValueError, match="Pass either query_id"):
        await agents_mcp._handle_get_query_status({})
    with pytest.raises(ValueError, match="Pass either query_id"):
        await agents_mcp._handle_get_query_status({"agent_name": "Mickey Marathon"})


async def test_timeout_error_carries_the_receipt_and_the_late_reply_is_stored(
    monkeypatch, fake_firestore, fake_vertex, request_ctx
):
    """The Forum stops waiting, but the engine call keeps running. When it
    finishes, the reply the caller missed is stored on the receipt and the
    cut-off marker is cleared, because the run did in fact finish."""
    from app.services.vertex_ai_service import VertexAIResponse

    user_id = await _seed_user(fake_firestore)

    async def _slow(*args, on_first_chunk=None, **kwargs):
        await asyncio.sleep(0.05)
        if on_first_chunk:
            on_first_chunk()
        return VertexAIResponse(text="late answer", chunk_count=2)

    monkeypatch.setattr(fake_vertex, "send_message", _slow)
    monkeypatch.setattr(agents_mcp, "QUERY_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(ValueError) as excinfo:
        await _query()

    key = _receipt_key(user_id)
    record = fake_firestore.a2a_query_records(key)[0]
    assert record.query_id in str(excinfo.value)
    assert "get_query_status" in str(excinfo.value)
    assert record.status == "timed_out"
    assert fake_firestore.a2a_sessions[key]["active_run"]["reason"] == CUT_OFF_TIMEOUT

    await asyncio.sleep(0.1)
    await _drain_background()

    record = fake_firestore.a2a_query_records(key)[0]
    assert record.status == "replied"
    assert record.reply == "late answer"
    assert record.delivered_at is not None
    assert fake_firestore.a2a_sessions[key]["active_run"] is None

    status = json.loads(
        await agents_mcp._handle_get_query_status({"query_id": record.query_id})
    )["query"]
    assert status["reply"] == "late answer"


async def test_timed_out_status_explains_the_work_may_have_landed(
    monkeypatch, fake_firestore, fake_vertex, request_ctx
):
    user_id = await _seed_user(fake_firestore)

    async def _never_answers(*args, **kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(fake_vertex, "send_message", _never_answers)
    with pytest.raises(ValueError):
        await _query()
    await _drain_background()

    record = fake_firestore.a2a_query_records(_receipt_key(user_id))[0]
    status = json.loads(
        await agents_mcp._handle_get_query_status({"query_id": record.query_id})
    )["query"]
    assert status["status"] == "timed_out"
    assert status["delivered"] is False
    assert "rather than resending" in status["meaning"]
    assert "reply" not in status


async def test_empty_reply_with_chunks_is_not_retried_and_says_delivered(
    fake_firestore, fake_vertex, request_ctx
):
    """On 2026-09-18 Mickey ran Maggie's relay (tool calls, no final text)
    and the Forum resent it on a fresh session, so she got it four times.
    Chunks mean the engine ran; only the zero-chunk dead-session signature
    justifies a retry (#28)."""
    from app.services.vertex_ai_service import VertexAIResponse

    user_id = await _seed_user(fake_firestore)
    key = _receipt_key(user_id)
    fake_firestore.a2a_sessions[key] = {
        "vertex_ai_session_id": "u:live",
        "engine_id": TARGET_VERTEX_ID,
    }
    fake_vertex.queue_response(TARGET_VERTEX_ID, VertexAIResponse(text="", chunk_count=6))
    fake_vertex.queue_response(TARGET_VERTEX_ID, VertexAIResponse(text="never sent", chunk_count=1))

    with pytest.raises(ValueError) as excinfo:
        await _query()
    await _drain_background()

    assert len(fake_vertex.messages_sent) == 1
    text = str(excinfo.value)
    assert "ran its turn" in text
    assert "rather than sending the same request again" in text
    record = fake_firestore.a2a_query_records(key)[0]
    assert record.query_id in text
    assert record.status == "empty_reply"
    assert record.delivered_at is not None
    assert "6 chunks" in record.error


async def test_dead_session_retry_is_one_receipt_with_two_attempts(
    fake_firestore, fake_vertex, request_ctx
):
    from app.services.vertex_ai_service import VertexAIResponse

    user_id = await _seed_user(fake_firestore)
    key = _receipt_key(user_id)
    fake_firestore.a2a_sessions[key] = {
        "vertex_ai_session_id": "u:dead",
        "engine_id": TARGET_VERTEX_ID,
    }
    fake_vertex.queue_response(TARGET_VERTEX_ID, VertexAIResponse(text="", chunk_count=0))
    fake_vertex.queue_response(TARGET_VERTEX_ID, VertexAIResponse(text="recovered", chunk_count=1))

    result = await _query()
    await _drain_background()

    assert result["reply"] == "recovered"
    records = fake_firestore.a2a_query_records(key)
    assert len(records) == 1
    assert records[0].attempts == 2
    assert records[0].status == "replied"
    assert records[0].session_id == fake_vertex.sessions_created[0]["session_id"]


async def test_retry_is_skipped_when_the_request_budget_is_spent(
    monkeypatch, caplog, fake_firestore, fake_vertex, request_ctx
):
    """Two full attempts used to be able to run 480s inside a 300s Cloud Run
    request; the caller then got a 504 and no Forum error text (#28)."""
    from app.services.vertex_ai_service import VertexAIResponse

    user_id = await _seed_user(fake_firestore)
    key = _receipt_key(user_id)
    fake_firestore.a2a_sessions[key] = {
        "vertex_ai_session_id": "u:dead",
        "engine_id": TARGET_VERTEX_ID,
    }
    fake_vertex.queue_response(TARGET_VERTEX_ID, VertexAIResponse(text="", chunk_count=0))
    fake_vertex.queue_response(TARGET_VERTEX_ID, VertexAIResponse(text="never sent", chunk_count=1))
    monkeypatch.setattr(
        agents_mcp, "REQUEST_BUDGET_SECONDS", agents_mcp.MIN_RETRY_SECONDS - 1
    )

    with caplog.at_level(logging.WARNING, logger="app.api.v1.agents_mcp"):
        with pytest.raises(ValueError, match="empty reply"):
            await _query()

    assert len(fake_vertex.messages_sent) == 1
    assert "not retrying" in caplog.text


async def test_attempt_timeout_is_capped_by_the_remaining_budget(
    monkeypatch, fake_firestore, fake_vertex, request_ctx
):
    user_id = await _seed_user(fake_firestore)
    fake_vertex.set_text_response(TARGET_VERTEX_ID, "ok")
    monkeypatch.setattr(agents_mcp, "REQUEST_BUDGET_SECONDS", 100)

    await _query()
    await _drain_background()

    record = fake_firestore.a2a_query_records(_receipt_key(user_id))[0]
    assert 99 < record.timeout_seconds <= 100


def test_request_budget_stays_under_cloud_runs_request_timeout():
    assert agents_mcp.REQUEST_BUDGET_SECONDS < 300
    assert agents_mcp.QUERY_TIMEOUT_SECONDS <= agents_mcp.REQUEST_BUDGET_SECONDS


async def test_a_lost_receipt_never_fails_the_relay(
    monkeypatch, fake_firestore, fake_vertex, request_ctx
):
    await _seed_user(fake_firestore)
    fake_firestore.a2a_query_error = RuntimeError("firestore is having a moment")
    fake_vertex.set_text_response(TARGET_VERTEX_ID, "still works")

    result = await _query()
    assert result["reply"] == "still works"
    assert result["query_id"] is None

    async def _never_answers(*args, **kwargs):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(fake_vertex, "send_message", _never_answers)
    with pytest.raises(ValueError, match="No delivery receipt could be written"):
        await _query()


def test_get_query_status_is_a_published_tool():
    assert "get_query_status" in [t.name for t in agents_mcp.TOOLS]
    query_tool = next(t for t in agents_mcp.TOOLS if t.name == "query_agent")
    assert "query_id" in query_tool.description
