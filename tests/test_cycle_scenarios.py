"""Whole cycles on the stand-in runner, each on two sets of settings — the defaults and «every switch on, the seats and
the run slots of ai-battles» — with the invariants И1–И5 checked after each (tests/cycle_invariants.py).

Why (audit of 27.11.2026): every switch had a test file of its own in which that switch alone was on. The defects of
26–27.11 were combinations — retake × a waiting dependency × a lane with nothing else to do — and no test ran a cycle
the way the game runs it. A defect of that kind must turn one of these red:
  №3  a ready task waits for a rejected task in silence           → scenario 2, И3
  №9  lanes with no work take a waiting task over and over        → scenario 1, И1 / И2
  №11 a cycle across midnight UTC reports a negative spend        → scenario 5, И5
  №14 a ready task that cannot start is taken again and again     → scenario 3, И1 / И2
  an exception in a lane leaves its task `in_progress` for ever   → scenario 4, И4
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

import team.store as store_mod
from team.cycle import Cycle
from team.runner import RunResult
from team.store import Store

from cycle_invariants import SETTINGS, all_switches_on, game_like, run_cycle, violations
from test_parallel_lanes import LaneRunner, _commit, _killed, _product, _settings, _state, _task

PHASES = ["review", "execute", "measure"]
BOTH = pytest.mark.parametrize("name", sorted(SETTINGS))


def _make(tmp_path: Path, name: str):
    st = _settings(tmp_path, SETTINGS[name])
    return st, _product(st)


def test_the_game_set_has_every_switch_on():
    """The set is built from the defaults: a new switch is in it by itself."""
    on = all_switches_on()
    for key in ("retake", "fair_seats", "early_execute", "refill", "refill_while_running"):
        assert on["flow"][key] is True
    assert on["escalation"]["needs_context"] is True and on["guards"]["crash_release"] is True
    g = game_like()["cycle"]
    assert g["parallel"]["max_runs"] == 6 and g["parallel"]["review_reserve"] == 2 and g["execute_slots"]["developer"] == 3


@BOTH
def test_1_six_tasks_three_roles_one_waits_for_a_dependency_in_work(tmp_path: Path, name: str):
    st, p = _make(tmp_path, name)
    dep = _task(p, "designer", "the dependency, in work in this cycle")
    waits = _task(p, "developer", "waits for the designer", depends_on=[dep])
    others = [_task(p, "developer", "dev a"), _task(p, "developer", "dev b"), _task(p, "game-designer", "rules"), _task(p, "analyst", "numbers")]
    r = LaneRunner(sleep=0.2)                                         # the designer is still at work while the other lanes run dry
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, PHASES)
    cyc.store.close()
    assert violations(cyc, p, res) == []
    assert _state(p, waits)["state"] == "ready"                       # its dependency is merged, not accepted: it goes on waiting
    assert _state(p, dep)["state"] != "ready" and r.calls.count("designer") == 1
    if name == "game":
        assert all(_state(p, t)["state"] != "ready" for t in others)  # with the game's seats nothing startable is left behind
    assert cyc.picks.get(waits, 0) <= 4


@BOTH
def test_2_dependencies_that_cannot_be_met(tmp_path: Path, name: str):
    st, p = _make(tmp_path, name)
    s = Store(p.dir)
    rejected = s.create_task(title="closed by the PO", type_="defect", problem="p", scope="src/", acceptance=["a"], goal="G1", created_by="po", role="developer", state="ready")
    s.transition(rejected["id"], "rejected", "po", "closed", force=True)
    blocked = s.create_task(title="blocked one", type_="defect", problem="p", scope="src/", acceptance=["a"], goal="G1", created_by="po", role="developer", state="ready")
    s.transition(blocked["id"], "blocked", "dispatcher", "no way", force=True)
    done = s.create_task(title="done without a result", type_="defect", problem="p", scope="src/", acceptance=["a"], goal="G1", created_by="po", role="developer", state="ready")
    s.transition(done["id"], "done", "dispatcher", "closed", force=True)
    s.close()
    on_rejected = _task(p, "developer", "waits for a rejected task", depends_on=[rejected["id"]])
    on_missing = _task(p, "designer", "waits for a task that does not exist", depends_on=["T-9999"])
    on_blocked = _task(p, "developer", "waits for a blocked task", depends_on=[blocked["id"]])
    on_done = _task(p, "game-designer", "waits for a done task without a result", depends_on=[done["id"]])
    a = _task(p, "developer", "circle a")
    b = _task(p, "developer", "circle b", depends_on=[a])
    s = Store(p.dir)
    ta = s.task(a); ta["depends_on"] = [b]; s.save_task(ta)
    s.close()
    free = _task(p, "developer", "nothing in its way")
    r = LaneRunner()
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, PHASES)
    cyc.store.close()
    assert violations(cyc, p, res) == []
    for t in (on_rejected, on_missing, on_blocked, on_done, a, b):
        assert _state(p, t)["state"] == "ready"                       # none of them ran
    assert _state(p, free)["state"] != "ready"
    assert r.calls.count("developer") == 1 and "designer" not in r.calls and "game-designer" not in r.calls


@BOTH
def test_3_a_task_that_is_ready_again_but_still_cannot_start(tmp_path: Path, name: str):
    """№14: the role asked, the task was put back to `ready` with its question still open."""
    st, p = _make(tmp_path, name)
    asks, plain = _task(p, "developer", "asks first"), _task(p, "developer", "plain")
    r = LaneRunner()

    def dev(cwd: Path, n: int):
        if cwd.name == asks:
            return {"status": "blocked", "summary": "s", "changes": [], "how_to_verify": [], "self_check": [], "question": "where is the text of OBS-1?"}
        s = Store(p.dir)                                               # while the other task runs, the first is moved back by hand
        t = s.task(asks)
        if t and t.get("state") in ("blocked", "waiting_owner"):
            s.transition(asks, "ready", "operator", "moved back, the question stays open", force=True)
        s.close()
        _commit(cwd, f"src/part-{cwd.name}.ts", "part\n")
    r.work["developer"] = dev
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, PHASES)
    cyc.store.close()
    assert violations(cyc, p, res) == []
    assert [c.name for c in r.cwds["developer"]].count(asks) == 1     # its question is open: it did not run a second time


@BOTH
def test_4_runs_that_fail_each_in_its_own_way(tmp_path: Path, name: str):
    st, p = _make(tmp_path, name)
    cap, slow = _task(p, "developer", "hits its cap"), _task(p, "developer", "runs out of time")
    silent = _task(p, "game-designer", "no answer")
    broken = _task(p, "designer", "the dispatcher's code raises")
    fine = _task(p, "analyst", "goes through")
    r = LaneRunner()

    def dev(cwd: Path, n: int):
        if cwd.name == cap:
            return RunResult(False, None, None, 3.0, 40, "error_max_budget_usd: the run reached its cap", duration_s=60.0)
        return _killed()

    def gd(cwd: Path, n: int):
        return RunResult(False, None, None, 0.2, 3, "no structured answer", duration_s=30.0)

    def des(cwd: Path, n: int):
        raise RuntimeError("the designer lane broke")
    r.work.update({"developer": dev, "game-designer": gd, "designer": des})
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, PHASES)
    cyc.store.close()
    bad = violations(cyc, p, res)
    if name == "defaults":
        # known and kept until `cycle.guards.crash_release` is the only behaviour: without the switch the task of a lane
        # that raised stays `in_progress` with a run id nobody holds
        assert len(bad) == 1 and bad[0].startswith(f"И4: {broken} is `in_progress` held by")
    else:
        assert bad == []
        assert _state(p, broken)["state"] == "ready" and _state(p, broken)["crashes"] == 1 and _state(p, broken)["not_before"]
    assert _state(p, fine)["state"] not in ("ready", "in_progress")
    for t in (cap, slow, silent):
        assert _state(p, t)["state"] in ("ready", "in_progress", "blocked")      # each has a way on: the next run, or the PO


def test_4b_after_three_crashes_the_po_is_asked_and_his_answer_puts_the_task_back(tmp_path: Path, monkeypatch):
    import team.cycle as cycle_mod
    monkeypatch.setattr(cycle_mod, "CRASH_RETRY_MIN", 0)             # the pause between two tries is not what is tested here
    st, p = _make(tmp_path, "game")
    broken = _task(p, "designer", "the dispatcher's code raises every time")
    for i in range(3):
        r = LaneRunner()
        r.work["designer"] = lambda cwd, n: (_ for _ in ()).throw(RuntimeError("broke"))
        cyc = Cycle(st, p, r, f"c{i}")
        res = run_cycle(cyc, PHASES)
        cyc.store.close()
        assert violations(cyc, p, res) == []
    t = _state(p, broken)
    assert t["state"] == "blocked" and t["crashes"] == 3
    s = Store(p.dir)
    q = [x for x in s.questions(["open"]) if broken in (x.get("blocks") or [])]
    assert len(q) == 1 and q[0]["to"] == "po"
    s.answer(q[0]["id"], "retry", "po")
    assert s.task(broken)["state"] == "ready"
    s.close()


@BOTH
def test_5_a_cycle_across_midnight_utc(tmp_path: Path, name: str, monkeypatch):
    """№11: what the day had spent before the cycle must not be subtracted from the cycle that ends on the next day."""
    st, p = _make(tmp_path, name)
    _task(p, "developer", "runs over midnight")
    s = Store(p.dir)
    s.book_spend("earlier-today-run", "developer", 5.0, True)
    s.close()
    shift = {"days": 0}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=shift["days"])
    monkeypatch.setattr(store_mod, "datetime", Clock)
    r = LaneRunner()

    def dev(cwd: Path, n: int):
        shift["days"] = 1                                              # the run ends after midnight
        _commit(cwd, "src/late.ts", "late\n")
    r.work["developer"] = dev
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, PHASES)
    cyc.store.close()
    assert violations(cyc, p, res) == []
    assert res["spent_usd"] > 0
