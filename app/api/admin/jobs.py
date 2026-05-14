# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Admin UI scheduled jobs CRUD (rendered HTML, form-encoded POSTs)."""
import logging

from cron_descriptor import get_description
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.core.dependencies import (
    get_firestore_service,
    get_scheduled_job_service,
    require_admin_user,
)
from app.api.admin._common import PLATFORMS, PLATFORM_LABELS, TIMEZONES, get_templates
from app.schemas.scheduled_job import ScheduledJobCreate, ScheduledJobUpdate
from app.services.firestore_service import FirestoreService
from app.services.scheduled_job_service import ScheduledJobService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs")


def _agent_name_map(agents) -> dict[str, str]:
    return {a.id: a.display_name for a in agents}


def _describe_cron(expression: str) -> str:
    """English description of a cron expression, or the raw expression on failure.

    cron-descriptor handles malformed input by raising; we fall back to the
    raw string so the column never goes blank.
    """
    try:
        return get_description(expression)
    except Exception:
        return expression


@router.get("")
async def list_jobs(
    request: Request,
    _email: str = Depends(require_admin_user),
    service: ScheduledJobService = Depends(get_scheduled_job_service),
    firestore: FirestoreService = Depends(get_firestore_service),
):
    jobs = await service.list_jobs()
    agents = await firestore.list_agents()
    schedule_descriptions = {job.id: _describe_cron(job.schedule) for job in jobs}
    return get_templates(request).TemplateResponse(
        request,
        "admin/jobs_list.html",
        {
            "jobs": jobs,
            "agent_names": _agent_name_map(agents),
            "schedule_descriptions": schedule_descriptions,
            "platform_labels": PLATFORM_LABELS,
        },
    )


@router.get("/new")
async def new_job_form(
    request: Request,
    _email: str = Depends(require_admin_user),
    firestore: FirestoreService = Depends(get_firestore_service),
):
    agents = await firestore.list_agents()
    users = await firestore.list_users()
    return get_templates(request).TemplateResponse(
        request,
        "admin/job_form.html",
        {
            "job": None,
            "form": {},
            "error": None,
            "agents": agents,
            "users": users,
            "platforms": list(PLATFORMS),
            "platform_labels": PLATFORM_LABELS,
            "timezones": TIMEZONES,
        },
    )


@router.post("/new")
async def create_job(
    request: Request,
    _email: str = Depends(require_admin_user),
    service: ScheduledJobService = Depends(get_scheduled_job_service),
    firestore: FirestoreService = Depends(get_firestore_service),
    name: str = Form(...),
    prompt: str = Form(...),
    agent_id: str = Form(...),
    user_id: str = Form(...),
    output_platform: str = Form("slack"),
    schedule: str = Form(...),
    timezone: str = Form("UTC"),
    enabled: str = Form(None),
):
    payload = ScheduledJobCreate(
        name=name,
        prompt=prompt,
        agent_id=agent_id,
        user_id=user_id,
        output_platform=output_platform,
        schedule=schedule,
        timezone=timezone,
        enabled=enabled is not None,
    )
    try:
        await service.create_job(payload)
    except ValueError as e:
        agents = await firestore.list_agents()
        users = await firestore.list_users()
        return get_templates(request).TemplateResponse(
            request,
            "admin/job_form.html",
            {
                "job": None,
                "form": payload.model_dump(),
                "error": str(e),
                "agents": agents,
                "users": users,
                "platforms": list(PLATFORMS),
                "platform_labels": PLATFORM_LABELS,
            },
            status_code=400,
        )
    return RedirectResponse("/admin/jobs", status_code=303)


@router.get("/{job_id}/edit")
async def edit_job_form(
    job_id: str,
    request: Request,
    _email: str = Depends(require_admin_user),
    service: ScheduledJobService = Depends(get_scheduled_job_service),
    firestore: FirestoreService = Depends(get_firestore_service),
):
    job = await service.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    agents = await firestore.list_agents()
    users = await firestore.list_users()
    return get_templates(request).TemplateResponse(
        request,
        "admin/job_form.html",
        {
            "job": job,
            "form": {},
            "error": None,
            "agents": agents,
            "users": users,
            "platforms": list(PLATFORMS),
            "platform_labels": PLATFORM_LABELS,
            "timezones": TIMEZONES,
        },
    )


@router.post("/{job_id}/edit")
async def update_job(
    job_id: str,
    request: Request,
    _email: str = Depends(require_admin_user),
    service: ScheduledJobService = Depends(get_scheduled_job_service),
    firestore: FirestoreService = Depends(get_firestore_service),
    name: str = Form(...),
    prompt: str = Form(...),
    schedule: str = Form(...),
    timezone: str = Form("UTC"),
    output_platform: str = Form("slack"),
    enabled: str = Form(None),
):
    updates = ScheduledJobUpdate(
        name=name,
        prompt=prompt,
        schedule=schedule,
        timezone=timezone,
        enabled=enabled is not None,
    )
    # output_platform isn't on ScheduledJobUpdate today; persist it directly
    # through the firestore layer so the admin can change delivery target.
    try:
        result = await service.update_job(job_id, updates)
        if result is None:
            raise HTTPException(status_code=404, detail="Job not found")
        await firestore.update_scheduled_job(job_id, {"output_platform": output_platform})
    except ValueError as e:
        job = await service.get_job(job_id)
        agents = await firestore.list_agents()
        users = await firestore.list_users()
        return get_templates(request).TemplateResponse(
            request,
            "admin/job_form.html",
            {
                "job": job,
                "form": {
                    "name": name,
                    "prompt": prompt,
                    "schedule": schedule,
                    "timezone": timezone,
                    "output_platform": output_platform,
                    "enabled": enabled is not None,
                },
                "error": str(e),
                "agents": agents,
                "users": users,
                "platforms": list(PLATFORMS),
                "platform_labels": PLATFORM_LABELS,
            },
            status_code=400,
        )
    return RedirectResponse("/admin/jobs", status_code=303)


@router.post("/{job_id}/delete")
async def delete_job(
    job_id: str,
    _email: str = Depends(require_admin_user),
    service: ScheduledJobService = Depends(get_scheduled_job_service),
):
    deleted = await service.delete_job(job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Job not found")
    return RedirectResponse("/admin/jobs", status_code=303)
