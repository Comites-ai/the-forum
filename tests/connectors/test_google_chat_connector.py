# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""GoogleChatConnector parse_event tests.

Skips connector construction (which would hit Secret Manager) by binding
parse_event as an unbound method directly. parse_event is a pure function
of its input and doesn't touch self.
"""
from app.services.platforms.google_chat_connector import GoogleChatConnector
from tests.conftest import load_fixture


def test_parse_event_message():
    event = GoogleChatConnector.parse_event(None, load_fixture("google_chat/message.json"))
    assert event.platform == "google_chat"
    assert event.user_id == "users/12345"
    assert event.user_email == "jane@example.com"
    assert event.space_id == "spaces/AAAA_SPACE_001"
    assert event.message_text == "Hello agent"
    assert event.files == []


def test_parse_event_extracts_sent_at_from_create_time():
    from datetime import datetime, UTC

    event = GoogleChatConnector.parse_event(None, load_fixture("google_chat/message.json"))
    # Fixture createTime is "2026-05-10T15:00:00Z"
    assert event.sent_at == datetime(2026, 5, 10, 15, 0, tzinfo=UTC)


def test_parse_event_with_uploaded_attachment():
    event = GoogleChatConnector.parse_event(
        None, load_fixture("google_chat/message_with_attachment.json")
    )
    assert len(event.files) == 1
    f = event.files[0]
    assert f["mimetype"] == "image/png"
    assert f["source"] == "uploaded"
    assert "data" in f["download_ref"]
    assert f["name"] == "diagram.png"


# ---- message ids for the conversation log (PLAT-43) ----


def test_parse_event_carries_the_message_name():
    payload = load_fixture("google_chat/message.json")
    event = GoogleChatConnector.parse_event(None, payload)
    assert event.message_id == payload["chat"]["messagePayload"]["message"]["name"]


def test_sent_message_ids_reads_name():
    assert GoogleChatConnector.sent_message_ids(None, {"name": "spaces/A/messages/B"}) == ["spaces/A/messages/B"]
    assert GoogleChatConnector.sent_message_ids(None, {}) == []
