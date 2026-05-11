# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Slack /events route — verification challenge, retry handling, signature gating, dispatch."""
import pytest

from app.models.agent import Agent, AgentPlatformConfig
from tests.conftest import load_fixture
from tests.fakes.fake_platform_connector import FakePlatformConnector


@pytest.fixture(autouse=True)
def patch_slack_connector(monkeypatch, request):
    """
    Replace SlackConnector inside the route with a fake.

    The route instantiates SlackConnector twice per event (once for sig check,
    once for sending). We hand back a FakePlatformConnector that always
    accepts the signature and records outbound messages, so we can assert
    end-to-end without HMAC plumbing for *every* test in this file.

    Tests that need to exercise the real signature path opt out by adding
    @pytest.mark.no_patch_connector.
    """
    if "no_patch_connector" in request.keywords:
        return
    fake = FakePlatformConnector(platform="slack", verify_result=True)
    monkeypatch.setattr(
        "app.api.v1.slack_events_v2.SlackConnector",
        lambda *args, **kwargs: fake,
    )
    request.node._fake_slack_connector = fake


def _seed_slack_agent(fake_firestore) -> str:
    agent = Agent(
        vertex_ai_agent_id="projects/x/locations/us-central1/reasoningEngines/abc",
        display_name="Slack Agent",
        platforms=[
            AgentPlatformConfig(
                platform="slack",
                slack_bot_id="U_BOT_001",
                slack_bot_token="xoxb-test",
            )
        ],
    )
    return fake_firestore.add_agent(agent, agent_id="agent-slack")


def test_url_verification_returns_challenge(client):
    response = client.post("/api/v1/slack/events", json=load_fixture("slack/url_verification.json"))
    assert response.status_code == 200
    assert response.json() == {
        "challenge": "3eZbrw1aBm2rZgRNFdxV2595E9CY3gmdALWMmHkvFXO7tYXAYM8P"
    }


def test_slack_retry_header_acknowledged_immediately(client):
    response = client.post(
        "/api/v1/slack/events",
        json=load_fixture("slack/app_mention.json"),
        headers={"X-Slack-Retry-Num": "1", "X-Slack-Retry-Reason": "http_timeout"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_event_with_no_matching_agent_acks_silently(client, fake_firestore):
    response = client.post(
        "/api/v1/slack/events", json=load_fixture("slack/app_mention.json")
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_app_mention_with_known_agent_returns_ok(
    client, fake_firestore, fake_vertex_ai
):
    _seed_slack_agent(fake_firestore)
    fake_vertex_ai.set_text_response(
        "projects/x/locations/us-central1/reasoningEngines/abc",
        "Hi from the agent",
    )

    response = client.post(
        "/api/v1/slack/events", json=load_fixture("slack/app_mention.json")
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_unknown_event_type_acks(client):
    response = client.post(
        "/api/v1/slack/events",
        json={"type": "totally_unknown_thing", "team_id": "T0001"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
