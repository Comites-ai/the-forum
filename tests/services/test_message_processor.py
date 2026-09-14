# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Keystone test: end-to-end MessageProcessorV2 flow with all fakes wired up.

Exercises:
  PlatformEvent → IdentityService (creates user) → FirestoreService (loads agent)
  → VertexAIService (returns canned response) → PlatformConnector (sends reply)
"""
from datetime import datetime, timedelta, UTC

import pytest

from app.models.agent import Agent, AgentPlatformConfig
from app.models.user import User, PlatformIdentity
from app.schemas.platform_event import PlatformEvent
from app.services.identity_service import IdentityService
from app.services.message_processor_v2 import (
    MessageProcessorV2,
    REJECTION_MULTIPLE_FILES,
    REJECTION_MULTIPLE_IMAGES,
    REJECTION_NON_IMAGE_FILES,
    accepted_file_types_for,
    describe_accepted_types,
    normalize_mimetype,
)
from app.services.vertex_ai_service import VertexAIResponse


def _slack_event(
    text: str = "hello agent",
    files: list | None = None,
    media_group_id: str | None = None,
    platform: str = "slack",
    sent_at=None,
):
    return PlatformEvent(
        platform=platform,
        user_id="U_USER_001",
        message_text=text,
        space_id="C_CHANNEL_001",
        files=files or [],
        sent_at=sent_at,
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


# ---- per-agent file capability (#15) ----


@pytest.fixture
def gcs_processor(fake_firestore, fake_vertex_ai, fake_gcs) -> MessageProcessorV2:
    """Processor with GCS wired, so files produce a token instead of being dropped."""
    identity = IdentityService(firestore_service=fake_firestore)
    return MessageProcessorV2(
        firestore=fake_firestore,
        vertex_ai=fake_vertex_ai,
        identity=identity,
        gcs=fake_gcs,
    )


def _seed_agent(fake_firestore, agent_id: str, accepted=None) -> str:
    agent = Agent(
        vertex_ai_agent_id="projects/x/locations/us-central1/reasoningEngines/abc",
        display_name="Test Agent",
        accepted_file_types=accepted,
        platforms=[
            AgentPlatformConfig(
                platform="slack", slack_bot_id="U_BOT_001", slack_bot_token="xoxb-test"
            )
        ],
    )
    return fake_firestore.add_agent(agent, agent_id=agent_id)


def _agent_message(fake_vertex_ai) -> str:
    return fake_vertex_ai.messages_sent[0]["message"]


# -- helpers --


def test_normalize_mimetype_lowercases_and_strips_parameters():
    assert normalize_mimetype({"mimetype": "Application/PDF; charset=binary"}) == "application/pdf"
    assert normalize_mimetype({}) == ""


def test_accepted_types_defaults_to_images_when_undeclared():
    agent = Agent(vertex_ai_agent_id="x", display_name="A")
    accepted = accepted_file_types_for(agent)
    assert "image/png" in accepted
    assert "application/pdf" not in accepted


def test_declared_types_replace_the_default_rather_than_extending_it():
    """
    Deliberate contract: declaring any type opts out of the image default.
    An agent wanting both must list image types explicitly.
    """
    agent = Agent(
        vertex_ai_agent_id="x", display_name="A", accepted_file_types=["application/pdf"]
    )
    assert accepted_file_types_for(agent) == ["application/pdf"]


def test_describe_accepted_types_collapses_images():
    assert describe_accepted_types(["image/png", "image/jpeg"]) == "images"
    assert (
        describe_accepted_types(["image/png", "application/pdf"]) == "images, and PDF"
    )
    assert describe_accepted_types(["application/pdf"]) == "PDF"


# -- gating --


async def test_declared_pdf_reaches_the_agent_as_a_file_token(
    gcs_processor, fake_firestore, fake_vertex_ai, fake_connector
):
    agent_id = _seed_agent(
        fake_firestore, "agent-pdf", accepted=["image/png", "application/pdf"]
    )
    fake_vertex_ai.set_text_response(
        "projects/x/locations/us-central1/reasoningEngines/abc", "read it"
    )
    event = _slack_event(
        text="here's my receipt",
        files=[{"mimetype": "application/pdf", "download_ref": "u1"}],
    )

    await gcs_processor.process_platform_event(
        event=event, connector=fake_connector, agent_id=agent_id
    )

    sent = _agent_message(fake_vertex_ai)
    assert "[FILE: gs://" in sent
    assert "| application/pdf]" in sent
    # Must NOT arrive as [IMAGE:, which is what deployed vision agents match on.
    assert "[IMAGE:" not in sent


async def test_images_still_use_the_image_token(
    gcs_processor, fake_firestore, fake_vertex_ai, fake_connector
):
    agent_id = _seed_agent(
        fake_firestore, "agent-both", accepted=["image/png", "application/pdf"]
    )
    fake_vertex_ai.set_text_response(
        "projects/x/locations/us-central1/reasoningEngines/abc", "nice photo"
    )
    event = _slack_event(files=[{"mimetype": "image/png", "download_ref": "u1"}])

    await gcs_processor.process_platform_event(
        event=event, connector=fake_connector, agent_id=agent_id
    )

    sent = _agent_message(fake_vertex_ai)
    assert "[IMAGE: gs://" in sent
    assert "[FILE:" not in sent


async def test_undeclared_agent_still_rejects_pdf_with_the_original_copy(
    gcs_processor, fake_firestore, fake_vertex_ai, fake_connector
):
    """Sam/Nora regression guard: no declaration means nothing changes."""
    agent_id = _seed_agent(fake_firestore, "agent-legacy", accepted=None)
    fake_vertex_ai.set_text_response(
        "projects/x/locations/us-central1/reasoningEngines/abc", "got it"
    )
    event = _slack_event(
        text="please review",
        files=[{"mimetype": "application/pdf", "download_ref": "u1"}],
    )

    await gcs_processor.process_platform_event(
        event=event, connector=fake_connector, agent_id=agent_id
    )

    texts = [m["text"] for m in fake_connector.sent_messages]
    assert REJECTION_NON_IMAGE_FILES in texts
    assert "[FILE:" not in _agent_message(fake_vertex_ai)


async def test_rejection_copy_names_what_the_agent_can_read(
    gcs_processor, fake_firestore, fake_vertex_ai, fake_connector
):
    agent_id = _seed_agent(
        fake_firestore, "agent-pdf2", accepted=["image/png", "application/pdf"]
    )
    fake_vertex_ai.set_text_response(
        "projects/x/locations/us-central1/reasoningEngines/abc", "ok"
    )
    event = _slack_event(
        files=[{"mimetype": "application/zip", "download_ref": "u1"}],
    )

    await gcs_processor.process_platform_event(
        event=event, connector=fake_connector, agent_id=agent_id
    )

    rejection = next(
        m["text"] for m in fake_connector.sent_messages if "can't read" in m["text"]
    )
    assert "images" in rejection and "PDF" in rejection


async def test_declaring_only_pdf_stops_images_reaching_the_agent(
    gcs_processor, fake_firestore, fake_vertex_ai, fake_connector
):
    """The sharp edge of the exact-set contract, pinned so it can't drift silently."""
    agent_id = _seed_agent(fake_firestore, "agent-pdfonly", accepted=["application/pdf"])
    fake_vertex_ai.set_text_response(
        "projects/x/locations/us-central1/reasoningEngines/abc", "ok"
    )
    event = _slack_event(files=[{"mimetype": "image/png", "download_ref": "u1"}])

    await gcs_processor.process_platform_event(
        event=event, connector=fake_connector, agent_id=agent_id
    )

    assert "[IMAGE:" not in _agent_message(fake_vertex_ai)
    assert any("can't read" in m["text"] for m in fake_connector.sent_messages)


async def test_multiple_documents_use_the_file_wording(
    gcs_processor, fake_firestore, fake_connector
):
    agent_id = _seed_agent(
        fake_firestore, "agent-multi", accepted=["image/png", "application/pdf"]
    )
    event = _slack_event(
        files=[
            {"mimetype": "application/pdf", "download_ref": "u1"},
            {"mimetype": "application/pdf", "download_ref": "u2"},
        ]
    )

    await gcs_processor.process_platform_event(
        event=event, connector=fake_connector, agent_id=agent_id
    )

    texts = [m["text"] for m in fake_connector.sent_messages]
    assert REJECTION_MULTIPLE_FILES in texts
    assert REJECTION_MULTIPLE_IMAGES not in texts


async def test_document_size_cap_is_independent_of_the_image_cap(
    gcs_processor, fake_firestore, fake_connector, monkeypatch
):
    from app.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "max_document_size_mb", 1, raising=False)
    monkeypatch.setattr(settings, "max_image_size_mb", 20, raising=False)

    agent_id = _seed_agent(
        fake_firestore, "agent-size", accepted=["image/png", "application/pdf"]
    )
    event = _slack_event(
        files=[
            {
                "mimetype": "application/pdf",
                "download_ref": "u1",
                "size": 5 * 1024 * 1024,
            }
        ]
    )

    await gcs_processor.process_platform_event(
        event=event, connector=fake_connector, agent_id=agent_id
    )

    assert any("too large" in m["text"] for m in fake_connector.sent_messages)
    assert any("1 MB" in m["text"] for m in fake_connector.sent_messages)


# 18:30 UTC on a Tuesday; localizations verified against zoneinfo.
SENT_AT = datetime(2026, 7, 7, 18, 30, tzinfo=UTC)


async def test_slack_message_uses_profile_timezone_and_seeds_default(
    processor, fake_firestore, fake_vertex_ai, fake_connector, seeded_agent
):
    fake_vertex_ai.set_text_response(
        "projects/x/locations/us-central1/reasoningEngines/abc", "ok"
    )
    fake_connector.set_user_info({
        "display_name": "Alice",
        "email": "alice@example.com",
        "tz": "America/Chicago",
        "tz_label": "Central Daylight Time",
    })

    await processor.process_platform_event(
        event=_slack_event("hi", sent_at=SENT_AT),
        connector=fake_connector,
        agent_id=seeded_agent,
    )

    sent_to_agent = fake_vertex_ai.messages_sent[0]["message"]
    assert sent_to_agent.startswith("[From: Alice] ")
    assert (
        "[The user sent this message at 1:30 PM on Tuesday, July 7, 2026 "
        "from the America/Chicago timezone.]"
    ) in sent_to_agent

    # First Slack message seeds the user's default timezone from the profile
    user = await fake_firestore.get_user_by_identity("slack", "U_USER_001")
    assert user.default_timezone == "America/Chicago"


async def test_non_slack_message_uses_user_default_timezone(
    processor, fake_firestore, fake_vertex_ai, seeded_agent
):
    from tests.fakes.fake_platform_connector import FakePlatformConnector

    fake_firestore.add_user(
        User(
            primary_name="Alice",
            default_timezone="Europe/London",
            identities=[
                PlatformIdentity(platform="discord", platform_user_id="U_USER_001")
            ],
        )
    )
    fake_vertex_ai.set_text_response(
        "projects/x/locations/us-central1/reasoningEngines/abc", "ok"
    )
    connector = FakePlatformConnector(platform="discord")
    connector.set_user_info({"display_name": "Alice", "email": None})

    await processor.process_platform_event(
        event=_slack_event("hi", platform="discord", sent_at=SENT_AT),
        connector=connector,
        agent_id=seeded_agent,
    )

    sent_to_agent = fake_vertex_ai.messages_sent[0]["message"]
    assert (
        "[This message was sent at 7:30 PM on Tuesday, July 7, 2026 "
        "in the Europe/London timezone, which is the user's default time zone.]"
    ) in sent_to_agent


async def test_non_slack_message_falls_back_to_settings_default_timezone(
    processor, fake_vertex_ai, seeded_agent
):
    from tests.fakes.fake_platform_connector import FakePlatformConnector

    fake_vertex_ai.set_text_response(
        "projects/x/locations/us-central1/reasoningEngines/abc", "ok"
    )
    connector = FakePlatformConnector(platform="telegram")
    connector.set_user_info({"display_name": "Bob", "email": None})

    await processor.process_platform_event(
        event=_slack_event("hi", platform="telegram", sent_at=SENT_AT),
        connector=connector,
        agent_id=seeded_agent,
    )

    # settings.default_user_timezone defaults to America/New_York
    sent_to_agent = fake_vertex_ai.messages_sent[0]["message"]
    assert (
        "[This message was sent at 2:30 PM on Tuesday, July 7, 2026 "
        "in the America/New_York timezone, which is the user's default time zone.]"
    ) in sent_to_agent


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


# ---- Conversation log write points (PLAT-43) ----


async def test_live_exchange_is_logged_in_both_directions(
    processor, fake_firestore, fake_vertex_ai, fake_connector, seeded_agent
):
    fake_vertex_ai.set_text_response(
        "projects/x/locations/us-central1/reasoningEngines/abc", "Hi from the agent"
    )
    fake_connector.set_user_info({"display_name": "Alice", "email": "alice@example.com"})
    event = PlatformEvent(
        platform="slack",
        user_id="U_USER_001",
        message_text="what's the weather?",
        space_id="C_CHANNEL_001",
        files=[{"mimetype": "application/pdf", "download_ref": "ignored"}],
        message_id="1757700000.000100",
        raw_event={},
    )

    await processor.process_platform_event(event=event, connector=fake_connector, agent_id=seeded_agent)

    user = await fake_firestore.get_user_by_identity("slack", "U_USER_001")
    logged = fake_firestore.logged_messages(seeded_agent, user.id)
    assert [m.direction for m in logged] == ["inbound", "outbound"]

    inbound, outbound = logged
    assert inbound.text == "what's the weather?"  # the user's words, not the [From:] wrapper
    assert inbound.author == "Alice"
    assert inbound.platform == "slack"
    assert inbound.kind == "live"
    assert inbound.platform_message_ids == ["1757700000.000100"]
    assert inbound.attachments == ["[file: application/pdf]"]
    assert inbound.expires_at == inbound.created_at + timedelta(days=7)

    assert outbound.text == "Hi from the agent"
    assert outbound.author == "Test Agent"
    assert outbound.platform_message_ids  # whatever the connector reported for the send
    # The rejection notice for the PDF went out too, but it is a side
    # message, not the reply: only the reply is logged.
    assert len(fake_connector.sent_messages) == 2
    assert outbound.text == fake_connector.sent_messages[-1]["text"]


async def test_forum_fallback_copy_is_logged_as_the_reply(
    processor, fake_firestore, fake_vertex_ai, fake_connector, seeded_agent
):
    fake_vertex_ai.set_response(
        "projects/x/locations/us-central1/reasoningEngines/abc",
        VertexAIResponse(text="", chunk_count=3, function_names=["lookup"]),
    )
    await processor.process_platform_event(
        event=_slack_event("hello"), connector=fake_connector, agent_id=seeded_agent
    )
    user = await fake_firestore.get_user_by_identity("slack", "U_USER_001")
    outbound = fake_firestore.logged_messages(seeded_agent, user.id)[-1]
    assert outbound.direction == "outbound"
    assert outbound.text == fake_connector.sent_messages[-1]["text"]
    assert "broken tool" in outbound.text or "didn't respond" in outbound.text


async def test_log_failure_never_costs_the_user_a_reply(
    processor, fake_firestore, fake_vertex_ai, fake_connector, seeded_agent
):
    fake_firestore.append_message_error = RuntimeError("firestore down")
    fake_vertex_ai.set_text_response(
        "projects/x/locations/us-central1/reasoningEngines/abc", "still here"
    )
    await processor.process_platform_event(
        event=_slack_event("hi"), connector=fake_connector, agent_id=seeded_agent
    )
    assert fake_connector.sent_messages[-1]["text"] == "still here"
    assert fake_firestore.messages == {}
