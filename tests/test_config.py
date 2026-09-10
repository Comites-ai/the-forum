# Copyright (C) 2025 Comites.ai
# SPDX-License-Identifier: AGPL-3.0-only

"""Tests for the Settings derived properties."""
from app.config import Settings


def _make_settings(**overrides) -> Settings:
    base = {
        "gcp_project_id": "test-project",
        "slack_signing_secret": "secret-1",
    }
    base.update(overrides)
    return Settings(**base)


def test_slack_signing_secrets_single_value():
    settings = _make_settings(slack_signing_secret="secret-1")
    assert settings.slack_signing_secrets == ["secret-1"]


def test_slack_signing_secrets_csv_parses_into_list():
    settings = _make_settings(slack_signing_secret="secret-1, secret-2 ,secret-3")
    assert settings.slack_signing_secrets == ["secret-1", "secret-2", "secret-3"]


def test_slack_signing_secrets_drops_empties():
    settings = _make_settings(slack_signing_secret="secret-1,, ,secret-2")
    assert settings.slack_signing_secrets == ["secret-1", "secret-2"]


def test_gcs_enabled_false_when_unset():
    settings = _make_settings(gcs_bucket_name="")
    assert settings.gcs_enabled is False


def test_gcs_enabled_true_when_bucket_configured():
    settings = _make_settings(gcs_bucket_name="my-bucket")
    assert settings.gcs_enabled is True


def test_cut_off_threshold_is_longer_than_the_heal_grace():
    """Two different questions with two different answers (PLAT-42).

    The heal grace asks "has this tool call gone quiet?"; the cut-off
    threshold asks "is this run ever coming back?". Collapsing them would
    make a slow-but-live run look abandoned.
    """
    from app.config import Settings

    settings = Settings(gcp_project_id="p", slack_signing_secret="s")
    assert settings.cut_off_after_seconds > settings.heal_grace_seconds
    # Longer than Cloud Run's default request timeout, so no run outlives it.
    assert settings.cut_off_after_seconds >= 300
    assert settings.heal_orphaned_tool_calls is False
