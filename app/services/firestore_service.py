# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Firestore service for agent and session management."""
import logging
from datetime import datetime, timedelta, UTC
from typing import List, Optional

from google.cloud.firestore import AsyncClient, FieldFilter, ArrayUnion

from app.config import get_settings
from app.core.exceptions import DuplicateScheduledJobError, ScheduledJobReadError
from app.models.agent import Agent
from app.models.session import Session
from app.models.scheduled_job import ScheduledJob, job_identity_key
from app.models.user import User, PlatformIdentity
from app.utils.datetime_helpers import to_aware_utc

logger = logging.getLogger(__name__)


class FirestoreService:
    """Handles all Firestore operations for agents and sessions."""

    def __init__(self):
        """Initialize Firestore client."""
        settings = get_settings()
        self.client = AsyncClient(project=settings.gcp_project_id)
        self.agents_collection = settings.firestore_agents_collection
        self.sessions_collection = settings.firestore_sessions_collection
        self.scheduled_jobs_collection = settings.firestore_scheduled_jobs_collection
        self.users_collection = "users"  # User identity collection
        # Agent-to-agent conversation state (see app/api/v1/agents_mcp.py).
        # Kept separate from `sessions` so A2A traffic never pollutes the
        # user-session model or the admin UI's session views.
        self.a2a_sessions_collection = "a2a_sessions"
        logger.info(f"Firestore client initialized for project: {settings.gcp_project_id}")

    async def get_agent_by_bot_id(self, bot_id: str) -> Optional[Agent]:
        """
        Retrieve agent configuration by Slack bot ID.

        Args:
            bot_id: Slack bot user ID (B...)

        Returns:
            Agent configuration if found, None otherwise
        """
        try:
            query = (
                self.client.collection(self.agents_collection)
                .where("slack_bot_id", "==", bot_id)
                .limit(1)
            )

            docs = [d async for d in query.stream()]

            if not docs:
                logger.warning(f"No agent found for bot_id: {bot_id}")
                return None

            data = docs[0].to_dict()
            agent = Agent(**data, id=docs[0].id)
            logger.info(f"Found agent: {agent.display_name} (id: {agent.id})")
            return agent

        except Exception as e:
            logger.error(f"Error fetching agent by bot_id {bot_id}: {e}")
            return None

    async def get_session(
        self, slack_user_id: str, agent_id: str
    ) -> Optional[Session]:
        """
        Get existing session for user + agent combination if not expired.

        Sessions expire after `session_timeout_minutes` of inactivity.
        If the session has expired, it will be deleted and None returned.

        Args:
            slack_user_id: Slack user ID (U...)
            agent_id: Agent ID from agents collection

        Returns:
            Session if found and not expired, None otherwise
        """
        try:
            settings = get_settings()
            session_key = f"{slack_user_id}_{agent_id}"
            doc = await self.client.collection(self.sessions_collection).document(session_key).get()

            if not doc.exists:
                logger.info(f"No existing session for {session_key}")
                return None

            data = doc.to_dict()

            # Check if session has expired
            last_activity = data.get("last_activity_at")
            if last_activity:
                # Handle both datetime objects and Firestore timestamps
                last_activity = to_aware_utc(last_activity)

                expiry_time = last_activity + timedelta(minutes=settings.session_timeout_minutes)
                if datetime.now(UTC) > expiry_time:
                    logger.info(
                        f"Session {session_key} expired (last activity: {last_activity}, "
                        f"timeout: {settings.session_timeout_minutes} minutes)"
                    )
                    # Delete the expired session
                    await self.client.collection(self.sessions_collection).document(session_key).delete()
                    return None

            session = Session(**data, id=doc.id)
            logger.info(f"Found existing session: {session.id}")
            return session

        except Exception as e:
            logger.error(f"Error fetching session for {slack_user_id}/{agent_id}: {e}")
            return None

    async def create_session(
        self, slack_user_id: str, agent_id: str, vertex_ai_session_id: str
    ) -> Session:
        """
        Create new session mapping.

        Args:
            slack_user_id: Slack user ID (U...)
            agent_id: Agent ID from agents collection
            vertex_ai_session_id: Vertex AI session ID

        Returns:
            Newly created Session

        Raises:
            Exception: If session creation fails
        """
        try:
            session_key = f"{slack_user_id}_{agent_id}"
            now = datetime.now(UTC)

            session_data = {
                "slack_user_id": slack_user_id,
                "agent_id": agent_id,
                "vertex_ai_session_id": vertex_ai_session_id,
                "created_at": now,
                "last_activity_at": now,
            }

            await self.client.collection(self.sessions_collection).document(
                session_key
            ).set(session_data)

            session = Session(**session_data, id=session_key)
            logger.info(f"Created new session: {session.id}")
            return session

        except Exception as e:
            logger.error(f"Error creating session for {slack_user_id}/{agent_id}: {e}")
            raise

    async def update_session_activity(self, session_id: str) -> None:
        """
        Update last activity timestamp for a session.

        Args:
            session_id: Session document ID

        Raises:
            Exception: If update fails
        """
        try:
            await self.client.collection(self.sessions_collection).document(
                session_id
            ).update({"last_activity_at": datetime.now(UTC)})

            logger.debug(f"Updated activity timestamp for session: {session_id}")

        except Exception as e:
            logger.error(f"Error updating session activity for {session_id}: {e}")
            raise

    async def mark_run_started(
        self, session_id: str, marker: dict, *, collection: Optional[str] = None
    ) -> None:
        """
        Record that a run is in flight on this session.

        Best-effort on purpose: losing the marker costs us the chance to
        notice a cut-off later, which is not worth failing a user's message
        over. See app/services/run_tracker.py.
        """
        try:
            await self.client.collection(
                collection or self.sessions_collection
            ).document(session_id).update({"active_run": marker})
        except Exception as e:
            logger.warning(f"Could not mark run started on session {session_id}: {e}")

    async def clear_active_run(
        self, session_id: str, *, collection: Optional[str] = None
    ) -> None:
        """Record that the run came back. Best-effort, same reasoning."""
        try:
            await self.client.collection(
                collection or self.sessions_collection
            ).document(session_id).update({"active_run": None})
        except Exception as e:
            logger.warning(f"Could not clear run marker on session {session_id}: {e}")

    async def get_agent_by_id(self, agent_id: str) -> Optional[Agent]:
        """
        Retrieve agent configuration by document ID.

        Args:
            agent_id: Firestore document ID

        Returns:
            Agent configuration if found, None otherwise
        """
        try:
            doc = await self.client.collection(self.agents_collection).document(agent_id).get()

            if not doc.exists:
                logger.warning(f"No agent found for id: {agent_id}")
                return None

            data = doc.to_dict()
            agent = Agent(**data, id=doc.id)
            logger.info(f"Found agent: {agent.display_name} (id: {agent.id})")
            return agent

        except Exception as e:
            logger.error(f"Error fetching agent by id {agent_id}: {e}")
            return None

    async def list_agents(self) -> list[Agent]:
        """
        List all agent configurations.

        Returns:
            List of all agent configurations
        """
        try:
            docs = await self.client.collection(self.agents_collection).get()
            agents = []
            for doc in docs:
                # Underscore-prefixed docs are markers, not agents — e.g. the
                # "_placeholder" doc the Firestore console forces you to create
                # when making the collection. Skip them silently instead of
                # emitting a multi-line validation warning on every listing.
                if doc.id.startswith("_"):
                    continue
                try:
                    data = doc.to_dict()
                    agent = Agent(**data, id=doc.id)
                    agents.append(agent)
                except Exception as validation_error:
                    logger.warning(f"Skipping agent {doc.id} due to validation error: {validation_error}")
                    continue

            logger.info(f"Listed {len(agents)} agents")
            return agents

        except Exception as e:
            logger.error(f"Error listing agents: {e}")
            return []

    async def get_agent_by_display_name(self, display_name: str) -> Optional[Agent]:
        """
        Retrieve agent configuration by display name (case-insensitive).

        Display names are the human-facing lookup key the deploy pipeline
        (register_agent.py) upserts on, so they are unique in practice.

        Args:
            display_name: The agent's display name, e.g. "Mickey Marathon"

        Returns:
            Agent configuration if found, None otherwise
        """
        wanted = display_name.strip().lower()
        if not wanted:
            return None
        for agent in await self.list_agents():
            if agent.display_name.strip().lower() == wanted:
                return agent
        logger.warning(f"No agent found with display_name: {display_name}")
        return None

    async def get_a2a_session(self, session_key: str) -> Optional[dict]:
        """
        Get the stored entry for an agent-to-agent conversation.

        Args:
            session_key: Deterministic key for (caller agent, target agent,
                on-behalf-of user) — see agents_mcp._a2a_session_key.

        Returns:
            The stored entry ({"vertex_ai_session_id", "engine_id", ...}) if
            one exists, None otherwise. Entries written before engine
            tracking (#18) have no "engine_id" key; callers must treat that
            as stale, since the session cannot be tied to a live engine.
        """
        try:
            doc = await self.client.collection(self.a2a_sessions_collection).document(session_key).get()
            if not doc.exists:
                return None
            return doc.to_dict()
        except Exception as e:
            logger.error(f"Error fetching a2a session {session_key}: {e}")
            return None

    async def save_a2a_session(
        self, session_key: str, vertex_ai_session_id: str, engine_id: str
    ) -> None:
        """
        Persist the Vertex AI session for an agent-to-agent conversation.

        Args:
            session_key: Deterministic key for (caller, target, user).
            vertex_ai_session_id: Combined session ID from VertexAIService.create_session.
            engine_id: The target agent's engine resource name at creation
                time. Sessions live on one engine, so this is what lets a
                later lookup detect that the agent was redeployed and the
                stored session is dead (#18).
        """
        try:
            await self.client.collection(self.a2a_sessions_collection).document(session_key).set({
                "vertex_ai_session_id": vertex_ai_session_id,
                "engine_id": engine_id,
                "updated_at": datetime.now(UTC),
            })
        except Exception as e:
            logger.error(f"Error saving a2a session {session_key}: {e}")

    async def delete_a2a_session(self, session_key: str) -> None:
        """Drop a stored agent-to-agent session entry (e.g. it proved dead)."""
        try:
            await self.client.collection(self.a2a_sessions_collection).document(session_key).delete()
        except Exception as e:
            logger.error(f"Error deleting a2a session {session_key}: {e}")

    async def get_scheduled_job(self, job_id: str) -> Optional[ScheduledJob]:
        """
        Get scheduled job by document ID.

        Args:
            job_id: Firestore document ID

        Returns:
            ScheduledJob if found, None otherwise
        """
        try:
            doc = await self.client.collection(self.scheduled_jobs_collection).document(job_id).get()

            if not doc.exists:
                logger.warning(f"No scheduled job found for id: {job_id}")
                return None

            data = doc.to_dict()
            # Handle Firestore timestamps
            for field in ["last_execution_at", "execution_started_at", "created_at", "updated_at"]:
                if field in data:
                    data[field] = to_aware_utc(data[field])

            job = ScheduledJob(**data, id=doc.id)
            logger.debug(f"Found scheduled job: {job.name} (id: {job.id})")
            return job

        except Exception as e:
            logger.error(f"Error fetching scheduled job {job_id}: {e}")
            return None

    async def create_scheduled_job(self, job_data: dict) -> ScheduledJob:
        """
        Create a new scheduled job document.

        Args:
            job_data: Dictionary of job fields

        Returns:
            Newly created ScheduledJob

        Raises:
            Exception: If creation fails
        """
        identity = job_identity_key(
            job_data.get("agent_id"), job_data.get("user_id"), job_data.get("name")
        )

        # Write-time uniqueness guard. This runs a plain equality query and
        # inspects only document ids — it never deserializes a candidate into
        # ScheduledJob — so it still holds when a malformed document would
        # defeat the model-level lookup in _find_existing_job (#14).
        if identity:
            existing_ids = [
                doc.id
                async for doc in self.client.collection(self.scheduled_jobs_collection)
                .where(filter=FieldFilter("identity_key", "==", identity))
                .stream()
            ]
            if existing_ids:
                raise DuplicateScheduledJobError(
                    f"Scheduled job with identity {identity} already exists "
                    f"(document {existing_ids[0]}); refusing to insert a duplicate"
                )

        try:
            now = datetime.now(UTC)
            job_data["created_at"] = now
            job_data["updated_at"] = now
            job_data["identity_key"] = identity

            doc_ref = self.client.collection(self.scheduled_jobs_collection).document()
            await doc_ref.set(job_data)

            job = ScheduledJob(**job_data, id=doc_ref.id)
            logger.info(f"Created scheduled job: {job.name} (id: {job.id})")
            return job

        except Exception as e:
            logger.error(f"Error creating scheduled job: {e}")
            raise

    async def update_scheduled_job(self, job_id: str, updates: dict) -> Optional[ScheduledJob]:
        """
        Update scheduled job fields.

        Args:
            job_id: Firestore document ID
            updates: Dictionary of fields to update

        Returns:
            Updated ScheduledJob

        Raises:
            Exception: If update fails
        """
        try:
            updates["updated_at"] = datetime.now(UTC)

            # Keep identity_key in step with the name, or a rename would leave
            # the uniqueness guard matching on the job's old name — blocking a
            # legitimate re-create of that name and missing real duplicates.
            if "name" in updates:
                current = await self.get_scheduled_job(job_id)
                if current:
                    updates["identity_key"] = job_identity_key(
                        current.agent_id, current.user_id, updates["name"]
                    )

            await self.client.collection(self.scheduled_jobs_collection).document(job_id).update(updates)

            logger.info(f"Updated scheduled job: {job_id}")
            return await self.get_scheduled_job(job_id)

        except Exception as e:
            logger.error(f"Error updating scheduled job {job_id}: {e}")
            raise

    async def delete_scheduled_job(self, job_id: str) -> None:
        """
        Delete scheduled job document.

        Args:
            job_id: Firestore document ID

        Raises:
            Exception: If deletion fails
        """
        try:
            await self.client.collection(self.scheduled_jobs_collection).document(job_id).delete()
            logger.info(f"Deleted scheduled job: {job_id}")

        except Exception as e:
            logger.error(f"Error deleting scheduled job {job_id}: {e}")
            raise

    async def list_scheduled_jobs(
        self,
        agent_id: Optional[str] = None,
        user_id: Optional[str] = None,
        enabled_only: bool = False,
        strict: bool = False,
    ) -> List[ScheduledJob]:
        """
        List scheduled jobs with optional filters.

        Failure handling is deliberately asymmetric, because the two callers
        need opposite behavior when a stored document won't parse (#14):

        * The dispatcher (get_due_jobs) must keep going — one bad document
          must not stop every other agent's jobs from running. It gets the
          default, skip-and-log behavior.
        * The upsert lookup (_find_existing_job) decides whether to *write*.
          A silently short result there means a duplicate insert, so it passes
          strict=True and gets an exception instead of a partial list.

        Query-level failures always raise. This function must never report an
        unreadable collection as an empty one.

        Args:
            agent_id: Filter by agent ID
            user_id: Filter by user ID
            enabled_only: Only return enabled jobs
            strict: Raise ScheduledJobReadError if any document fails to parse,
                rather than skipping it

        Returns:
            List of ScheduledJob objects

        Raises:
            ScheduledJobReadError: If strict and a document cannot be parsed
            Exception: If the underlying Firestore query fails
        """
        query = self.client.collection(self.scheduled_jobs_collection)

        if agent_id:
            query = query.where("agent_id", "==", agent_id)
        if user_id:
            query = query.where("user_id", "==", user_id)
        if enabled_only:
            query = query.where("enabled", "==", True)

        jobs = []
        skipped = []
        async for doc in query.stream():
            # Per-document, so one unparseable document costs exactly one job
            # instead of blanking the entire result set.
            try:
                data = doc.to_dict()
                # Handle Firestore timestamps
                for field in ["last_execution_at", "execution_started_at", "created_at", "updated_at"]:
                    if field in data:
                        data[field] = to_aware_utc(data[field])
                jobs.append(ScheduledJob(**data, id=doc.id))
            except Exception as e:
                skipped.append(doc.id)
                logger.error(
                    f"Skipping unparseable scheduled job document {doc.id}: {e}"
                )
                if strict:
                    raise ScheduledJobReadError(
                        f"Scheduled job document {doc.id} could not be parsed; "
                        f"refusing to report a partial job list"
                    ) from e

        if skipped:
            logger.error(
                f"Listed {len(jobs)} scheduled jobs; skipped {len(skipped)} "
                f"unparseable document(s): {', '.join(skipped)}"
            )
        else:
            logger.info(f"Listed {len(jobs)} scheduled jobs")
        return jobs

    async def acquire_job_execution_lock(
        self,
        job_id: str,
        execution_id: str,
        lock_timeout_seconds: int = 300,
    ) -> bool:
        """
        Acquire execution lock for a scheduled job.

        Uses simple read-then-write pattern. Not perfectly atomic but sufficient
        for preventing most duplicate executions.

        Args:
            job_id: Firestore document ID
            execution_id: Unique execution ID for this attempt
            lock_timeout_seconds: Lock expiry time in seconds

        Returns:
            True if lock acquired, False if job is already being executed
        """
        try:
            doc_ref = self.client.collection(self.scheduled_jobs_collection).document(job_id)
            doc = await doc_ref.get()

            if not doc.exists:
                return False

            data = doc.to_dict()

            # Check if job is enabled
            if not data.get("enabled", True):
                logger.info(f"Job {job_id} is disabled, skipping")
                return False

            # Check if already being executed (lock is held)
            execution_started_at = data.get("execution_started_at")
            if execution_started_at:
                # Handle Firestore timestamp
                execution_started_at = to_aware_utc(execution_started_at)

                lock_expiry = execution_started_at + timedelta(seconds=lock_timeout_seconds)
                if datetime.now(UTC) < lock_expiry:
                    logger.info(f"Job {job_id} is already being executed, skipping")
                    return False
                else:
                    logger.warning(f"Job {job_id} lock expired, allowing new execution")

            # Check for duplicate execution ID
            if data.get("last_execution_id") == execution_id:
                logger.info(f"Job {job_id} already executed with id {execution_id}, skipping")
                return False

            # Acquire lock
            await doc_ref.update({
                "execution_started_at": datetime.now(UTC),
                "last_execution_id": execution_id,
            })

            logger.info(f"Acquired execution lock for job {job_id}")
            return True

        except Exception as e:
            logger.error(f"Error acquiring lock for job {job_id}: {e}")
            return False

    async def release_job_execution_lock(
        self,
        job_id: str,
        success: bool,
        error: Optional[str] = None,
    ) -> None:
        """
        Release execution lock and update job status.

        Args:
            job_id: Firestore document ID
            success: Whether execution succeeded
            error: Error message if failed
        """
        try:
            updates = {
                "execution_started_at": None,
                "last_execution_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }

            if success:
                updates["consecutive_failures"] = 0
                updates["last_error"] = None
            else:
                updates["last_error"] = error
                # Increment failures using a transaction
                doc_ref = self.client.collection(self.scheduled_jobs_collection).document(job_id)
                doc = await doc_ref.get()
                if doc.exists:
                    current_failures = doc.to_dict().get("consecutive_failures", 0)
                    updates["consecutive_failures"] = current_failures + 1

            await self.client.collection(self.scheduled_jobs_collection).document(job_id).update(updates)
            logger.info(f"Released execution lock for job {job_id} (success={success})")

        except Exception as e:
            logger.error(f"Error releasing lock for job {job_id}: {e}")

    # User Identity Management Methods

    async def create_user(self, user: User) -> str:
        """
        Create a new user document.

        Args:
            user: User object to create

        Returns:
            Created user's document ID

        Raises:
            Exception: If creation fails
        """
        try:
            now = datetime.now(UTC)
            user_data = user.model_dump(exclude={"id"})
            user_data["created_at"] = now
            user_data["updated_at"] = now

            # Convert PlatformIdentity objects to dicts
            user_data["identities"] = [
                identity.model_dump() for identity in user.identities
            ]

            doc_ref = self.client.collection(self.users_collection).document()
            await doc_ref.set(user_data)

            logger.info(f"Created user: {doc_ref.id} ({user.primary_name})")
            return doc_ref.id

        except Exception as e:
            logger.error(f"Error creating user: {e}")
            raise

    async def get_user_by_id(self, user_id: str) -> Optional[User]:
        """
        Get user by document ID.

        Args:
            user_id: Firestore document ID

        Returns:
            User if found, None otherwise
        """
        try:
            doc = await self.client.collection(self.users_collection).document(user_id).get()

            if not doc.exists:
                logger.debug(f"No user found for id: {user_id}")
                return None

            data = doc.to_dict()

            # Handle Firestore timestamps
            for field in ["created_at", "updated_at"]:
                if field in data:
                    data[field] = to_aware_utc(data[field])

            # Handle timestamp in identities
            if "identities" in data:
                for identity in data["identities"]:
                    if "linked_at" in identity:
                        identity["linked_at"] = to_aware_utc(identity["linked_at"])

            user = User(**data, id=doc.id)
            logger.debug(f"Found user: {user.primary_name} (id: {user.id})")
            return user

        except Exception as e:
            logger.error(f"Error fetching user by id {user_id}: {e}")
            return None

    async def get_user_by_identity(
        self, platform: str, platform_user_id: str
    ) -> Optional[User]:
        """
        Find user by platform identity.

        Args:
            platform: Platform name (e.g., "slack", "google_chat")
            platform_user_id: Platform-specific user ID

        Returns:
            User if found, None otherwise
        """
        try:
            # Query for user with matching platform identity
            # Note: This is a simplified query. For more robust matching, we may need
            # to fetch all users and filter in memory or create a composite index.
            query = self.client.collection(self.users_collection).limit(100)

            # Fetch and filter in memory (since array_contains doesn't work well with objects)
            matching_user = None
            async for doc in query.stream():
                data = doc.to_dict()
                identities = data.get("identities", [])
                for identity in identities:
                    if (identity.get("platform") == platform and
                        identity.get("platform_user_id") == platform_user_id):
                        matching_user = (doc, data)
                        break
                if matching_user:
                    break

            if not matching_user:
                logger.debug(f"No user found for {platform}:{platform_user_id}")
                return None

            doc, data = matching_user

            # Handle Firestore timestamps
            for field in ["created_at", "updated_at"]:
                if field in data:
                    data[field] = to_aware_utc(data[field])

            # Handle timestamp in identities
            if "identities" in data:
                for identity in data["identities"]:
                    if "linked_at" in identity:
                        identity["linked_at"] = to_aware_utc(identity["linked_at"])

            user = User(**data, id=doc.id)
            logger.debug(f"Found user {user.id} for {platform}:{platform_user_id}")
            return user

        except Exception as e:
            logger.error(f"Error fetching user by identity {platform}:{platform_user_id}: {e}")
            return None

    async def get_user_by_email(self, email: str) -> Optional[User]:
        """
        Find user by email address.

        Used for auto-linking (especially for Google Chat users).

        Args:
            email: Email address to search for

        Returns:
            User if found, None otherwise
        """
        try:
            query = (
                self.client.collection(self.users_collection)
                .where("email", "==", email)
                .limit(1)
            )

            docs = [d async for d in query.stream()]

            if not docs:
                logger.debug(f"No user found for email: {email}")
                return None

            data = docs[0].to_dict()

            # Handle Firestore timestamps
            for field in ["created_at", "updated_at"]:
                if field in data:
                    data[field] = to_aware_utc(data[field])

            # Handle timestamp in identities
            if "identities" in data:
                for identity in data["identities"]:
                    if "linked_at" in identity:
                        identity["linked_at"] = to_aware_utc(identity["linked_at"])

            user = User(**data, id=docs[0].id)
            logger.debug(f"Found user {user.id} for email: {email}")
            return user

        except Exception as e:
            logger.error(f"Error fetching user by email {email}: {e}")
            return None

    async def get_user_by_any_name(self, name: str) -> Optional[User]:
        """
        Resolve a free-text name to a single canonical user.

        This is the unified user-resolution used by the scheduler MCP server.
        The agent passes the name from the `[From: <name>] ...` message prefix,
        which may match either the user's canonical ``primary_name`` OR any of
        their per-platform ``display_name`` values (e.g. a Slack handle). The
        match is case-insensitive and whitespace-trimmed.

        Resolution must be *deterministic*: ``create`` and ``list`` both call
        this, so for a given name they must always land on the same user
        record even when two users could match. We therefore sort all matches
        by ``created_at`` (then ``id`` as a tiebreaker) and return the first.
        A plain ``primary_name``-only equality query returned a
        non-deterministic "first match" from the stream, which let ``create``
        and ``list`` disagree.

        Volume is small (one row per real human across all platforms), so an
        unfiltered scan + in-memory match is fine — same approach as
        ``list_users``.

        Args:
            name: Free-text name from the message prefix.

        Returns:
            The single canonical User if at least one matches; None otherwise.
        """
        if not name or not name.strip():
            return None
        needle = name.strip().casefold()

        try:
            matches: list[User] = []
            async for doc in self.client.collection(self.users_collection).stream():
                data = doc.to_dict()
                candidates = [data.get("primary_name")]
                candidates.extend(
                    identity.get("display_name")
                    for identity in data.get("identities", [])
                )
                if any(
                    isinstance(c, str) and c.strip().casefold() == needle
                    for c in candidates
                ):
                    for field in ["created_at", "updated_at"]:
                        if field in data:
                            data[field] = to_aware_utc(data[field])
                    for identity in data.get("identities", []):
                        if "linked_at" in identity:
                            identity["linked_at"] = to_aware_utc(identity["linked_at"])
                    matches.append(User(**data, id=doc.id))

            if not matches:
                logger.debug(f"No user found for name: {name!r}")
                return None
            if len(matches) > 1:
                logger.warning(
                    f"Name {name!r} matched {len(matches)} users; returning the "
                    f"canonical (earliest-created) one. Disambiguate upstream if wrong."
                )
            matches.sort(key=lambda u: (u.created_at, u.id or ""))
            user = matches[0]
            logger.debug(f"Resolved name {name!r} to user {user.id}")
            return user

        except Exception as e:
            logger.error(f"Error resolving user by name {name!r}: {e}")
            return None

    async def list_users(self) -> List[User]:
        """
        List all users in the users collection.

        Used by the admin UI to populate the user dropdown on the scheduled
        job form. Volume is small (one row per real human across all
        platforms), so an unfiltered scan is fine.
        """
        try:
            users: List[User] = []
            async for doc in self.client.collection(self.users_collection).stream():
                data = doc.to_dict()
                for field in ("created_at", "updated_at"):
                    if field in data:
                        data[field] = to_aware_utc(data[field])
                if "identities" in data:
                    for identity in data["identities"]:
                        if "linked_at" in identity:
                            identity["linked_at"] = to_aware_utc(identity["linked_at"])
                try:
                    users.append(User(**data, id=doc.id))
                except Exception as validation_error:
                    logger.warning(
                        f"Skipping user {doc.id} due to validation error: {validation_error}"
                    )
                    continue
            users.sort(key=lambda u: u.primary_name.lower())
            return users
        except Exception as e:
            logger.error(f"Error listing users: {e}")
            return []

    async def update_user(self, user_id: str, fields: dict) -> None:
        """
        Update top-level fields on a user document.

        Used by the admin UI (primary_name, email, default_timezone) and by
        the message processor to seed default_timezone from a Slack profile.
        Does not touch the identities array — use add_user_identity for that.

        Args:
            user_id: User document ID
            fields: Field names and new values to set

        Raises:
            Exception: If update fails
        """
        try:
            doc_ref = self.client.collection(self.users_collection).document(user_id)
            await doc_ref.update({
                **fields,
                "updated_at": datetime.now(UTC),
            })
            logger.info(f"Updated user {user_id}: {sorted(fields.keys())}")
        except Exception as e:
            logger.error(f"Error updating user {user_id}: {e}")
            raise

    async def add_user_identity(
        self, user_id: str, identity: PlatformIdentity
    ) -> None:
        """
        Add a new platform identity to an existing user.

        Args:
            user_id: User document ID
            identity: PlatformIdentity to add

        Raises:
            Exception: If update fails
        """
        try:
            doc_ref = self.client.collection(self.users_collection).document(user_id)

            # Use array union to add identity
            await doc_ref.update({
                "identities": ArrayUnion([identity.model_dump()]),
                "updated_at": datetime.now(UTC)
            })

            logger.info(f"Added {identity.platform} identity to user {user_id}")

        except Exception as e:
            logger.error(f"Error adding identity to user {user_id}: {e}")
            raise

    # New user-based session methods

    async def get_session_by_user(
        self, user_id: str, agent_id: str
    ) -> Optional[Session]:
        """
        Get existing session for unified user + agent combination if not expired.

        Sessions expire after `session_timeout_minutes` of inactivity.
        If the session has expired, it will be deleted and None returned.

        Args:
            user_id: Unified user ID from users collection
            agent_id: Agent ID from agents collection

        Returns:
            Session if found and not expired, None otherwise
        """
        try:
            settings = get_settings()
            session_key = f"{user_id}_{agent_id}"
            doc = await self.client.collection(self.sessions_collection).document(session_key).get()

            if not doc.exists:
                logger.info(f"No existing session for user {user_id} + agent {agent_id}")
                return None

            data = doc.to_dict()

            # Check if session has expired
            last_activity = data.get("last_activity_at")
            if last_activity:
                # Handle both datetime objects and Firestore timestamps
                last_activity = to_aware_utc(last_activity)

                expiry_time = last_activity + timedelta(minutes=settings.session_timeout_minutes)
                if datetime.now(UTC) > expiry_time:
                    logger.info(
                        f"Session {session_key} expired (last activity: {last_activity}, "
                        f"timeout: {settings.session_timeout_minutes} minutes)"
                    )
                    # Delete the expired session
                    await self.client.collection(self.sessions_collection).document(session_key).delete()
                    return None

            session = Session(**data, id=doc.id)
            logger.info(f"Found existing session: {session.id}")
            return session

        except Exception as e:
            logger.error(f"Error fetching session for user {user_id}/agent {agent_id}: {e}")
            return None

    async def create_session_for_user(
        self, user_id: str, agent_id: str, vertex_ai_session_id: str, platform: str
    ) -> Session:
        """
        Create new session mapping for unified user.

        Args:
            user_id: Unified user ID from users collection
            agent_id: Agent ID from agents collection
            vertex_ai_session_id: Vertex AI session ID
            platform: Platform this session was created from

        Returns:
            Newly created Session

        Raises:
            Exception: If session creation fails
        """
        try:
            session_key = f"{user_id}_{agent_id}"
            now = datetime.now(UTC)

            session_data = {
                "user_id": user_id,
                "agent_id": agent_id,
                "vertex_ai_session_id": vertex_ai_session_id,
                "platforms_used": [platform],
                "last_active_platform": platform,
                "created_at": now,
                "last_activity_at": now,
            }

            await self.client.collection(self.sessions_collection).document(
                session_key
            ).set(session_data)

            session = Session(**session_data, id=session_key)
            logger.info(f"Created new session: {session.id} for user {user_id}")
            return session

        except Exception as e:
            logger.error(f"Error creating session for user {user_id}/agent {agent_id}: {e}")
            raise

    async def get_agent_by_scheduler_api_key_hash(self, key_hash: str) -> Optional[Agent]:
        """
        Look up an agent by the SHA-256 hash of its scheduler MCP API key.

        Used by the scheduler MCP endpoint to authenticate incoming requests
        and resolve which agent is calling.

        Args:
            key_hash: SHA-256 hex digest of the API key from X-API-Key header

        Returns:
            Agent if a matching hash is found, None otherwise
        """
        try:
            query = (
                self.client.collection(self.agents_collection)
                .where(filter=FieldFilter("scheduler_api_key_hash", "==", key_hash))
                .limit(1)
            )
            docs = [d async for d in query.stream()]
            if not docs:
                return None
            data = docs[0].to_dict()
            return Agent(**data, id=docs[0].id)
        except Exception as e:
            logger.error(f"Error fetching agent by scheduler API key hash: {e}")
            return None

    async def list_recent_sessions_for_agent(
        self, agent_id: str, limit: int = 10
    ) -> List[Session]:
        """
        List the most-recently-active sessions for an agent.

        Used by the admin UI to populate the per-agent recent-sessions table
        and to derive per-platform "last used" timestamps.

        The agent_id filter is applied server-side; ordering by
        last_activity_at would need a composite Firestore index
        (agent_id + last_activity_at), so instead we fetch all matching
        sessions and sort/slice in memory. Per-agent session volume is
        bounded (a session is a (user, agent) pair and gets expired on
        access after session_timeout_minutes of inactivity), so this stays
        cheap for any realistic deployment.
        """
        try:
            query = self.client.collection(self.sessions_collection).where(
                filter=FieldFilter("agent_id", "==", agent_id)
            )

            sessions: List[Session] = []
            async for doc in query.stream():
                data = doc.to_dict()
                for field in ("last_activity_at", "created_at"):
                    if field in data:
                        data[field] = to_aware_utc(data[field])
                try:
                    sessions.append(Session(**data, id=doc.id))
                except Exception as validation_error:
                    logger.warning(
                        f"Skipping session {doc.id} due to validation error: {validation_error}"
                    )
                    continue
            sessions.sort(
                key=lambda s: s.last_activity_at,
                reverse=True,
            )
            return sessions[:limit]

        except Exception as e:
            logger.error(f"Error listing recent sessions for agent {agent_id}: {e}")
            return []

    async def update_session_platforms(
        self, session_id: str, platform: str
    ) -> None:
        """
        Add a platform to the session's platforms_used list if not already present.

        Args:
            session_id: Session document ID
            platform: Platform to add (e.g., "slack", "google_chat")

        Raises:
            Exception: If update fails
        """
        try:
            # Use ArrayUnion to add platform if not already present, and track
            # the most recently used platform so the scheduler MCP can default
            # output_platform to it.
            await self.client.collection(self.sessions_collection).document(
                session_id
            ).update({
                "platforms_used": ArrayUnion([platform]),
                "last_active_platform": platform,
                "last_activity_at": datetime.now(UTC)
            })

            logger.debug(f"Updated platforms for session: {session_id} (added {platform})")

        except Exception as e:
            logger.error(f"Error updating session platforms for {session_id}: {e}")
            raise
