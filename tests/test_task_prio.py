"""`team task <product> prio` — the priority of a task changed by a command, not by editing its file (27.11: the operator
raised ten tasks of the game by hand because there was no command; `move --to ready --priority` answers «ready → ready is
not allowed»)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from team import locks
from team.store import Store

from test_parallel_lanes import _events, _product, _settings, _state, _task

REPO = Path(__file__).resolve().parent.parent


def _cli(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(REPO), "TEAM_ROOT": str(root)}
    return subprocess.run([sys.executable, "-m", "team.cli", *args], cwd=root, env=env, capture_output=True, text=True, timeout=60)


def _root(tmp_path: Path):
    (tmp_path / "team.yaml").write_text("paths:\n  products_dir: products\n  data_dir: data\n  repos_dir: data/repos\n", encoding="utf-8")
    st = _settings(tmp_path)
    p = _product(st)
    return st, p


def test_the_priority_is_changed_with_a_trace(tmp_path: Path):
    st, p = _root(tmp_path)
    tid = _task(p, "developer", "a task of the tower")
    assert _state(p, tid)["priority"] == "P2"
    cp = _cli(tmp_path, "task", "fleets", "prio", "--id", tid, "--priority", "P1", "--by", "operator", "--note", "the tower first")
    assert cp.returncode == 0 and f"{tid}: P2 → P1" in cp.stdout, cp.stdout + cp.stderr
    t = _state(p, tid)
    assert t["priority"] == "P1" and t["state"] == "ready"
    assert t["history"][-1]["actor"] == "operator" and "priority P2 → P1: the tower first" in t["history"][-1]["note"]
    ev = _events(p, "task.priority")
    assert len(ev) == 1 and ev[0]["subject"] == tid and ev[0]["detail"] == {"from": "P2", "to": "P1", "note": "the tower first"}
    again = _cli(tmp_path, "task", "fleets", "prio", "--id", tid, "--priority", "P1")
    assert again.returncode == 0 and "is P1 already" in again.stdout and len(_events(p, "task.priority")) == 1


def test_missing_arguments_an_unknown_task_and_new_still_defaults_to_p2(tmp_path: Path):
    st, p = _root(tmp_path)
    assert _cli(tmp_path, "task", "fleets", "prio", "--id", "T-0001").returncode == 2
    assert _cli(tmp_path, "task", "fleets", "prio", "--id", "T-9999", "--priority", "P1").returncode == 2
    cp = _cli(tmp_path, "task", "fleets", "new", "--title", "made by hand", "--type", "defect", "--ready", "--by", "operator")
    assert cp.returncode == 0 and _state(p, cp.stdout.strip())["priority"] == "P2"


def test_a_task_a_lane_of_the_running_cycle_works_on_is_not_touched(tmp_path: Path):
    st, p = _root(tmp_path)
    tid = _task(p, "developer")
    s = Store(p.dir)
    with locks.product_cycle(st, p.slug, note="a cycle") as free:
        assert free
        s.mark_working(tid, "exec-developer-1")
        cp = _cli(tmp_path, "task", "fleets", "prio", "--id", tid, "--priority", "P0")
        s.clear_working()
    s.close()
    assert cp.returncode == 75 and "exec-developer-1" in cp.stderr and _state(p, tid)["priority"] == "P2"
