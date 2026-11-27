"""The checker checked: every rule of tests/cycle_invariants.py that no revert of the code can turn red by itself is fed
a made-up history and must name it. A rule that never fires is not a rule."""
from __future__ import annotations

from pathlib import Path

from team.cycle import Cycle
from team.store import Store

from cycle_invariants import game_like, peak_at_once, violations
from test_parallel_lanes import LaneRunner, _product, _settings, _task


def _idle_cycle(tmp_path: Path):
    st = _settings(tmp_path, game_like())
    p = _product(st)
    cyc = Cycle(st, p, LaneRunner(), "c1")
    res = cyc.run(["measure"])
    return st, p, cyc, res


def _bad(cyc, p, res, runner=None) -> str:
    return " | ".join(violations(cyc, p, res, runner))


def test_a_clean_cycle_has_no_violations(tmp_path: Path):
    st, p, cyc, res = _idle_cycle(tmp_path)
    cyc.store.close()
    assert violations(cyc, p, res) == []


def test_i7_names_a_task_the_review_gave_up_on_without_a_bound_question(tmp_path: Path):
    st, p, cyc, res = _idle_cycle(tmp_path)
    t = _task(p, "developer")
    cyc.store.transition(t, "blocked", "reviewer", "failed review 2×: it does not work", force=True)
    w = _task(p, "developer", "waits for the owner")
    cyc.store.transition(w, "waiting_owner", "dispatcher", "asked", force=True)
    cyc.store.close()
    text = _bad(cyc, p, res)
    assert f"И7: {t} is blocked after its review gave up" in text and f"И7: {w} waits for the owner" in text


def test_i12_names_a_task_started_after_the_drain(tmp_path: Path):
    st, p, cyc, res = _idle_cycle(tmp_path)
    t = _task(p, "developer")
    cyc.store.event("cycle.drain", cyc.cycle_id, "dispatcher", {"request": "x"})
    cyc.store.transition(t, "claimed", "dispatcher", "c1-exec", assignee_run=None)
    cyc.store.transition(t, "ready", "dispatcher", "back", force=True)
    cyc.store.close()
    assert f"И12: {t} was started after the cycle took a drain request" in _bad(cyc, p, res)


def test_i13_names_a_dependent_without_the_trace(tmp_path: Path):
    st, p, cyc, res = _idle_cycle(tmp_path)
    dep = _task(p, "developer", "the support")
    child = _task(p, "developer", "built on it", depends_on=[dep])
    cyc.store.transition(dep, "in_progress", "reviewer", "failed: the edge case", force=True)
    cyc.store.transition(child, "released", "dispatcher", "merged", force=True)
    cyc.store.close()
    assert f"И13: {child} was started on {dep}" in _bad(cyc, p, res)


def test_i14_names_a_task_that_is_churned(tmp_path: Path):
    st, p, cyc, res = _idle_cycle(tmp_path)
    t = _task(p, "developer")
    for _ in range(3):
        cyc.store.transition(t, "in_progress", "dispatcher", "started", force=True)
        cyc.store.transition(t, "ready", "dispatcher", "no run", force=True)
    cyc.store.close()
    assert f"И14: {t} went in_progress → ready 3 times in the cycle with 0 run(s)" in _bad(cyc, p, res)


def test_i10_names_too_many_executors_at_once(tmp_path: Path):
    st, p, cyc, res = _idle_cycle(tmp_path)
    cyc.store.close()
    r = LaneRunner()
    r.spans = [("developer", 0.0, 10.0)] * 3 + [("game-designer", 1.0, 9.0)] * 2 + [("designer", 2.0, 8.0), ("reviewer", 2.0, 3.0)]
    assert peak_at_once(r, ("developer", "game-designer", "designer")) == 6
    assert "И10: 6 executor runs at once, the reserve for reviews leaves them 4" in _bad(cyc, p, res, r)
    cyc.lanes.gate.max_wait["main"] = 499.4
    assert "И10: a run of the main thread waited 499.4s" in _bad(cyc, p, res, r)


def test_i5_names_a_negative_spend_and_a_day_over_its_cap(tmp_path: Path):
    st, p, cyc, res = _idle_cycle(tmp_path)
    cyc.store.book_spend("c1-x", "developer", p.daily_cap + 1.0, True)
    cyc.store.close()
    text = _bad(cyc, p, {**res, "spent_usd": -345.13})
    assert "И5: the cycle reports a spend of -345.13" in text and "over the cap" in text
