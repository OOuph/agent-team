"""`cycle.escalation.review_exit`: a task its review gave up on has a way back that needs no person (owner 27.11).

ai-battles: after «failed twice» or «unproven three times» the task went `blocked` and the PO got a question bound to
nothing — his answer moved no task, he could only close it. 14 such cases on 26–27.11: six closed, seven taken out by
hand (six of them after 5.8 hours), and T-0119, T-0201, T-0313 lay there since 22–25.11."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from team import cycle as cycle_mod
from team import prompts
from team.cycle import Cycle
from team.store import Store

from cycle_invariants import SETTINGS, run_cycle, violations
from test_parallel_lanes import FAIL, PO_NOTHING, LaneRunner, _commit, _events, _product, _released, _settings, _state, _task

ON = {"cycle": {"escalation": {"review_exit": True}}}


def _fail_twice(tmp_path: Path, over: dict):
    st = _settings(tmp_path, over)
    p = _product(st)
    tid = _released(p)
    s = Store(p.dir)
    t = s.task(tid); t["review_fails"] = 1; s.save_task(t)            # one failed review behind it
    s.close()
    r = LaneRunner()
    r.answers["reviewer"] = dict(FAIL); r.answers["tester"] = dict(FAIL)
    cyc = Cycle(st, p, r, "c1"); run_cycle(cyc, ["review"]); cyc.store.close()
    return st, p, tid, r


def test_off_the_question_is_bound_to_nothing_as_before(tmp_path: Path):
    st, p, tid, r = _fail_twice(tmp_path, {})
    s = Store(p.dir)
    q = s.questions(["open"])
    assert _state(p, tid)["state"] == "blocked" and len(q) == 1 and q[0]["to"] == "po" and not q[0].get("blocks")
    s.answer(q[0]["id"], "fix the edge case", "po")
    assert s.task(tid)["state"] == "blocked"                           # the dead end: the answer moved nothing
    s.close()


def test_on_the_question_is_bound_and_the_answer_puts_the_task_back_with_the_answer_in_the_prompt(tmp_path: Path):
    st, p, tid, r = _fail_twice(tmp_path, ON)
    s = Store(p.dir)
    q = s.questions(["open"])
    assert len(q) == 1 and q[0]["to"] == "po" and q[0]["blocks"] == [tid] and "puts it back to work" in q[0]["question"]
    assert s.task(tid)["state"] == "blocked" and s.task(tid)["review_asks"] == 1
    s.answer(q[0]["id"], "Fix only the edge case 35 %: the car on the edge is inside the corridor.", "po")
    assert s.task(tid)["state"] == "ready"
    s.close()
    r2 = LaneRunner()
    cyc = Cycle(st, p, r2, "c2"); res = run_cycle(cyc, ["execute"]); cyc.store.close()
    assert violations(cyc, p, res) == []
    assert "the car on the edge is inside the corridor" in r2.prompts["developer"][0]
    assert _state(p, tid)["state"] == "released"


def test_after_two_rounds_with_the_po_the_owner_is_asked(tmp_path: Path):
    st, p, tid, r = _fail_twice(tmp_path, ON)
    for n in (2, 3):
        s = Store(p.dir)
        q = s.questions(["open"])[0]
        s.answer(q["id"], "try this", q["to"])
        s.close()
        rr = LaneRunner()
        rr.answers["reviewer"] = dict(FAIL); rr.answers["tester"] = dict(FAIL)
        rr.work["developer"] = lambda cwd, k, n=n: _commit(cwd, f"src/try-{n}.ts", "x\n")
        cyc = Cycle(st, p, rr, f"c{n}"); run_cycle(cyc, ["execute", "review"]); cyc.store.close()
        s = Store(p.dir)
        q = s.questions(["open"])
        assert len(q) == 1 and q[0]["blocks"] == [tid] and q[0]["to"] == ("po" if n == 2 else "owner"), (n, q)
        assert s.task(tid)["state"] == ("blocked" if n == 2 else "waiting_owner")
        s.close()


def _old_blocked(p, hours: float, **fields) -> str:
    tid = _task(p, "developer", "lies in blocked")
    s = Store(p.dir)
    s.transition(tid, "blocked", "reviewer", "inconclusive 3× [other]: not accepted without proof: nobody saw the result", force=True, blocked_by="review", **fields)
    t = s.task(tid)
    t["history"][-1]["ts"] = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    s.save_task(t)
    s.close()
    return tid


def test_blocked_tasks_nobody_moves_are_listed_for_the_po(tmp_path: Path):
    st = _settings(tmp_path, ON)
    p = _product(st)
    old = _old_blocked(p, 44.7)
    fresh = _old_blocked(p, 0.5)
    asked = _old_blocked(p, 9.0)
    s = Store(p.dir)
    s.ask(from_="developer", to="po", question="which of the two?", task_id=asked)      # a question is bound: its answer will move it
    s.close()
    r = LaneRunner()
    cyc = Cycle(st, p, r, "c1")
    assert [x["task_id"] for x in cyc.blocked_without_question()] == [old]
    run_cycle(cyc, ["triage"])
    cyc.store.close()
    prompt = r.prompts["po"][0]
    assert "BLOCKED TASKS NOBODY IS MOVING" in prompt and old in prompt.split("BLOCKED TASKS NOBODY IS MOVING")[1][:900] and "return_tasks" in prompt
    off = Cycle(_settings(tmp_path / "off"), _product(_settings(tmp_path / "off")), LaneRunner(), "c1")
    assert off.blocked_block() == ""
    off.store.close()


@pytest.mark.parametrize("name", sorted(SETTINGS))
def test_the_po_returns_a_blocked_task_and_the_role_sees_what_to_fix(tmp_path: Path, name: str):
    from team import config as C
    st = _settings(tmp_path, C._merge(SETTINGS[name], ON))
    p = _product(st)
    old = _old_blocked(p, 110.0, review_inconclusive=3)
    twice = _old_blocked(p, 50.0, review_asks=2)
    r = LaneRunner()
    r.answers["po"] = {**PO_NOTHING, "return_tasks": [{"task_id": old, "what_to_fix": "Add the output of npm run sim:pickups before and after to the result."},
                                                     {"task_id": twice, "what_to_fix": "one more try"},
                                                     {"task_id": "T-9999", "what_to_fix": "x"}]}
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, ["triage", "execute"])
    cyc.store.close()
    assert violations(cyc, p, res) == []
    t = _state(p, old)
    assert t["state"] != "blocked" and t["review_asks"] == 1 and t["po_fix"][0]["what_to_fix"].startswith("Add the output of npm run sim:pickups")
    assert "THE PO SENT THIS TASK BACK TO WORK" in r.prompts["developer"][0] and "npm run sim:pickups" in r.prompts["developer"][0]
    assert [e["subject"] for e in _events(p, "task.returned")] == [old]
    w = _state(p, twice)                                               # returned twice already: the owner decides
    assert w["state"] == "waiting_owner"
    s = Store(p.dir)
    q = [x for x in s.questions(["open"]) if twice in (x.get("blocks") or [])]
    assert len(q) == 1 and q[0]["to"] == "owner" and "one more try" in q[0]["question"]
    s.close()
    assert prompts.fix_block({"po_fix": []}) == ""


def test_off_return_tasks_does_nothing(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    old = _old_blocked(p, 110.0)
    r = LaneRunner()
    r.answers["po"] = {**PO_NOTHING, "return_tasks": [{"task_id": old, "what_to_fix": "x y z"}]}
    cyc = Cycle(st, p, r, "c1"); run_cycle(cyc, ["triage"]); cyc.store.close()
    assert _state(p, old)["state"] == "blocked"
