"""Tests never reach the outside world: secrets are removed from the environment and the publish tool is forced into
dry-run. Lesson of 02.11: the Demo product's $25 budget test sent its digest to the real ops channel when the dispatcher
ran the suite on the server (the test command inherited the dispatcher's .env)."""
from __future__ import annotations

import os

import pytest

SECRET_PREFIXES = ("TELEGRAM_", "BLUESKY_", "DEVTO_", "MASTODON_", "GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


@pytest.fixture(autouse=True)
def _no_network_side_effects(monkeypatch):
    for k in list(os.environ):
        if k.startswith(SECRET_PREFIXES):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TEAM_PUBLISH_DRY_RUN", "1")
    yield
