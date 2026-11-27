"""Whole cycles on the stand-in runner, each on two sets of settings — the defaults and «every switch on, the seats and
the run slots of ai-battles» — with the invariants И1–И5 checked after each (tests/cycle_invariants.py).

Why (audit of 27.11.2026): every switch had a test file of its own in which that switch alone was on. The defects of
26–27.11 were combinations — retake × a waiting dependency × a lane with nothing else to do — and no test ran a cycle
the way the game runs it. A defect of that kind must turn one of these red:
  №3  a ready task waits for a rejected task in silence           → scenario 2, И3
  №9  lanes with no work take a waiting task over and over        → scenario 1, И1 / И2
  №11 a cycle across midnight UTC reports a negative spend        → scenario 5, И5
  №14 a ready task that cannot start is taken again and again     → scenario 3, И1 / И2
  an exception in a lane leaves its task `in_progress` for ever   → scenario 4, И4
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

import team.store as store_mod
from team import config as C
from team.cycle import Cycle
from team.runner import RunResult
from team.store import Store

from cycle_invariants import SETTINGS, all_switches_on, game_like, run_cycle, violations
from test_parallel_lanes import DONE, LaneRunner, _commit, _events, _killed, _product, _settings, _state, _task

PHASES = ["review", "execute", "measure"]
BOTH = pytest.mark.parametrize("name", sorted(SETTINGS))


def _make(tmp_path: Path, name: str):
    st = _settings(tmp_path, SETTINGS[name])
    return st, _product(st)


def test_the_game_set_has_every_switch_on():
    """The set is built from the defaults: a new switch is in it by itself."""
    on = all_switches_on()
    for key in ("retake", "fair_seats", "early_execute", "refill", "refill_while_running"):
        assert on["flow"][key] is True
    assert on["escalation"]["needs_context"] is True and on["guards"]["crash_release"] is True
    g = game_like()["cycle"]
    assert g["parallel"]["max_runs"] == 6 and g["parallel"]["review_reserve"] == 2 and g["execute_slots"]["developer"] == 3


@BOTH
def test_1_six_tasks_three_roles_one_waits_for_a_dependency_in_work(tmp_path: Path, name: str):
    st, p = _make(tmp_path, name)
    dep = _task(p, "designer", "the dependency, in work in this cycle")
    waits = _task(p, "developer", "waits for the designer", depends_on=[dep])
    s0 = Store(p.dir)
    stuck = s0.create_task(title="blocked, nobody moves it in this cycle", type_="defect", problem="p", scope="src/", acceptance=["a"], goal="G1",
                           created_by="po", role="developer", state="ready")
    s0.transition(stuck["id"], "blocked", "dispatcher", "no way", force=True)
    s0.close()
    waits_long = _task(p, "developer", "waits for the whole cycle", depends_on=[stuck["id"]])
    others = [_task(p, "developer", "dev a"), _task(p, "developer", "dev b"), _task(p, "game-designer", "rules"), _task(p, "analyst", "numbers")]
    r = LaneRunner(sleep=0.2)                                         # the designer is still at work while the other lanes run dry
    r.answers["designer"] = {**DONE, "summary": "DESIGN.md §12 now describes the tower screen: the layout of the three zones, every state "
                                               "of the floor card, the copy of every label and what the screen shows while the list loads."}
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, PHASES)
    cyc.store.close()
    assert violations(cyc, p, res) == []
    if name == "game":
        # `cycle.dependencies.at_merge` is among the switches: the dependency was merged, so the task was taken in this cycle
        assert _state(p, waits)["state"] != "ready" and any(c.name == waits for c in r.cwds["developer"])
    else:
        assert _state(p, waits)["state"] == "ready"                   # its dependency is merged, not accepted: it goes on waiting
    assert _state(p, waits_long)["state"] == "ready" and cyc.picks.get(waits_long, 0) <= 4      # … and this one waits on both sets
    assert _state(p, dep)["state"] != "ready" and r.calls.count("designer") == 1
    if name == "game":
        assert all(_state(p, t)["state"] != "ready" for t in others)  # with the game's seats nothing startable is left behind
    assert cyc.picks.get(waits, 0) <= 4 + 1


@BOTH
def test_2_dependencies_that_cannot_be_met(tmp_path: Path, name: str):
    st, p = _make(tmp_path, name)
    s = Store(p.dir)
    rejected = s.create_task(title="closed by the PO", type_="defect", problem="p", scope="src/", acceptance=["a"], goal="G1", created_by="po", role="developer", state="ready")
    s.transition(rejected["id"], "rejected", "po", "closed", force=True)
    blocked = s.create_task(title="blocked one", type_="defect", problem="p", scope="src/", acceptance=["a"], goal="G1", created_by="po", role="developer", state="ready")
    s.transition(blocked["id"], "blocked", "dispatcher", "no way", force=True)
    done = s.create_task(title="done without a result", type_="defect", problem="p", scope="src/", acceptance=["a"], goal="G1", created_by="po", role="developer", state="ready")
    s.transition(done["id"], "done", "dispatcher", "closed", force=True)
    s.close()
    on_rejected = _task(p, "developer", "waits for a rejected task", depends_on=[rejected["id"]])
    on_missing = _task(p, "designer", "waits for a task that does not exist", depends_on=["T-9999"])
    on_blocked = _task(p, "developer", "waits for a blocked task", depends_on=[blocked["id"]])
    on_done = _task(p, "game-designer", "waits for a done task without a result", depends_on=[done["id"]])
    a = _task(p, "developer", "circle a")
    b = _task(p, "developer", "circle b", depends_on=[a])
    s = Store(p.dir)
    ta = s.task(a); ta["depends_on"] = [b]; s.save_task(ta)
    s.close()
    free = _task(p, "developer", "nothing in its way")
    r = LaneRunner()
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, PHASES)
    cyc.store.close()
    assert violations(cyc, p, res) == []
    for t in (on_rejected, on_missing, on_blocked, on_done, a, b):
        assert _state(p, t)["state"] == "ready"                       # none of them ran
    assert _state(p, free)["state"] != "ready"
    assert r.calls.count("developer") == 1 and "designer" not in r.calls and "game-designer" not in r.calls


@BOTH
def test_3_a_task_that_is_ready_again_but_still_cannot_start(tmp_path: Path, name: str):
    """№14: the role asked, the task was put back to `ready` with its question still open."""
    st, p = _make(tmp_path, name)
    asks, plain = _task(p, "developer", "asks first"), _task(p, "developer", "plain")
    r = LaneRunner()

    def dev(cwd: Path, n: int):
        if cwd.name == asks:
            return {"status": "blocked", "summary": "s", "changes": [], "how_to_verify": [], "self_check": [], "question": "where is the text of OBS-1?"}
        s = Store(p.dir)                                               # while the other task runs, the first is moved back by hand
        t = s.task(asks)
        if t and t.get("state") in ("blocked", "waiting_owner"):
            s.transition(asks, "ready", "operator", "moved back, the question stays open", force=True)
        s.close()
        _commit(cwd, f"src/part-{cwd.name}.ts", "part\n")
    r.work["developer"] = dev
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, PHASES)
    cyc.store.close()
    assert violations(cyc, p, res) == []
    assert [c.name for c in r.cwds["developer"]].count(asks) == 1     # its question is open: it did not run a second time


@BOTH
def test_4_runs_that_fail_each_in_its_own_way(tmp_path: Path, name: str):
    st, p = _make(tmp_path, name)
    cap, slow = _task(p, "developer", "hits its cap"), _task(p, "developer", "runs out of time")
    silent = _task(p, "game-designer", "no answer")
    broken = _task(p, "designer", "the dispatcher's code raises")
    fine = _task(p, "analyst", "goes through")
    r = LaneRunner()

    def dev(cwd: Path, n: int):
        if cwd.name == cap:
            return RunResult(False, None, None, 3.0, 40, "error_max_budget_usd: the run reached its cap", duration_s=60.0)
        return _killed()

    def gd(cwd: Path, n: int):
        return RunResult(False, None, None, 0.2, 3, "no structured answer", duration_s=30.0)

    def des(cwd: Path, n: int):
        raise RuntimeError("the designer lane broke")
    r.work.update({"developer": dev, "game-designer": gd, "designer": des})
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, PHASES)
    cyc.store.close()
    bad = violations(cyc, p, res)
    if name == "defaults":
        # known and kept until `cycle.guards.crash_release` is the only behaviour: without the switch the task of a lane
        # that raised stays `in_progress` with a run id nobody holds
        assert len(bad) == 1 and bad[0].startswith(f"И4: {broken} is `in_progress` held by")
    else:
        assert bad == []
        assert _state(p, broken)["state"] == "ready" and _state(p, broken)["crashes"] == 1 and _state(p, broken)["not_before"]
    assert _state(p, fine)["state"] not in ("ready", "in_progress")
    split = [e["subject"] for e in _events(p, "task.budget_split")]
    if name == "game":
        assert split == [cap] and [c.name for c in r.cwds["developer"]].count(cap) == 1      # И11: one run at the cap, then the PO splits it
    else:
        assert split == []
    for t in (cap, slow, silent):
        assert _state(p, t)["state"] in ("ready", "in_progress", "blocked")      # each has a way on: the next run, or the PO


def test_4b_after_three_crashes_the_po_is_asked_and_his_answer_puts_the_task_back(tmp_path: Path, monkeypatch):
    import team.cycle as cycle_mod
    monkeypatch.setattr(cycle_mod, "CRASH_RETRY_MIN", 0)             # the pause between two tries is not what is tested here
    st, p = _make(tmp_path, "game")
    broken = _task(p, "designer", "the dispatcher's code raises every time")
    for i in range(3):
        r = LaneRunner()
        r.work["designer"] = lambda cwd, n: (_ for _ in ()).throw(RuntimeError("broke"))
        cyc = Cycle(st, p, r, f"c{i}")
        res = run_cycle(cyc, PHASES)
        cyc.store.close()
        assert violations(cyc, p, res) == []
    t = _state(p, broken)
    assert t["state"] == "blocked" and t["crashes"] == 3
    s = Store(p.dir)
    q = [x for x in s.questions(["open"]) if broken in (x.get("blocks") or [])]
    assert len(q) == 1 and q[0]["to"] == "po"
    s.answer(q[0]["id"], "retry", "po")
    assert s.task(broken)["state"] == "ready"
    s.close()


@BOTH
def test_5_a_cycle_across_midnight_utc(tmp_path: Path, name: str, monkeypatch):
    """№11: what the day had spent before the cycle must not be subtracted from the cycle that ends on the next day."""
    st, p = _make(tmp_path, name)
    _task(p, "developer", "runs over midnight")
    s = Store(p.dir)
    s.book_spend("earlier-today-run", "developer", 5.0, True)
    s.close()
    shift = {"days": 0}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=shift["days"])
    monkeypatch.setattr(store_mod, "datetime", Clock)
    r = LaneRunner()

    def dev(cwd: Path, n: int):
        shift["days"] = 1                                              # the run ends after midnight
        _commit(cwd, "src/late.ts", "late\n")
    r.work["developer"] = dev
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, PHASES)
    cyc.store.close()
    assert violations(cyc, p, res) == []
    assert res["spent_usd"] > 0


# ------------------------------------------------------------------ batch 1 of the audit's defects (27.11): И6, И8, И9, И10

LONG = "The work is in the repository; the report names every file that changed and how each acceptance criterion can be checked by hand."


@BOTH
def test_6_a_role_without_a_seat_and_a_role_alias(tmp_path: Path, name: str):
    """№2 (26.11): a task for a role that has no seat lay three hours and nothing said why."""
    st, p = _make(tmp_path, name)
    alias = _task(p, "artist", "for the designer under its other name")
    lost = _task(p, "sound", "for a role nobody seats")
    r = LaneRunner()
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, PHASES)
    cyc.store.close()
    assert violations(cyc, p, res, r) == []
    assert _state(p, alias)["state"] != "ready" and r.calls.count("designer") == 1       # the alias is the designer's task
    assert _state(p, lost)["state"] == "ready"
    if name == "game":
        left = [x for e in _events(p, "execute.left") for x in e["detail"]["tasks"]]
        assert [x["task"] for x in left] == [lost] and "has no seat" in left[0]["why"]


@BOTH
def test_7_an_answer_in_mid_cycle_and_a_free_seat(tmp_path: Path, name: str):
    """№8 (27.11, T-0624): answered at 08:58, its seat free from 09:10, taken by the next cycle near 09:50."""
    st, p = _make(tmp_path, name)
    asks, other = _task(p, "developer", "asks first"), _task(p, "designer", "runs meanwhile")
    r = LaneRunner()

    def dev(cwd: Path, n: int):
        if r.cwds["developer"].count(cwd) == 1 and cwd.name == asks and not getattr(dev, "asked", False):
            dev.asked = True
            return {"status": "needs_context", "summary": "s", "changes": [], "how_to_verify": [], "self_check": [], "missing": ["the text of OBS-2063"],
                    "question": "what does OBS-2063 say?"}
        _commit(cwd, f"src/part-{cwd.name}.ts", "part\n")

    def des(cwd: Path, n: int):
        s = Store(p.dir)                                               # while the designer works, the question is answered
        for _ in range(100):
            qs = s.questions(["open"])
            if qs:
                for q in qs:
                    s.answer(q["id"], "OBS-2063: the label is cut on a narrow screen", "po")
                break
            import time as _t
            _t.sleep(0.05)
        s.close()
        _commit(cwd, "DESIGN.md", "design\n")
    r.work.update({"developer": dev, "designer": des})
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, PHASES)
    cyc.store.close()
    assert violations(cyc, p, res, r) == []
    runs = [c.name for c in r.cwds["developer"]].count(asks)
    if name == "game":
        assert runs == 2 and _state(p, asks)["state"] != "ready"       # taken again in the same cycle, with the answer
        assert "the label is cut on a narrow screen" in r.prompts["developer"][-1]
    else:
        assert runs == 1                                               # by default: the next cycle


def _game_backlog(p, devs: int = 6, gds: int = 4) -> None:
    for i in range(devs):
        _task(p, "developer", f"dev {i}")
    for i in range(gds):
        _task(p, "game-designer", f"rules {i}")
    _task(p, "designer", "the one designer task")
    _task(p, "analyst", "the one analyst task")


def test_8_every_role_with_a_ready_task_runs_in_the_first_round(tmp_path: Path):
    """№10 (27.11): three developers and two game designers took the slots; the one designer waited for the whole cycle.
    The game's seats 3+2+1+1+1, six run slots, two kept for the reviews: four executors at once."""
    st, p = _make(tmp_path, "game")
    _game_backlog(p)
    r = LaneRunner(wait_peak=4)                                        # no run returns before four are going at once
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, ["execute"])
    cyc.store.close()
    assert violations(cyc, p, res, r) == [] and not r.waited_out
    first = [role for role, _, _ in sorted(r.spans, key=lambda x: x[1])][:4]
    assert sorted(first) == ["analyst", "designer", "developer", "game-designer"], first      # И9: one of each role before a second of any


@BOTH
def test_9_reviews_and_the_po_beside_busy_seats(tmp_path: Path, name: str):
    """№15, №16 (27.11): six seat lanes held all six slots — reviews waited 10–56 minutes, the PO 499–601 s."""
    st, p = _make(tmp_path, name)
    s = Store(p.dir)
    released = []
    for i in range(3):
        t = s.create_task(title=f"merged {i}", type_="defect", problem="p", scope="src/", acceptance=["works"], goal="G1", created_by="po",
                          role="developer", state="ready")
        s.transition(t["id"], "released", "dispatcher", "merged", force=True, released_at="2020-01-01T00:00:00+00:00",
                     released_sha=__import__("team.gitops", fromlist=["head"]).head(p.repo_dir), results=[{"status": "done", "summary": LONG}])
        released.append(t["id"])
    s.close()
    _game_backlog(p)
    r = LaneRunner(sleep=0.15)
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, ["review", "triage", "execute"])
    cyc.store.close()
    assert violations(cyc, p, res, r) == []                            # И10: executors within their share; the PO did not wait
    assert all(_state(p, t)["state"] == "accepted" for t in released)
    if name == "game":
        checks = [a for role, a, b in r.spans if role in ("reviewer", "tester")]
        last_exec_end = max(b for role, a, b in r.spans if role in ("developer", "game-designer", "designer", "analyst"))
        assert len(checks) == 3 and max(checks) < last_exec_end        # every review ran while the seats were still at work



# ------------------------------------------------------------------ batch 2 (27.11): И7, И11–И14

@BOTH
def test_10_the_days_cap_is_used_up_with_ten_tasks_ready(tmp_path: Path, name: str):
    """№19 (24.11): with the cap of the day used up, every cycle walked all 26 ready tasks ready → in_progress → ready."""
    st = _settings(tmp_path, C._merge(SETTINGS[name], {"budget": {"max_usd_per_product_per_day": 1.0, "max_usd_per_run": 0.5, "min_usd_per_run": 0.3}}))
    p = _product(st)
    ids = [_task(p, "developer", f"task {i}") for i in range(10)]
    r = LaneRunner(cost=None)                                          # every run spends its whole cap of $0.5: two fill the day
    r.work["developer"] = lambda cwd, n: _commit(cwd, f"src/part-{cwd.name}.ts", "part\n")
    for i in range(2):
        if i:
            __import__("time").sleep(1.1)                              # the events carry whole seconds: the second cycle must not share one with the first
        cyc = Cycle(st, p, r, f"c{i}")
        res = run_cycle(cyc, ["execute"])
        cyc.store.close()
        assert violations(cyc, p, res, r) == []                        # И14 and И5 in both cycles
    assert len(r.calls) == 2
    untouched = [t for t in ids if not [h for h in _state(p, t)["history"] if h.get("to") == "in_progress"]]
    assert len(untouched) >= 5, untouched                              # half of the ten were never walked through in_progress; the rest once at most (И14)


@BOTH
def test_11_a_task_left_in_progress_by_a_cycle_that_is_gone(tmp_path: Path, name: str):
    """№24 (27.11, T-0572): the cycle that held the task was cut short; the task stayed `in_progress` for a run nobody runs."""
    st, p = _make(tmp_path, name)
    orphan = _task(p, "developer", "its cycle is gone")
    s = Store(p.dir)
    s.transition(orphan, "claimed", "dispatcher", "old-exec", assignee_run=f"c0-exec-{orphan}")
    s.transition(orphan, "in_progress", "dispatcher", "started", assignee_run=f"c0-exec-{orphan}")
    s.close()
    r = LaneRunner()
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, PHASES)
    cyc.store.close()
    bad = violations(cyc, p, res, r)
    if name == "defaults":
        # known and kept while `cycle.guards.crash_release` is a switch: nothing releases such a task
        assert len(bad) == 1 and bad[0].startswith(f"И4: {orphan} is `in_progress` held by c0-exec-")
        assert "developer" not in r.calls
    else:
        assert bad == []
        assert [e["subject"] for e in _events(p, "task.run_orphaned")] == [orphan]
        assert r.calls.count("developer") == 1 and _state(p, orphan)["state"] == "released"


@BOTH
def test_12_the_review_gives_up_on_a_task(tmp_path: Path, name: str):
    """№21: failed twice — the PO's question was bound to nothing, his answer moved no task (T-0119, T-0201, T-0313)."""
    from test_parallel_lanes import FAIL, _released
    st, p = _make(tmp_path, name)
    tid = _released(p)
    s = Store(p.dir)
    t = s.task(tid); t["review_fails"] = 1; s.save_task(t)
    s.close()
    r = LaneRunner()
    r.answers["reviewer"] = dict(FAIL); r.answers["tester"] = dict(FAIL)
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, ["review", "execute"])
    cyc.store.close()
    assert violations(cyc, p, res, r) == []                            # И7 on the game's settings
    s = Store(p.dir)
    q = s.questions(["open"])
    assert s.task(tid)["state"] == "blocked" and len(q) == 1
    assert (q[0].get("blocks") == [tid]) is (name == "game")
    s.close()


@BOTH
def test_13_dependencies_that_are_finished_without_a_result_or_go_in_a_circle(tmp_path: Path, name: str):
    """№28: a dependency that is `done` with a one-word result never counts as met, and nothing said so."""
    st, p = _make(tmp_path, name)
    s = Store(p.dir)
    done = s.create_task(title="done with a stub", type_="defect", problem="p", scope="src/", acceptance=["a"], goal="G1", created_by="po", role="developer", state="ready")
    s.transition(done["id"], "done", "dispatcher", "closed", force=True, results=[{"status": "done", "summary": "done"}])
    s.close()
    on_stub = _task(p, "developer", "waits for a result that will never come", depends_on=[done["id"]])
    a = _task(p, "designer", "circle a")
    b = _task(p, "designer", "circle b", depends_on=[a])
    s = Store(p.dir)
    ta = s.task(a); ta["depends_on"] = [b]; s.save_task(ta)
    s.close()
    cyc = Cycle(st, p, LaneRunner(), "c1")
    res = run_cycle(cyc, PHASES)
    cyc.store.close()
    assert violations(cyc, p, res) == []
    told = sorted(e["subject"] for e in _events(p, "task.dependency_dead"))
    assert told == (sorted([on_stub, a, b]) if name == "game" else [])
