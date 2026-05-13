# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Admin auth: login page, OAuth callback, IAM gate, logout."""


def test_login_page_renders_without_session(admin_client):
    response = admin_client.get("/admin/login", follow_redirects=False)
    assert response.status_code == 200
    body = response.text
    assert "Sign in with Google" in body
    # Privacy notice is present so the user knows we'll use their token.
    assert "Cloud Logging" in body or "credentials" in body.lower()


def test_admin_root_redirects_to_login(admin_client):
    response = admin_client.get("/admin/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_unauthenticated_protected_route_redirects(admin_client):
    response = admin_client.get("/admin/agents", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"


def test_auth_callback_success_lands_on_agents(admin_client):
    # IAM check allows admin@example.com (set in the fake fixture).
    response = admin_client.get(
        "/admin/auth/callback?code=test&state=test",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/agents"

    # Follow-up request should be authenticated (no redirect to login).
    response = admin_client.get("/admin/agents", follow_redirects=False)
    assert response.status_code == 200


def test_auth_callback_non_owner_renders_forbidden(
    admin_client, fake_admin_auth
):
    fake_admin_auth.authorized_emails.clear()  # Nobody is owner.
    response = admin_client.get(
        "/admin/auth/callback?code=test&state=test",
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert "roles/owner" in response.text
    assert "admin@example.com" in response.text

    # Session was not established — subsequent protected page redirects.
    follow = admin_client.get("/admin/agents", follow_redirects=False)
    assert follow.status_code == 303


def test_logout_clears_session(admin_client):
    # Log in
    admin_client.get("/admin/auth/callback?code=test&state=test", follow_redirects=False)
    assert admin_client.get("/admin/agents", follow_redirects=False).status_code == 200

    # Log out
    response = admin_client.get("/admin/auth/logout", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"
    # And now protected routes redirect again.
    assert admin_client.get("/admin/agents", follow_redirects=False).status_code == 303


def test_admin_ui_disabled_when_oauth_env_missing(client):
    # The default `client` fixture has admin_ui_enabled=False (no OAuth env).
    # /admin/* should 404, leaving existing deployments untouched.
    response = client.get("/admin/login")
    assert response.status_code == 404
