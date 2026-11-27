"""Every seat with a startable task gets a run slot in turn (`cycle.flow.fair_seats`, 27.11).

ai-battles, cycle 10:45–11:59: the designer's task T-0646 was created at 11:08 and its seat got a lane only at 11:48 —
a seat lane is started by a pass of the execute phase, and the pass waits for every lane of the one before. And with a
reserve for the reviews there are fewer slots than seat lanes: a lane holds its slot until it ends."""
from __future__ import annotations

import threading
import time
from pathlib import Path

from team import flow, lanes
from team.cycle import Cycle

from test_parallel_lanes import WAIT_S, LaneRunner, _commit, _events, _product, _settings, _state, _task

ON = {"cycle": {"flow": {"refill": True, "fair_seats": True}}}
OFF = {"cycle": {"flow": {"refill": True}}}


def test_default_is_off():
    assert flow.DEFAULTS["fair_seats"] is False and flow.settings({"flow": {"fair_seats": True}}).fair_seats is True


def test_a_lane_gives_its_slot_to_a_waiting_lane_and_gets_it_back():
    gate = lanes.RunGate(1)
    order, go = [], threading.Event()

    def first():
        with gate.slot(lanes.LANE_EXECUTE, "a"):
            order.append("a1")
            assert gate.yield_slot(lanes.LANE_EXECUTE, "a") is False          # nobody waits: the slot stays
            go.wait(WAIT_S)
            assert gate.yield_slot(lanes.LANE_EXECUTE, "a") is True           # b waits: b works, then a again
            order.append("a2")

    def second():
        with gate.slot(lanes.LANE_EXECUTE, "b"):
            order.append("b")

    ta = threading.Thread(target=first); ta.start()
    while "a1" not in order:
        time.sleep(0.01)
    tb = threading.Thread(target=second); tb.start()
    while gate.in_flight(lanes.LANE_EXECUTE) < 2:
        time.sleep(0.01)
    go.set(); ta.join(WAIT_S); tb.join(WAIT_S)
    assert order == ["a1", "b", "a2"] and gate.at_work() == 0


def test_a_waiting_review_does_not_take_the_slot_of_a_yielding_seat():
    gate = lanes.RunGate(2, {lanes.LANE_REVIEW: 1})
    with gate.slot(lanes.LANE_EXECUTE, "a"):
        assert gate.yield_slot(lanes.LANE_EXECUTE, "a") is False


def _late_designer(tmp_path: Path, over: dict):
    """A developer with two tasks; the designer's task appears while the first one runs."""
    st = _settings(tmp_path, over)
    p = _product(st)
    d1, d2 = _task(p, "developer", "first"), _task(p, "developer", "second")
    r = LaneRunner()
    made: list[str] = []

    def dev(cwd: Path, n: int):
        if not made:
            made.append(_task(p, "designer", "appears in mid-cycle"))
        _commit(cwd, f"src/part-{cwd.name}.ts", "part\n")
    r.work["developer"] = dev
    cyc = Cycle(st, p, r, "c1"); cyc.run(["execute"]); cyc.store.close()
    return p, r, made[0]


def test_a_seat_whose_first_task_appears_in_mid_cycle_gets_its_lane_at_once(tmp_path: Path):
    p, r, late = _late_designer(tmp_path, ON)
    ev = _events(p, "flow.seat_started")
    assert len(ev) == 1 and r.calls.count("designer") == 1
    assert _state(p, late)["state"] != "ready"


def test_off_the_seat_waits_for_the_next_pass_as_before(tmp_path: Path):
    p, r, late = _late_designer(tmp_path, OFF)
    assert _events(p, "flow.seat_started") == [] and r.calls.count("designer") == 1      # the next pass of the same cycle


def test_the_first_lane_of_every_seat_is_queued_before_the_second_lane_of_any(tmp_path: Path):
    over = {"cycle": {"flow": {"refill": True, "fair_seats": True}, "execute_slots": {"developer": 3, "designer": 1},
                      "parallel": {"enabled": True, "max_runs": 4, "overlap_roles": ["developer"]}}}
    st = _settings(tmp_path, over)
    p = _product(st)
    for i in range(3):
        _task(p, "developer", f"dev {i}")
    _task(p, "designer", "the one designer task")
    cyc = Cycle(st, p, LaneRunner(), "c1")
    cyc.execute_prepare()
    labels: list[str] = []
    cyc.lanes.submit = lambda phase, kind, label, fn, *a, **k: labels.append(label)      # type: ignore[method-assign]
    assert cyc.start_seat_lanes(1) == 4
    assert labels == ["exec-developer-1", "exec-designer", "exec-developer-2", "exec-developer-3"]
    cyc.store.close()
