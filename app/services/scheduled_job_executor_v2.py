# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Scheduled job execution service (v2 - multi-platform)."""
import logging
from datetime import datetime, timedelta, UTC
from typing import Optional

from app.config import get_settings
from app.core.exceptions import ResourceExhaustedError
from app.services.firestore_service import FirestoreService
from app.services.vertex_ai_service import VertexAIService
from app.services.identity_service import IdentityService
from app.services.platforms.slack_connector import SlackConnector
from app.services.platforms.google_chat_connector import GoogleChatConnector
from app.services.platforms.telegram_connector import TelegramConnector
from app.services.platforms.base import PlatformConnector

logger = logging.getLogger(__name__)


class ScheduledJobExecutorV2:
    """Executes scheduled jobs with multi-platform support."""

    def __init__(
        self,
        firestore: FirestoreService,
        vertex_ai: VertexAIService,
        identity: IdentityService,
    ):
        """
        Initialize the job executor.

        Args:
            firestore: Firestore service for data access
            vertex_ai: Vertex AI service for agent calls
            identity: Identity service for user resolution
        """
        self.firestore = firestore
        self.vertex_ai = vertex_ai
        self.identity = identity
        self.settings = get_settings()

    async def execute_job(self, job_id: str, execution_id: str) -> bool:
        """
        Execute a scheduled job.

        Flow:
        1. Acquire execution lock (Firestore transaction)
        2. Verify job is enabled
        3. Get agent configuration
        4. Resolve platform-specific recipient ID for the user
        5. Get or create Vertex AI session
        6. Send prompt to agent
        7. Send response to user via appropriate platform
        8. Update execution tracking

        Args:
            job_id: Firestore document ID of the job
            execution_id: Unique execution ID for idempotency

        Returns:
            True if execution succeeded, False if skipped or failed
        """
        job = None
        try:
            # Step 1: Acquire execution lock
            lock_acquired = await self.firestore.acquire_job_execution_lock(
                job_id=job_id,
                execution_id=execution_id,
                lock_timeout_seconds=self.settings.scheduled_job_lock_timeout_seconds,
            )

            if not lock_acquired:
                logger.info(f"Could not acquire lock for job {job_id}, skipping")
                return False

            # Step 2: Get and validate job
            job = await self.firestore.get_scheduled_job(job_id)
            if not job:
                logger.warning(f"Job {job_id} not found")
                return False

            if not job.enabled:
                logger.info(f"Job {job_id} is disabled, skipping")
                await self.firestore.release_job_execution_lock(job_id, success=True)
                return False

            logger.info(f"Executing scheduled job: {job.name} (id: {job_id})")

            # Step 3: Get agent configuration
            agent = await self.firestore.get_agent_by_id(job.agent_id)
            if not agent:
                error_msg = f"Agent {job.agent_id} not found"
                logger.error(error_msg)
                await self.firestore.release_job_execution_lock(
                    job_id, success=False, error=error_msg
                )
                return False

            # Step 4: Get platform-specific recipient ID
            recipient_id = await self.identity.get_platform_identity(
                user_id=job.user_id,
                platform=job.output_platform
            )
            if not recipient_id:
                error_msg = f"User {job.user_id} has no {job.output_platform} identity"
                logger.error(error_msg)
                await self.firestore.release_job_execution_lock(
                    job_id, success=False, error=error_msg
                )
                return False

            # Step 6: Create platform connector
            connector = await self._create_connector(agent, job.output_platform)
            if not connector:
                error_msg = f"Could not create connector for {job.output_platform}"
                logger.error(error_msg)
                await self.firestore.release_job_execution_lock(
                    job_id, success=False, error=error_msg
                )
                return False

            # Step 7: Get user display name
            user_info = await connector.get_user_info(recipient_id)
            user_display_name = user_info.get("display_name", recipient_id)

            # Step 8: Get existing session or create new one
            session_id = await self._get_or_create_session(
                user_id=job.user_id,
                agent_id=job.agent_id,
                vertex_ai_agent_id=agent.vertex_ai_agent_id,
                platform=job.output_platform
            )
            logger.info(f"Using Vertex AI session: {session_id}")

            # Step 9: Send prompt to Vertex AI agent
            prefixed_prompt = (
                f"[From: {user_display_name} | {job.output_platform}_id: {recipient_id}] {job.prompt}"
            )

            response = await self.vertex_ai.send_message(
                agent_id=agent.vertex_ai_agent_id,
                session_id=session_id,
                message=prefixed_prompt,
            )

            # Step 10: Only send if agent provided an actual response
            if response.text and response.text.strip():
                # Open conversation and send response
                conversation_id = await connector.open_conversation(recipient_id)

                # Format message with job name for context
                formatted_message = f"*Scheduled: {job.name}*\n\n{response.text}"

                await connector.send_message(
                    recipient_id=conversation_id,
                    text=formatted_message,
                )
                logger.info(f"Sent response to {job.output_platform} for job {job_id}")

                # Step 11: Mark success and release lock
                await self.firestore.release_job_execution_lock(job_id, success=True)

                # Clear any pending retry (if this was a retry execution)
                if job.retry_at:
                    await self.firestore.update_scheduled_job(job_id, {
                        "retry_at": None,
                        "retry_reason": None,
                    })
                    logger.info(f"Cleared retry for job {job_id} after successful execution")

                logger.info(f"Successfully executed job {job_id}")
            else:
                # Agent returned empty response - treat as failure
                if response.has_unanswered_function_calls:
                    tool_name = response.function_names[-1] if response.function_names else "unknown"
                    error_msg = f"Tool '{tool_name}' did not respond (possible permission issue)"
                elif response.has_rate_limit_error:
                    tool_info = next(
                        (e for e in response.function_errors if e.get("error_type") == "rate_limit"),
                        {}
                    )
                    tool_name = tool_info.get("tool_name", "unknown")
                    error_msg = f"Tool '{tool_name}' hit rate limit"
                else:
                    error_msg = f"Empty response ({response.chunk_count} chunks)"

                logger.warning(f"Job {job_id} failed: {error_msg}")
                await self.firestore.release_job_execution_lock(job_id, success=False, error=error_msg)

                # Notify user every 1440 consecutive failures (~24 hours if job runs every minute)
                new_failure_count = job.consecutive_failures + 1
                if new_failure_count % 1440 == 0:
                    last_success = job.last_execution_at
                    if last_success:
                        since_str = last_success.strftime("%Y-%m-%d %H:%M UTC")
                    else:
                        since_str = "it was created"

                    try:
                        conversation_id = await connector.open_conversation(recipient_id)
                        await connector.send_message(
                            recipient_id=conversation_id,
                            text=f"My scheduled job *{job.name}* has not been working since {since_str}.",
                        )
                        logger.info(f"Sent failure notification for job {job_id} ({new_failure_count} failures)")
                    except Exception as notify_err:
                        logger.warning(f"Failed to send failure notification for job {job_id}: {notify_err}")

            return True

        except ResourceExhaustedError as e:
            # Google API rate limit - schedule a silent retry in 1 minute
            logger.warning(f"Rate limit hit for job {job_id}: {e}")

            retry_at = datetime.now(UTC) + timedelta(minutes=1)
            await self.firestore.update_scheduled_job(job_id, {
                "retry_at": retry_at,
                "retry_reason": "rate_limit_429",
            })
            logger.info(f"Scheduled retry for job {job_id} at {retry_at}")

            # Release lock (not a failure, just rate limited)
            await self.firestore.release_job_execution_lock(job_id, success=True)
            return False

        except Exception as e:
            error_msg = str(e)
            logger.exception(f"Error executing job {job_id}: {e}")

            # Release lock with error
            if job:
                await self.firestore.release_job_execution_lock(
                    job_id, success=False, error=error_msg
                )

            return False

    async def _create_connector(
        self,
        agent,
        platform: str
    ) -> Optional[PlatformConnector]:
        """
        Create platform connector for the specified platform.

        Args:
            agent: Agent instance
            platform: Platform name (e.g., "slack", "google_chat", "telegram")

        Returns:
            Platform connector instance, or None if platform not supported
        """
        if platform == "slack":
            slack_config = agent.get_slack_config()
            if not slack_config:
                logger.error(f"Agent {agent.id} has no Slack configuration")
                return None

            # Validate that we have either direct token or Secret Manager config
            has_direct_token = slack_config.slack_bot_token is not None
            has_secret_config = (
                slack_config.slack_bot_token_secret is not None and
                slack_config.slack_bot_token_project_id is not None
            )

            if not has_direct_token and not has_secret_config:
                logger.error(
                    f"Agent {agent.id} Slack config missing bot token. "
                    f"Need either slack_bot_token OR (slack_bot_token_secret + slack_bot_token_project_id)"
                )
                return None

            return SlackConnector(
                bot_token=slack_config.slack_bot_token if has_direct_token else None,
                bot_token_secret=slack_config.slack_bot_token_secret if has_secret_config else None,
                bot_token_project_id=slack_config.slack_bot_token_project_id if has_secret_config else None,
                signing_secret=None  # Not needed for sending
            )

        elif platform == "google_chat":
            google_chat_config = agent.get_google_chat_config()
            if not google_chat_config or not google_chat_config.google_chat_service_account_secret:
                logger.error(f"Agent {agent.id} has no Google Chat configuration")
                return None

            return GoogleChatConnector(
                service_account_secret_name=google_chat_config.google_chat_service_account_secret,
                project_id=google_chat_config.google_chat_project_id  # None for backward compatibility
            )

        elif platform == "telegram":
            telegram_config = agent.get_telegram_config()
            if not telegram_config:
                logger.error(f"Agent {agent.id} has no Telegram configuration")
                return None

            # Validate that we have either direct token or Secret Manager config
            has_direct_token = telegram_config.telegram_bot_token is not None
            has_secret_config = (
                telegram_config.telegram_bot_token_secret is not None and
                telegram_config.telegram_bot_token_project_id is not None
            )

            if not has_direct_token and not has_secret_config:
                logger.error(
                    f"Agent {agent.id} Telegram config missing bot token. "
                    f"Need either telegram_bot_token OR (telegram_bot_token_secret + telegram_bot_token_project_id)"
                )
                return None

            return TelegramConnector(
                bot_token=telegram_config.telegram_bot_token if has_direct_token else None,
                bot_token_secret=telegram_config.telegram_bot_token_secret if has_secret_config else None,
                bot_token_project_id=telegram_config.telegram_bot_token_project_id if has_secret_config else None,
                webhook_secret=None  # Not needed for sending
            )

        logger.error(f"Unsupported platform: {platform}")
        return None

    async def _get_or_create_session(
        self,
        user_id: str,
        agent_id: str,
        vertex_ai_agent_id: str,
        platform: str
    ) -> str:
        """
        Get existing session or create new one.

        This ensures that when a scheduled job sends a message to a user,
        any replies from the user will continue in the same conversation.

        Args:
            user_id: Unified user ID from users collection
            agent_id: Agent ID from agents collection
            vertex_ai_agent_id: Vertex AI agent resource name
            platform: Platform this message will be sent to

        Returns:
            Vertex AI session ID
        """
        # Try to get existing session
        session = await self.firestore.get_session_by_user(
            user_id=user_id,
            agent_id=agent_id
        )

        if session:
            # Update last activity timestamp and track platform usage
            await self.firestore.update_session_platforms(session.id, platform)
            logger.info(f"Using existing session: {session.id}")
            return session.vertex_ai_session_id

        # No existing session, create new one in Vertex AI
        vertex_session_id = await self.vertex_ai.create_session(vertex_ai_agent_id)

        # Store in Firestore
        await self.firestore.create_session_for_user(
            user_id=user_id,
            agent_id=agent_id,
            vertex_ai_session_id=vertex_session_id,
            platform=platform
        )

        logger.info(f"Created new session: {vertex_session_id}")
        return vertex_session_id

    async def test_execute_job(self, job_id: str) -> dict:
        """
        Test run a job without affecting execution tracking.

        Useful for validating job configuration before enabling scheduling.

        Args:
            job_id: Firestore document ID of the job

        Returns:
            Dict with success status and response or error
        """
        try:
            # Get job
            job = await self.firestore.get_scheduled_job(job_id)
            if not job:
                return {"success": False, "error": "Job not found"}

            # Get agent
            agent = await self.firestore.get_agent_by_id(job.agent_id)
            if not agent:
                return {"success": False, "error": f"Agent {job.agent_id} not found"}

            # Get platform-specific recipient ID
            recipient_id = await self.identity.get_platform_identity(
                user_id=job.user_id,
                platform=job.output_platform
            )
            if not recipient_id:
                return {"success": False, "error": f"User has no {job.output_platform} identity"}

            # Create connector
            connector = await self._create_connector(agent, job.output_platform)
            if not connector:
                return {"success": False, "error": f"Could not create {job.output_platform} connector"}

            # Get user display name
            user_info = await connector.get_user_info(recipient_id)
            user_display_name = user_info.get("display_name", recipient_id)

            # Create temporary session
            session_id = await self.vertex_ai.create_session(agent.vertex_ai_agent_id)

            # Send to agent
            prefixed_prompt = (
                f"[From: {user_display_name} | {job.output_platform}_id: {recipient_id}] {job.prompt}"
            )
            response = await self.vertex_ai.send_message(
                agent_id=agent.vertex_ai_agent_id,
                session_id=session_id,
                message=prefixed_prompt,
            )

            # Send to platform
            conversation_id = await connector.open_conversation(recipient_id)
            formatted_message = f"*[TEST] Scheduled: {job.name}*\n\n{response.text}"
            await connector.send_message(
                recipient_id=conversation_id,
                text=formatted_message,
            )

            return {
                "success": True,
                "response": response.text,
                "message": f"Test execution completed, response sent to {job.output_platform}",
            }

        except Exception as e:
            logger.exception(f"Error in test execution for job {job_id}: {e}")
            return {"success": False, "error": str(e)}
