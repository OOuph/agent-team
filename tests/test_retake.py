"""A task that became ready again inside the cycle is taken again in that cycle (27.11, ai-battles T-0624: stopped on a
question at 08:41, answered at 08:58, its role's seat free from 09:10 — and taken only by the next cycle near 09:50).

The rule that kept it out is real: a lane adds every task it has dealt with to the cycle's `seen` set, and a task in
that set is never offered again in the cycle. `cycle.flow.retake` (off by default) lets such a task — answered, or
failed and returned to `ready` — be taken once more (`retake_max`) while refill is open and its seat is free."""
from __future__ import annotations

from pathlib import Path

from team import flow
from team.cycle import Cycle
from team.store import Store

from test_parallel_lanes import LaneRunner, _commit, _events, _product, _settings, _state, _task

ON = {"cycle": {"flow": {"refill": True, "retake": True}, "escalation": {"needs_context": True}}}
OFF = {"cycle": {"flow": {"refill": True}, "escalation": {"needs_context": True}}}
ASK = {"status": "needs_context", "summary": "s", "changes": [], "how_to_verify": [], "self_check": [], "missing": ["the text of OBS-2063"]}


def test_defaults():
    assert flow.DEFAULTS["retake"] is False and flow.DEFAULTS["retake_max"] == 1
    assert flow.settings({"flow": {"refill": True, "retake": True, "retake_max": 9}}).retake_max == 3      # clamped


def _run(tmp_path: Path, over: dict, answer: bool = True):
    st = _settings(tmp_path, over)
    p = _product(st)
    first, second = _task(p, "developer", "asks first"), _task(p, "developer", "plain task")
    r = LaneRunner()

    def dev(cwd: Path, n: int):
        if cwd.name == first and r.cwds["developer"].count(cwd) == 1:
            return dict(ASK)                                          # its first run stops on a question
        if cwd.name == second and answer:
            s = Store(p.dir)                                          # while the second task runs, the question is answered
            for q in s.questions(["open"]):
                s.answer(q["id"], "here is the text", "owner")
            s.close()
        _commit(cwd, f"src/part-{cwd.name}.ts", "part\n")
    r.work["developer"] = dev
    cyc = Cycle(st, p, r, "c1"); cyc.run(["execute"]); cyc.store.close()
    return p, r, first, second


def test_an_answered_task_is_taken_again_in_the_same_cycle(tmp_path: Path):
    p, r, first, second = _run(tmp_path, ON)
    names = [c.name for c in r.cwds["developer"]]
    assert names == [first, second, first]                            # asked, the other ran, then it ran again with the answer
    assert _state(p, first)["state"] == "released" and "ANSWERS TO THIS TASK'S QUESTIONS" in r.prompts["developer"][2]
    ev = _events(p, "flow.retake")
    assert len(ev) == 1 and ev[0]["subject"] == first


def test_off_it_waits_for_the_next_cycle_as_before(tmp_path: Path):
    p, r, first, second = _run(tmp_path, OFF)
    assert [c.name for c in r.cwds["developer"]] == [first, second] and _state(p, first)["state"] == "ready"


def test_a_task_that_keeps_coming_back_is_not_run_in_a_loop(tmp_path: Path):
    st = _settings(tmp_path, {"cycle": {"flow": {"refill": True, "retake": True}}})
    p = _product(st)
    tid = _task(p, "developer", "never commits")
    r = LaneRunner()
    r.work["developer"] = lambda cwd, n: None                         # «done» without a commit → back to `ready` every time
    cyc = Cycle(st, p, r, "c1"); cyc.run(["execute"]); cyc.store.close()
    assert r.calls.count("developer") <= 2                            # the first run and at most `retake_max` more


def test_a_task_that_is_ready_but_not_startable_is_picked_again_only_once(tmp_path: Path):
    """27.11, first live cycle: T-0619 came back `ready` with its question still open; it was picked and refused three
    times in a minute, because only runs were counted."""
    st = _settings(tmp_path, ON)
    p = _product(st)
    tid = _task(p, "developer", "ready but waits for its question")
    cyc = Cycle(st, p, LaneRunner(), "c1")
    cyc.execute_prepare()
    seen = {tid}
    cyc._handled[tid] = 1                                              # a lane ran it once in this cycle
    slots = cyc._exec_slots
    picks = [cyc.next_for_seat("developer", slots, seen) for _ in range(5)]
    assert [bool(x) for x in picks] == [True, False, False, False, False]
    assert len(_events(p, "flow.retake")) == 1
    cyc.store.close()
