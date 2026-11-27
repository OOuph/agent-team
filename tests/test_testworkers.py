"""`cycle.test_workers`: a limit on the workers of the test tools for the runs of one cycle (27.11)."""
from __future__ import annotations

import os
from pathlib import Path

from team import testworkers as tw
from team.cycle import Cycle

from test_parallel_lanes import LaneRunner, _product, _settings, _task


def test_off_by_default():
    assert tw.env_for({}, {}, 6) == {} and tw.env_for(None, None, 6) == {}


def test_a_number_and_auto():
    assert tw.env_for({}, {"cycle": {"test_workers": {"count": 2}}}, 6) == {"VITEST_MAX_FORKS": "2", "VITEST_MAX_THREADS": "2"}
    assert tw.env_for({"test_workers": {"count": "auto"}}, {}, 6, cores=8) == {"VITEST_MAX_FORKS": "1", "VITEST_MAX_THREADS": "1"}
    assert tw.env_for({}, {"cycle": {"test_workers": {"count": "auto", "env": ["E2E_WORKERS"]}}}, 3, cores=8) == {"E2E_WORKERS": "2"}
    assert tw.env_for({"test_workers": {"count": 4}}, {"cycle": {"test_workers": {"count": 0}}}, 6) == {}      # the product switches it off


def test_bad_values_switch_it_off_and_bad_names_are_dropped():
    assert tw.env_for({}, {"cycle": {"test_workers": {"count": "many"}}}, 6) == {}
    assert tw.env_for({}, {"cycle": {"test_workers": {"count": True}}}, 6) == {}
    assert tw.env_for({}, {"cycle": {"test_workers": {"count": 2, "env": ["PATH", "TEAM_X", "ANTHROPIC_API_KEY", "a b", "OK_NAME"]}}}, 6) == {"OK_NAME": "2"}


def test_applied_sets_and_restores(monkeypatch):
    monkeypatch.setenv("VITEST_MAX_FORKS", "7")
    monkeypatch.delenv("VITEST_MAX_THREADS", raising=False)
    with tw.applied({"VITEST_MAX_FORKS": "2", "VITEST_MAX_THREADS": "2"}):
        assert os.environ["VITEST_MAX_FORKS"] == "2" and os.environ["VITEST_MAX_THREADS"] == "2"
    assert os.environ["VITEST_MAX_FORKS"] == "7" and "VITEST_MAX_THREADS" not in os.environ


def test_the_cycle_sets_it_for_its_runs_and_only_for_them(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("VITEST_MAX_FORKS", raising=False)
    st = _settings(tmp_path, {"cycle": {"test_workers": {"count": 3}}})
    p = _product(st)
    _task(p, "developer", "a task")
    r = LaneRunner()
    seen: list[str | None] = []
    r.work["developer"] = lambda cwd, n: seen.append(os.environ.get("VITEST_MAX_FORKS"))
    cyc = Cycle(st, p, r, "c1"); cyc.run(["execute"]); cyc.store.close()
    assert seen == ["3"] and "VITEST_MAX_FORKS" not in os.environ
