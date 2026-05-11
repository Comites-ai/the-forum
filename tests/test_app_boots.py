# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Smoke tests: catches anything that breaks the import graph or app factory."""


def test_health_endpoint_returns_200(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


def test_root_endpoint_returns_app_metadata(client):
    response = client.get("/")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "running"
    assert "service" in body
    assert "environment" in body


def test_source_endpoint_returns_agpl_link(client):
    response = client.get("/source")
    assert response.status_code == 200
    body = response.json()
    assert body["license"] == "AGPL-3.0"
    assert "repository" in body
