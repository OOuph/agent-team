"""Tests never reach the outside world: secrets are removed from the environment and the publish tool is forced into
dry-run. Lesson of 02.11: the Demo product's $25 budget test sent its digest to the real ops channel when the dispatcher
ran the suite on the server (the test command inherited the dispatcher's .env)."""
from __future__ import annotations

import os
import signal
import threading

import pytest

# ------------------------------------------------------------------ parameters
TEST_TIMEOUT_S = int(os.environ.get("TEAM_TEST_TIMEOUT_S", "180"))    # one test may run this long; 0 = no limit. A test that hangs
                                                                      # must FAIL (27.11: the test of PR #536 «hangs without the fix»)

SECRET_PREFIXES = ("TELEGRAM_", "BLUESKY_", "DEVTO_", "MASTODON_", "THREADS_", "X_API_", "X_ACCESS_", "X_CLIENT_", "X_REFRESH_", "X_OAUTH2_", "GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")


@pytest.fixture(autouse=True)
def _no_network_side_effects(monkeypatch):
    for k in list(os.environ):
        if k.startswith(SECRET_PREFIXES):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("TEAM_PUBLISH_DRY_RUN", "1")
    yield


class TestTimeout(Exception):
    """A test ran longer than TEST_TIMEOUT_S (or its `@pytest.mark.timeout_s(n)`)."""


def pytest_configure(config):
    config.addinivalue_line("markers", "timeout_s(seconds): this test's own time limit instead of TEAM_TEST_TIMEOUT_S")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """No plugin: an alarm in the main thread. The scheduler loops under test are Python code and lock waits, both of
    which a signal interrupts; a test stuck in C code without releasing the interpreter is out of reach."""
    mark = item.get_closest_marker("timeout_s")
    limit = int(mark.args[0]) if mark and mark.args else TEST_TIMEOUT_S
    usable = limit > 0 and hasattr(signal, "SIGALRM") and threading.current_thread() is threading.main_thread()
    if not usable:
        yield
        return

    def _alarm(signum, frame):
        raise TestTimeout(f"{item.nodeid} ran longer than {limit}s — a test that hangs is a failed test")

    old = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(limit)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
