# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Keystone test: end-to-end MessageProcessorV2 flow with all fakes wired up.

Exercises:
  PlatformEvent → IdentityService (creates user) → FirestoreService (loads agent)
  → VertexAIService (returns canned response) → PlatformConnector (sends reply)
"""
import pytest

from app.models.agent import Agent, AgentPlatformConfig
from app.schemas.platform_event import PlatformEvent
from app.services.identity_service import IdentityService
from app.services.message_processor_v2 import (
    MessageProcessorV2,
    REJECTION_MULTIPLE_IMAGES,
    REJECTION_NON_IMAGE_FILES,
)
from app.services.vertex_ai_service import VertexAIResponse


def _slack_event(text: str = "hello agent", files: list | None = None, media_group_id: str | None = None):
    return PlatformEvent(
        platform="slack",
        user_id="U_USER_001",
        message_text=text,
        space_id="C_CHANNEL_001",
        files=files or [],
        media_group_id=media_group_id,
        raw_event={},
    )


@pytest.fixture
def seeded_agent(fake_firestore) -> str:
    agent = Agent(
        vertex_ai_agent_id="projects/x/locations/us-central1/reasoningEngines/abc",
        display_name="Test Agent",
        platforms=[
            AgentPlatformConfig(
                platform="slack", slack_bot_id="U_BOT_001", slack_bot_token="xoxb-test"
            )
        ],
    )
    return fake_firestore.add_agent(agent, agent_id="agent-1")


@pytest.fixture
def processor(fake_firestore, fake_vertex_ai) -> MessageProcessorV2:
    identity = IdentityService(firestore_service=fake_firestore)
    return MessageProcessorV2(
        firestore=fake_firestore,
        vertex_ai=fake_vertex_ai,
        identity=identity,
        gcs=None,
    )


async def test_happy_path_text_only(
    processor, fake_firestore, fake_vertex_ai, fake_connector, seeded_agent
):
    fake_vertex_ai.set_text_response(
        "projects/x/locations/us-central1/reasoningEngines/abc",
        "Hi from the agent",
    )
    fake_connector.set_user_info({"display_name": "Alice", "email": "alice@example.com"})

    await processor.process_platform_event(
        event=_slack_event("what's the weather?"),
        connector=fake_connector,
        agent_id=seeded_agent,
    )

    assert len(fake_connector.sent_messages) == 1
    sent = fake_connector.sent_messages[0]
    assert sent["text"] == "Hi from the agent"
    assert sent["recipient_id"] == "C_CHANNEL_001"

    assert len(fake_vertex_ai.messages_sent) == 1
    sent_to_agent = fake_vertex_ai.messages_sent[0]["message"]
    assert sent_to_agent.startswith("[From: Alice] ")
    assert "what's the weather?" in sent_to_agent

    user = await fake_firestore.get_user_by_identity("slack", "U_USER_001")
    assert user is not None
    assert user.primary_name == "Alice"


async def test_session_is_reused_on_second_message(
    processor, fake_vertex_ai, fake_connector, seeded_agent
):
    fake_vertex_ai.set_text_response(
        "projects/x/locations/us-central1/reasoningEngines/abc", "first"
    )
    await processor.process_platform_event(
        event=_slack_event("first"), connector=fake_connector, agent_id=seeded_agent
    )

    fake_vertex_ai.set_text_response(
        "projects/x/locations/us-central1/reasoningEngines/abc", "second"
    )
    await processor.process_platform_event(
        event=_slack_event("second"), connector=fake_connector, agent_id=seeded_agent
    )

    assert len(fake_vertex_ai.sessions_created) == 1
    assert len(fake_vertex_ai.messages_sent) == 2
    assert fake_connector.sent_messages[-1]["text"] == "second"


async def test_unknown_agent_silently_returns(
    processor, fake_connector
):
    await processor.process_platform_event(
        event=_slack_event(), connector=fake_connector, agent_id="ghost-agent"
    )
    assert fake_connector.sent_messages == []


async def test_multiple_images_hard_rejects(
    processor, fake_connector, seeded_agent
):
    event = _slack_event(
        files=[
            {"mimetype": "image/png", "download_ref": "u1"},
            {"mimetype": "image/jpeg", "download_ref": "u2"},
        ]
    )
    await processor.process_platform_event(
        event=event, connector=fake_connector, agent_id=seeded_agent
    )
    assert any(m["text"] == REJECTION_MULTIPLE_IMAGES for m in fake_connector.sent_messages)


async def test_non_image_files_warn_and_continue(
    processor, fake_vertex_ai, fake_connector, seeded_agent
):
    fake_vertex_ai.set_text_response(
        "projects/x/locations/us-central1/reasoningEngines/abc", "got it"
    )
    event = _slack_event(
        text="please review",
        files=[{"mimetype": "application/pdf", "download_ref": "ignored"}],
    )
    await processor.process_platform_event(
        event=event, connector=fake_connector, agent_id=seeded_agent
    )
    texts_sent = [m["text"] for m in fake_connector.sent_messages]
    assert REJECTION_NON_IMAGE_FILES in texts_sent
    assert "got it" in texts_sent
    sent_to_agent = fake_vertex_ai.messages_sent[0]["message"]
    assert "Note to Agent" in sent_to_agent


async def test_empty_agent_response_falls_back_to_apology(
    processor, fake_vertex_ai, fake_connector, seeded_agent
):
    fake_vertex_ai.set_response(
        "projects/x/locations/us-central1/reasoningEngines/abc",
        VertexAIResponse(text="", chunk_count=1),
    )
    await processor.process_platform_event(
        event=_slack_event(), connector=fake_connector, agent_id=seeded_agent
    )
    assert len(fake_connector.sent_messages) == 1
    assert "wasn't able to process" in fake_connector.sent_messages[0]["text"]
