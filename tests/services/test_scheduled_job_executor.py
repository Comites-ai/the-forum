# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""ScheduledJobExecutorV2 tests against the fakes.

Focus: the reply-delivery decision — normal replies are delivered, empty
replies are failures, and [SILENT]-prefixed replies are recorded as
successes with nothing delivered.
"""
import pytest

from app.models.agent import Agent
from app.models.scheduled_job import ScheduledJob
from app.services.scheduled_job_executor_v2 import ScheduledJobExecutorV2, SILENT_SENTINEL
from app.services.vertex_ai_service import VertexAIResponse

from tests.fakes.fake_platform_connector import FakePlatformConnector


AGENT_ID = "agent-1"
VERTEX_AGENT_ID = "projects/p/locations/l/reasoningEngines/1"
USER_ID = "user-1"


class StubIdentityService:
    """Resolves every (user, platform) to a fixed recipient id."""

    def __init__(self, recipient_id: str = "U_RECIPIENT"):
        self.recipient_id = recipient_id

    async def get_platform_identity(self, user_id: str, platform: str):
        return self.recipient_id


@pytest.fixture
def fake_connector() -> FakePlatformConnector:
    return FakePlatformConnector(platform="slack")


@pytest.fixture
def executor(fake_firestore, fake_vertex_ai, fake_connector) -> ScheduledJobExecutorV2:
    ex = ScheduledJobExecutorV2(
        firestore=fake_firestore,
        vertex_ai=fake_vertex_ai,
        identity=StubIdentityService(),
    )

    async def _fake_create_connector(agent, platform):
        return fake_connector

    ex._create_connector = _fake_create_connector
    return ex


@pytest.fixture
def job_id(fake_firestore) -> str:
    fake_firestore.add_agent(
        Agent(vertex_ai_agent_id=VERTEX_AGENT_ID, display_name="Agent"),
        agent_id=AGENT_ID,
    )
    job = ScheduledJob(
        name="hourly check",
        prompt="Anything new?",
        agent_id=AGENT_ID,
        user_id=USER_ID,
        output_platform="slack",
        schedule="0 * * * *",
        timezone="UTC",
        enabled=True,
    )
    data = job.model_dump(exclude={"id"})
    fake_firestore.scheduled_jobs["job-1"] = data
    return "job-1"


def _job_doc(fake_firestore, job_id):
    return fake_firestore.scheduled_jobs[job_id]


async def test_normal_reply_is_delivered_and_marked_success(
    executor, fake_firestore, fake_vertex_ai, fake_connector, job_id
):
    fake_vertex_ai.set_text_response(VERTEX_AGENT_ID, "You ran 5 miles, nice work.")

    assert await executor.execute_job(job_id, execution_id="exec-1") is True

    assert len(fake_connector.sent_messages) == 1
    assert "You ran 5 miles" in fake_connector.sent_messages[0]["text"]
    assert "*Scheduled: hourly check*" in fake_connector.sent_messages[0]["text"]
    doc = _job_doc(fake_firestore, job_id)
    assert doc["consecutive_failures"] == 0
    assert doc["last_error"] is None


async def test_silent_reply_is_success_with_no_delivery(
    executor, fake_firestore, fake_vertex_ai, fake_connector, job_id
):
    fake_vertex_ai.set_text_response(
        VERTEX_AGENT_ID, f"{SILENT_SENTINEL} nothing new since last check"
    )

    assert await executor.execute_job(job_id, execution_id="exec-1") is True

    assert fake_connector.sent_messages == []
    doc = _job_doc(fake_firestore, job_id)
    assert doc["consecutive_failures"] == 0
    assert doc["last_error"] is None
    assert doc["last_execution_at"] is not None
    assert doc["execution_started_at"] is None  # lock released


async def test_silent_reply_clears_pending_retry(
    executor, fake_firestore, fake_vertex_ai, fake_connector, job_id
):
    from datetime import datetime, UTC

    doc = _job_doc(fake_firestore, job_id)
    doc["retry_at"] = datetime.now(UTC)
    doc["retry_reason"] = "rate_limit_429"
    fake_vertex_ai.set_text_response(VERTEX_AGENT_ID, SILENT_SENTINEL)

    assert await executor.execute_job(job_id, execution_id="exec-1") is True

    doc = _job_doc(fake_firestore, job_id)
    assert doc["retry_at"] is None
    assert doc["retry_reason"] is None
    assert fake_connector.sent_messages == []


async def test_sentinel_must_be_prefix_not_substring(
    executor, fake_firestore, fake_vertex_ai, fake_connector, job_id
):
    fake_vertex_ai.set_text_response(
        VERTEX_AGENT_ID, f"I stayed quiet — replying {SILENT_SENTINEL} — as instructed."
    )

    assert await executor.execute_job(job_id, execution_id="exec-1") is True

    # Sentinel mid-message does not suppress delivery.
    assert len(fake_connector.sent_messages) == 1


async def test_empty_reply_is_failure(
    executor, fake_firestore, fake_vertex_ai, fake_connector, job_id
):
    fake_vertex_ai.set_response(
        VERTEX_AGENT_ID, VertexAIResponse(text="", chunk_count=0)
    )

    assert await executor.execute_job(job_id, execution_id="exec-1") is True

    assert fake_connector.sent_messages == []
    doc = _job_doc(fake_firestore, job_id)
    assert doc["consecutive_failures"] == 1
    assert "Empty response" in doc["last_error"]


async def test_test_execute_job_honors_sentinel(
    executor, fake_firestore, fake_vertex_ai, fake_connector, job_id
):
    fake_vertex_ai.set_text_response(VERTEX_AGENT_ID, f"{SILENT_SENTINEL} all quiet")

    result = await executor.test_execute_job(job_id)

    assert result["success"] is True
    assert fake_connector.sent_messages == []
    assert "silently" in result["message"]


# ---------------------------------------------------------------------------
# Cut-off runs (PLAT-42)
# ---------------------------------------------------------------------------


async def test_a_scheduled_run_that_answers_clears_its_marker(
    executor, fake_firestore, fake_vertex_ai, fake_connector, job_id
):
    """Nobody watches a scheduled run, so its bookkeeping has to be its own."""
    fake_vertex_ai.set_text_response(VERTEX_AGENT_ID, "All quiet.")
    await executor.execute_job(job_id, execution_id="exec-1")

    session = next(iter(fake_firestore.sessions.values()))
    assert session["active_run"] is None


async def test_a_scheduled_run_that_ends_on_a_silent_tool_leaves_its_marker(
    executor, fake_firestore, fake_vertex_ai, fake_connector, job_id
):
    fake_vertex_ai.set_response(
        VERTEX_AGENT_ID,
        VertexAIResponse(
            text="",
            chunk_count=2,
            breakdown={"function_call": 1, "function_response": 0},
            function_names=["write_sheet"],
        ),
    )
    await executor.execute_job(job_id, execution_id="exec-1")

    session = next(iter(fake_firestore.sessions.values()))
    assert session["active_run"]["last_tool"] == "write_sheet"


# ---- Conversation log write points (PLAT-43) ----


async def test_scheduled_exchange_is_logged_with_the_job_as_author(
    executor, fake_firestore, fake_vertex_ai, fake_connector, job_id
):
    fake_vertex_ai.set_text_response(VERTEX_AGENT_ID, "Nothing new since last night.")

    assert await executor.execute_job(job_id, execution_id="exec-log") is True

    logged = fake_firestore.logged_messages(AGENT_ID, USER_ID)
    assert [m.direction for m in logged] == ["inbound", "outbound"]
    prompt, reply = logged
    assert prompt.kind == "scheduled"
    assert prompt.author == "hourly check"
    assert prompt.text == "Anything new?"
    assert prompt.platform == "slack"
    assert reply.kind == "scheduled"
    assert reply.author == "Agent"
    assert reply.text == fake_connector.sent_messages[-1]["text"]
    assert reply.text.startswith("*Scheduled: hourly check*")
    assert reply.platform_message_ids == ["fake-msg-1"]


async def test_silent_scheduled_reply_logs_only_the_prompt(
    executor, fake_firestore, fake_vertex_ai, job_id
):
    fake_vertex_ai.set_text_response(VERTEX_AGENT_ID, f"{SILENT_SENTINEL} nothing to say")

    assert await executor.execute_job(job_id, execution_id="exec-silent") is True

    logged = fake_firestore.logged_messages(AGENT_ID, USER_ID)
    assert [m.direction for m in logged] == ["inbound"]
