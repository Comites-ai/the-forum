# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""MessageProcessorV2's handling of runs that never came back (PLAT-42)."""
from datetime import UTC, datetime, timedelta

import pytest

from app.core.exceptions import AgentStreamError
from app.models.agent import Agent, AgentPlatformConfig
from app.schemas.platform_event import PlatformEvent
from app.services.identity_service import IdentityService
from app.services.message_processor_v2 import (
    ERR_STREAM_BROKEN,
    MessageProcessorV2,
)
from app.services.run_tracker import CUT_OFF_STREAM_BROKE
from app.services.session_healer import HealOutcome
from app.services.vertex_ai_service import VertexAIResponse

ENGINE = "projects/x/locations/us-central1/reasoningEngines/abc"


def _slack_event(text: str = "hello") -> PlatformEvent:
    return PlatformEvent(
        platform="slack",
        user_id="U_USER_001",
        message_text=text,
        space_id="C_CHANNEL_001",
        files=[],
        sent_at=None,
        media_group_id=None,
        raw_event={},
    )


class RecordingHealer:
    """A healer that says yes and remembers being asked."""

    def __init__(self, outcome: HealOutcome | None = None):
        self.calls: list[dict] = []
        self.outcome = outcome or HealOutcome(healed=True, reason="healed")

    async def heal(self, *, agent_id, session_id, reason, elapsed_seconds=None):
        self.calls.append(
            {
                "agent_id": agent_id,
                "session_id": session_id,
                "reason": reason,
                "elapsed_seconds": elapsed_seconds,
            }
        )
        return self.outcome


@pytest.fixture
def seeded_agent(fake_firestore) -> str:
    agent = Agent(
        vertex_ai_agent_id=ENGINE,
        display_name="Test Agent",
        platforms=[
            AgentPlatformConfig(
                platform="slack", slack_bot_id="U_BOT_001", slack_bot_token="xoxb-test"
            )
        ],
    )
    return fake_firestore.add_agent(agent, agent_id="agent-1")


@pytest.fixture
def healer() -> RecordingHealer:
    return RecordingHealer()


@pytest.fixture
def processor(fake_firestore, fake_vertex_ai, healer) -> MessageProcessorV2:
    return MessageProcessorV2(
        firestore=fake_firestore,
        vertex_ai=fake_vertex_ai,
        identity=IdentityService(firestore_service=fake_firestore),
        gcs=None,
        healer=healer,
    )


def _session_doc(fake_firestore) -> dict:
    assert len(fake_firestore.sessions) == 1
    return next(iter(fake_firestore.sessions.values()))


async def _turn(processor, fake_connector, seeded_agent, text="hello"):
    await processor.process_platform_event(
        event=_slack_event(text), connector=fake_connector, agent_id=seeded_agent
    )


# ---------------------------------------------------------------------------
# Marking
# ---------------------------------------------------------------------------


async def test_a_turn_that_answers_clears_its_marker(
    processor, fake_firestore, fake_vertex_ai, fake_connector, seeded_agent
):
    fake_vertex_ai.set_text_response(ENGINE, "Here you go")
    await _turn(processor, fake_connector, seeded_agent)
    assert _session_doc(fake_firestore)["active_run"] is None


async def test_a_turn_that_ends_on_a_silent_tool_leaves_its_marker(
    processor, fake_firestore, fake_vertex_ai, fake_connector, seeded_agent
):
    """This is the CHI-12 shape: a call went out, nothing came back."""
    fake_vertex_ai.set_response(
        ENGINE,
        VertexAIResponse(
            text="",
            chunk_count=3,
            breakdown={"function_call": 1, "function_response": 0},
            function_names=["write_sheet"],
        ),
    )
    await _turn(processor, fake_connector, seeded_agent)

    marker = _session_doc(fake_firestore)["active_run"]
    assert marker is not None
    assert marker["last_tool"] == "write_sheet"


async def test_a_broken_stream_records_why_it_broke(
    processor, fake_firestore, fake_vertex_ai, fake_connector, seeded_agent
):
    fake_vertex_ai.set_error(ENGINE, AgentStreamError("connection reset"))
    await _turn(processor, fake_connector, seeded_agent)

    assert fake_connector.sent_messages[-1]["text"] == ERR_STREAM_BROKEN
    marker = _session_doc(fake_firestore)["active_run"]
    assert marker["reason"] == CUT_OFF_STREAM_BROKE


# ---------------------------------------------------------------------------
# Recovering
# ---------------------------------------------------------------------------


async def test_the_second_turn_heals_what_the_first_turn_stranded(
    processor, fake_firestore, fake_vertex_ai, fake_connector, healer, seeded_agent
):
    """End to end: a silent-tool turn, then a later turn that repairs it."""
    fake_vertex_ai.set_response(
        ENGINE,
        VertexAIResponse(
            text="",
            chunk_count=3,
            breakdown={"function_call": 1, "function_response": 0},
            function_names=["write_sheet"],
        ),
    )
    await _turn(processor, fake_connector, seeded_agent, "do the thing")
    assert healer.calls == []

    # Age the marker past the grace period, then try again.
    doc = _session_doc(fake_firestore)
    doc["active_run"]["started_at"] = datetime.now(UTC) - timedelta(seconds=900)
    fake_vertex_ai.set_text_response(ENGINE, "Done now")

    await _turn(processor, fake_connector, seeded_agent, "try again")

    assert len(healer.calls) == 1
    call = healer.calls[0]
    assert call["agent_id"] == ENGINE
    assert call["session_id"] == doc["vertex_ai_session_id"]
    assert call["elapsed_seconds"] > 800
    assert fake_connector.sent_messages[-1]["text"] == "Done now"
    # The successful turn cleared the marker behind it.
    assert _session_doc(fake_firestore)["active_run"] is None


async def test_a_fresh_marker_is_left_for_the_run_that_owns_it(
    processor, fake_firestore, fake_vertex_ai, fake_connector, healer, seeded_agent
):
    """A message arriving mid-turn must not be read as a cut-off."""
    fake_vertex_ai.set_response(
        ENGINE,
        VertexAIResponse(
            text="",
            chunk_count=1,
            breakdown={"function_call": 1, "function_response": 0},
            function_names=["write_sheet"],
        ),
    )
    await _turn(processor, fake_connector, seeded_agent, "first")
    await _turn(processor, fake_connector, seeded_agent, "second")

    assert healer.calls == []
