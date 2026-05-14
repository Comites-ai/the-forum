# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Cloud Logging queries for the admin UI.

Powers two reads:
  * the per-agent "Last Error" card on the detail page
  * the per-platform "Last used" cells on the agents list

Both use the operator's OAuth access token (granted at login) so that
the permissions used here exactly match the IAM check that gated login.
No service-account log access is required.
"""
import logging
from datetime import datetime, timedelta, UTC
from typing import Dict, Optional

import httpx

logger = logging.getLogger(__name__)


LOGGING_ENTRIES_URL = "https://logging.googleapis.com/v2/entries:list"


class AdminLoggingService:
    """Fetches the most recent error log entry for a given agent."""

    def __init__(self, http_client: Optional[httpx.AsyncClient] = None):
        self._http_client = http_client

    async def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=10.0)
        return self._http_client

    @staticmethod
    def _build_filter(service_name: str, agent_id: str) -> str:
        agent_id_escaped = agent_id.replace('"', '\\"')
        service_name_escaped = service_name.replace('"', '\\"')
        # The agent-id match is OR'd across three log shapes:
        #   - jsonPayload.agent_id="..."   future-looking: when the app
        #     explicitly attaches agent_id as a structured field.
        #   - jsonPayload.message:"..."    when running with google-cloud-
        #     logging StructuredHandler, the formatted message text lives
        #     here. This is the common case in production.
        #   - textPayload:"..."            legacy: pre-structured-logging
        #     entries land here as raw stdout text.
        return (
            f'resource.type="cloud_run_revision" '
            f'AND resource.labels.service_name="{service_name_escaped}" '
            f'AND severity>=ERROR '
            f'AND (jsonPayload.agent_id="{agent_id_escaped}" '
            f'OR jsonPayload.message:"{agent_id_escaped}" '
            f'OR textPayload:"{agent_id_escaped}")'
        )

    async def get_last_error_for_agent(
        self,
        access_token: str,
        project_id: str,
        service_name: str,
        agent_id: str,
    ) -> Optional[dict]:
        """
        Return the most recent ERROR+ Cloud Logging entry mentioning this agent.

        Returns None if there are no matching entries, or if the API call
        fails for any reason (a missing log is far more common than a real
        outage, and the admin UI should degrade gracefully).
        """
        client = await self._client()
        payload = {
            "resourceNames": [f"projects/{project_id}"],
            "filter": self._build_filter(service_name, agent_id),
            "orderBy": "timestamp desc",
            "pageSize": 1,
        }
        try:
            response = await client.post(
                LOGGING_ENTRIES_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        except httpx.HTTPError as e:
            logger.warning(f"Cloud Logging request failed: {e}")
            return None

        if response.status_code != 200:
            body = " ".join(response.text.split())[:500]
            logger.warning(
                "Cloud Logging entries:list returned %s: %s",
                response.status_code,
                body,
            )
            return None

        entries = response.json().get("entries") or []
        return entries[0] if entries else None

    @staticmethod
    def _build_last_used_filter(service_name: str, agent_id: str, window_days: int) -> str:
        agent_id_escaped = agent_id.replace('"', '\\"')
        service_name_escaped = service_name.replace('"', '\\"')
        since = (datetime.now(UTC) - timedelta(days=window_days)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        return (
            f'resource.type="cloud_run_revision" '
            f'AND resource.labels.service_name="{service_name_escaped}" '
            f'AND jsonPayload.event="message_processed" '
            f'AND jsonPayload.agent_id="{agent_id_escaped}" '
            f'AND timestamp>="{since}"'
        )

    async def get_last_used_per_platform(
        self,
        access_token: str,
        project_id: str,
        service_name: str,
        agent_id: str,
        window_days: int = 7,
        page_size: int = 200,
    ) -> Dict[str, datetime]:
        """
        Return the most recent message_processed timestamp per platform,
        within the last `window_days`.

        One Cloud Logging API call per agent (not per platform): we ask for
        the N most-recent message_processed entries for this agent, then
        bucket them by jsonPayload.platform in memory. Because entries
        arrive in timestamp-descending order, the first time we see a given
        platform is its most-recent activity. Returns an empty dict if the
        API call fails or no entries match (so the admin UI degrades to
        blank cells, never a 500).
        """
        client = await self._client()
        payload = {
            "resourceNames": [f"projects/{project_id}"],
            "filter": self._build_last_used_filter(service_name, agent_id, window_days),
            "orderBy": "timestamp desc",
            "pageSize": page_size,
        }
        try:
            response = await client.post(
                LOGGING_ENTRIES_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        except httpx.HTTPError as e:
            logger.warning(f"Cloud Logging request failed: {e}")
            return {}

        if response.status_code != 200:
            body = " ".join(response.text.split())[:500]
            logger.warning(
                "Cloud Logging entries:list (last-used) returned %s: %s",
                response.status_code,
                body,
            )
            return {}

        result: Dict[str, datetime] = {}
        for entry in response.json().get("entries") or []:
            payload_obj = entry.get("jsonPayload") or {}
            platform = payload_obj.get("platform")
            if not platform or platform in result:
                continue
            ts_str = entry.get("timestamp")
            if not ts_str:
                continue
            try:
                # Cloud Logging timestamps are RFC 3339 with 'Z' or offset.
                # Python 3.11+ handles 'Z' natively; earlier needs replace.
                result[platform] = datetime.fromisoformat(
                    ts_str.replace("Z", "+00:00")
                )
            except ValueError:
                continue
        return result

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
