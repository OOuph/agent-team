"""One run of a role killed runs of other roles (ai-battles, 27.11 23:33 and 23:52 UTC).

A designer's run stopped «its» server with `pkill -f "dist/server/index.js"` and later with `kill` of two PIDs printed by
`pgrep -f "dist/server/index"`. The pattern matched the command lines of other roles' runs: three of them died with
«no result event (exit 143)». The dispatcher called that an outage, closed refill, and three developer seats stood idle
to the end of the cycle with forty tasks ready. In the same cycle a seat tried to take a task its review lane held, and
the exception failed the execute phase."""
from __future__ import annotations

from pathlib import Path

import pytest

from team import cycle as cycle_mod
from team.cycle import Cycle, _Feed, is_outage
from team.runner import RunResult

from cycle_invariants import SETTINGS, run_cycle, violations
from test_parallel_lanes import LaneRunner, _commit, _events, _product, _settings, _state, _task

BOTH = pytest.mark.parametrize("name", sorted(SETTINGS))


class Capture:
    def __init__(self):
        self.kw, self.prompts = [], []

    def run(self, role, prompt, schema, **kw):
        self.kw.append(kw); self.prompts.append(prompt)
        return RunResult(True, {"summary_md": "x"}, {"total_cost_usd": 0.1}, 0.1, 1, None, session_id="s")


def test_stopping_processes_by_name_is_denied_to_every_role_and_the_role_is_told_how_to_stop_its_own(tmp_path: Path):
    st = _settings(tmp_path, {"cycle": {"guards": {"no_name_kill": True}}})
    p = _product(st)
    r = Capture()
    cyc = Cycle(st, p, r, "c1")
    for role in ("designer", "developer", "reviewer"):
        cyc.role_run(role, "do the task", None, cwd=tmp_path / "w", label=role)
    cyc.store.close()
    for kw, prompt in zip(r.kw, r.prompts):
        assert set(cycle_mod.NAME_KILL_RULES) <= set(kw["disallowed_tools"])
        assert "STOP ONLY WHAT YOU STARTED, AND ONLY BY ITS PID" in prompt and "`kill <pid>`" in prompt
    off = Capture()
    c2 = Cycle(_settings(tmp_path / "off"), _product(_settings(tmp_path / "off")), off, "c1")
    c2.role_run("designer", "do the task", None, cwd=tmp_path / "w", label="des")
    c2.store.close()
    assert "disallowed_tools" not in off.kw[0] and "STOP ONLY WHAT YOU STARTED" not in off.prompts[0]


def test_no_role_card_puts_a_killable_pattern_into_the_command_line_of_its_runs():
    """The developer's card quoted the very path the designer's pattern matched; the card travels in the arguments of
    every developer run."""
    roles = Path(cycle_mod.__file__).parent / "roles"
    for f in roles.glob("*.md"):
        assert "dist/server/index" not in f.read_text(encoding="utf-8"), f.name


def test_a_run_ended_by_a_signal_is_not_an_outage():
    assert is_outage("no result event (exit 143): ", True) is False        # SIGTERM from another process
    assert is_outage("no result event (exit 137): ", True) is False        # SIGKILL
    assert is_outage("no result event (exit -15): ", True) is False
    assert is_outage("no result event (exit 1): boom", True) is True       # the CLI itself failed: the next run would too
    assert is_outage("API Error: 529 overloaded", True) is True and is_outage(None, True) is True and is_outage("x", False) is True


@BOTH
def test_a_killed_run_does_not_idle_the_other_seats(tmp_path: Path, name: str):
    st = _settings(tmp_path, SETTINGS[name])
    p = _product(st)
    killed = _task(p, "developer", "its run is killed by another process")
    rest = [_task(p, "developer", f"dev {i}") for i in range(5)]
    r = LaneRunner()

    def dev(cwd: Path, n: int):
        if cwd.name == killed and not getattr(dev, "done", False):
            dev.done = True
            return RunResult(False, None, None, 0.5, 0, "no result event (exit 143): ", duration_s=900.0)
        _commit(cwd, f"src/part-{cwd.name}.ts", "part\n")
    r.work["developer"] = dev
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, ["execute"])
    cyc.store.close()
    assert violations(cyc, p, res, r) == []
    if name == "game":
        assert all(_state(p, t)["state"] != "ready" for t in rest)         # the seats went on through the whole queue
        assert not [e for e in _events(p, "flow.refill_closed") if "outage" in str(e["detail"])]
        assert _state(p, killed)["state"] != "ready"                       # and the killed task itself was taken again in the cycle


def test_a_seat_leaves_alone_a_task_another_lane_of_the_cycle_holds(tmp_path: Path):
    """28.11 23:57: «task T-0767 is already in lane review-T-0767» — the review queued inside the cycle still held the
    task when its seat reached it; the RuntimeError failed the execute phase."""
    st = _settings(tmp_path, SETTINGS["game"])
    p = _product(st)
    held, free = _task(p, "game-designer", "its review lane still holds it"), _task(p, "game-designer", "free")
    r = LaneRunner()
    cyc = Cycle(st, p, r, "c1")
    cyc.execute_prepare()
    with cyc.lanes._mu:
        cyc.lanes._working[held] = "review-" + held                        # as `Lanes.submit(..., task_id=...)` marks it
    seen: set[str] = set()
    cyc._refill_closed = "closed for the test: the lane reads its feed"
    cyc.seat_lane("game-designer", _Feed([cyc.store.task(held), cyc.store.task(free)]), {"game-designer": 2}, {}, seen)
    assert held not in seen and _state(p, held)["state"] == "ready"         # left alone, no exception
    assert r.calls.count("game-designer") == 1 and _state(p, free)["state"] != "ready"
    cyc.store.close()
