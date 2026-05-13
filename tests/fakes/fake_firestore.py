# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""In-memory FirestoreService stand-in for tests.

Mirrors the public async API of FirestoreService with dict-backed storage.
Designed to behave like Firestore *enough* that service-layer code is none
the wiser, not to be a faithful Firestore emulator.
"""
from datetime import datetime, timedelta, UTC
from typing import List, Optional
import uuid

from app.models.agent import Agent
from app.models.session import Session
from app.models.scheduled_job import ScheduledJob
from app.models.user import User, PlatformIdentity


SESSION_TIMEOUT_MINUTES = 180


class FakeFirestoreService:
    def __init__(self):
        self.agents: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.scheduled_jobs: dict[str, dict] = {}
        self.users: dict[str, dict] = {}

    # ---- Test setup helpers (not on the real interface) ----

    def add_agent(self, agent: Agent, agent_id: Optional[str] = None) -> str:
        """Insert an agent for test setup. Returns the assigned id."""
        agent_id = agent_id or agent.id or f"agent-{uuid.uuid4().hex[:8]}"
        data = agent.model_dump(exclude={"id"})
        self.agents[agent_id] = data
        return agent_id

    def add_user(self, user: User, user_id: Optional[str] = None) -> str:
        """Insert a user for test setup. Returns the assigned id."""
        user_id = user_id or user.id or f"user-{uuid.uuid4().hex[:8]}"
        data = user.model_dump(exclude={"id"})
        self.users[user_id] = data
        return user_id

    # ---- Agent methods ----

    async def get_agent_by_bot_id(self, bot_id: str) -> Optional[Agent]:
        for agent_id, data in self.agents.items():
            if data.get("slack_bot_id") == bot_id:
                return Agent(**data, id=agent_id)
            for platform_cfg in data.get("platforms") or []:
                if platform_cfg.get("platform") == "slack" and platform_cfg.get("slack_bot_id") == bot_id:
                    return Agent(**data, id=agent_id)
        return None

    async def get_agent_by_id(self, agent_id: str) -> Optional[Agent]:
        data = self.agents.get(agent_id)
        if not data:
            return None
        return Agent(**data, id=agent_id)

    async def list_agents(self) -> list[Agent]:
        return [Agent(**data, id=aid) for aid, data in self.agents.items()]

    async def get_agent_by_scheduler_api_key_hash(self, key_hash: str) -> Optional[Agent]:
        for agent_id, data in self.agents.items():
            if data.get("scheduler_api_key_hash") == key_hash:
                return Agent(**data, id=agent_id)
        return None

    # ---- Session methods (legacy slack-only and new user-based) ----

    async def get_session(self, slack_user_id: str, agent_id: str) -> Optional[Session]:
        session_key = f"{slack_user_id}_{agent_id}"
        return self._read_session_if_fresh(session_key)

    async def create_session(
        self, slack_user_id: str, agent_id: str, vertex_ai_session_id: str
    ) -> Session:
        session_key = f"{slack_user_id}_{agent_id}"
        now = datetime.now(UTC)
        data = {
            "user_id": slack_user_id,
            "agent_id": agent_id,
            "vertex_ai_session_id": vertex_ai_session_id,
            "created_at": now,
            "last_activity_at": now,
        }
        self.sessions[session_key] = data
        return Session(**data, id=session_key)

    async def update_session_activity(self, session_id: str) -> None:
        if session_id in self.sessions:
            self.sessions[session_id]["last_activity_at"] = datetime.now(UTC)

    async def get_session_by_user(self, user_id: str, agent_id: str) -> Optional[Session]:
        session_key = f"{user_id}_{agent_id}"
        return self._read_session_if_fresh(session_key)

    async def create_session_for_user(
        self, user_id: str, agent_id: str, vertex_ai_session_id: str, platform: str
    ) -> Session:
        session_key = f"{user_id}_{agent_id}"
        now = datetime.now(UTC)
        data = {
            "user_id": user_id,
            "agent_id": agent_id,
            "vertex_ai_session_id": vertex_ai_session_id,
            "platforms_used": [platform],
            "last_active_platform": platform,
            "created_at": now,
            "last_activity_at": now,
        }
        self.sessions[session_key] = data
        return Session(**data, id=session_key)

    async def update_session_platforms(self, session_id: str, platform: str) -> None:
        if session_id not in self.sessions:
            return
        platforms = self.sessions[session_id].setdefault("platforms_used", [])
        if platform not in platforms:
            platforms.append(platform)
        self.sessions[session_id]["last_active_platform"] = platform
        self.sessions[session_id]["last_activity_at"] = datetime.now(UTC)

    def _read_session_if_fresh(self, session_key: str) -> Optional[Session]:
        data = self.sessions.get(session_key)
        if not data:
            return None
        last_activity = data.get("last_activity_at")
        if last_activity:
            expiry = last_activity + timedelta(minutes=SESSION_TIMEOUT_MINUTES)
            if datetime.now(UTC) > expiry:
                del self.sessions[session_key]
                return None
        return Session(**data, id=session_key)

    # ---- Scheduled job methods ----

    async def get_scheduled_job(self, job_id: str) -> Optional[ScheduledJob]:
        data = self.scheduled_jobs.get(job_id)
        if not data:
            return None
        return ScheduledJob(**data, id=job_id)

    async def create_scheduled_job(self, job_data: dict) -> ScheduledJob:
        job_id = f"job-{uuid.uuid4().hex[:8]}"
        now = datetime.now(UTC)
        data = dict(job_data)
        data["created_at"] = now
        data["updated_at"] = now
        self.scheduled_jobs[job_id] = data
        return ScheduledJob(**data, id=job_id)

    async def update_scheduled_job(
        self, job_id: str, updates: dict
    ) -> Optional[ScheduledJob]:
        if job_id not in self.scheduled_jobs:
            return None
        self.scheduled_jobs[job_id].update(updates)
        self.scheduled_jobs[job_id]["updated_at"] = datetime.now(UTC)
        return await self.get_scheduled_job(job_id)

    async def delete_scheduled_job(self, job_id: str) -> None:
        self.scheduled_jobs.pop(job_id, None)

    async def list_scheduled_jobs(
        self,
        agent_id: Optional[str] = None,
        user_id: Optional[str] = None,
        enabled_only: bool = False,
    ) -> List[ScheduledJob]:
        results = []
        for jid, data in self.scheduled_jobs.items():
            if agent_id is not None and data.get("agent_id") != agent_id:
                continue
            if user_id is not None and data.get("user_id") != user_id:
                continue
            if enabled_only and not data.get("enabled", True):
                continue
            results.append(ScheduledJob(**data, id=jid))
        return results

    async def acquire_job_execution_lock(
        self, job_id: str, execution_id: str, lock_timeout_seconds: int = 300
    ) -> bool:
        data = self.scheduled_jobs.get(job_id)
        if not data:
            return False
        if not data.get("enabled", True):
            return False
        existing_lock = data.get("execution_started_at")
        if existing_lock:
            lock_expiry = existing_lock + timedelta(seconds=lock_timeout_seconds)
            if datetime.now(UTC) < lock_expiry:
                return False
        if data.get("last_execution_id") == execution_id:
            return False
        data["execution_started_at"] = datetime.now(UTC)
        data["last_execution_id"] = execution_id
        return True

    async def release_job_execution_lock(
        self, job_id: str, success: bool, error: Optional[str] = None
    ) -> None:
        data = self.scheduled_jobs.get(job_id)
        if not data:
            return
        data["execution_started_at"] = None
        data["last_execution_at"] = datetime.now(UTC)
        data["updated_at"] = datetime.now(UTC)
        if success:
            data["consecutive_failures"] = 0
            data["last_error"] = None
        else:
            data["last_error"] = error
            data["consecutive_failures"] = data.get("consecutive_failures", 0) + 1

    # ---- User identity methods ----

    async def create_user(self, user: User) -> str:
        user_id = f"user-{uuid.uuid4().hex[:8]}"
        data = user.model_dump(exclude={"id"})
        data["identities"] = [identity.model_dump() for identity in user.identities]
        now = datetime.now(UTC)
        data["created_at"] = now
        data["updated_at"] = now
        self.users[user_id] = data
        return user_id

    async def get_user_by_id(self, user_id: str) -> Optional[User]:
        data = self.users.get(user_id)
        if not data:
            return None
        return User(**data, id=user_id)

    async def get_user_by_identity(
        self, platform: str, platform_user_id: str
    ) -> Optional[User]:
        for uid, data in self.users.items():
            for identity in data.get("identities", []):
                if (
                    identity.get("platform") == platform
                    and identity.get("platform_user_id") == platform_user_id
                ):
                    return User(**data, id=uid)
        return None

    async def get_user_by_email(self, email: str) -> Optional[User]:
        for uid, data in self.users.items():
            if data.get("email") == email:
                return User(**data, id=uid)
        return None

    async def get_user_by_primary_name(self, primary_name: str) -> Optional[User]:
        for uid, data in self.users.items():
            if data.get("primary_name") == primary_name:
                return User(**data, id=uid)
        return None

    async def add_user_identity(
        self, user_id: str, identity: PlatformIdentity
    ) -> None:
        data = self.users.get(user_id)
        if not data:
            return
        identities = data.setdefault("identities", [])
        identities.append(identity.model_dump())
        data["updated_at"] = datetime.now(UTC)
