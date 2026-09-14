# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""TelegramConnector verify_request and parse_event tests."""
from types import SimpleNamespace

from app.services.platforms.telegram_connector import TelegramConnector
from tests.conftest import load_fixture


def _make_connector(*, webhook_secret: str | None = "tg-secret"):
    return TelegramConnector(bot_token="123:abc", webhook_secret=webhook_secret)


def _make_request(headers: dict):
    return SimpleNamespace(headers=headers)


# ---- verify_request ----


async def test_verify_request_accepts_matching_secret_token():
    connector = _make_connector()
    request = _make_request({"X-Telegram-Bot-Api-Secret-Token": "tg-secret"})
    assert await connector.verify_request(request) is True


async def test_verify_request_rejects_wrong_secret_token():
    connector = _make_connector()
    request = _make_request({"X-Telegram-Bot-Api-Secret-Token": "wrong"})
    assert await connector.verify_request(request) is False


async def test_verify_request_passes_when_no_secret_configured():
    connector = _make_connector(webhook_secret=None)
    request = _make_request({})
    assert await connector.verify_request(request) is True


# ---- parse_event ----


def test_parse_event_private_message():
    connector = _make_connector()
    event = connector.parse_event(load_fixture("telegram/private_message.json"))
    assert event.platform == "telegram"
    assert event.user_id == "987654321"
    assert event.space_id == "987654321"
    assert event.message_text == "Hello bot!"
    assert event.files == []


def test_parse_event_extracts_sent_at_from_date():
    from datetime import datetime, UTC

    connector = _make_connector()
    event = connector.parse_event(load_fixture("telegram/private_message.json"))
    # Fixture date is 1234567890 (unix epoch seconds)
    assert event.sent_at == datetime(2009, 2, 13, 23, 31, 30, tzinfo=UTC)


def test_parse_event_photo_picks_largest_size():
    connector = _make_connector()
    event = connector.parse_event(load_fixture("telegram/photo_message.json"))
    assert len(event.files) == 1
    photo = event.files[0]
    assert photo["mimetype"] == "image/jpeg"
    assert photo["download_ref"] == "AgACAgI_large"
    assert photo["size"] == 56789


# ---- message ids for the conversation log (PLAT-43) ----


def test_parse_event_carries_the_message_id():
    connector = _make_connector()
    update = load_fixture("telegram/private_message.json")
    event = connector.parse_event(update)
    assert event.message_id == str(update["message"]["message_id"])


def test_sent_message_ids_reads_result_message_id():
    connector = _make_connector()
    assert connector.sent_message_ids({"ok": True, "result": {"message_id": 42}}) == ["42"]
    assert connector.sent_message_ids({"ok": False}) == []
