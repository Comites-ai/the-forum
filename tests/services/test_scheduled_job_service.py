# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""ScheduledJobService tests against the fake firestore."""
import pytest

from app.models.agent import Agent
from app.schemas.scheduled_job import ScheduledJobCreate, ScheduledJobUpdate
from app.services.scheduled_job_service import ScheduledJobService


@pytest.fixture
def agent_id_in_firestore(fake_firestore) -> str:
    agent = Agent(vertex_ai_agent_id="re-1", display_name="Agent")
    return fake_firestore.add_agent(agent, agent_id="agent-1")


@pytest.fixture
def service(fake_firestore) -> ScheduledJobService:
    return ScheduledJobService(firestore=fake_firestore)


def _make_create(**overrides) -> ScheduledJobCreate:
    base = dict(
        name="morning brief",
        prompt="summarize",
        agent_id="agent-1",
        user_id="user-1",
        output_platform="slack",
        schedule="0 9 * * 1-5",
        timezone="UTC",
        enabled=True,
    )
    base.update(overrides)
    return ScheduledJobCreate(**base)


# ---- validation ----


def test_validate_cron_accepts_valid(service):
    assert service._validate_cron_expression("0 9 * * 1-5") is True


def test_validate_cron_rejects_invalid(service):
    assert service._validate_cron_expression("not a cron") is False


# ---- create_job ----


async def test_create_job_persists_and_returns(service, agent_id_in_firestore):
    job = await service.create_job(_make_create())
    assert job.id is not None
    assert job.name == "morning brief"


async def test_create_job_rejects_invalid_cron(service, agent_id_in_firestore):
    with pytest.raises(ValueError, match="Invalid cron"):
        await service.create_job(_make_create(schedule="not a cron"))


async def test_create_job_rejects_invalid_timezone(service, agent_id_in_firestore):
    with pytest.raises(ValueError, match="Invalid timezone"):
        await service.create_job(_make_create(timezone="Mars/Olympus_Mons"))


async def test_create_job_rejects_unknown_agent(service):
    with pytest.raises(ValueError, match="Agent not found"):
        await service.create_job(_make_create(agent_id="ghost"))


# ---- list / get / delete / update ----


async def test_list_jobs_filters_by_user(service, agent_id_in_firestore):
    await service.create_job(_make_create(name="job-a", user_id="user-1"))
    await service.create_job(_make_create(name="job-b", user_id="user-2"))
    user1_jobs = await service.list_jobs(user_id="user-1")
    assert {j.name for j in user1_jobs} == {"job-a"}


async def test_get_job_returns_none_when_missing(service):
    assert await service.get_job("nonexistent") is None


async def test_delete_job_removes_it(service, agent_id_in_firestore):
    job = await service.create_job(_make_create())
    assert await service.delete_job(job.id) is True
    assert await service.get_job(job.id) is None


async def test_delete_job_returns_false_when_missing(service):
    assert await service.delete_job("nonexistent") is False


async def test_update_job_changes_fields(service, agent_id_in_firestore):
    job = await service.create_job(_make_create())
    updated = await service.update_job(
        job.id, ScheduledJobUpdate(enabled=False, name="renamed")
    )
    assert updated.enabled is False
    assert updated.name == "renamed"


async def test_update_job_returns_none_when_missing(service):
    assert await service.update_job("nope", ScheduledJobUpdate(enabled=False)) is None
