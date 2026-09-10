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

from app.core.exceptions import DuplicateScheduledJobError, ScheduledJobReadError
from app.models.agent import Agent
from app.models.session import Session
from app.models.scheduled_job import ScheduledJob, job_identity_key
from app.models.user import User, PlatformIdentity


SESSION_TIMEOUT_MINUTES = 180


class FakeFirestoreService:
    def __init__(self):
        self.agents: dict[str, dict] = {}
        self.sessions: dict[str, dict] = {}
        self.scheduled_jobs: dict[str, dict] = {}
        self.users: dict[str, dict] = {}
        self.a2a_sessions: dict[str, dict] = {}
        # Set by tests to simulate a Firestore query failure (permissions,
        # transient error, missing index) on the scheduled_jobs collection.
        self.scheduled_jobs_query_error: Optional[Exception] = None

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

    async def get_agent_by_display_name(self, display_name: str) -> Optional[Agent]:
        wanted = display_name.strip().lower()
        if not wanted:
            return None
        for agent in await self.list_agents():
            if agent.display_name.strip().lower() == wanted:
                return agent
        return None

    # ---- A2A session methods ----

    async def get_a2a_session(self, session_key: str) -> Optional[dict]:
        return self.a2a_sessions.get(session_key)

    async def save_a2a_session(
        self, session_key: str, vertex_ai_session_id: str, engine_id: str
    ) -> None:
        self.a2a_sessions[session_key] = {
            "vertex_ai_session_id": vertex_ai_session_id,
            "engine_id": engine_id,
        }

    async def delete_a2a_session(self, session_key: str) -> None:
        self.a2a_sessions.pop(session_key, None)

    # ---- In-flight run markers (see app/services/run_tracker.py) ----

    def _run_marker_store(self, collection: Optional[str]) -> dict[str, dict]:
        return self.a2a_sessions if collection == "a2a_sessions" else self.sessions

    async def mark_run_started(
        self, session_id: str, marker: dict, *, collection: Optional[str] = None
    ) -> None:
        store = self._run_marker_store(collection)
        # Firestore's update() no-ops loudly on a missing doc; the real
        # service swallows that, so the fake simply skips it too.
        if session_id in store:
            store[session_id]["active_run"] = marker

    async def clear_active_run(
        self, session_id: str, *, collection: Optional[str] = None
    ) -> None:
        store = self._run_marker_store(collection)
        if session_id in store:
            store[session_id]["active_run"] = None

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

    async def list_recent_sessions_for_agent(
        self, agent_id: str, limit: int = 10
    ) -> List[Session]:
        matches = []
        for sid, data in self.sessions.items():
            if data.get("agent_id") != agent_id:
                continue
            try:
                matches.append(Session(**data, id=sid))
            except Exception:
                continue
        matches.sort(key=lambda s: s.last_activity_at, reverse=True)
        return matches[:limit]

    # Test helper: insert a session record directly so route tests can seed
    # per-agent sessions without exercising the create path.
    def add_session(self, session_data: dict, session_id: str | None = None) -> str:
        sid = session_id or f"session-{uuid.uuid4().hex[:8]}"
        self.sessions[sid] = dict(session_data)
        return sid

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
        identity = job_identity_key(
            job_data.get("agent_id"), job_data.get("user_id"), job_data.get("name")
        )
        # Mirrors the real write-time guard: an equality scan over stored
        # identity_key values that never parses candidate documents.
        if identity:
            clash = next(
                (
                    jid
                    for jid, data in self.scheduled_jobs.items()
                    if data.get("identity_key") == identity
                ),
                None,
            )
            if clash:
                raise DuplicateScheduledJobError(
                    f"Scheduled job with identity {identity} already exists "
                    f"(document {clash}); refusing to insert a duplicate"
                )

        job_id = f"job-{uuid.uuid4().hex[:8]}"
        now = datetime.now(UTC)
        data = dict(job_data)
        data["created_at"] = now
        data["updated_at"] = now
        data["identity_key"] = identity
        self.scheduled_jobs[job_id] = data
        return ScheduledJob(**data, id=job_id)

    async def update_scheduled_job(
        self, job_id: str, updates: dict
    ) -> Optional[ScheduledJob]:
        if job_id not in self.scheduled_jobs:
            return None
        updates = dict(updates)
        if "name" in updates:
            current = self.scheduled_jobs[job_id]
            updates["identity_key"] = job_identity_key(
                current.get("agent_id"), current.get("user_id"), updates["name"]
            )
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
        strict: bool = False,
    ) -> List[ScheduledJob]:
        if self.scheduled_jobs_query_error is not None:
            raise self.scheduled_jobs_query_error

        results = []
        for jid, data in self.scheduled_jobs.items():
            if agent_id is not None and data.get("agent_id") != agent_id:
                continue
            if user_id is not None and data.get("user_id") != user_id:
                continue
            if enabled_only and not data.get("enabled", True):
                continue
            try:
                results.append(ScheduledJob(**data, id=jid))
            except Exception as e:
                if strict:
                    raise ScheduledJobReadError(
                        f"Scheduled job document {jid} could not be parsed; "
                        f"refusing to report a partial job list"
                    ) from e
                continue
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

    async def get_user_by_any_name(self, name: str) -> Optional[User]:
        if not name or not name.strip():
            return None
        needle = name.strip().casefold()
        matches = []
        for uid, data in self.users.items():
            candidates = [data.get("primary_name")]
            candidates.extend(
                identity.get("display_name")
                for identity in data.get("identities", [])
            )
            if any(
                isinstance(c, str) and c.strip().casefold() == needle
                for c in candidates
            ):
                matches.append(User(**data, id=uid))
        if not matches:
            return None
        matches.sort(key=lambda u: (u.created_at, u.id or ""))
        return matches[0]

    async def list_users(self) -> List[User]:
        users = []
        for uid, data in self.users.items():
            try:
                users.append(User(**data, id=uid))
            except Exception:
                continue
        users.sort(key=lambda u: u.primary_name.lower())
        return users

    async def update_user(self, user_id: str, fields: dict) -> None:
        data = self.users.get(user_id)
        if not data:
            raise ValueError(f"User {user_id} not found")
        data.update(fields)
        data["updated_at"] = datetime.now(UTC)

    async def add_user_identity(
        self, user_id: str, identity: PlatformIdentity
    ) -> None:
        data = self.users.get(user_id)
        if not data:
            return
        identities = data.setdefault("identities", [])
        identities.append(identity.model_dump())
        data["updated_at"] = datetime.now(UTC)
