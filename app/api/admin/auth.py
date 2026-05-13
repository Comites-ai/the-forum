# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Admin UI login, OAuth callback, and logout routes."""
import logging
from typing import Optional

from authlib.integrations.starlette_client import OAuth, OAuthError
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from app.config import get_settings
from app.core.dependencies import get_admin_auth_service
from app.api.admin._common import get_templates
from app.services.admin_auth_service import AdminAuthService

logger = logging.getLogger(__name__)

router = APIRouter()


def build_oauth_client() -> OAuth:
    """Construct the Authlib OAuth registry for Google.

    Called once at app startup. Pulled out of module scope so test fixtures
    can stub it without polluting global state.
    """
    settings = get_settings()
    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=settings.oauth_client_id,
        client_secret=settings.oauth_client_secret,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={
            "scope": (
                "openid email profile "
                "https://www.googleapis.com/auth/cloud-platform.read-only"
            )
        },
    )
    return oauth


def _get_oauth(request: Request) -> OAuth:
    return request.app.state.oauth


@router.get("/")
async def admin_root(request: Request):
    """Land on agents if signed in, else show login."""
    session = request.scope.get("session") or {}
    if session.get("authorized"):
        return RedirectResponse("/admin/agents", status_code=303)
    return RedirectResponse("/admin/login", status_code=303)


@router.get("/login")
async def login_page(request: Request):
    """Render the public login screen."""
    session = request.scope.get("session") or {}
    if session.get("authorized"):
        return RedirectResponse("/admin/agents", status_code=303)
    templates = get_templates(request)
    return templates.TemplateResponse(request, "admin/login.html")


@router.get("/auth/login")
async def auth_start(request: Request):
    """Kick off the Google OAuth dance."""
    settings = get_settings()
    oauth = _get_oauth(request)
    redirect_uri = settings.oauth_redirect_uri
    return await oauth.google.authorize_redirect(request, redirect_uri)


@router.get("/auth/callback")
async def auth_callback(
    request: Request,
    auth_service: AdminAuthService = Depends(get_admin_auth_service),
):
    """Exchange the OAuth code and gate the session on the IAM check."""
    settings = get_settings()
    oauth = _get_oauth(request)
    try:
        token = await oauth.google.authorize_access_token(request)
    except OAuthError as e:
        logger.warning(f"OAuth callback failed: {e}")
        return RedirectResponse("/admin/login", status_code=303)

    userinfo = token.get("userinfo") or {}
    email: Optional[str] = userinfo.get("email")
    access_token: Optional[str] = token.get("access_token")

    if not email or not access_token:
        logger.warning("OAuth callback missing email or access_token in token payload")
        return RedirectResponse("/admin/login", status_code=303)

    authorized = await auth_service.check_iam_role(access_token, email)
    if not authorized:
        templates = get_templates(request)
        # Do not establish a session — keep the cookie empty so refreshing
        # the page does not put the user in limbo.
        request.session.clear()
        return templates.TemplateResponse(
            request,
            "admin/forbidden.html",
            {
                "email": email,
                "required_role": settings.admin_required_role,
                "project_id": settings.gcp_project_id,
            },
            status_code=403,
        )

    request.session["authorized"] = True
    request.session["email"] = email
    request.session["access_token"] = access_token
    return RedirectResponse("/admin/agents", status_code=303)


@router.get("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)
