"""`cycle.test_budget`: how often a role's run may run the product's WHOLE test suite (28.11).

ai-battles 27.11: 247 runs of the unit suite inside developer runs — 2.6 per run, 10.4 of the 29.8 hours developers worked
— each ~1000 CPU seconds: a third of an 8-core server that stood at a load of 28–37."""
from __future__ import annotations

from pathlib import Path

from team import config as C
from team.cycle import Cycle
from team.runner import RunResult

from test_parallel_lanes import _product, _settings


class Capture:
    def __init__(self):
        self.prompts = []

    def run(self, role, prompt, schema, **kw):
        self.prompts.append(prompt)
        return RunResult(True, {"summary_md": "x"}, {"total_cost_usd": 0.1}, 0.1, 1, None, session_id="s")


def _product_with_tests(tmp_path: Path, over: dict):
    st = _settings(tmp_path, over)
    p = _product(st)
    raw = (p.dir / "product.yaml").read_text(encoding="utf-8").replace("  kind: service\n", "  kind: service\n  test_cmd: npm ci && npm run build && npm test\n")
    (p.dir / "product.yaml").write_text(raw, encoding="utf-8")
    return st, C.load_product(st, p.dir)


def test_off_by_default_and_bad_values(tmp_path: Path):
    st, p = _product_with_tests(tmp_path, {})
    assert C.TEST_BUDGET_DEFAULTS == {"full_runs": 0, "roles": ["developer", "designer"]} and p.test_budget_cfg["full_runs"] == 0
    r = Capture()
    cyc = Cycle(st, p, r, "c1"); cyc.role_run("developer", "do the task", None, cwd=tmp_path / "w", label="dev"); cyc.store.close()
    assert "THE WHOLE TEST SUITE" not in r.prompts[0]
    st2, p2 = _product_with_tests(tmp_path / "b", {"cycle": {"test_budget": {"full_runs": "few", "roles": "developer"}}})
    assert p2.test_budget_cfg == {"full_runs": 0, "roles": ["developer", "designer"]}


def test_the_named_roles_are_told_and_the_others_are_not(tmp_path: Path):
    st, p = _product_with_tests(tmp_path, {"cycle": {"test_budget": {"full_runs": 1}}})
    r = Capture()
    cyc = Cycle(st, p, r, "c1")
    for role in ("developer", "designer", "reviewer", "game-designer"):
        cyc.role_run(role, "do the task", None, cwd=tmp_path / "w", label=role)
    cyc.store.close()
    dev, des, rev, gd = r.prompts
    assert "THE WHOLE TEST SUITE: AT MOST once IN THIS RUN" in dev and "`npm ci && npm run build && npm test`" in dev
    assert "run only the tests of what you touched" in dev and "before the review" in dev
    assert "THE WHOLE TEST SUITE" in des and "THE WHOLE TEST SUITE" not in rev and "THE WHOLE TEST SUITE" not in gd


def test_a_product_without_a_test_command_gets_no_rule(tmp_path: Path):
    st = _settings(tmp_path, {"cycle": {"test_budget": {"full_runs": 2}}})
    p = _product(st)
    r = Capture()
    cyc = Cycle(st, p, r, "c1"); cyc.role_run("developer", "do the task", None, cwd=tmp_path / "w", label="dev"); cyc.store.close()
    assert "THE WHOLE TEST SUITE" not in r.prompts[0]
