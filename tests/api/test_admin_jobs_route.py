# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Admin scheduled jobs CRUD via the rendered admin UI."""
from app.models.agent import Agent


def _login(client):
    client.get("/admin/auth/callback?code=t&state=t", follow_redirects=False)


def _seed_agent(fake_firestore):
    agent = Agent(vertex_ai_agent_id="re-1", display_name="Imperator")
    return fake_firestore.add_agent(agent, agent_id="agent-1")


def _form_payload(**overrides):
    data = {
        "name": "morning brief",
        "prompt": "summarize my calendar",
        "agent_id": "agent-1",
        "user_id": "user-1",
        "output_platform": "slack",
        "schedule": "0 9 * * 1-5",
        "timezone": "UTC",
        "enabled": "1",
    }
    data.update(overrides)
    return data


def test_jobs_list_renders_empty_state(admin_client):
    _login(admin_client)
    response = admin_client.get("/admin/jobs")
    assert response.status_code == 200
    assert "No scheduled jobs yet" in response.text


def test_create_job_via_form_redirects_and_persists(admin_client, fake_firestore):
    _seed_agent(fake_firestore)
    _login(admin_client)
    response = admin_client.post(
        "/admin/jobs/new",
        data=_form_payload(),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/jobs"

    listed = admin_client.get("/admin/jobs")
    assert "morning brief" in listed.text


def test_create_job_with_invalid_cron_re_renders_form(admin_client, fake_firestore):
    _seed_agent(fake_firestore)
    _login(admin_client)
    response = admin_client.post(
        "/admin/jobs/new",
        data=_form_payload(schedule="not a cron"),
        follow_redirects=False,
    )
    assert response.status_code == 400
    assert "Invalid cron" in response.text


def test_edit_job_updates_field(admin_client, fake_firestore):
    _seed_agent(fake_firestore)
    _login(admin_client)
    admin_client.post("/admin/jobs/new", data=_form_payload(), follow_redirects=False)
    job_id = next(iter(fake_firestore.scheduled_jobs.keys()))

    response = admin_client.post(
        f"/admin/jobs/{job_id}/edit",
        data={
            "name": "renamed",
            "prompt": "summarize my calendar",
            "schedule": "0 10 * * 1-5",
            "timezone": "UTC",
            "output_platform": "telegram",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    listed = admin_client.get("/admin/jobs")
    assert "renamed" in listed.text
    assert "Telegram" in listed.text


def test_delete_job_removes_it(admin_client, fake_firestore):
    _seed_agent(fake_firestore)
    _login(admin_client)
    admin_client.post("/admin/jobs/new", data=_form_payload(), follow_redirects=False)
    job_id = next(iter(fake_firestore.scheduled_jobs.keys()))

    response = admin_client.post(
        f"/admin/jobs/{job_id}/delete", follow_redirects=False
    )
    assert response.status_code == 303
    assert job_id not in fake_firestore.scheduled_jobs


def test_jobs_list_links_to_agent(admin_client, fake_firestore):
    _seed_agent(fake_firestore)
    _login(admin_client)
    admin_client.post("/admin/jobs/new", data=_form_payload(), follow_redirects=False)
    response = admin_client.get("/admin/jobs")
    assert '/admin/agents/agent-1' in response.text
