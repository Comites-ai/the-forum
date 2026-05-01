"""Firestore service for agent and session management."""
import logging
from datetime import datetime, timedelta
from typing import List, Optional

from google.cloud.firestore import AsyncClient, FieldFilter, ArrayUnion

from app.config import get_settings
from app.models.agent import Agent
from app.models.session import Session
from app.models.scheduled_job import ScheduledJob
from app.models.user import User, PlatformIdentity

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
                if hasattr(last_activity, 'timestamp'):
                    last_activity = datetime.fromtimestamp(last_activity.timestamp())

                expiry_time = last_activity + timedelta(minutes=settings.session_timeout_minutes)
                if datetime.utcnow() > expiry_time:
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
            now = datetime.utcnow()

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
            ).update({"last_activity_at": datetime.utcnow()})

            logger.debug(f"Updated activity timestamp for session: {session_id}")

        except Exception as e:
            logger.error(f"Error updating session activity for {session_id}: {e}")
            raise

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
                if field in data and data[field] and hasattr(data[field], "timestamp"):
                    data[field] = datetime.fromtimestamp(data[field].timestamp())

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
        try:
            now = datetime.utcnow()
            job_data["created_at"] = now
            job_data["updated_at"] = now

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
            updates["updated_at"] = datetime.utcnow()

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
    ) -> List[ScheduledJob]:
        """
        List scheduled jobs with optional filters.

        Args:
            agent_id: Filter by agent ID
            user_id: Filter by user ID
            enabled_only: Only return enabled jobs

        Returns:
            List of ScheduledJob objects
        """
        try:
            query = self.client.collection(self.scheduled_jobs_collection)

            if agent_id:
                query = query.where("agent_id", "==", agent_id)
            if user_id:
                query = query.where("user_id", "==", user_id)
            if enabled_only:
                query = query.where("enabled", "==", True)

            jobs = []
            async for doc in query.stream():
                data = doc.to_dict()
                # Handle Firestore timestamps
                for field in ["last_execution_at", "execution_started_at", "created_at", "updated_at"]:
                    if field in data and data[field] and hasattr(data[field], "timestamp"):
                        data[field] = datetime.fromtimestamp(data[field].timestamp())
                jobs.append(ScheduledJob(**data, id=doc.id))

            logger.info(f"Listed {len(jobs)} scheduled jobs")
            return jobs

        except Exception as e:
            logger.error(f"Error listing scheduled jobs: {e}")
            return []

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
                if hasattr(execution_started_at, "timestamp"):
                    execution_started_at = datetime.fromtimestamp(execution_started_at.timestamp())

                lock_expiry = execution_started_at + timedelta(seconds=lock_timeout_seconds)
                if datetime.utcnow() < lock_expiry:
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
                "execution_started_at": datetime.utcnow(),
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
                "last_execution_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
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
            now = datetime.utcnow()
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
                if field in data and data[field] and hasattr(data[field], "timestamp"):
                    data[field] = datetime.fromtimestamp(data[field].timestamp())

            # Handle timestamp in identities
            if "identities" in data:
                for identity in data["identities"]:
                    if "linked_at" in identity and hasattr(identity["linked_at"], "timestamp"):
                        identity["linked_at"] = datetime.fromtimestamp(identity["linked_at"].timestamp())

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
                if field in data and data[field] and hasattr(data[field], "timestamp"):
                    data[field] = datetime.fromtimestamp(data[field].timestamp())

            # Handle timestamp in identities
            if "identities" in data:
                for identity in data["identities"]:
                    if "linked_at" in identity and hasattr(identity["linked_at"], "timestamp"):
                        identity["linked_at"] = datetime.fromtimestamp(identity["linked_at"].timestamp())

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
                if field in data and data[field] and hasattr(data[field], "timestamp"):
                    data[field] = datetime.fromtimestamp(data[field].timestamp())

            # Handle timestamp in identities
            if "identities" in data:
                for identity in data["identities"]:
                    if "linked_at" in identity and hasattr(identity["linked_at"], "timestamp"):
                        identity["linked_at"] = datetime.fromtimestamp(identity["linked_at"].timestamp())

            user = User(**data, id=docs[0].id)
            logger.debug(f"Found user {user.id} for email: {email}")
            return user

        except Exception as e:
            logger.error(f"Error fetching user by email {email}: {e}")
            return None

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
                "updated_at": datetime.utcnow()
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
                if hasattr(last_activity, 'timestamp'):
                    last_activity = datetime.fromtimestamp(last_activity.timestamp())

                expiry_time = last_activity + timedelta(minutes=settings.session_timeout_minutes)
                if datetime.utcnow() > expiry_time:
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
            now = datetime.utcnow()

            session_data = {
                "user_id": user_id,
                "agent_id": agent_id,
                "vertex_ai_session_id": vertex_ai_session_id,
                "platforms_used": [platform],
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
            # Use ArrayUnion to add platform if not already present
            await self.client.collection(self.sessions_collection).document(
                session_id
            ).update({
                "platforms_used": ArrayUnion([platform]),
                "last_activity_at": datetime.utcnow()
            })

            logger.debug(f"Updated platforms for session: {session_id} (added {platform})")

        except Exception as e:
            logger.error(f"Error updating session platforms for {session_id}: {e}")
            raise
