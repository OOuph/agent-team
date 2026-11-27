"""`cycle.dependencies.at_merge`: a dependency is met once it is merged, not only once it is accepted (owner 27.11).

ai-battles 27.11: a review takes an hour from the merge; all 23 tasks with a dependency that started that day had waited
for «accepted» after the dependency was already in the main branch — median 54 minutes, 20.4 hours in all."""
from __future__ import annotations

from pathlib import Path

import pytest

from team import config as C
from team import cycle as cycle_mod
from team import prompts
from team.cycle import Cycle
from team.store import Store

from cycle_invariants import SETTINGS, run_cycle, violations
from test_parallel_lanes import DONE, LaneRunner, _commit, _events, _product, _released, _settings, _state, _task

ON = {"cycle": {"dependencies": {"at_merge": True}}}
FAIL = {"verdict": "fail", "summary": "wrong", "first_failure": "the corridor check lets a car outside 35–65 % through", "checks": [], "observations": []}


def test_settings(tmp_path: Path):
    assert C.DEPENDENCIES_DEFAULTS["at_merge"] is False and C.DEFAULTS["cycle"]["dependencies"]["at_merge"] is False
    p = _product(_settings(tmp_path))
    assert p.dependencies_cfg == {"at_merge": False, "roles": []} and cycle_mod.merged_counts(p.dependencies_cfg) is None
    p2 = _product(_settings(tmp_path / "b", {"cycle": {"dependencies": {"at_merge": "yes", "roles": "developer"}}}))
    assert p2.dependencies_cfg == {"at_merge": False, "roles": []}                       # wrong values keep the defaults
    ok = cycle_mod.merged_counts({"at_merge": True, "roles": ["developer"]})
    assert ok({"state": "released", "role": "developer"}) and not ok({"state": "released", "role": "designer"})
    assert not ok({"state": "in_progress", "role": "developer"}) and not ok({"state": "review", "role": "developer"})


RESULT = {"status": "done", "summary": "The corridor check is in src/engine/corridor.ts and runs for every generated car; unit tests cover both edges of the "
                                        "35–65 % corridor and the case of a car that is exactly on the edge."}


def _merged(p) -> str:
    """A task that is merged and waits for its review, with a real result on record (a one-word result is a stub, and a
    stub never meets a dependency)."""
    tid = _released(p)
    s = Store(p.dir)
    t = s.task(tid)
    t["results"] = [dict(RESULT)]
    s.save_task(t)
    s.close()
    return tid


def _pair(tmp_path: Path, over: dict):
    st = _settings(tmp_path, over)
    p = _product(st)
    dep = _merged(p)                                                   # merged, its review still to come
    waits = _task(p, "developer", "built on the merged task", depends_on=[dep])
    return st, p, dep, waits


def test_off_the_task_waits_for_the_review_as_before(tmp_path: Path):
    st, p, dep, waits = _pair(tmp_path, {})
    r = LaneRunner()
    cyc = Cycle(st, p, r, "c1"); res = run_cycle(cyc, ["execute"]); cyc.store.close()
    assert _state(p, waits)["state"] == "ready" and "developer" not in r.calls
    assert violations(cyc, p, res) == []


def test_on_the_task_is_taken_once_its_dependency_is_merged(tmp_path: Path):
    st, p, dep, waits = _pair(tmp_path, ON)
    r = LaneRunner()
    cyc = Cycle(st, p, r, "c1"); res = run_cycle(cyc, ["execute"]); cyc.store.close()
    assert _state(p, dep)["state"] == "released"                       # nobody reviewed it in this cycle
    assert _state(p, waits)["state"] != "ready" and [c.name for c in r.cwds["developer"]] == [waits]
    assert violations(cyc, p, res) == []


def test_the_scope_can_be_narrowed_to_the_role_of_the_dependency(tmp_path: Path):
    st, p, dep, waits = _pair(tmp_path, {"cycle": {"dependencies": {"at_merge": True, "roles": ["designer"]}}})
    r = LaneRunner()
    cyc = Cycle(st, p, r, "c1"); run_cycle(cyc, ["execute"]); cyc.store.close()
    assert _state(p, waits)["state"] == "ready"                        # the dependency is a developer's task: it must be accepted


@pytest.mark.parametrize("name", sorted(SETTINGS))
def test_a_failed_review_of_the_support_leaves_a_trace_and_touches_nothing(tmp_path: Path, name: str):
    over = C._merge(SETTINGS[name], ON)
    st = _settings(tmp_path, over)
    p = _product(st)
    dep = _merged(p)
    waits = _task(p, "developer", "built on the merged task", depends_on=[dep])
    untouched = _task(p, "designer", "has nothing to do with it")
    r = LaneRunner()
    r.answers["reviewer"] = dict(FAIL)
    r.answers["tester"] = dict(FAIL)
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, ["execute", "review"])                        # the dependent is started, then the support fails its review
    cyc.store.close()
    assert violations(cyc, p, res) == []
    d = _state(p, dep)
    assert d["state"] == "in_progress" and d["review_fails"] == 1      # the support goes back to its author, as always
    w = _state(p, waits)
    assert w["state"] not in ("ready", "blocked", "rejected")          # the dependent was started and is not touched
    notes = w["support_failed"]
    assert len(notes) == 1 and notes[0]["task"] == dep and "corridor check" in notes[0]["why"]
    ev = _events(p, cycle_mod.SUPPORT_FAILED_EVENT)
    assert [e["subject"] for e in ev] == [waits] and ev[0]["detail"]["support"] == dep
    assert not _state(p, untouched).get("support_failed")
    block = prompts.support_block(w)
    assert "опора получила незачёт" in block and dep in block and "corridor check" in block
    assert prompts.support_block(_state(p, untouched)) == ""


def test_a_task_not_started_yet_simply_waits_again(tmp_path: Path):
    st, p, dep, waits = _pair(tmp_path, ON)
    r = LaneRunner()
    r.answers["reviewer"] = dict(FAIL); r.answers["tester"] = dict(FAIL)
    cyc = Cycle(st, p, r, "c1"); run_cycle(cyc, ["review"]); cyc.store.close()      # the review fails before anyone took the dependent
    assert _state(p, dep)["state"] == "in_progress" and _state(p, waits)["state"] == "ready"
    assert not _state(p, waits).get("support_failed") and _events(p, cycle_mod.SUPPORT_FAILED_EVENT) == []
    s = Store(p.dir)
    assert cycle_mod.unmet_dependencies(s, s.task(waits), cycle_mod.merged_counts({"at_merge": True})) == [dep]
    s.close()


def test_a_rejected_dependency_is_still_reported_as_dead(tmp_path: Path):
    """PR #505 is not undone: «merged counts» says nothing about a task the PO closed."""
    st = _settings(tmp_path, ON)
    p = _product(st)
    s = Store(p.dir)
    gone = s.create_task(title="closed", type_="defect", problem="p", scope="src/", acceptance=["a"], goal="G1", created_by="po", role="developer", state="ready")
    s.transition(gone["id"], "rejected", "po", "closed", force=True)
    s.close()
    waits = _task(p, "developer", "waits for a closed task", depends_on=[gone["id"]])
    cyc = Cycle(st, p, LaneRunner(), "c1"); res = run_cycle(cyc, ["execute"]); cyc.store.close()
    assert violations(cyc, p, res) == [] and _state(p, waits)["state"] == "ready"
    assert len(_events(p, cycle_mod.DEAD_DEPENDENCY_EVENT)) == 1
