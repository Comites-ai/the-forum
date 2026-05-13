# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""AdminAuthService unit tests: IAM policy parsing + HTTP error handling."""
import httpx
import pytest

from app.services.admin_auth_service import AdminAuthService


def _policy(role: str, members: list[str]) -> dict:
    return {"bindings": [{"role": role, "members": members}]}


def test_policy_grants_role_returns_true_for_matching_binding():
    service = AdminAuthService(project_id="p", required_role="roles/owner")
    policy = _policy("roles/owner", ["user:admin@example.com", "user:other@example.com"])
    assert service._policy_grants_role(policy, "admin@example.com") is True


def test_policy_grants_role_returns_false_when_member_missing():
    service = AdminAuthService(project_id="p", required_role="roles/owner")
    policy = _policy("roles/owner", ["user:other@example.com"])
    assert service._policy_grants_role(policy, "admin@example.com") is False


def test_policy_grants_role_returns_false_when_role_differs():
    service = AdminAuthService(project_id="p", required_role="roles/owner")
    policy = _policy("roles/editor", ["user:admin@example.com"])
    assert service._policy_grants_role(policy, "admin@example.com") is False


def test_configurable_role_is_honored():
    service = AdminAuthService(project_id="p", required_role="roles/viewer")
    policy = _policy("roles/viewer", ["user:viewer@example.com"])
    assert service._policy_grants_role(policy, "viewer@example.com") is True
    assert service._policy_grants_role(
        _policy("roles/owner", ["user:viewer@example.com"]), "viewer@example.com"
    ) is False


@pytest.mark.asyncio
async def test_check_iam_role_returns_true_on_match():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"bindings": [
                {"role": "roles/owner", "members": ["user:admin@example.com"]}
            ]},
        )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        service = AdminAuthService(
            project_id="p", required_role="roles/owner", http_client=http
        )
        assert await service.check_iam_role("tok", "admin@example.com") is True


@pytest.mark.asyncio
async def test_check_iam_role_returns_false_on_non_200():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        service = AdminAuthService(
            project_id="p", required_role="roles/owner", http_client=http
        )
        assert await service.check_iam_role("tok", "admin@example.com") is False
