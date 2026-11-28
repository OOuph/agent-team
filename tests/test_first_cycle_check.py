"""The check of the first cycle after a change of the dispatcher's code (team/firstcycle.py, audit step 2)."""
from __future__ import annotations

from pathlib import Path

import pytest

from team import firstcycle as fc
from team.cycle import Cycle

from cycle_invariants import SETTINGS, run_cycle
from test_parallel_lanes import LaneRunner, _events, _product, _settings, _task

ON = {"first_cycle_check": {"enabled": True}}


@pytest.fixture(autouse=True)
def _a_quiet_server(monkeypatch):
    """The load of the machine that runs the tests is not what is tested (a test sets it where it matters)."""
    monkeypatch.setattr(fc, "server_load", lambda: (0.1, 0.8, 8))


def _log(st) -> list[str]:
    f = st.data_dir / fc.LOG_FILE
    return f.read_text(encoding="utf-8").splitlines() if f.exists() else []


def _cycle(tmp_path: Path, monkeypatch, over: dict, commits: list[str]):
    st = _settings(tmp_path, over)
    p = _product(st)
    it = iter(commits)
    last = {"v": commits[-1]}

    def version(root):
        try:
            last["v"] = next(it)
        except StopIteration:
            pass
        return last["v"]
    monkeypatch.setattr(fc.flow, "code_version", version)
    return st, p


def test_settings_defaults_and_bad_values():
    assert fc.settings({}, {})["enabled"] is False
    cfg = fc.settings({"first_cycle_check": {"enabled": True, "cycles": 3, "refusals_per_task": "many", "idle_picks": -1, "typo": 1}},
                      {"cycle": {"first_cycle_check": {"review_wait_s": 60}}})
    assert cfg["enabled"] is True and cfg["cycles"] == 3 and cfg["refusals_per_task"] == 3 and cfg["idle_picks"] == 10 and cfg["review_wait_s"] == 60


def test_the_window_is_the_first_cycles_after_the_commit_changed(tmp_path: Path):
    d = tmp_path / "data"
    assert fc.due(d, "p", "aaa", 1) is False            # the first commit ever seen: remembered, nothing to compare with
    assert fc.due(d, "p", "aaa", 1) is False
    assert fc.due(d, "p", "bbb", 2) is True             # changed: the first cycle …
    assert fc.due(d, "p", "bbb", 2) is True             # … and the second of the window
    assert fc.due(d, "p", "bbb", 2) is False
    assert fc.due(d, "p", "", 2) is False               # the commit could not be read: nothing is claimed
    assert fc.due(d, "other", "bbb", 1) is False        # per product


def test_off_it_writes_nothing(tmp_path: Path, monkeypatch):
    st, p = _cycle(tmp_path, monkeypatch, {}, ["aaa", "bbb"])
    _task(p, "developer")
    for i in range(2):
        cyc = Cycle(st, p, LaneRunner(), f"c{i}"); cyc.run(["execute"]); cyc.store.close()
    assert _log(st) == [] and _events(p, fc.EVENT) == [] and not (st.data_dir / fc.STATE_DIR).exists()


def test_a_healthy_first_cycle_is_green_and_the_next_one_is_not_checked(tmp_path: Path, monkeypatch):
    st, p = _cycle(tmp_path, monkeypatch, ON, ["aaa", "bbb", "bbb"])
    r = LaneRunner()                                                   # one runner: its n-th run commits its n-th line
    for i in range(3):
        _task(p, "developer", f"task {i}")
        cyc = Cycle(st, p, r, f"c{i}"); cyc.run(["execute"]); cyc.store.close()
    lines = _log(st)
    assert len(lines) == 1 and " fleets c1 bbb GREEN runs=1 executed=1 " in lines[0]
    ev = _events(p, fc.EVENT)
    assert len(ev) == 1 and ev[0]["detail"]["verdict"] == "GREEN" and ev[0]["detail"]["findings"] == []
    assert not (p.dir / "STOP").exists()


def test_what_is_red(tmp_path: Path, monkeypatch):
    st, p = _cycle(tmp_path, monkeypatch, ON, ["bbb"])
    _task(p, "developer")
    cyc = Cycle(st, p, LaneRunner(), "c1")
    res = cyc.run(["execute"])
    cfg = fc.settings(st.raw, p.raw)
    assert fc.findings(cyc, res, cfg) == []
    cyc.picks["T-0572"] = 69                                           # 27.11: lanes with no work took a waiting task again and again
    cyc._not_started.extend({"task": "T-0572", "role": "developer", "why": "waits for T-0629 (`depends_on`)"} for _ in range(69))
    cyc.lanes.gate.max_wait.update({"main": 580.0, "review": 2036.0})
    cyc.store.event("phase.error", "execute", "dispatcher", "lane exec-designer: boom")
    found = fc.findings(cyc, {**res, "spent_usd": -345.13}, cfg)
    text = " | ".join(found)
    assert "T-0572 was refused 69 times" in text and "T-0572 was taken by a lane 69 times" in text
    assert "took tasks 69 times more than they carried through" in text
    assert "spend of -345.13" in text and "1 phase error(s): execute" in text
    assert "the PO waited 580s" in text and "a review waited 2036s" in text
    cyc.store.close()


def test_failed_runs_and_leftovers_are_red(tmp_path: Path, monkeypatch):
    st, p = _cycle(tmp_path, monkeypatch, ON, ["bbb"])
    cyc = Cycle(st, p, LaneRunner(), "c1")
    res = cyc.run(["execute"])
    for i in range(4):
        cyc.store.event("run", f"c1-dev-{i}", "developer", {"ok": i == 0, "cost": 0.1, "turns": 1, "error": "x"})
    t = _task(p, "developer")
    cyc.store.claim(t, "developer", "c1-exec-x")
    text = " | ".join(fc.findings(cyc, res, fc.settings(st.raw, p.raw)))
    assert "3 of 4 runs failed" in text and "left after the cycle: leases" in text
    cyc.store.close()


@pytest.mark.parametrize("name", sorted(SETTINGS))
def test_the_idle_lanes_of_27_11_make_the_first_cycle_red(tmp_path: Path, monkeypatch, name: str):
    """The defect of PR #536 put back: a refused task is offered again at once. The first cycle on the new commit says so
    in the operator's log — and nothing else happens: no STOP, no rollback."""
    import team.cycle as cycle_mod
    over = {**SETTINGS[name], **ON}
    st, p = _cycle(tmp_path, monkeypatch, over, ["aaa", "bbb"])
    cyc0 = Cycle(st, p, LaneRunner(), "c0"); cyc0.run(["execute"]); cyc0.store.close()
    dep = _task(p, "designer", "in work")
    waits = _task(p, "developer", "waits for it", depends_on=[dep])
    _task(p, "developer", "other")
    orig = cycle_mod.Cycle.seat_lane

    def leaky(self, key, feed, slots, used, seen):
        # the defect itself, limited so the test ends: after every refusal the task is offered again, 30 times
        for _ in range(30):
            orig(self, key, feed, slots, used, seen)
            with self._seat_lock:
                self._taken.discard(waits)
    if name == "game":
        monkeypatch.setattr(cycle_mod.Cycle, "seat_lane", leaky)
    cyc = Cycle(st, p, LaneRunner(sleep=0.1), "c1")
    run_cycle(cyc, ["execute"])
    cyc.store.close()
    lines = _log(st)
    assert len(lines) == 1
    if name == "game":
        assert " RED " in lines[0] and f"{waits} was refused" in lines[0]
        assert _events(p, fc.EVENT)[0]["detail"]["verdict"] == "RED"
    else:
        assert " GREEN " in lines[0]
    assert not (p.dir / "STOP").exists()



def test_the_thresholds_of_the_second_batch(tmp_path: Path, monkeypatch):
    """What no scenario can catch: the wait of a seat lane, executor runs with nothing to show, tasks walked into work and
    back, the load of the server."""
    st, p = _cycle(tmp_path, monkeypatch, ON, ["bbb"])
    monkeypatch.setattr(fc, "server_load", lambda: (0.4, 3.2, 8))
    t = _task(p, "developer")
    cyc = Cycle(st, p, LaneRunner(), "c1")
    res = cyc.run(["execute"])
    cfg = fc.settings(st.raw, p.raw)
    assert fc.findings(cyc, res, cfg) == []
    cyc.lanes.gate.max_wait["execute"] = 3549.1                        # 27.11: the second game designer lane waited that long
    for i in range(5):
        cyc.store.event("run", f"c1-dev-T-90{i:02d}", "developer", {"ok": True, "cost": 1.0, "turns": 9, "error": ""})
    walked = _task(p, "developer", "walked")
    for _ in range(3):
        cyc.store.transition(walked, "in_progress", "dispatcher", "started", force=True)
        cyc.store.transition(walked, "ready", "dispatcher", "no run (budget or dry-run)", force=True)
    monkeypatch.setattr(fc, "server_load", lambda: (3.4, 27.0, 8))
    text = " | ".join(fc.findings(cyc, res, cfg))
    assert "a seat lane waited 3549s" in text
    assert "5 of 6 executor runs carried no task through" in text
    assert f"1 task(s) went in_progress → ready without a run more than 1 time(s): {walked} ×3" in text
    assert "the server's 5-minute load is 27.0 on 8 cores = 3.4 per core" in text
    quiet = fc.settings({"first_cycle_check": {"enabled": True, "execute_wait_s": 4000, "no_result_share": 0.9, "churn_per_task": 5, "load_per_core": 4}}, {})
    assert fc.findings(cyc, res, quiet) == []                          # every threshold is a setting
    cyc.store.close()



def test_many_partial_results_make_the_first_cycle_red(tmp_path: Path, monkeypatch):
    """28.11: a deny rule kept the roles from their tests; 7 of 15 developer and designer runs of one cycle ended
    «partial», and the check of the first cycle on that setting was GREEN."""
    st, p = _cycle(tmp_path, monkeypatch, ON, ["bbb"])
    cyc = Cycle(st, p, LaneRunner(), "c1")
    res = cyc.run(["execute"])
    cfg = fc.settings(st.raw, p.raw)
    ids = [_task(p, "developer", f"t{i}") for i in range(6)]
    for i, tid in enumerate(ids):
        cyc.store.event("run", f"c1-dev-{tid}", "developer", {"ok": True, "cost": 1.0, "turns": 5, "error": ""})
        cyc.store.transition(tid, "in_progress", "dispatcher", "partial — continues next cycle" if i < 3 else "started", force=True)
    cyc.n_executed = 6
    text = " | ".join(fc.findings(cyc, res, cfg))
    assert "3 of 6 executor runs ended «partial» (limit 30%)" in text and ids[0] in text
    assert not [x for x in fc.findings(cyc, res, {**cfg, "partial_share": 0.6}) if "partial" in x]
    cyc.store.close()
