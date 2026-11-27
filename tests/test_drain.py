"""Drain on request (team/drain.py, owner 27.11: «надо разбираться с обновлением фреймворка, это важнее, чем разработка»).

A cycle of ai-battles lasts 58–80 minutes; an update of the code or an edit of product.yaml waited for its end five
times on 27.11. A request file makes the running cycle finish the runs in flight and end."""
from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

from team import drain, locks
from team.cycle import Cycle
from team.store import Store

from cycle_invariants import SETTINGS, run_cycle, violations
from test_parallel_lanes import LaneRunner, _commit, _events, _product, _settings, _state, _task

BOTH = pytest.mark.parametrize("name", sorted(SETTINGS))


@pytest.fixture(autouse=True)
def _look_every_time(monkeypatch):
    """A real cycle looks at the request files every CACHE_S seconds; the stand-in runs here last 0.3 s."""
    monkeypatch.setattr(drain, "CACHE_S", 0.0)
PHASES = ["review", "triage", "execute", "measure"]


@BOTH
@pytest.mark.parametrize("where", ["product", "all"])
def test_a_request_in_the_middle_of_execution(tmp_path: Path, name: str, where: str):
    st = _settings(tmp_path, SETTINGS[name])
    p = _product(st)
    devs = [_task(p, "developer", f"dev {i}") for i in range(8)]
    des = [_task(p, "designer", f"design {i}") for i in range(3)]
    r = LaneRunner(sleep=0.3)
    asked = threading.Event()

    def dev(cwd: Path, n: int):
        if not asked.is_set():
            asked.set()                                                # the first developer run is going: the operator asks
            drain.place(drain.product_file(st, p.slug) if where == "product" else drain.global_file(st), "test", "an edit waits")
        _commit(cwd, f"src/part-{cwd.name}.ts", "part\n")
    r.work["developer"] = dev
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, PHASES)
    go, why = cyc.chain_reason(res)
    cyc.store.close()
    assert violations(cyc, p, res) == []
    started = len(r.cwds.get("developer", [])) + len(r.cwds.get("designer", []))
    seats = 4 if name == "game" else 2                                 # developer and designer lanes that could be in flight at the request
    assert 1 <= started <= seats, (started, r.calls)                   # the runs in flight finished, nothing new was started
    left = [t for t in devs + des if _state(p, t)["state"] == "ready"]
    assert len(left) == len(devs) + len(des) - started                 # … and what was not started is untouched
    assert "measure" not in res["phases"] and "analyst" not in r.calls # no further phase
    assert len(_events(p, "cycle.drain")) == 1
    assert go is False and ("drain" in why if name == "game" else "chain is off" in why)      # and no chained cycle
    assert drain.requested(st, p.slug)                                 # the request stands until its owner removes it


@BOTH
def test_a_request_with_nothing_running_ends_the_cycle_at_once(tmp_path: Path, name: str):
    st = _settings(tmp_path, SETTINGS[name])
    p = _product(st)
    _task(p, "developer")
    drain.place(drain.product_file(st, p.slug), "test")
    r = LaneRunner()
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, PHASES, limit_s=20)
    cyc.store.close()
    assert res["runs"] == 0 and res["phases"] == [] and r.calls == []
    assert violations(cyc, p, res) == []


def test_a_forgotten_request_is_removed_and_work_goes_on(tmp_path: Path, caplog):
    st = _settings(tmp_path, {"drain": {"max_min": 90}})
    p = _product(st)
    t = _task(p, "developer")
    f = drain.place(drain.product_file(st, p.slug), "a script that died")
    old = time.time() - 91 * 60
    os.utime(f, (old, old))
    with caplog.at_level("WARNING"):
        assert drain.requested(st, p.slug) is None
    assert not f.exists() and "over drain.max_min" in caplog.text
    cyc = Cycle(st, p, LaneRunner(), "c1")
    res = run_cycle(cyc, ["execute"])
    cyc.store.close()
    assert res["runs"] == 1 and _state(p, t)["state"] != "ready" and _events(p, "cycle.drain") == []


def test_settings():
    class S:
        raw = {"drain": {"max_min": "soon"}}
    assert drain.max_min(S()) == drain.DEFAULT_MAX_MIN
    S.raw = {"drain": {"max_min": 30}}
    assert drain.max_min(S()) == 30


def test_no_new_cycle_of_the_product_starts_while_the_request_stands(tmp_path: Path):
    from team import cli
    src = Path(cli.__file__).read_text(encoding="utf-8")
    assert "drain.requested(st, p.slug)" in src                        # `_run_cycles` skips the product, `_chain` drops it
    st = _settings(tmp_path)
    p = _product(st)
    assert drain.requested(st, p.slug) is None
    drain.place(drain.global_file(st), "update-code.sh")
    assert drain.requested(st, p.slug) == str(drain.global_file(st))
    assert drain.clear(drain.global_file(st)) and drain.requested(st, p.slug) is None


def test_hold_runs_the_command_under_the_lock_and_removes_the_request(tmp_path: Path, capsys):
    st = _settings(tmp_path)
    p = _product(st)
    seen = {}

    def runner(argv, cwd=None):
        seen["argv"], seen["request"] = argv, drain.requested(st, p.slug)
        seen["locked"] = locks.product_busy(st, p.slug)
        return subprocess.CompletedProcess(argv, 3)
    assert drain.hold(st, p.slug, ["edit", "product.yaml"], "operator", "raise the cap", 5, runner=runner) == 3
    assert seen["argv"] == ["edit", "product.yaml"] and seen["request"] and seen["locked"]
    assert drain.requested(st, p.slug) is None and not locks.product_busy(st, p.slug)
    assert "lock taken" in capsys.readouterr().out

    def boom(argv, cwd=None):
        raise RuntimeError("the command broke")
    with pytest.raises(RuntimeError):
        drain.hold(st, p.slug, ["x"], "operator", "", 5, runner=boom)
    assert drain.requested(st, p.slug) is None                         # removed on an error as well


def test_hold_gives_up_when_the_lock_is_not_free_in_time(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    ran = []
    with locks.product_cycle(st, p.slug, note="a cycle") as free:
        assert free
        box = {}
        th = threading.Thread(target=lambda: box.setdefault("rc", drain.hold(st, p.slug, ["x"], "operator", "", 1, runner=lambda a, cwd=None: ran.append(a))))
        th.start(); th.join(30)
    assert box["rc"] == 75 and ran == [] and drain.requested(st, p.slug) is None


def test_hold_waits_for_a_running_cycle_to_drain_and_then_runs(tmp_path: Path):
    """The whole path: a cycle with refill and many tasks, `team hold` asked in the middle — the command runs after the
    runs in flight, long before the tasks are used up."""
    st = _settings(tmp_path, SETTINGS["game"])
    p = _product(st)
    for i in range(12):
        _task(p, "developer", f"dev {i}")
    r = LaneRunner(sleep=0.3)
    box: dict = {}
    started = threading.Event()

    def dev(cwd: Path, n: int):
        started.set()
        _commit(cwd, f"src/part-{cwd.name}.ts", "part\n")
    r.work["developer"] = dev

    def cycle():
        with locks.product_cycle(st, p.slug, note="cycle") as free:
            assert free
            cyc = Cycle(st, p, r, "c1")
            box["res"] = cyc.run(["execute"])
            cyc.store.close()
    th = threading.Thread(target=cycle); th.start()
    assert started.wait(30)

    def cmd(argv, cwd=None):
        box["runs_at_hold"] = len(r.cwds["developer"])
        return subprocess.CompletedProcess(argv, 0)
    assert drain.hold(st, p.slug, ["edit"], "operator", "", 60, runner=cmd) == 0
    th.join(30)
    assert box["runs_at_hold"] <= 3 and len(r.cwds["developer"]) == box["runs_at_hold"]      # three developer seats, no refill after the request
    s = Store(p.dir)
    assert len(s.tasks(["ready"])) == 12 - box["runs_at_hold"]
    s.close()
