"""Seats start on ready tasks at the start of the cycle, beside collect and the PO (27.11, night analysis of ai-battles:
the first 8 minutes of every cycle — collect, sweeps, a 3-minute PO run — the seats were empty although the dispatcher
itself had logged «next cycle right away — 3 startable task(s) waiting»; 3.0 seat-hours in a night).
`cycle.flow.early_execute` (off by default; needs the parallel cycle and `refill`)."""
from __future__ import annotations

from pathlib import Path

from team import flow
from team.cycle import Cycle
from team.store import Store

from test_flow import _dev_files
from test_parallel_lanes import LaneRunner, _events, _product, _settings, _state, _task

EARLY = {"cycle": {"flow": {"refill": True, "early_execute": True}}}


def _obs(p) -> None:
    s = Store(p.dir)
    s.add_observation({"title": "something new", "classification": "need_hypothesis", "fingerprint": "f1", "source": "t"})    # so that the PO runs
    s.close()


def test_default_is_off():
    assert flow.DEFAULTS["early_execute"] is False
    assert flow.settings({"flow": {"refill": True, "early_execute": "yes"}}).early_execute is False


def test_the_developer_starts_before_the_po_has_finished(tmp_path: Path):
    st = _settings(tmp_path, EARLY)
    p = _product(st)
    ids = [_task(p, "developer", f"task {i}") for i in range(2)]
    _obs(p)
    r = LaneRunner(wait_roles={"po": {"developer"}})                     # the PO's run does not return before a developer's run began
    _dev_files(r)
    cyc = Cycle(st, p, r, "c1")
    res = cyc.run()
    cyc.store.close()
    assert r.waited_out is False and r.calls.index("developer") < len(r.calls) and "po" in r.calls
    assert r.overlap("po", "developer")                                  # they ran side by side
    assert all(_state(p, i)["state"] in ("released", "accepted") for i in ids)
    assert "execute" in res["phases"] and "triage" in res["phases"]
    ev = _events(p, "execute.early")
    assert len(ev) == 1 and ev[0]["detail"]["lanes"] >= 1
    assert r.calls.count("developer") == 2                               # each task once: the main execute phase did not start them again


def test_off_the_developer_waits_for_triage_as_before(tmp_path: Path):
    st = _settings(tmp_path, {"cycle": {"flow": {"refill": True}}})
    p = _product(st)
    _task(p, "developer", "task")
    _obs(p)
    r = LaneRunner()
    _dev_files(r)
    cyc = Cycle(st, p, r, "c1"); cyc.run(); cyc.store.close()
    assert r.calls.index("po") < r.calls.index("developer") and not r.overlap("po", "developer") and not _events(p, "execute.early")


def test_a_task_the_po_creates_meanwhile_is_taken_in_the_same_cycle_and_seats_are_not_overfilled(tmp_path: Path):
    st = _settings(tmp_path, EARLY)
    p = _product(st)
    [_task(p, "developer", f"task {i}") for i in range(3)]
    _obs(p)
    r = LaneRunner(sleep=0.05)
    _dev_files(r)
    r.answers["po"] = {"decisions": [], "answers": [], "owner_questions": [], "no_new_tasks": False, "digest_md": "d",
                       "ready_tasks": [{"title": "cut by the PO now", "type": "defect", "problem": "p", "scope": "s", "acceptance": ["a"], "negative_cases": [],
                                        "goal": "G1", "priority": "P2", "size": "S", "role": "developer"}]}
    cyc = Cycle(st, p, r, "c1"); cyc.run(); cyc.store.close()
    s = Store(p.dir)
    new = next(t for t in s.tasks() if t["title"] == "cut by the PO now")
    assert new["state"] in ("released", "accepted")
    devs = [(a, b) for role, a, b in r.spans if role == "developer"]
    seats = st.cycle["execute_slots"].get("developer", 1)
    for a, b in devs:                                                    # never more developer runs at once than the seat has places
        assert sum(1 for c, d in devs if c < b and a < d) <= max(1, seats)
    s.close()


def test_dry_run_and_sequential_cycles_do_not_start_early(tmp_path: Path):
    st = _settings(tmp_path, {"cycle": {"flow": {"refill": True, "early_execute": True}, "parallel": {"enabled": False}}})
    p = _product(st)
    _task(p, "developer", "task")
    _obs(p)
    r = LaneRunner()
    _dev_files(r)
    cyc = Cycle(st, p, r, "c1"); cyc.run(); cyc.store.close()
    assert not _events(p, "execute.early") and r.calls.index("po") < r.calls.index("developer")
