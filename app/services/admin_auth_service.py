# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""OAuth + IAM verification for the admin UI.

The admin UI uses Google OAuth to identify the operator, then calls Cloud
Resource Manager getIamPolicy on the deployment's GCP project to verify
the operator holds the required role (default: roles/owner). Only operators
who hold that role on the project that hosts this Cloud Run service are
allowed in.
"""
import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


IAM_POLICY_URL = (
    "https://cloudresourcemanager.googleapis.com/v3/projects/{project_id}:getIamPolicy"
)


class AdminAuthService:
    """Verifies that an authenticated user owns the GCP project."""

    def __init__(
        self,
        project_id: str,
        required_role: str = "roles/owner",
        http_client: Optional[httpx.AsyncClient] = None,
    ):
        self.project_id = project_id
        self.required_role = required_role
        self._http_client = http_client

    async def _client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=10.0)
        return self._http_client

    async def check_iam_role(self, access_token: str, email: str) -> bool:
        """
        Return True iff `email` has `self.required_role` on `self.project_id`.

        Calls Cloud Resource Manager v3 getIamPolicy with the user's OAuth
        access token. Inheriting/transitive roles (folder, org) are not
        considered — only direct project-level bindings, which is the
        narrowest and safest interpretation of "owns this deployment."
        """
        client = await self._client()
        url = IAM_POLICY_URL.format(project_id=self.project_id)
        try:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={"options": {"requestedPolicyVersion": 3}},
            )
        except httpx.HTTPError as e:
            logger.warning(f"Cloud Resource Manager request failed: {e}")
            return False

        if response.status_code != 200:
            body = " ".join(response.text.split())[:500]
            logger.warning(
                "Cloud Resource Manager getIamPolicy returned %s: %s",
                response.status_code,
                body,
            )
            return False

        return self._policy_grants_role(response.json(), email)

    def _policy_grants_role(self, policy: dict, email: str) -> bool:
        member = f"user:{email}"
        for binding in policy.get("bindings", []):
            if binding.get("role") != self.required_role:
                continue
            if member in binding.get("members", []):
                return True
        return False

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
