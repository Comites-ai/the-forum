# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Service for managing scheduled jobs with Firestore as source of truth."""
import logging
from datetime import datetime
from typing import List, Optional
import pytz

from croniter import croniter

from app.config import get_settings
from app.models.scheduled_job import ScheduledJob
from app.schemas.scheduled_job import ScheduledJobCreate, ScheduledJobUpdate
from app.services.firestore_service import FirestoreService

logger = logging.getLogger(__name__)


class ScheduledJobService:
    """
    Manages scheduled job lifecycle with Firestore as the single source of truth.

    A single Cloud Scheduler dispatcher job calls the /process endpoint periodically,
    which checks Firestore for jobs that are due and executes them.
    """

    def __init__(self, firestore: FirestoreService):
        """
        Initialize the scheduled job service.

        Args:
            firestore: FirestoreService instance for data access
        """
        self.firestore = firestore
        self.settings = get_settings()

    def _validate_cron_expression(self, cron: str) -> bool:
        """
        Validate a cron expression.

        Args:
            cron: Cron expression string

        Returns:
            True if valid, False otherwise
        """
        try:
            croniter(cron)
            return True
        except (ValueError, KeyError):
            return False

    def _is_job_due(self, job: ScheduledJob) -> bool:
        """
        Check if a job is due to run based on its cron schedule or retry time.

        A job is due if:
        1. It has a retry_at time and now >= retry_at (one-time retry), OR
        2. The current time is past the next scheduled run time per cron

        Args:
            job: ScheduledJob to check

        Returns:
            True if job should run now
        """
        try:
            # Get the timezone for this job
            tz = pytz.timezone(job.timezone)
            now = datetime.now(tz)

            # Check for one-time retry first
            if job.retry_at:
                retry_time = job.retry_at
                if retry_time.tzinfo is None:
                    retry_time = pytz.UTC.localize(retry_time)
                retry_time = retry_time.astimezone(tz)

                if now >= retry_time:
                    logger.info(
                        f"Job {job.id} ({job.name}) is due for retry: "
                        f"retry_at={retry_time}, now={now}, reason={job.retry_reason}"
                    )
                    return True

            # Determine the base time for cron calculation
            if job.last_execution_at:
                # Use last execution time
                base_time = job.last_execution_at
                if base_time.tzinfo is None:
                    base_time = pytz.UTC.localize(base_time)
                base_time = base_time.astimezone(tz)
            else:
                # Never executed - use creation time
                base_time = job.created_at
                if base_time.tzinfo is None:
                    base_time = pytz.UTC.localize(base_time)
                base_time = base_time.astimezone(tz)

            # Get the next scheduled time after the base time
            cron = croniter(job.schedule, base_time)
            next_run = cron.get_next(datetime)

            # Job is due if we're past the next scheduled time
            is_due = now >= next_run

            if is_due:
                logger.debug(
                    f"Job {job.id} ({job.name}) is due: "
                    f"next_run={next_run}, now={now}"
                )

            return is_due

        except Exception as e:
            logger.error(f"Error checking if job {job.id} is due: {e}")
            return False

    async def get_due_jobs(self) -> List[ScheduledJob]:
        """
        Find all enabled jobs that are due to run.

        Returns:
            List of ScheduledJob objects that should be executed
        """
        # Get all enabled jobs
        jobs = await self.firestore.list_scheduled_jobs(enabled_only=True)

        # Filter to jobs that are due
        due_jobs = [job for job in jobs if self._is_job_due(job)]

        logger.info(f"Found {len(due_jobs)} jobs due out of {len(jobs)} enabled jobs")
        return due_jobs

    async def create_job(self, job_data: ScheduledJobCreate) -> ScheduledJob:
        """
        Create a new scheduled job.

        Args:
            job_data: Job creation data

        Returns:
            Created ScheduledJob

        Raises:
            ValueError: If validation fails
        """
        # Validate cron expression
        if not self._validate_cron_expression(job_data.schedule):
            raise ValueError(f"Invalid cron expression: {job_data.schedule}")

        # Validate timezone
        try:
            pytz.timezone(job_data.timezone)
        except pytz.UnknownTimeZoneError:
            raise ValueError(f"Invalid timezone: {job_data.timezone}")

        # Validate agent exists
        agent = await self.firestore.get_agent_by_id(job_data.agent_id)
        if not agent:
            raise ValueError(f"Agent not found: {job_data.agent_id}")

        # Create Firestore document
        job = await self.firestore.create_scheduled_job(job_data.model_dump())

        logger.info(f"Created scheduled job: {job.name} (id: {job.id})")
        return job

    async def update_job(self, job_id: str, updates: ScheduledJobUpdate) -> Optional[ScheduledJob]:
        """
        Update job configuration.

        Args:
            job_id: Firestore document ID
            updates: Fields to update

        Returns:
            Updated ScheduledJob or None if not found

        Raises:
            ValueError: If validation fails
        """
        # Get existing job
        job = await self.firestore.get_scheduled_job(job_id)
        if not job:
            return None

        update_dict = updates.model_dump(exclude_unset=True)

        # Validate cron if being updated
        if "schedule" in update_dict and not self._validate_cron_expression(update_dict["schedule"]):
            raise ValueError(f"Invalid cron expression: {update_dict['schedule']}")

        # Validate timezone if being updated
        if "timezone" in update_dict:
            try:
                pytz.timezone(update_dict["timezone"])
            except pytz.UnknownTimeZoneError:
                raise ValueError(f"Invalid timezone: {update_dict['timezone']}")

        # Update Firestore
        updated_job = await self.firestore.update_scheduled_job(job_id, update_dict)

        logger.info(f"Updated scheduled job: {job_id}")
        return updated_job

    async def delete_job(self, job_id: str) -> bool:
        """
        Delete job from Firestore.

        Args:
            job_id: Firestore document ID

        Returns:
            True if deleted
        """
        job = await self.firestore.get_scheduled_job(job_id)
        if not job:
            return False

        await self.firestore.delete_scheduled_job(job_id)
        logger.info(f"Deleted scheduled job: {job_id}")
        return True

    async def get_job(self, job_id: str) -> Optional[ScheduledJob]:
        """
        Get a single job by ID.

        Args:
            job_id: Firestore document ID

        Returns:
            ScheduledJob if found
        """
        return await self.firestore.get_scheduled_job(job_id)

    async def list_jobs(
        self,
        agent_id: Optional[str] = None,
        user_id: Optional[str] = None,
    ) -> List[ScheduledJob]:
        """
        List jobs with optional filtering.

        Args:
            agent_id: Filter by agent ID
            user_id: Filter by user ID

        Returns:
            List of ScheduledJob objects
        """
        return await self.firestore.list_scheduled_jobs(
            agent_id=agent_id,
            user_id=user_id,
        )
