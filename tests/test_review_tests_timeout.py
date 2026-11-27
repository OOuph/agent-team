"""product.yaml `review: {tests_timeout_s: N}` — the time limit of the tests before a review (27.11, ai-battles Q-0260:
`npm run build && npm test` was cut at the fixed 900 s twice on an overloaded server)."""
from __future__ import annotations

from pathlib import Path

from team import cycle as cycle_mod
from team import gitops
from team.cycle import Cycle

from test_parallel_lanes import LaneRunner, _product, _settings


def _cycle(tmp_path: Path, extra: str = ""):
    st = _settings(tmp_path)
    p = _product(st, extra=extra)
    return Cycle(st, p, LaneRunner(), "c1"), p


def test_default_and_override_and_bad_values(tmp_path: Path, caplog):
    c, _ = _cycle(tmp_path / "a")
    assert c.review_tests_timeout() == cycle_mod.REVIEW_TEST_TIMEOUT_S == 900
    c.store.close()
    c, _ = _cycle(tmp_path / "b", "review:\n  tests_timeout_s: 1800\n")
    assert c.review_tests_timeout() == 1800
    c.store.close()
    for bad in ("soon", "5", "99999", "true"):
        c, _ = _cycle(tmp_path / f"bad-{bad}", f"review:\n  tests_timeout_s: {bad}\n")
        with caplog.at_level("WARNING"):
            assert c.review_tests_timeout() == 900
        c.store.close()
    assert "review.tests_timeout_s" in caplog.text


def test_the_limit_reaches_the_command(tmp_path: Path):
    c, p = _cycle(tmp_path, "review:\n  tests_timeout_s: 60\n")
    sha = gitops.head(p.repo_dir)
    ok = cycle_mod.run_tests_for_review(p.repo_dir, sha, tmp_path / "t1", "echo fine", timeout_s=1)
    assert "fine" in ok
    slow = cycle_mod.run_tests_for_review(p.repo_dir, sha, tmp_path / "t2", "sleep 5", timeout_s=1)
    assert "timed out after 1 s" in slow                              # the given limit, not the fixed 900 s
    c.store.close()
