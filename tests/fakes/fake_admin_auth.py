# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""In-memory AdminAuthService stand-in for tests."""
from typing import Set


class FakeAdminAuthService:
    """Lets a test pre-declare which emails are authorized."""

    def __init__(self, authorized_emails: Set[str] | None = None):
        self.authorized_emails = set(authorized_emails or [])
        self.calls: list[tuple[str, str]] = []

    async def check_iam_role(self, access_token: str, email: str) -> bool:
        self.calls.append((access_token, email))
        return email in self.authorized_emails

    async def aclose(self) -> None:  # pragma: no cover
        return None
