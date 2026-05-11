# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Scheduled jobs CRUD API: create / list / get / patch / delete."""
from app.models.agent import Agent


def _seed_agent(fake_firestore) -> str:
    agent = Agent(vertex_ai_agent_id="re-1", display_name="Agent")
    return fake_firestore.add_agent(agent, agent_id="agent-1")


def _payload(**overrides) -> dict:
    base = {
        "name": "morning brief",
        "prompt": "summarize my calendar",
        "agent_id": "agent-1",
        "user_id": "user-1",
        "output_platform": "slack",
        "schedule": "0 9 * * 1-5",
        "timezone": "UTC",
        "enabled": True,
    }
    base.update(overrides)
    return base


def test_create_returns_201_with_id(client, fake_firestore):
    _seed_agent(fake_firestore)
    response = client.post("/api/v1/scheduled-jobs", json=_payload())
    assert response.status_code == 201
    body = response.json()
    assert body["id"]
    assert body["name"] == "morning brief"


def test_create_with_invalid_cron_returns_400(client, fake_firestore):
    _seed_agent(fake_firestore)
    response = client.post("/api/v1/scheduled-jobs", json=_payload(schedule="not a cron"))
    assert response.status_code == 400
    assert "Invalid cron" in response.json()["detail"]


def test_create_with_unknown_agent_returns_400(client):
    response = client.post("/api/v1/scheduled-jobs", json=_payload(agent_id="ghost"))
    assert response.status_code == 400
    assert "Agent not found" in response.json()["detail"]


def test_list_returns_created_jobs(client, fake_firestore):
    _seed_agent(fake_firestore)
    client.post("/api/v1/scheduled-jobs", json=_payload(name="a"))
    client.post("/api/v1/scheduled-jobs", json=_payload(name="b"))
    response = client.get("/api/v1/scheduled-jobs")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert {j["name"] for j in body["jobs"]} == {"a", "b"}


def test_list_filters_by_user(client, fake_firestore):
    _seed_agent(fake_firestore)
    client.post("/api/v1/scheduled-jobs", json=_payload(name="alice", user_id="u-alice"))
    client.post("/api/v1/scheduled-jobs", json=_payload(name="bob", user_id="u-bob"))
    response = client.get("/api/v1/scheduled-jobs", params={"user_id": "u-alice"})
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["jobs"][0]["name"] == "alice"


def test_get_missing_returns_404(client):
    response = client.get("/api/v1/scheduled-jobs/does-not-exist")
    assert response.status_code == 404


def test_patch_updates_fields(client, fake_firestore):
    _seed_agent(fake_firestore)
    created = client.post("/api/v1/scheduled-jobs", json=_payload()).json()
    response = client.patch(
        f"/api/v1/scheduled-jobs/{created['id']}",
        json={"enabled": False, "name": "renamed"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is False
    assert body["name"] == "renamed"


def test_delete_removes_job(client, fake_firestore):
    _seed_agent(fake_firestore)
    created = client.post("/api/v1/scheduled-jobs", json=_payload()).json()
    response = client.delete(f"/api/v1/scheduled-jobs/{created['id']}")
    assert response.status_code == 204
    assert client.get(f"/api/v1/scheduled-jobs/{created['id']}").status_code == 404


def test_delete_missing_returns_404(client):
    response = client.delete("/api/v1/scheduled-jobs/does-not-exist")
    assert response.status_code == 404
