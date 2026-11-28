"""`team task <product> take` / `give` — the operator takes a task for manual work and gives it back (28.11, pokerbr К8:
a task the operator had moved by hand was reviewed and replanned by the machine all the same)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from team import locks
from team.store import Store

from test_parallel_lanes import _events, _product, _settings, _state, _task

REPO = Path(__file__).resolve().parent.parent
HOLD_ON = "cycle:\n  escalation:\n    operator_hold: true\n"


def _cli(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(REPO), "TEAM_ROOT": str(root)}
    return subprocess.run([sys.executable, "-m", "team.cli", *args], cwd=root, env=env, capture_output=True, text=True, timeout=60)


def _root(tmp_path: Path, on: bool = True):
    (tmp_path / "team.yaml").write_text("paths:\n  products_dir: products\n  data_dir: data\n  repos_dir: data/repos\n" + (HOLD_ON if on else ""), encoding="utf-8")
    st = _settings(tmp_path)
    return st, _product(st)


def test_take_marks_the_task_and_give_clears_it(tmp_path: Path):
    st, p = _root(tmp_path)
    tid = _task(p, "developer", "checked on the live site by hand")
    cp = _cli(tmp_path, "task", "fleets", "take", "--id", tid, "--by", "operator", "--note", "deploy by hand")
    assert cp.returncode == 0 and f"{tid}: taken by operator" in cp.stdout, cp.stdout + cp.stderr
    t = _state(p, tid)
    assert t["held_by"] == "operator" and t["held_note"] == "deploy by hand" and t["state"] == "ready"
    assert "taken by operator" in t["history"][-1]["note"] and [e["subject"] for e in _events(p, "task.held")] == [tid]
    cp = _cli(tmp_path, "task", "fleets", "give", "--id", tid, "--by", "operator")
    assert cp.returncode == 0 and "given back" in cp.stdout
    t = _state(p, tid)
    assert "held_by" not in t and "held_at" not in t and "given back by operator (was held by operator)" in t["history"][-1]["note"]
    assert _events(p, "task.given")[0]["detail"]["was_held_by"] == "operator"


def test_take_refuses_while_the_switch_is_off_and_for_an_unknown_task(tmp_path: Path):
    st, p = _root(tmp_path, on=False)
    tid = _task(p, "developer")
    cp = _cli(tmp_path, "task", "fleets", "take", "--id", tid, "--by", "operator")
    assert cp.returncode == 2 and "operator_hold" in cp.stderr and "held_by" not in _state(p, tid)
    assert _cli(tmp_path, "task", "fleets", "take", "--by", "operator").returncode == 2
    assert _cli(tmp_path, "task", "fleets", "give", "--id", "T-9999").returncode == 2


def test_a_task_a_lane_of_the_running_cycle_works_on_is_not_taken(tmp_path: Path):
    st, p = _root(tmp_path)
    tid = _task(p, "developer")
    s = Store(p.dir)
    with locks.product_cycle(st, p.slug, note="a cycle") as free:
        assert free
        s.mark_working(tid, "exec-developer-1")
        cp = _cli(tmp_path, "task", "fleets", "take", "--id", tid, "--by", "operator")
        assert cp.returncode == locks.EX_TEMPFAIL and "exec-developer-1" in cp.stderr and "held_by" not in _state(p, tid)
    s.close()
