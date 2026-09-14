# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""DiscordConnector unit tests.

We do not exercise the route handler's OIDC verification here — that lives
in tests of app/api/v1/discord_events.py. These tests focus on the
connector's contract: verify_request (no-op), parse_event (worker payload
shape), and the construction-time invariants.
"""
import pytest
from types import SimpleNamespace

from app.services.platforms.discord_connector import DiscordConnector
from tests.conftest import load_fixture


def _make_connector():
    return DiscordConnector(bot_token="discord-test-token")


# ---- construction ----


def test_requires_token_or_secret_config():
    with pytest.raises(ValueError):
        DiscordConnector()


# ---- verify_request ----


async def test_verify_request_is_a_no_op():
    # OIDC verification happens at the route handler before this connector
    # is instantiated; the connector itself trusts incoming events.
    connector = _make_connector()
    request = SimpleNamespace(headers={})
    assert await connector.verify_request(request) is True


# ---- parse_event ----


def test_parse_event_dm_text():
    connector = _make_connector()
    event = connector.parse_event(load_fixture("discord/dm_text.json"))
    assert event.platform == "discord"
    assert event.user_id == "987654321098765432"
    assert event.space_id == "111222333444555666"
    assert event.message_text == "Hello bot!"
    assert event.user_email is None
    assert event.files == []


def test_parse_event_derives_sent_at_from_snowflake():
    from datetime import datetime, UTC

    connector = _make_connector()
    event = connector.parse_event(load_fixture("discord/dm_text.json"))
    # Fixture message_id 777888999000111222 decodes to this creation time
    assert event.sent_at == datetime(
        2020, 11, 16, 13, 33, 9, 840000, tzinfo=UTC
    )


def test_parse_event_dm_with_attachment():
    connector = _make_connector()
    event = connector.parse_event(load_fixture("discord/dm_with_attachment.json"))
    assert len(event.files) == 1
    attachment = event.files[0]
    assert attachment["mimetype"] == "image/png"
    assert attachment["download_ref"].startswith("https://cdn.discordapp.com/")
    assert attachment["name"] == "photo.png"
    assert attachment["size"] == 56789


def test_parse_event_rejects_missing_ids():
    connector = _make_connector()
    with pytest.raises(ValueError):
        connector.parse_event({"event_type": "dm_message", "text": "hi"})


def test_parse_event_skips_attachment_without_url():
    connector = _make_connector()
    event = connector.parse_event({
        "event_type": "dm_message",
        "user_id": "1",
        "channel_id": "2",
        "text": "x",
        "attachments": [{"filename": "no-url.png"}],
    })
    assert event.files == []
    # No message_id in this payload → no derivable timestamp
    assert event.sent_at is None


# ---- message ids for the conversation log (PLAT-43) ----


def test_parse_event_carries_the_message_id():
    connector = _make_connector()
    event = connector.parse_event(load_fixture("discord/dm_text.json"))
    assert event.message_id == "777888999000111222"


async def test_chunked_send_reports_every_chunk_id(monkeypatch):
    connector = _make_connector()
    sent: list[str] = []

    async def fake_send_single(recipient_id, text):
        sent.append(text)
        return {"id": f"m{len(sent)}", "content": text}

    monkeypatch.setattr(connector, "_send_single", fake_send_single)

    result = await connector.send_message("chan", "para one\n\n" + ("x" * 1900) + "\n\npara three")

    assert len(sent) > 1
    assert result["id"] == f"m{len(sent)}"
    assert connector.sent_message_ids(result) == [f"m{i + 1}" for i in range(len(sent))]


def test_sent_message_ids_falls_back_to_single_id():
    connector = _make_connector()
    assert connector.sent_message_ids({"id": "123"}) == ["123"]
    assert connector.sent_message_ids({}) == []
