"""A task that was merged and accepted is a met dependency, whatever its summary says (ai-battles 28.11, T-0766).

T-0766 was merged, deployed, accepted at 03:04 and closed at 04:10 «no user metric of its own: closed without a
measurement». Its summary held the word «placeholder» — one of the marks of a stub result — so the next cycle called it
«done without a usable result», told the PO its dependents could never start (T-0679, T-0779, T-0780: the tower) and
left them waiting. The same happened to T-0668 the day before."""
from __future__ import annotations

from pathlib import Path

import pytest

from team import cycle as cycle_mod
from team.cycle import Cycle, _Feed
from team.store import Store

from cycle_invariants import SETTINGS, run_cycle, violations
from test_parallel_lanes import LaneRunner, _commit, _events, _product, _settings, _state, _task

BOTH = pytest.mark.parametrize("name", sorted(SETTINGS))
SUMMARY = ("Tower seat numbers are in src/server/tower.ts with unit tests for floors 1–10; no e2e or @live specs (repo_only). "
           "tests-change-reason: F-0000 placeholder (no register entry found); one new test file, no test deleted.")


def _closed_after_acceptance(p) -> str:
    s = Store(p.dir)
    t = s.create_task(title="merged, accepted, closed without a measurement", type_="defect", problem="p", scope="src/", acceptance=["a"],
                      goal="G1", created_by="po", role="developer", state="ready")
    s.transition(t["id"], "released", "dispatcher", "merged 9c7c35a", force=True, released_sha="9c7c35a", results=[{"status": "done", "summary": SUMMARY}])
    s.transition(t["id"], "accepted", "reviewer", "passed", force=True, accepted_at="2026-11-28T03:04:24+00:00")
    s.transition(t["id"], "done", "dispatcher", "no user metric of its own: closed without a measurement", force=True)
    s.close()
    return t["id"]


def test_the_summary_is_a_stub_by_the_old_test_and_the_task_is_proven():
    assert cycle_mod.is_stub_result(SUMMARY) is True                     # the word «placeholder»
    assert cycle_mod.proven({"released_sha": "9c7c35a"}) and cycle_mod.proven({"accepted_at": "2026-11-28T03:04:24+00:00"})
    assert cycle_mod.proven({"history": [{"to": "released"}, {"to": "accepted"}, {"to": "done"}]})
    assert not cycle_mod.proven({"history": [{"to": "in_progress"}, {"to": "done"}]}) and not cycle_mod.proven({})


@BOTH
def test_a_task_behind_an_accepted_and_closed_dependency_starts(tmp_path: Path, name: str):
    st = _settings(tmp_path, SETTINGS[name])
    p = _product(st)
    dep = _closed_after_acceptance(p)
    child = _task(p, "developer", "the tower task behind it", depends_on=[dep])
    r = LaneRunner()
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, ["execute"])
    cyc.store.close()
    assert violations(cyc, p, res, r) == []
    assert [c.name for c in r.cwds["developer"]] == [child] and _state(p, child)["state"] != "ready"
    assert _events(p, cycle_mod.DEAD_DEPENDENCY_EVENT) == []
    s = Store(p.dir)
    assert not [q for q in s.questions() if child in str(q.get("question"))]
    s.close()


@BOTH
def test_a_task_closed_with_nothing_behind_it_is_still_no_dependency_met(tmp_path: Path, name: str):
    st = _settings(tmp_path, SETTINGS[name])
    p = _product(st)
    s = Store(p.dir)
    stub = s.create_task(title="closed with a stub", type_="research", problem="p", scope="", acceptance=["a"], goal="G1", created_by="po", role="analyst", state="ready")
    s.transition(stub["id"], "done", "dispatcher", "closed", force=True, results=[{"status": "done", "summary": "placeholder"}])
    s.close()
    child = _task(p, "developer", "waits for a result that never came", depends_on=[stub["id"]])
    r = LaneRunner()
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, ["execute"])
    cyc.store.close()
    assert violations(cyc, p, res, r) == [] and _state(p, child)["state"] == "ready" and "developer" not in r.calls


def test_after_a_refusal_the_lane_goes_straight_to_the_next_task(tmp_path: Path):
    """28.11 04:21–04:29: three developer lanes each met a waiting task first, gave their slots away and came back four
    minutes later for the next one — which waited, too. With 51 tasks ready no developer task was in work."""
    st = _settings(tmp_path, SETTINGS["game"])
    p = _product(st)
    s = Store(p.dir)
    hold = s.create_task(title="blocked", type_="defect", problem="p", scope="src/", acceptance=["a"], goal="G1", created_by="po", role="developer", state="ready")
    s.transition(hold["id"], "blocked", "dispatcher", "no way", force=True)
    s.close()
    w1 = _task(p, "developer", "waits 1", depends_on=[hold["id"]])
    w2 = _task(p, "developer", "waits 2", depends_on=[hold["id"]])
    a = _task(p, "developer", "runs first")
    b = _task(p, "developer", "runs second")
    r = LaneRunner()
    r.work["developer"] = lambda cwd, n: _commit(cwd, f"src/part-{cwd.name}.ts", "part\n")
    cyc = Cycle(st, p, r, "c1")
    cyc.execute_prepare()
    yields = []
    cyc.lanes.yield_slot = lambda kind: yields.append(len(r.calls)) or False      # type: ignore[method-assign]
    cyc._refill_closed = "closed for the test: the lane reads its feed in order"
    order = [cyc.store.task(x) for x in (w1, w2, a, b)]
    cyc.seat_lane("developer", _Feed(order), {"developer": 4}, {}, set())
    assert [c.name for c in r.cwds["developer"]] == [a, b]
    assert yields == [1, 2]                                            # the slot is offered after each task that RAN, never after a refusal
    cyc.store.close()
