# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Shared pytest fixtures and test environment setup."""
# Set env vars BEFORE importing anything from `app`. The Settings class has
# required fields and is instantiated (and lru_cached) at first import.
import os

os.environ.setdefault("GCP_PROJECT_ID", "test-project")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-signing-secret")
os.environ.setdefault("ENVIRONMENT", "test")
# Admin UI off by default for the regular `client` fixture. Tests that
# exercise the admin UI use the `admin_client` fixture which sets these
# at fixture time before creating the app.

import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.services.scheduled_job_service import ScheduledJobService

from tests.fakes.fake_firestore import FakeFirestoreService
from tests.fakes.fake_vertex_ai import FakeVertexAIService
from tests.fakes.fake_gcs import FakeGCSService
from tests.fakes.fake_slack_service import FakeSlackService
from tests.fakes.fake_platform_connector import FakePlatformConnector
from tests.fakes.fake_admin_auth import FakeAdminAuthService
from tests.fakes.fake_logging import FakeLoggingService
from tests.fakes.fake_admin_vertex import FakeAdminVertexService


FIXTURES_DIR = Path(__file__).parent / "fixtures"


def load_fixture(relative_path: str) -> dict[str, Any]:
    """Load a JSON fixture by path relative to tests/fixtures/."""
    with open(FIXTURES_DIR / relative_path) as f:
        return json.load(f)


@pytest.fixture
def fake_firestore() -> FakeFirestoreService:
    return FakeFirestoreService()


@pytest.fixture
def fake_vertex_ai() -> FakeVertexAIService:
    return FakeVertexAIService()


@pytest.fixture
def fake_gcs() -> FakeGCSService:
    return FakeGCSService()


@pytest.fixture
def fake_slack() -> FakeSlackService:
    return FakeSlackService()


@pytest.fixture
def fake_connector() -> FakePlatformConnector:
    return FakePlatformConnector(platform="slack")


@pytest.fixture
def client(fake_firestore, fake_vertex_ai, fake_gcs, fake_slack):
    """
    FastAPI TestClient with fakes wired into app.state.

    Skips the lifespan (no `with` block) so we don't try to instantiate real
    GCP clients. The MCP scheduler endpoint won't work without lifespan, but
    no MVP tests hit it.
    """
    app = create_app()
    app.state.firestore = fake_firestore
    app.state.vertex_ai = fake_vertex_ai
    app.state.slack = fake_slack
    app.state.gcs = fake_gcs
    app.state.scheduled_job_service = ScheduledJobService(firestore=fake_firestore)
    return TestClient(app)


@pytest.fixture
def fake_admin_auth() -> FakeAdminAuthService:
    return FakeAdminAuthService(authorized_emails={"admin@example.com"})


@pytest.fixture
def fake_admin_logging() -> FakeLoggingService:
    return FakeLoggingService()


@pytest.fixture
def fake_admin_vertex() -> FakeAdminVertexService:
    return FakeAdminVertexService()


@pytest.fixture
def admin_client(
    monkeypatch,
    fake_firestore,
    fake_vertex_ai,
    fake_gcs,
    fake_slack,
    fake_admin_auth,
    fake_admin_logging,
    fake_admin_vertex,
):
    """TestClient with the admin UI enabled and OAuth stubbed.

    Reloads app.config so settings.admin_ui_enabled is True for this client.
    Replaces the OAuth client on app.state with a stub that bypasses Google.
    """
    monkeypatch.setenv("OAUTH_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("OAUTH_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv(
        "OAUTH_REDIRECT_URI", "http://testserver/admin/auth/callback"
    )
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret")

    # Pydantic Settings is lru_cached — clear so it reloads with env vars.
    from app.config import get_settings
    get_settings.cache_clear()

    app = create_app()
    app.state.firestore = fake_firestore
    app.state.vertex_ai = fake_vertex_ai
    app.state.slack = fake_slack
    app.state.gcs = fake_gcs
    app.state.scheduled_job_service = ScheduledJobService(firestore=fake_firestore)
    app.state.admin_auth = fake_admin_auth
    app.state.admin_logging = fake_admin_logging
    app.state.admin_vertex = fake_admin_vertex

    # Stub out the OAuth dance: authorize_redirect → redirect to a fake
    # callback URL; authorize_access_token → return a canned token.
    class StubGoogle:
        @staticmethod
        async def authorize_redirect(request, redirect_uri):
            from fastapi.responses import RedirectResponse
            return RedirectResponse(
                redirect_uri + "?code=test-code&state=test-state", status_code=302
            )

        @staticmethod
        async def authorize_access_token(request):
            return {
                "access_token": "test-access-token",
                "userinfo": {"email": "admin@example.com", "name": "Admin"},
            }

    class StubOAuth:
        google = StubGoogle()

    app.state.oauth = StubOAuth()

    client = TestClient(app)
    yield client
    get_settings.cache_clear()


@pytest.fixture
def slack_signed_request():
    """
    Returns a helper that builds (body_str, headers) for a signed Slack request.

    Uses the SLACK_SIGNING_SECRET set in conftest. Tests can override the
    timestamp to exercise replay protection.
    """
    def _sign(payload: dict, *, timestamp: int | None = None, secret: str | None = None):
        secret = secret or os.environ["SLACK_SIGNING_SECRET"]
        ts = str(timestamp if timestamp is not None else int(time.time()))
        body = json.dumps(payload)
        sig_basestring = f"v0:{ts}:{body}"
        signature = "v0=" + hmac.new(
            secret.encode(),
            sig_basestring.encode(),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "X-Slack-Request-Timestamp": ts,
            "X-Slack-Signature": signature,
            "Content-Type": "application/json",
        }
        return body, headers

    return _sign
