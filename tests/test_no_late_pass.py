"""`cycle.flow.no_late_pass`: after refill has closed the execute phase starts no further pass (28.11).

ai-battles `20261128T0246`: refill closed at minute 30; the developers' three seats had used their places and stood idle
from about 03:10; at 03:51 — minute 65 — «execute pass 2» seated the one seat that still had a place, a game designer,
for a 25-minute run. The cycle lasted over 70 minutes with 49 tasks ready; the next cycle, which seats everybody, could
not start."""
from __future__ import annotations

from pathlib import Path

from team import flow
from team.cycle import Cycle
from team.store import Store

from cycle_invariants import game_like, run_cycle, violations
from test_parallel_lanes import LaneRunner, _commit, _product, _settings, _state, _task
from team import config as C


def test_default_is_off():
    assert flow.DEFAULTS["no_late_pass"] is False and flow.settings({"flow": {"no_late_pass": True}}).no_late_pass is True


def _late_task(tmp_path: Path, on: bool):
    """One developer task; while it runs refill is closed and a game designer's task appears."""
    over = C._merge(game_like(), {"cycle": {"flow": {"no_late_pass": on, "fair_seats": False}}})
    st = _settings(tmp_path, over)
    p = _product(st)
    _task(p, "developer", "the cycle's work")
    r = LaneRunner()
    made: list[str] = []
    box: dict = {}

    def dev(cwd: Path, n: int):
        box["cyc"].close_refill("the test: the refill window is over")
        made.append(_task(p, "game-designer", "appears after refill has closed"))
        _commit(cwd, "src/part.ts", "part\n")
    r.work["developer"] = dev
    cyc = Cycle(st, p, r, "c1")
    box["cyc"] = cyc
    res = run_cycle(cyc, ["execute"])
    cyc.store.close()
    assert violations(cyc, p, res, r) == []
    return p, r, made[0]


def test_on_nothing_is_seated_after_refill_closed(tmp_path: Path):
    p, r, late = _late_task(tmp_path, True)
    assert "game-designer" not in r.calls and _state(p, late)["state"] == "ready"      # the next cycle's


def test_off_a_later_pass_seats_it_as_before(tmp_path: Path):
    p, r, late = _late_task(tmp_path, False)
    assert r.calls.count("game-designer") == 1 and _state(p, late)["state"] != "ready"
