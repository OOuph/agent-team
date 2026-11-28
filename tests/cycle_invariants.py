"""Invariants of one cycle, checked after every scenario (audit of 27.11.2026, step 1).

The tests of a single switch check a log line or the state of one task. These check what must hold for EVERY cycle,
whatever the settings:

И1  a seat lane takes a task at most EXECUTE_WAVES_MAX + retake_max times in a cycle without the task changing state
    (27.11: T-0572 was taken 69 times in one cycle, T-0619 three times in a minute)
И2  the cycle ends: `run_cycle` runs it in a thread and fails when it is still alive after the limit; every test has a
    time limit of its own (tests/conftest.py)
И3  a ready task whose dependency can never be met (rejected with nothing in its place, or missing) has an event and a
    question about it (hittheline 26.11: three ready tasks waited 100 minutes in silence)
И4  after the cycle no task is `claimed`, no lease is left, and no task is `in_progress` held by a run nobody runs
    (an exception in a lane left the task there for ever)
И5  the spend of the cycle and of every day is not negative, and the day is not over its cap
    (27.11: «$-345.13» for a cycle that crossed midnight UTC)
И6  every task still ready after the execute phase has its reason on record (`cycle.guards.idle_reasons`)
И7  a task its review gave up on, and a task that waits for the owner, has an open question bound to it
И8  a free seat and a startable task give a run in the same cycle (with refill, unless refill was closed)
И9  every role with a ready task runs in the first round (checked by scenario 8, from the order of the runs)
И10 the executors leave the reserve of run slots to the reviews; a main run outside the slots does not wait
И11 a run that ended at its budget sends the task to be split after the FIRST such run (checked by scenario 4)
И12 after a drain request was taken no task is started
И13 a failed review of a task leaves a trace on every task that was started on it
И14 no task goes ready → in_progress → ready without a run more than once in a cycle
"""
from __future__ import annotations

import copy
import threading
from typing import Any

from team import config as C
from team import cycle as cycle_mod
from team import flow
from team.store import Store

# ------------------------------------------------------------------ parameters
STOP_WAIT_S = 20.0                # … and after the limit the cycle gets this long to stop when the test closes refill
CYCLE_LIMIT_S = 60.0              # И2: a scenario's cycle on the stand-in runner takes seconds
GAME_SLOTS = {"developer": 3, "game-designer": 2, "designer": 1, "analyst": 1, "marketer": 1}      # ai-battles, 27.11
GAME_PARALLEL = {"enabled": True, "max_runs": 6, "review_max_runs": 3, "review_reserve": 2, "overlap_roles": ["developer", "game-designer"]}
NOT_SWITCHES = {("parallel", "enabled")}                          # set by GAME_PARALLEL
SPEND_EPS = 1e-6
REVIEW_GAVE_UP = ("failed review", "inconclusive")                # how the note of a task begins that its review gave up on


def all_switches_on() -> dict[str, Any]:
    """`cycle` settings with EVERY on/off switch on, built from the defaults themselves: a switch added tomorrow to
    `flow.DEFAULTS` or to a block of `config.DEFAULTS["cycle"]` is in this set without anyone remembering to add it."""
    out: dict[str, Any] = {}
    for block, val in C.DEFAULTS["cycle"].items():
        if isinstance(val, dict):
            on = {k: True for k, v in val.items() if v is False and (block, k) not in NOT_SWITCHES}
            if on:
                out[block] = on
    out.setdefault("flow", {}).update({k: True for k, v in flow.DEFAULTS.items() if v is False})
    return out


def game_like() -> dict[str, Any]:
    """Every switch on, the seats and the run slots of ai-battles."""
    cyc = copy.deepcopy(all_switches_on())
    cyc["parallel"] = {**cyc.get("parallel", {}), **GAME_PARALLEL}
    cyc["execute_slots"] = dict(GAME_SLOTS)
    cyc["test_workers"] = {"count": 3}
    cyc.setdefault("guards", {})["partial_limit"] = 3            # a number, not a switch: the count pokerbr gets (28.11)
    return {"cycle": cyc}


SETTINGS = {"defaults": {}, "game": game_like()}                  # the two sets every scenario runs on


def run_cycle(cyc: Any, phases: list[str] | None = None, limit_s: float = CYCLE_LIMIT_S) -> dict[str, Any]:
    """И2: the cycle in a thread; still running after `limit_s` = the test fails (and says so, instead of hanging)."""
    box: dict[str, Any] = {}

    def body() -> None:
        try:
            box["res"] = cyc.run(phases)
        except BaseException as exc:  # noqa: BLE001 — handed to the caller's thread
            box["exc"] = exc

    th = threading.Thread(target=body, name="scenario-cycle", daemon=True)
    th.start()
    th.join(limit_s)
    if th.is_alive():
        # stop it before failing: the lane threads are not daemons, a cycle left spinning would keep the whole test run alive
        picks = dict(cyc.picks)
        cyc.close_refill("the scenario's time limit")
        (cyc.p.dir / "STOP").write_text("the scenario's time limit\n", encoding="utf-8")
        th.join(STOP_WAIT_S)
        raise AssertionError(f"И2: the cycle did not end in {limit_s:g}s (tasks taken by the lanes so far: {picks}; "
                             f"stopped by the test: {'yes' if not th.is_alive() else 'NO, still running'})")
    if "exc" in box:
        raise box["exc"]
    return box["res"]


EXEC_ROLES = ("developer", "designer", "game-designer", "analyst", "marketer")


def peak_at_once(runner: Any, roles: tuple[str, ...]) -> int:
    """The most runs of `roles` the stand-in runner had going at one moment (from its spans)."""
    marks = sorted([(a, 1) for r, a, b in runner.spans if r in roles] + [(b, -1) for r, a, b in runner.spans if r in roles], key=lambda x: (x[0], x[1]))
    now = peak = 0
    for _, d in marks:
        now += d
        peak = max(peak, now)
    return peak


def violations(cyc: Any, product: C.Product, result: dict[str, Any] | None = None, runner: Any = None) -> list[str]:
    """What the finished cycle broke; [] = nothing. Opens its own store: the cycle's may be closed already. `runner` (the
    stand-in runner of the scenario): the rules about run slots are checked from its spans, too."""
    bad: list[str] = []
    s = Store(product.dir)
    try:
        tasks = {t["id"]: t for t in s.tasks()}
        events = s.events(limit=100000)
        started = next((c["started"] for c in s.cycles(limit=500) if c["cycle_id"] == cyc.cycle_id), None)
        moves: dict[str, int] = {}
        for e in events:
            if e["kind"] == "task.transition":
                moves[e["subject"]] = moves.get(e["subject"], 0) + 1
        # И1
        allowed = cycle_mod.EXECUTE_WAVES_MAX + int(product.flow.retake_max if product.flow.retake else 0)
        for tid, n in sorted(cyc.picks.items()):
            if n > allowed + moves.get(tid, 0):
                bad.append(f"И1: {tid} was taken {n} times by the seat lanes with {moves.get(tid, 0)} change(s) of state (allowed {allowed} + changes)")
        # И3
        told = {e["subject"] for e in events if e["kind"] == cycle_mod.DEAD_DEPENDENCY_EVENT}
        questions = " ".join(str(q.get("question") or "") for q in s.questions())
        for tid, t in sorted(tasks.items()):
            if t.get("state") != "ready":
                continue
            dead = cycle_mod.dead_dependencies(s, t, product.dependencies_cfg["report_stuck"])
            if dead and (tid not in told or tid not in questions):
                bad.append(f"И3: {tid} is ready and waits for {dead}, which can never be met — and there is no event with a question about it")
        # И4
        leases = [r[0] for r in s.db.execute("SELECT task_id FROM claims").fetchall()]
        if leases:
            bad.append(f"И4: leases left after the cycle: {sorted(leases)}")
        for tid, t in sorted(tasks.items()):
            if t.get("state") == "claimed":
                bad.append(f"И4: {tid} is still `claimed`")
            if (t.get("state") == "in_progress" and t.get("assignee_run") and not t.get("last_review")
                    and (t.get("results") or [{}])[-1].get("status") != "partial"):
                bad.append(f"И4: {tid} is `in_progress` held by {t.get('assignee_run')} with no lease: every later cycle will say «a run holds it»")
        executed = result is not None and "execute" in (result.get("phases") or [])
        # И6: every task still ready has its reason on record (needs `cycle.guards.idle_reasons`)
        if executed and product.guards_cfg["idle_reasons"] and not cyc.dry_run:
            left = [e for e in events if e["kind"] == "execute.left" and e["subject"] == cyc.cycle_id]
            named = {x["task"]: x.get("why") for e in left for x in (e["detail"] or {}).get("tasks") or []}
            for tid, t in sorted(tasks.items()):
                if t.get("state") == "ready" and not str(named.get(tid) or "").strip():
                    bad.append(f"И6: {tid} is still ready after the execute phase and no reason is on record for it")
        # И8: a free seat and a startable task give a run in this cycle (with refill; unless refill was closed)
        if executed and product.flow.refill and product.parallel.enabled and not cyc._refill_closed and not cyc.dry_run:
            slots = cyc._exec_slots or {}
            open_q = {q["id"] for q in s.questions(["open"])}
            for tid, t in sorted(tasks.items()):
                if t.get("state") != "ready" or tid in cyc._not_done or cyc.seat_key(str(t.get("role") or "developer"), slots) is None:
                    continue
                if cyc.is_held(t):
                    continue                       # the operator took it (`cycle.escalation.operator_hold`): И6 holds the reason
                nb = cycle_mod.parse_ts(t.get("not_before"))
                if nb and cycle_mod._now() < nb:
                    continue
                if (t.get("blocked_by") in open_q) or cycle_mod.unmet_dependencies(s, t, cyc._dep_merged):
                    continue
                bad.append(f"И8: {tid} is startable, refill was open to the end, and no lane took it in this cycle")
        # И10: the execute lanes leave the reserve to the reviews; a run of the main thread does not wait when it is outside the slots
        if runner is not None and product.parallel.enabled and product.parallel.review_reserve > 0:
            limit = product.parallel.max_runs - product.parallel.review_reserve
            peak = peak_at_once(runner, EXEC_ROLES)
            if peak > limit:
                bad.append(f"И10: {peak} executor runs at once, the reserve for reviews leaves them {limit}")
        if product.parallel.enabled and product.parallel.main_outside and cyc.lanes.gate.max_wait.get("main", 0.0) > 0:
            bad.append(f"И10: a run of the main thread waited {cyc.lanes.gate.max_wait['main']:.1f}s for a slot though it is outside the slots")
        # this cycle's events, oldest first. The time of an event is kept in whole seconds: a scenario that runs two cycles
        # of one product must let a second pass between them, or the first one's events are counted here as well
        mine = [e for e in reversed(events) if started is None or e["ts"] >= started]
        # И7: a task the review gave up on, and a task that waits for the owner, has an open question bound to it
        bound = {tid: q for q in s.questions(["open"]) for tid in (q.get("blocks") or [])}
        for tid, t in sorted(tasks.items()):
            note = str(((t.get("history") or [{}])[-1]).get("note") or "")
            if t.get("state") == "waiting_owner" and (tid not in bound or bound[tid].get("to") != "owner"):
                bad.append(f"И7: {tid} waits for the owner and no open question to the owner is bound to it")
            if (t.get("state") == "blocked" and product.escalation_cfg["review_exit"] and note.startswith(REVIEW_GAVE_UP) and tid not in bound):
                bad.append(f"И7: {tid} is blocked after its review gave up and no open question is bound to it: nothing will move it")
        # И12: after a drain request was taken, no task is started
        seen_drain = False
        for e in mine:
            if e["kind"] == "cycle.drain" and e["subject"] == cyc.cycle_id:
                seen_drain = True
            elif seen_drain and e["kind"] == "task.transition" and (e["detail"] or {}).get("to") == "claimed":
                bad.append(f"И12: {e['subject']} was started after the cycle took a drain request")
        # И13: a failed review of a task leaves a trace on every task that was started on it (with `dependencies.at_merge`)
        if product.dependencies_cfg["at_merge"]:
            failed = {e["subject"] for e in mine if e["kind"] == "task.transition" and e["actor"] == "reviewer"
                      and str((e["detail"] or {}).get("note") or "").startswith("failed")}
            for tid, t in sorted(tasks.items()):
                for dep in [str(x) for x in (t.get("depends_on") or [])]:
                    if dep in failed and t.get("state") in cycle_mod.SUPPORT_STARTED and dep not in {x.get("task") for x in (t.get("support_failed") or [])}:
                        bad.append(f"И13: {tid} was started on {dep}, which failed its review in this cycle, and its card says nothing about it")
        # И14: no task goes ready → in_progress → ready without a run more than once in a cycle
        back: dict[str, int] = {}
        runs: dict[str, int] = {}
        for e in mine:
            d = e["detail"] or {}
            if e["kind"] == "task.transition" and d.get("from") == "in_progress" and d.get("to") == "ready":
                back[e["subject"]] = back.get(e["subject"], 0) + 1
            elif e["kind"] == "task.run_crashed":
                runs[e["subject"]] = runs.get(e["subject"], 0) + 1          # the code raised: that was an attempt, not a walk
            elif e["kind"] == "run":
                for tid in tasks:
                    if str(e["subject"]).endswith("-" + tid):
                        runs[tid] = runs.get(tid, 0) + 1
        for tid, n in sorted(back.items()):
            if n - runs.get(tid, 0) > 1:
                bad.append(f"И14: {tid} went in_progress → ready {n} times in the cycle with {runs.get(tid, 0)} run(s): it is being churned")
        # И5
        if result is not None and float(result.get("spent_usd") or 0.0) < -SPEND_EPS:
            bad.append(f"И5: the cycle reports a spend of {result.get('spent_usd')}")
        for c in s.cycles():
            if c["cycle_id"] == cyc.cycle_id and float(c.get("spent_usd") or 0.0) < -SPEND_EPS:
                bad.append(f"И5: cycle {c['cycle_id']} is recorded with a spend of {c['spent_usd']}")
        for day, usd in s.spent_by_day().items():
            if usd < -SPEND_EPS:
                bad.append(f"И5: day {day} has a spend of {usd}")
            if usd > product.daily_cap + SPEND_EPS:
                bad.append(f"И5: day {day} spent {usd:.2f}, over the cap of {product.daily_cap:.2f}")
    finally:
        s.close()
    return bad
