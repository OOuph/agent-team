"""Run guards (rework slice 4, owner 26.11, point 6 of the guide review: «петли лечить»).

`cycle.guards.loop`: a run that makes the same tool call with the same input AND the same output `loop_repeats` times in
a row is stopped (waiting for a background command — the same call, a changing output — is not a loop).
`cycle.guards.cost`: the cap of a run is at most `cost_factor` × the role's median spend per run, so a run that costs
many times what the role usually costs is stopped by the CLI's own budget limit. Both write to the friction register.
Off by default. The time limit and the inactivity watchdog are not touched."""
from __future__ import annotations

import json
import stat
import time
from pathlib import Path

from team import config as C
from team import friction
from team.cycle import Cycle
from team.runner import KILLED_LOOP, LoopGuard, RunResult, Runner
from team.store import Store


def _use(i: int, cmd: str, parent: str | None = None) -> dict:
    return {"type": "assistant", "parent_tool_use_id": parent,
            "message": {"content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": cmd}}]}}


def _res(i: int, out: str, parent: str | None = None) -> dict:
    return {"type": "user", "parent_tool_use_id": parent, "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": out}]}}


def _feed(g: LoopGuard, pairs: list[tuple[str, str]]) -> str | None:
    hit = None
    for i, (cmd, out) in enumerate(pairs):
        hit = g.feed(_use(i, cmd)) or g.feed(_res(i, out)) or hit
    return hit


def test_five_identical_calls_with_identical_output_are_a_loop():
    assert _feed(LoopGuard(5), [("npm test", "FAIL x")] * 4) is None
    hit = _feed(LoopGuard(5), [("npm test", "FAIL x")] * 5)
    assert hit and "5 identical" in hit and "Bash" in hit and "npm test" in hit


def test_same_call_with_changing_output_is_waiting_not_a_loop():
    pairs = [("tail -1 build.log", f"step {i} of 40") for i in range(30)]
    assert _feed(LoopGuard(5), pairs) is None


def test_a_different_call_in_between_resets_the_count():
    pairs = [("npm test", "FAIL")] * 4 + [("git status", "clean")] + [("npm test", "FAIL")] * 4
    assert _feed(LoopGuard(5), pairs) is None


def test_structured_output_and_subagents_are_counted_apart():
    g = LoopGuard(3)
    for i in range(6):                                     # the role's own calls and a subagent's interleave: three each, not six in a row
        parent = "sub" if i % 2 else None
        hit = g.feed(_use(i, "ls", parent=parent)) or g.feed(_res(i, "a", parent=parent))
        assert (hit is None) == (i < 4), i                 # each stream reaches its own third repeat at i = 4 and i = 5
    g2 = LoopGuard(2)
    for i in range(4):                                     # StructuredOutput retries are the CLI's business, not a loop of the role
        e = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"t{i}", "name": "StructuredOutput", "input": {"a": 1}}]}}
        assert g2.feed(e) is None and g2.feed(_res(i, "ok")) is None


def test_zero_repeats_means_off():
    assert _feed(LoopGuard(0), [("x", "y")] * 50) is None


FAKE = r'''#!/usr/bin/env python3
import sys, json, time
sys.stdin.read()
n = int(open(sys.argv[0] + ".n").read())
vary = open(sys.argv[0] + ".vary").read() == "1"
for i in range(n):
    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": "npm test"}}]}}), flush=True)
    print(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": f"out {i if vary else 0}"}]}}), flush=True)
    time.sleep(0.02)
time.sleep(float(open(sys.argv[0] + ".sleep").read()))
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "", "structured_output": {"ok": True}, "total_cost_usd": 0.01, "num_turns": n}), flush=True)
'''


def _fake(tmp_path: Path, n: int, vary: bool, sleep: float) -> Runner:
    fake = tmp_path / "fakeclaude"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "fakeclaude.n").write_text(str(n)); (tmp_path / "fakeclaude.vary").write_text("1" if vary else "0")
    (tmp_path / "fakeclaude.sleep").write_text(str(sleep))
    return Runner({"binary": str(fake)}, {"dev": {"description": "d", "prompt": "p"}})


def test_runner_stops_a_looping_run_and_says_why(tmp_path: Path):
    r = _fake(tmp_path, 8, vary=False, sleep=30)
    t0 = time.time()
    res = r.run("dev", "task", {"type": "object"}, max_usd=0.1, cwd=tmp_path / "w", loop_repeats=5, run_dir=tmp_path / "run")
    assert time.time() - t0 < 15                           # stopped at the fifth repeat, not after the 30 s the run would take
    assert not res.ok and res.killed == KILLED_LOOP and res.error.startswith("loop: 5 identical tool calls in a row")
    assert "npm test" in res.error


def test_runner_lets_a_waiting_run_finish_and_is_off_by_default(tmp_path: Path):
    r = _fake(tmp_path, 8, vary=True, sleep=0)
    assert r.run("dev", "task", {"type": "object"}, max_usd=0.1, cwd=tmp_path / "w", loop_repeats=5).ok
    r2 = _fake(tmp_path, 8, vary=False, sleep=0)
    assert r2.run("dev", "task", {"type": "object"}, max_usd=0.1, cwd=tmp_path / "w").ok       # no loop_repeats: the old behaviour


# ------------------------------------------------------------------ the cycle: settings, the cost cap, the register

class CapRunner:
    def __init__(self, result: RunResult):
        self.result, self.kw = result, []

    def run(self, role, prompt, schema, **kw):
        self.kw.append(kw)
        return self.result


def _setup(tmp_path: Path, guards: dict | None) -> tuple[C.Settings, C.Product]:
    raw = C._merge(C.DEFAULTS, {"cycle": {"guards": guards}} if guards is not None else {})
    st = C.Settings(raw, tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")
    d = st.products_dir / "p1"
    d.mkdir(parents=True)
    (d / "product.yaml").write_text("slug: p1\nname: p1\nhost: ''\nbudget_usd_per_day: 500\n")
    (d / "VISION.md").write_text("# p1\n")
    return st, C.load_product(st, d)


OK = RunResult(True, {"x": 1}, {"total_cost_usd": 0.1}, 0.1, 3, None, session_id="s", duration_s=1.0)


def test_guards_are_off_by_default_and_bad_values_fall_back(tmp_path: Path):
    st, p = _setup(tmp_path, None)
    assert p.guards_cfg == {"loop": False, "loop_repeats": 5, "cost": False, "cost_factor": 8.0, "cost_min_runs": 20, "cost_days": 7, "cost_floor_usd": 1.0,
                            "idle_reasons": False, "reap_orphans": False, "no_name_kill": False, "budget_halt": False, "crash_release": False, "partial_limit": 0}
    st2, p2 = _setup(tmp_path / "b", {"loop": "yes", "loop_repeats": 1, "cost": True, "cost_factor": "many"})
    g = p2.guards_cfg
    assert g["loop"] is False and g["loop_repeats"] == 5 and g["cost"] is True and g["cost_factor"] == 8.0
    r = CapRunner(OK)
    c = Cycle(st, p, r, "c1")
    c.role_run("tester", "prompt", None, cwd=tmp_path / "w", label="x", cap=2.0)
    assert r.kw[0]["max_usd"] == 2.0 and not r.kw[0].get("loop_repeats")
    c.store.close()


def test_loop_guard_reaches_the_runner_and_a_loop_is_written_to_the_register(tmp_path: Path):
    st, p = _setup(tmp_path, {"loop": True, "loop_repeats": 4})
    bad = RunResult(False, None, None, None, 0, "loop: 4 identical tool calls in a row (Bash `npm test`)", killed=KILLED_LOOP, duration_s=60.0)
    r = CapRunner(bad)
    c = Cycle(st, p, r, "c1")
    c.role_run("developer", "prompt", None, cwd=tmp_path / "w", label="dev-T-0001", cap=2.0)
    assert r.kw[0]["loop_repeats"] == 4
    ev = [e for e in c.store.events() if e["kind"] == "run.loop"]
    assert len(ev) == 1 and ev[0]["actor"] == "developer" and "npm test" in ev[0]["detail"]["what"]
    recs = friction.collect_product(p, c.store)
    assert any(x["kind"] == "run_loop" and "npm test" in x["summary"] for x in recs)
    c.store.close()


def test_cost_guard_caps_a_run_at_a_multiple_of_the_roles_median(tmp_path: Path):
    st, p = _setup(tmp_path, {"cost": True, "cost_factor": 8, "cost_min_runs": 5, "cost_floor_usd": 0.2})
    s = Store(p.dir)
    for i in range(9):
        s.book_spend(f"r{i}", "tester", 0.10 if i < 8 else 5.0, True)        # median 0.10; one runaway run does not move it
    s.book_spend("d1", "developer", 1.0, True)                               # too few runs of this role: no guard
    s.close()
    over = RunResult(False, None, {"subtype": "error_max_budget_usd", "is_error": True}, 0.8, 9, "error_max_budget_usd", duration_s=60.0)
    r = CapRunner(over)
    c = Cycle(st, p, r, "c1")
    assert abs(c.cost_guard_cap("tester") - 0.8) < 1e-9 and c.cost_guard_cap("developer") is None
    c.role_run("tester", "prompt", None, cwd=tmp_path / "w", label="review-T-0001", cap=2.0)
    assert abs(r.kw[0]["max_usd"] - 0.8) < 1e-9                              # 8 × the median, below the caller's $2
    ev = [e for e in c.store.events() if e["kind"] == "run.cost_guard"]
    assert len(ev) == 1 and ev[0]["detail"]["median"] == 0.1 and ev[0]["detail"]["cap"] == 0.8
    assert any(x["kind"] == "run_cost" for x in friction.collect_product(p, c.store))
    c.role_run("developer", "prompt", None, cwd=tmp_path / "w", label="dev-T-0002", cap=2.0)
    assert r.kw[1]["max_usd"] == 2.0                                         # no median yet: the caller's cap
    c.store.close()


def test_cost_guard_never_goes_below_the_floor_or_above_the_callers_cap(tmp_path: Path):
    st, p = _setup(tmp_path, {"cost": True, "cost_factor": 8, "cost_min_runs": 5})
    s = Store(p.dir)
    for i in range(6):
        s.book_spend(f"r{i}", "reviewer", 0.01, True)
        s.book_spend(f"p{i}", "po", 3.0, True)
    s.close()
    r = CapRunner(OK)
    c = Cycle(st, p, r, "c1")
    c.role_run("reviewer", "prompt", None, cwd=tmp_path / "w", label="a", cap=2.0)
    c.role_run("po", "prompt", None, cwd=tmp_path / "w", label="b", cap=2.0)
    assert r.kw[0]["max_usd"] == 1.0 and r.kw[1]["max_usd"] == 2.0           # floor $1; 8 × $3 is above the caller's cap
    assert not [e for e in c.store.events() if e["kind"] == "run.cost_guard"]
    c.store.close()
