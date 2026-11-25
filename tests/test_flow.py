"""Continuous work of the seats and writes from outside a running cycle (owner 25.11: «кто дал замок и сколько было
простоев из-за него? если у нас так простои постоянно, то надо что-то менять»; team/flow.py, team/store.py StoreLock).

Measured on ai-battles 21–25.11: the execute seats worked 18 % of the four run slots while 8–28 ready tasks waited —
a seat did one task per cycle and waited for the longest run, then 5–10 minutes passed before the next cycle; and the
operator's `team obs` / `team task new` waited up to a whole cycle (45 min) for the product lock. Here: a freed seat
takes the next task inside the cycle (`cycle.flow.refill`), the next cycle follows at once (`cycle.flow.chain`), both
stop where they would cost what the owner did not approve, and the store's own lock lets another process write while
the cycle runs — except to a task a lane is working on. FakeRunner only — no network, no models."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from team import cadence, cli, flow, locks
from team import config as C
from team.cycle import Cycle
from team.runner import RunResult
from team.store import Store, StoreLock

from test_parallel_lanes import LaneRunner, _commit, _events, _product, _settings, _state, _task

REFILL = {"cycle": {"flow": {"refill": True}}}
ROOT = Path(__file__).resolve().parent.parent


def _dev_files(r: LaneRunner) -> None:
    """Every developer run commits a file of its own: the merges of many tasks in one cycle never conflict."""
    r.work["developer"] = lambda cwd, n: _commit(cwd, f"src/part-{cwd.name}.ts", "part\n")


def _billing(st: C.Settings, **state) -> None:
    st.data_dir.mkdir(parents=True, exist_ok=True)
    (st.data_dir / "billing.json").write_text(json.dumps(state), encoding="utf-8")


# ------------------------------------------------------------------ settings

def test_flow_is_off_by_default_and_bad_values_fall_back(tmp_path: Path, caplog):
    assert flow.settings({}, {}) == flow.Config() and not flow.Config().refill and not flow.Config().chain
    cfg = flow.settings({"flow": {"refill": True, "refill_min": 30}}, {"cycle": {"flow": {"chain": True, "chain_max": 3, "max_utilization": 0.9}}}, "game")
    assert (cfg.refill, cfg.refill_min, cfg.chain, cfg.chain_max, cfg.max_utilization, cfg.on_credits) == (True, 30.0, True, 3, 0.9, False)
    assert flow.settings({"flow": True}, {}).refill and flow.settings({"flow": True}, {}).chain          # the short form
    assert not flow.settings({"flow": True}, {"cycle": {"flow": False}}).refill                           # the product switches it off
    with caplog.at_level("WARNING", logger="team.flow"):
        cfg = flow.settings({"flow": {"refill": "yes", "refill_min": 9999, "chain_max": 2.5, "typo": 1}}, {"cycle": {"flow": [1]}}, "game")
    assert cfg.refill is False and cfg.refill_min == 180 and cfg.chain_max == flow.DEFAULTS["chain_max"]
    text = caplog.text
    assert "refill='yes' is not true/false" in text and "outside" in text and "unknown key(s) typo" in text and "must be a mapping" in text
    st = _settings(tmp_path, REFILL)
    p = _product(st, extra="cycle:\n  flow:\n    chain: true\n")
    assert p.flow.refill and p.flow.chain and "freed seat" in p.flow.describe() and "back to back" in p.flow.describe()
    assert "off" in flow.Config().describe()


def test_extra_work_is_not_allowed_on_credits_or_above_the_utilization_ceiling(tmp_path: Path):
    st = _settings(tmp_path)
    assert flow.allowed(st, flow.Config()) is None                                  # no billing state yet
    _billing(st, using_credits=False, utilization=0.8)
    assert flow.allowed(st, flow.Config()) is None                                  # ceiling 1.0 = no ceiling
    assert "80%" in flow.allowed(st, flow.Config(max_utilization=0.75))
    assert flow.allowed(st, flow.Config(max_utilization=0.85)) is None
    _billing(st, using_credits=True, utilization=1.0)
    assert "credits" in flow.allowed(st, flow.Config())
    assert flow.allowed(st, flow.Config(on_credits=True)) is None


# ------------------------------------------------------------------ refill inside the execute phase

def test_without_refill_a_seat_does_one_task_per_cycle(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    ids = [_task(p, "developer", f"task {i}") for i in range(3)]
    r = LaneRunner()
    _dev_files(r)
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["execute"])
    cyc.store.close()
    assert r.calls == ["developer"] and [_state(p, i)["state"] for i in ids] == ["released", "ready", "ready"]
    assert cyc.n_executed == 1 and cyc.n_refilled == 0 and not _events(p, "flow.refill_closed")


def test_a_freed_seat_takes_the_next_task_in_the_same_cycle(tmp_path: Path):
    st = _settings(tmp_path, REFILL)
    p = _product(st)
    ids = [_task(p, "developer", f"task {i}") for i in range(4)]
    r = LaneRunner(sleep=0.05)
    _dev_files(r)
    cyc = Cycle(st, p, r, "c1")
    res = cyc.run(["execute"])
    cyc.store.close()
    assert r.calls == ["developer"] * 4 and r.peak == 1                    # one lane per role: one after another (invariant 3)
    assert all(_state(p, i)["state"] == "released" for i in ids) and res["runs"] == 4
    assert cyc.n_executed == 4 and cyc.n_refilled == 3
    assert [Path(c).name for c in r.cwds["developer"]] == ids              # in the order of the backlog, each in its own worktree


def test_refill_keeps_the_places_of_a_seat_as_the_bound_of_work_at_once(tmp_path: Path):
    st = _settings(tmp_path, C._merge(REFILL, {"cycle": {"execute_slots": {"developer": 2}, "parallel": {"overlap_roles": ["developer"]}}}))
    p = _product(st)
    ids = [_task(p, "developer", f"task {i}") for i in range(7)]
    r = LaneRunner(wait_peak=2)                                            # no run returns before two were in flight together
    _dev_files(r)
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["execute"])
    cyc.store.close()
    assert not r.waited_out and len(r.calls) == 7 and r.peak == 2                               # two places: never three at once, and both stay busy
    assert all(_state(p, i)["state"] == "released" for i in ids)
    assert sorted(Path(c).name for c in r.cwds["developer"]) == sorted(ids)      # no task twice
    assert cyc.n_refilled == 5


def test_refill_ends_when_the_window_is_over(tmp_path: Path):
    st = _settings(tmp_path, REFILL)
    p = _product(st)
    ids = [_task(p, "developer", f"task {i}") for i in range(4)]
    r = LaneRunner()
    cyc = Cycle(st, p, r, "c1")

    def work(cwd: Path, n: int):
        _commit(cwd, f"src/part-{cwd.name}.ts", "part\n")
        if n == 2:
            cyc._refill_deadline = time.monotonic() - 1                      # the phase is now older than refill_min
    r.work["developer"] = work
    cyc.run(["execute"])
    cyc.store.close()
    assert [_state(p, i)["state"] for i in ids] == ["released", "released", "ready", "ready"]
    ev = _events(p, "flow.refill_closed")
    assert len(ev) == 1 and "refill_min" in ev[0]["detail"]["why"] and ev[0]["detail"]["refilled"] == 1


def test_refill_stays_open_past_the_window_while_a_review_holds_the_cycle(tmp_path: Path):
    """The window is over after the first developer task, but a review of this cycle is still running: the cycle is held
    by it anyway, so the seat goes on; when the review is done, nothing more starts. And never past refill_max_min."""
    from test_parallel_lanes import PASS, _released
    for name, last_open, expect in (("review-holds", True, 3), ("past-max", False, 1)):
        st = _settings(tmp_path / name, REFILL)
        p = _product(st)
        _released(p)
        ids = [_task(p, "developer", f"task {i}") for i in range(8)]
        r = LaneRunner()
        cyc = Cycle(st, p, r, "c1")
        review_may_end = threading.Event()

        def dev(cwd: Path, n: int, cyc=cyc, last_open=last_open, review_may_end=review_may_end):
            _commit(cwd, f"src/part-{cwd.name}.ts", "part\n")
            if n == 1:
                cyc._refill_deadline = time.monotonic() - 1                  # refill_min is over
                if not last_open:
                    cyc._refill_last = time.monotonic() - 1                  # … and refill_max_min too
            if n == 3 or not last_open:
                review_may_end.set()                                         # the review ends while the third task runs

        def review(cwd: Path, n: int, review_may_end=review_may_end):
            review_may_end.wait(20)
            return dict(PASS)
        r.work["developer"], r.work["reviewer"] = dev, review
        cyc.run(["review", "execute"])
        cyc.store.close()
        done = [i for i in ids if _state(p, i)["state"] == "released"]
        assert expect <= len(done) < 8 and (last_open or len(done) == 1), (name, done)           # it went on for the review, and stopped after it
        why = _events(p, "flow.refill_closed")[0]["detail"]["why"]
        assert ("no review is running" in why) if last_open else ("refill_max_min" in why), (name, why)


def test_refill_stops_after_a_run_that_failed_so_an_outage_does_not_burn_the_backlog(tmp_path: Path):
    st = _settings(tmp_path, REFILL)
    p = _product(st)
    ids = [_task(p, "developer", f"task {i}") for i in range(4)]
    r = LaneRunner()
    r.work["developer"] = lambda cwd, n: (RunResult(False, None, None, 0.1, 1, "API error: overloaded", duration_s=1.0) if n >= 2
                                          else _commit(cwd, f"src/part-{cwd.name}.ts", "part\n"))
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["execute"])
    cyc.store.close()
    assert r.calls == ["developer"] * 2                                      # the third and fourth were not thrown at the outage
    assert [_state(p, i)["state"] for i in ids] == ["released", "ready", "ready", "ready"]
    assert _state(p, ids[1]).get("attempts") == 1 and not _state(p, ids[2]).get("attempts")
    assert "failed" in _events(p, "flow.refill_closed")[0]["detail"]["why"]


def test_refill_stops_at_the_daily_cap_without_churning_the_ready_tasks(tmp_path: Path):
    st = _settings(tmp_path, C._merge(REFILL, {"budget": {"max_usd_per_product_per_day": 1.0, "max_usd_per_run": 0.5, "min_usd_per_run": 0.3}}))
    p = _product(st)
    ids = [_task(p, "developer", f"task {i}") for i in range(5)]
    r = LaneRunner(cost=None)                                                # a run spends its whole cap
    _dev_files(r)
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["execute"])
    spent = cyc.store.spent_today()
    cyc.store.close()
    assert len(r.calls) == 2 and spent <= 1.0 + 1e-9                          # two runs of $0.5 fill the day
    assert [_state(p, i)["state"] for i in ids] == ["released", "released", "ready", "ready", "ready"]
    moves = [h["note"] for i in ids[3:] for h in _state(p, i)["history"]]     # the fourth and fifth were never touched
    assert not [m for m in moves if "no run" in m]


def test_refill_is_off_on_credits_unless_the_owner_allows_it(tmp_path: Path):
    for name, over, state, expect in (
            ("credits", {}, {"using_credits": True, "utilization": 1.0}, 1),
            ("credits-allowed", {"on_credits": True}, {"using_credits": True, "utilization": 1.0}, 3),
            ("ceiling", {"max_utilization": 0.75}, {"using_credits": False, "utilization": 0.8}, 1),
            ("under-ceiling", {"max_utilization": 0.9}, {"using_credits": False, "utilization": 0.8}, 3)):
        st = _settings(tmp_path / name, {"cycle": {"flow": {"refill": True, **over}}})
        p = _product(st)
        for i in range(3):
            _task(p, "developer", f"task {i}")
        _billing(st, **state)
        r = LaneRunner()
        _dev_files(r)
        cyc = Cycle(st, p, r, "c1")
        cyc.run(["execute"])
        cyc.store.close()
        assert len(r.calls) == expect, name


def test_refill_reads_the_backlog_again_and_sees_what_changed_meanwhile(tmp_path: Path):
    """While the first task runs the operator defers the second and adds an urgent one: the freed seat takes the urgent
    one first and never starts the deferred one — the list made at the start of the pass is not trusted."""
    st = _settings(tmp_path, REFILL)
    p = _product(st)
    a, b, c = (_task(p, "developer", f"task {i}", priority="P2") for i in range(3))
    made: list[str] = []
    r = LaneRunner()

    def work(cwd: Path, n: int):
        _commit(cwd, f"src/part-{cwd.name}.ts", "part\n")
        if n == 1:
            s = Store(p.dir)                                                  # another connection, as the CLI would open
            s.transition(b, "deferred", "owner", "not now")
            made.append(s.create_task(title="urgent", type_="defect", problem="p", scope="src/", acceptance=["works"], goal="G1",
                                      created_by="owner", role="developer", state="ready", priority="P0")["id"])
            s.close_thread()
    r.work["developer"] = work
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["execute"])
    cyc.store.close()
    assert [Path(x).name for x in r.cwds["developer"]] == [a, made[0], c]
    assert _state(p, b)["state"] == "deferred" and _state(p, made[0])["state"] == "released"


def test_a_task_moved_before_its_turn_in_the_feed_is_not_started(tmp_path: Path):
    """Two developer tasks of one pass, one seat with two places per cycle and no refill: the second is deferred from
    outside while the first runs — the lane reads it again before it starts and leaves it alone (no lane error)."""
    st = _settings(tmp_path, {"cycle": {"execute_slots": {"developer": 2}}})
    p = _product(st)
    a, b = _task(p, "developer", "first"), _task(p, "developer", "second")
    r = LaneRunner()

    def work(cwd: Path, n: int):
        _commit(cwd, f"src/part{n}.ts", "part\n")
        s = Store(p.dir)
        s.transition(b, "deferred", "owner", "not now")
        s.close_thread()
    r.work["developer"] = work
    cyc = Cycle(st, p, r, "c1")
    res = cyc.run(["execute"])
    cyc.store.close()
    assert res["phases"] == ["execute"] and r.calls == ["developer"] and not _events(p, "phase.error")
    assert _state(p, a)["state"] == "released" and _state(p, b)["state"] == "deferred"


# ------------------------------------------------------------------ the next cycle back to back

class _Clock(datetime):
    """`datetime.now` that moves a minute on every call: cycle ids are UTC minutes, the fake cycles take milliseconds."""
    tick = 0

    @classmethod
    def now(cls, tz=None):
        cls.tick += 1
        return datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc) + timedelta(minutes=cls.tick)


def _chain_setup(tmp_path: Path, monkeypatch, n_tasks: int, flow_yaml: str = "    chain: true\n    chain_pause_s: 0\n"):
    st = _settings(tmp_path)
    p = _product(st, "game", extra=f"cycle:\n  every_min: 5\n  flow:\n{flow_yaml}")
    ids = [_task(p, "developer", f"task {i}") for i in range(n_tasks)]
    r = LaneRunner()
    _dev_files(r)
    monkeypatch.setattr(cli.C, "load", lambda *a, **k: st)
    monkeypatch.setattr(cli, "load_roles", lambda d: {"developer": object()})
    monkeypatch.setattr(cli, "Runner", lambda claude, roles: r)
    monkeypatch.setattr(cli, "PHASES", ("execute",))
    monkeypatch.setattr("team.cycle.PHASES", ("execute",))
    monkeypatch.setattr(cli, "datetime", _Clock)
    monkeypatch.setattr(flow, "code_version", lambda root: "abc123")
    return st, p, ids, r


def _cycles(p: C.Product) -> list[dict]:
    s = Store(p.dir)
    try:
        return s.cycles()
    finally:
        s.close()


def test_the_next_cycle_starts_right_after_one_that_did_work_while_work_waits(tmp_path: Path, monkeypatch, caplog):
    st, p, ids, r = _chain_setup(tmp_path, monkeypatch, 3)
    pauses: list[bool] = []
    monkeypatch.setattr(cli, "_chain_sleep", lambda s: pauses.append(locks.product_busy(st, p.slug) or locks.busy(locks.code_lock_path(st))))
    with caplog.at_level("INFO"):
        assert cli.main(["cycle", "--due"]) == 0
    assert all(_state(p, i)["state"] == "released" for i in ids) and len(r.calls) == 3      # one seat, one task per cycle, three cycles
    assert len(_cycles(p)) == 3 and all(c["finished"] for c in _cycles(p))
    assert pauses == [False, False]                                          # between two cycles no lock is held: the operator's window
    assert "next cycle right away" in caplog.text and "no startable task is waiting" in caplog.text
    assert not cadence.check(st, p).due                                      # afterwards the cadence decides again
    assert cli.main(["cycle", "--due"]) == 0 and len(_cycles(p)) == 3


def test_a_chain_is_bounded_and_off_by_default(tmp_path: Path, monkeypatch):
    st, p, ids, r = _chain_setup(tmp_path / "max", monkeypatch, 5, "    chain: true\n    chain_pause_s: 0\n    chain_max: 2\n")
    assert cli.main(["cycle", "--due"]) == 0
    assert len(_cycles(p)) == 2 and len(r.calls) == 2                        # chain_max cycles in one process, the rest by cron
    st, p, ids, r = _chain_setup(tmp_path / "off", monkeypatch, 3, "    chain: false\n")
    assert cli.main(["cycle", "--due"]) == 0
    assert len(_cycles(p)) == 1 and len(r.calls) == 1


def test_a_chain_ends_on_new_code_a_waiting_update_a_stop_file_and_credits(tmp_path: Path, monkeypatch):
    # the checkout changed under the process
    st, p, ids, r = _chain_setup(tmp_path / "code", monkeypatch, 3)
    heads = iter(["abc123", "def456"])
    monkeypatch.setattr(flow, "code_version", lambda root: next(heads, "def456"))
    assert cli.main(["cycle", "--due"]) == 0 and len(_cycles(p)) == 1
    # a code update announced itself at the gate
    st, p, ids, r = _chain_setup(tmp_path / "gate", monkeypatch, 3)
    gate = locks._open(locks.gate_path(st))
    monkeypatch.setattr(cli, "_chain_sleep", lambda s: locks._try(gate, locks.fcntl.LOCK_EX))      # the update arrives in the pause
    assert cli.main(["cycle", "--due"]) == 0 and len(_cycles(p)) == 1
    gate.close()
    monkeypatch.undo()
    # STOP written while the first cycle ran
    st, p, ids, r = _chain_setup(tmp_path / "stop", monkeypatch, 3)
    monkeypatch.setattr(cli, "_chain_sleep", lambda s: p.stop_file.write_text("owner\n"))
    assert cli.main(["cycle", "--due"]) == 0 and len(_cycles(p)) == 1
    monkeypatch.undo()
    # the team went on credits: extra cycles are real money
    st, p, ids, r = _chain_setup(tmp_path / "credits", monkeypatch, 3)
    _billing(st, using_credits=True, utilization=1.0)
    assert cli.main(["cycle", "--due"]) == 0 and len(_cycles(p)) == 1


def test_no_chain_after_a_cycle_that_carried_nothing_through(tmp_path: Path, monkeypatch):
    st, p, ids, r = _chain_setup(tmp_path, monkeypatch, 3)
    r.work["developer"] = lambda cwd, n: RunResult(False, None, None, 0.1, 1, "API error: overloaded", duration_s=1.0)
    monkeypatch.setattr("team.cycle.Cycle.execute_task", lambda self, t: False)       # nothing starts (budget, limits)
    assert cli.main(["cycle", "--due"]) == 0
    assert len(_cycles(p)) == 1                                              # no spinning: the next look is cron's


def test_a_chained_cycle_waits_for_the_script_that_took_the_product_lock_in_the_window(tmp_path: Path, monkeypatch):
    st, p, ids, r = _chain_setup(tmp_path, monkeypatch, 2, "    chain: true\n    chain_pause_s: 0\n    chain_lock_wait_s: 20\n")
    seen: list[str] = []

    def operator_script(_s):                                                 # runs in the pause: takes the lock, edits, lets go
        def hold():
            with locks.product_cycle(st, p.slug, note="operator script") as free:
                seen.append("got the lock" if free else "busy")
                (p.dir / "VISION.md").write_text("# VISION\n\nGoals: G1. New section.\n", encoding="utf-8")
                started.set()
                time.sleep(0.5)
        started = threading.Event()
        threading.Thread(target=hold, daemon=True).start()
        assert started.wait(10)
    monkeypatch.setattr(cli, "_chain_sleep", operator_script)
    assert cli.main(["cycle", "--due"]) == 0
    assert seen == ["got the lock"] and len(_cycles(p)) == 2                 # the second cycle ran after the script, not instead of it
    assert "New section" in (p.dir / "VISION.md").read_text(encoding="utf-8")


# ------------------------------------------------------------------ writes from another process beside a running cycle

WRITER = """
import sys
from pathlib import Path
from team.store import Store
s = Store(Path(sys.argv[1]))
for i in range(int(sys.argv[3])):
    s.add_observation({"title": f"obs {sys.argv[2]}-{i}", "classification": "need_hypothesis", "actual": "x", "url": "", "source": "owner", "expected": "", "evidence": []})
    s.create_task(title=f"task {sys.argv[2]}-{i}", type_="defect", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="owner")
s.close()
"""


def test_writers_of_several_processes_never_take_the_same_id(tmp_path: Path):
    """Ids are «highest file + 1»: before the store's flock two processes could both take T-0007 and one file replaced
    the other — the reason the operator ran every `team obs` under the product lock and waited for the whole cycle."""
    d = tmp_path / "products" / "game"
    Store(d).close()
    n_proc, n_each = 4, 12
    procs = [subprocess.Popen([sys.executable, "-c", WRITER, str(d), str(k), str(n_each)], cwd=str(ROOT)) for k in range(n_proc)]
    s = Store(d)
    mine = [s.create_task(title=f"cycle task {i}", type_="defect", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po")["id"] for i in range(n_each)]
    assert all(pr.wait(timeout=120) == 0 for pr in procs)
    tasks, obs = s.tasks(), s.observations()
    assert len(tasks) == (n_proc + 1) * n_each and len({t["id"] for t in tasks}) == len(tasks) and len(set(mine)) == n_each
    assert len(obs) == n_proc * n_each and len({o["title"] for o in obs}) == len(obs)
    assert len([e for e in s.events(limit=1000) if e["kind"] == "task.create"]) == len(tasks)
    s.close()


def test_the_store_lock_is_one_per_product_in_a_process_and_excludes_another_process(tmp_path: Path):
    d = tmp_path / "products" / "game"
    a, b = Store(d), Store(d)
    assert a.lock is b.lock and isinstance(a.lock, StoreLock)
    with a.lock:                                                             # a write through the second instance re-enters
        b.event("probe", "x", "test")
        with a.lock:
            pass
        holder = "import fcntl, sys, time\nfh = open(sys.argv[1], 'a+')\ntry:\n    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)\n    print('free')\nexcept OSError:\n    print('held')\n"
        out = subprocess.run([sys.executable, "-c", holder, str(d / "store.lock")], capture_output=True, text=True, timeout=30).stdout.strip()
        assert out == "held"
    out = subprocess.run([sys.executable, "-c", holder, str(d / "store.lock")], capture_output=True, text=True, timeout=30).stdout.strip()
    assert out == "free"                                                     # nothing is held between two writes
    a.close(); b.close()


def test_a_lane_marks_its_task_in_the_store_for_the_length_of_its_work(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    tid = _task(p, "developer")
    seen: list[str | None] = []
    r = LaneRunner()

    def work(cwd: Path, n: int):
        _commit(cwd, "src/a.ts", "a\n")
        s = Store(p.dir)
        seen.append(s.working_lane(tid))
        s.close_thread()
    r.work["developer"] = work
    s0 = Store(p.dir)
    s0.mark_working("T-9999", "exec-developer")                              # left behind by a cycle that died
    s0.close()
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["execute"])
    assert seen == ["exec-developer"] and cyc.store.working_lane(tid) is None and cyc.store.working_lane("T-9999") is None
    cyc.store.close()


def test_task_move_from_outside_passes_beside_a_cycle_but_not_under_a_lane(tmp_path: Path, monkeypatch, capsys):
    st = _settings(tmp_path)
    p = _product(st, "game")
    in_lane, idle = _task(p, "developer", "in a lane"), _task(p, "developer", "nobody works on it")
    monkeypatch.setattr(cli.C, "load", lambda *a, **k: st)
    s = Store(p.dir)
    s.mark_working(in_lane, "exec-developer-1")
    with locks.product_cycle(st, p.slug, note="cycle=c1") as free:           # the product's cycle is running
        assert free
        assert cli.main(["task", "game", "move", "--id", idle, "--to", "deferred", "--note", "later"]) == 0
        assert cli.main(["task", "game", "move", "--id", in_lane, "--to", "deferred"]) == locks.EX_TEMPFAIL
        assert "lane exec-developer-1" in capsys.readouterr().err and _state(p, in_lane)["state"] == "ready"
        threading.Timer(0.6, lambda: (s.unmark_working(in_lane), s.close_thread())).start()      # the lane ends
        t0 = time.monotonic()
        assert cli.main(["task", "game", "move", "--id", in_lane, "--to", "deferred", "--wait-s", "20"]) == 0
        assert 0.3 < time.monotonic() - t0 < 15
    assert _state(p, idle)["state"] == "deferred" and _state(p, in_lane)["state"] == "deferred"
    # no cycle running: a mark left by a dead cycle does not hold the operator up
    s.mark_working(idle, "exec-developer-1")
    assert cli.main(["task", "game", "move", "--id", idle, "--to", "ready"]) == 0 and _state(p, idle)["state"] == "ready"
    s.close()


def test_obs_and_task_new_from_the_cli_pass_while_the_product_lock_is_held(tmp_path: Path, monkeypatch, capsys):
    st = _settings(tmp_path)
    p = _product(st, "game")
    monkeypatch.setattr(cli.C, "load", lambda *a, **k: st)
    with locks.product_cycle(st, p.slug, note="cycle=c1") as free:
        assert free
        t0 = time.monotonic()
        assert cli.main(["obs", "game", "--title", "owner said", "--text", "words"]) == 0
        assert cli.main(["task", "game", "new", "--title", "do it", "--ready"]) == 0
        assert time.monotonic() - t0 < 5
    out = capsys.readouterr().out.split()
    assert out[0].startswith("OBS-") and out[1].startswith("T-") and _state(p, out[1])["state"] == "ready"
