# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Aggregator router for the admin UI (mounted at /admin)."""
from fastapi import APIRouter

from app.api.admin import agents, auth, jobs

router = APIRouter(prefix="/admin", tags=["admin"])
router.include_router(auth.router)
router.include_router(agents.router)
router.include_router(jobs.router)
