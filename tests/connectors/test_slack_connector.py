# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""SlackConnector tests: signature verification (security-critical) and event parsing."""
import hashlib
import hmac
import json
import time
from types import SimpleNamespace

import pytest

from app.services.platforms.slack_connector import SlackConnector
from tests.conftest import load_fixture


SIGNING_SECRET = "test-secret"


def _sign(body: str, *, timestamp: int | None = None) -> tuple[str, str]:
    ts = str(timestamp if timestamp is not None else int(time.time()))
    sig = "v0=" + hmac.new(
        SIGNING_SECRET.encode(),
        f"v0:{ts}:{body}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return ts, sig


def _make_request(*, headers: dict, body: bytes):
    """Mock just enough of starlette.Request for verify_request."""

    async def _body():
        return body

    return SimpleNamespace(headers=headers, body=_body)


def _make_connector() -> SlackConnector:
    return SlackConnector(bot_token="xoxb-test", signing_secret=SIGNING_SECRET)


# ---- verify_request ----


async def test_verify_request_accepts_valid_signature():
    connector = _make_connector()
    body = '{"type":"event_callback"}'
    ts, sig = _sign(body)
    request = _make_request(
        headers={"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig},
        body=body.encode(),
    )
    assert await connector.verify_request(request) is True


async def test_verify_request_rejects_bad_signature():
    connector = _make_connector()
    body = '{"type":"event_callback"}'
    ts = str(int(time.time()))
    request = _make_request(
        headers={"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": "v0=deadbeef"},
        body=body.encode(),
    )
    assert await connector.verify_request(request) is False


async def test_verify_request_rejects_signature_for_tampered_body():
    connector = _make_connector()
    original = '{"type":"event_callback"}'
    tampered = '{"type":"event_callback","extra":"injected"}'
    ts, sig = _sign(original)
    request = _make_request(
        headers={"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig},
        body=tampered.encode(),
    )
    assert await connector.verify_request(request) is False


async def test_verify_request_rejects_old_timestamp_replay():
    connector = _make_connector()
    body = '{"type":"event_callback"}'
    old_ts = int(time.time()) - 60 * 10
    ts, sig = _sign(body, timestamp=old_ts)
    request = _make_request(
        headers={"X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig},
        body=body.encode(),
    )
    assert await connector.verify_request(request) is False


async def test_verify_request_rejects_invalid_timestamp_string():
    connector = _make_connector()
    body = '{"type":"event_callback"}'
    request = _make_request(
        headers={"X-Slack-Request-Timestamp": "not-a-number", "X-Slack-Signature": "v0=x"},
        body=body.encode(),
    )
    assert await connector.verify_request(request) is False


async def test_verify_request_rejects_missing_headers():
    connector = _make_connector()
    request = _make_request(headers={}, body=b"{}")
    assert await connector.verify_request(request) is False


# ---- parse_event ----


def test_parse_event_app_mention():
    connector = _make_connector()
    event = connector.parse_event(load_fixture("slack/app_mention.json"))
    assert event.platform == "slack"
    assert event.user_id == "U_USER_001"
    assert event.space_id == "C_CHANNEL_001"
    assert "hello agent" in event.message_text
    assert event.files == []


def test_parse_event_direct_message():
    connector = _make_connector()
    event = connector.parse_event(load_fixture("slack/direct_message.json"))
    assert event.user_id == "U_USER_001"
    assert event.space_id == "D_CHANNEL_001"
    assert event.message_text == "hi there"


def test_parse_event_file_share_normalises_attachment():
    connector = _make_connector()
    event = connector.parse_event(load_fixture("slack/file_share.json"))
    assert len(event.files) == 1
    f = event.files[0]
    assert f["mimetype"] == "image/png"
    assert f["download_ref"].startswith("https://files.slack.com/")
    assert f["name"] == "cat.png"
    assert f["size"] == 12345


def test_parse_event_raises_on_missing_user():
    connector = _make_connector()
    payload = {"event": {"channel": "C_001"}}
    with pytest.raises(ValueError):
        connector.parse_event(payload)
