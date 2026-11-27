"""Refill survives another seat's failed run and lasts while the cycle is running (27.11, night analysis of ai-battles).

Night 26–27.11: in 5 cycles of 11 refill closed because «the run failed» — almost always a game designer's run that hit
its $3 cap — and every developer seat then stood idle until the cycle ended (3.1 seat-hours); in the other 6 it closed
at `refill_min` = 20 minutes while runs of the cycle went on for another 15–40 (1.2 seat-hours).
`cycle.flow.refill_keep_on_failure` and `cycle.flow.refill_while_running`, both off by default."""
from __future__ import annotations

import time
from pathlib import Path

from team import flow
from team.cycle import Cycle, is_outage
from team.runner import RunResult

from test_flow import _dev_files
from test_parallel_lanes import LaneRunner, _commit, _events, _product, _settings, _state, _task

KEEP = {"cycle": {"flow": {"refill": True, "refill_keep_on_failure": True}}}
HOLD = {"cycle": {"flow": {"refill": True, "refill_while_running": True}}}


def test_defaults_are_off():
    assert flow.DEFAULTS["refill_keep_on_failure"] is False and flow.DEFAULTS["refill_while_running"] is False
    cfg = flow.settings({"flow": {"refill": True, "refill_keep_on_failure": "yes"}})
    assert cfg.refill_keep_on_failure is False and cfg.refill_while_running is False


def test_what_counts_as_an_outage():
    for e in ("API error: overloaded", "rate limit reached", "cli error (exit 1): boom", "claude: not found", "529 upstream", "Credit balance is too low",
              "usage limit reached", None):
        assert is_outage(e, started=e is not None) is True, e
    for e in ("error_max_budget_usd", "timeout after 1500s", "no structured_output in the result", "loop: 5 identical tool calls in a row", "inactive for 300s"):
        assert is_outage(e, started=True) is False, e


def test_a_run_that_hit_its_own_cap_does_not_close_refill_for_the_others(tmp_path: Path):
    st = _settings(tmp_path, KEEP)
    p = _product(st)
    ids = [_task(p, "developer", f"task {i}") for i in range(5)]
    r = LaneRunner()
    r.work["developer"] = lambda cwd, n: (RunResult(False, None, {"subtype": "error_max_budget_usd"}, 3.2, 9, "error_max_budget_usd", duration_s=1.0) if n == 2
                                          else _commit(cwd, f"src/part-{cwd.name}.ts", "part\n"))
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["execute"])
    cyc.store.close()
    states = [_state(p, i)["state"] for i in ids]
    assert r.calls == ["developer"] * 5 and states == ["released", "ready", "released", "released", "released"]   # the seat went on after the failure
    assert not [e for e in _events(p, "flow.refill_closed") if "failed" in e["detail"]["why"]]
    assert _events(p, "flow.run_failed")[0]["detail"]["task"] == ids[1]


def test_an_outage_still_closes_refill_and_so_do_repeated_failures(tmp_path: Path):
    st = _settings(tmp_path / "a", KEEP)
    p = _product(st)
    ids = [_task(p, "developer", f"task {i}") for i in range(4)]
    r = LaneRunner()
    r.work["developer"] = lambda cwd, n: (RunResult(False, None, None, 0.1, 1, "API error: overloaded", duration_s=1.0) if n >= 2
                                          else _commit(cwd, f"src/part-{cwd.name}.ts", "part\n"))
    cyc = Cycle(st, p, r, "c1"); cyc.run(["execute"]); cyc.store.close()
    assert r.calls == ["developer"] * 2 and "outage" in _events(p, "flow.refill_closed")[0]["detail"]["why"]
    st = _settings(tmp_path / "b", KEEP)
    p = _product(st)
    ids = [_task(p, "developer", f"task {i}") for i in range(8)]
    r = LaneRunner()
    r.work["developer"] = lambda cwd, n: RunResult(False, None, None, 3.2, 9, "error_max_budget_usd", duration_s=1.0)
    cyc = Cycle(st, p, r, "c1"); cyc.run(["execute"]); cyc.store.close()
    assert len(r.calls) == cyc.REFILL_FAILURES_MAX                          # every run fails: it stops, the backlog is not burned
    assert "failed runs in this cycle" in _events(p, "flow.refill_closed")[0]["detail"]["why"]


def test_off_one_failed_run_closes_refill_as_before(tmp_path: Path):
    st = _settings(tmp_path, {"cycle": {"flow": {"refill": True}}})
    p = _product(st)
    [_task(p, "developer", f"task {i}") for i in range(4)]
    r = LaneRunner()
    r.work["developer"] = lambda cwd, n: (RunResult(False, None, None, 3.2, 9, "error_max_budget_usd", duration_s=1.0) if n == 2
                                          else _commit(cwd, f"src/part-{cwd.name}.ts", "part\n"))
    cyc = Cycle(st, p, r, "c1"); cyc.run(["execute"]); cyc.store.close()
    assert r.calls == ["developer"] * 2


def test_refill_lasts_while_another_seat_is_still_working(tmp_path: Path):
    """Past refill_min the window stays open while a run of the execute phase is still going (the cycle is held by it
    anyway), up to refill_max_min; off — it closes at refill_min, as before."""
    for name, extra, expect_more in (("hold", HOLD, True), ("off", {"cycle": {"flow": {"refill": True}}}, False)):
        st = _settings(tmp_path / name, {"cycle": {**extra["cycle"], "execute_slots": {"developer": 1, "designer": 1}}})
        p = _product(st)
        devs = [_task(p, "developer", f"dev {i}") for i in range(4)]
        _task(p, "designer", "long design task")
        r = LaneRunner()
        cyc = Cycle(st, p, r, "c1")

        def dev(cwd: Path, n: int, cyc=cyc):
            _commit(cwd, f"src/part-{cwd.name}.ts", "part\n")
            if n == 1:
                cyc._refill_deadline = time.monotonic() - 1                  # refill_min is over after the first developer task
            time.sleep(0.05)

        def designer(cwd: Path, n: int):
            time.sleep(1.5)                                                  # the designer's run holds the cycle meanwhile
            _commit(cwd, "public/look.css", "x\n")
        r.work["developer"], r.work["designer"] = dev, designer
        cyc.run(["execute"])
        cyc.store.close()
        done = [i for i in devs if _state(p, i)["state"] == "released"]
        assert (len(done) >= 3) if expect_more else (len(done) == 1), (name, done)
