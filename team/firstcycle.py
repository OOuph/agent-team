"""The check of the first cycle after a change of the dispatcher's code (audit of 27.11.2026, step 2).

27.11: a defect of the scheduler — lanes with no work took a waiting task about once a minute — lived through three
cycles of ai-battles, 10:45–14:47 UTC, and nobody saw it until a person read the log. The tests run before a merge; this
runs after it, on the first real cycle of every product on the new commit, over what the cycle left behind: its events,
its counters, its run gate. It only SAYS what it found: a log line, an event, and one line in
`data/framework/canary.log` for the operator. No STOP and no rollback (the owner has not asked for either).

team.yaml `first_cycle_check` (product.yaml `cycle.first_cycle_check` on top, key by key):
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import flow

log = logging.getLogger("team.firstcycle")

# ------------------------------------------------------------------ parameters
DEFAULTS: dict[str, Any] = {
    "enabled": False,             # off = nothing is read and nothing is written
    "cycles": 1,                  # the window: this many cycles of a product after the commit changed are checked (1..20)
    "refusals_per_task": 3,       # more «not started» answers for one task in one cycle is red (27.11: 69, 51, 47; normal: 1)
    "picks_per_task": 4,          # a lane took one task more often than this (the passes of a cycle allow 3, a retake 1 more)
    "idle_picks": 10,             # tasks taken by the seat lanes minus tasks carried through, in the whole cycle
    "fail_share": 0.5,            # share of failed runs above which the cycle is red …
    "min_runs": 4,                # … when it had at least this many runs
    "main_wait_s": 300,           # the PO (a main lane) waited longer than this for a run slot
    "review_wait_s": 1800,        # a review waited longer than this for a run slot
    "execute_wait_s": 1800,       # a seat lane waited longer than this for a run slot
    "no_result_share": 0.5,       # share of executor runs that carried no task through, above which the cycle is red (from min_runs)
    "partial_share": 0.3,         # share of executor runs that ended «partial — continues next cycle» (28.11: 7 % before a deny
                                  # rule that kept the roles from their tests, 36–40 % with it — and nothing said so for four hours)
    "churn_per_task": 1,          # a task went in_progress → ready without a run more often than this
    "signalled_runs": 0,          # more runs of the cycle ended by a signal than this (28.11: one designer run killed three others
                                  # with `pkill -f`; they ended with exit 143 and the check of that cycle was GREEN)
    "dead_finished": 0,           # more «the dependency can never be met» reports than this where the dependency is in fact
                                  # merged or accepted (28.11: accepted T-0766 was called dead for a word in its summary; the tower waited)
    "cycle_min": 120.0,           # the cycle ran longer than this many minutes (28.11: 84 minutes for one late seat; normal 60–100)
    "idle_tail_share": 0.5,       # share of the cycle that lay AFTER the last executor run ended, above which it is red (cycles
                                  # of at least IDLE_TAIL_MIN_CYCLE_S; 27–28.11: seats stood idle for 44 % of a cycle)
    "load_per_core": 2.5,         # the 5-minute load of the server per core at the end of the cycle (27.11: 18–33 on 8 cores = 2.3–4.1)
}
_RANGES = {"cycles": (1, 20, int), "refusals_per_task": (1, 1000, int), "picks_per_task": (1, 1000, int), "idle_picks": (0, 100000, int),
           "fail_share": (0.0, 1.0, float), "min_runs": (1, 1000, int), "main_wait_s": (1, 86400, float), "review_wait_s": (1, 86400, float),
           "execute_wait_s": (1, 86400, float), "no_result_share": (0.0, 1.0, float), "partial_share": (0.0, 1.0, float), "churn_per_task": (0, 1000, int), "load_per_core": (0.1, 100.0, float),
           "signalled_runs": (0, 1000, int), "dead_finished": (0, 1000, int), "cycle_min": (1.0, 1440.0, float), "idle_tail_share": (0.0, 1.0, float)}
EXEC_ROLES = ("developer", "designer", "game-designer", "analyst", "marketer", "tester")
LOG_FILE = "framework/canary.log"             # under the data directory: one line per checked cycle, newest last
STATE_DIR = "framework/first-cycle"           # <slug>.json: the commit this product's cycles were last checked on, and how many
EVENT = "canary.first_cycle"
FINDINGS_IN_LINE = 6
IDLE_TAIL_MIN_CYCLE_S = 1200.0                # a cycle shorter than this has no «idle tail» worth a word
SIGNAL_RE = re.compile(r"exit (143|137|130|-15|-9|-2)\b")      # a run ended by SIGTERM / SIGKILL / SIGINT (as team/windowreport.py counts it)
FINISHED_STATES = ("released", "accepted", "measured", "done")  # a dependency in one of these is merged work, not a dead end


def settings(team_raw: dict[str, Any] | None, product_raw: dict[str, Any] | None, slug: str = "-") -> dict[str, Any]:
    out = dict(DEFAULTS)
    own = (product_raw or {}).get("cycle")
    for where, src in (("team.yaml", (team_raw or {}).get("first_cycle_check")),
                       (f"{slug} product.yaml", own.get("first_cycle_check") if isinstance(own, dict) else None)):
        if not isinstance(src, dict):
            continue
        for k, v in src.items():
            if k not in out:
                log.warning("%s: first_cycle_check has an unknown key %r — ignored", where, k)
            elif k == "enabled":
                if isinstance(v, bool):
                    out[k] = v
                else:
                    log.warning("%s: first_cycle_check.enabled=%r is not true/false — %s in use", where, v, out[k])
            else:
                lo, hi, typ = _RANGES[k]
                try:
                    num = typ(v)
                    if isinstance(v, bool) or not lo <= num <= hi:
                        raise ValueError
                    out[k] = num
                except (TypeError, ValueError):
                    log.warning("%s: first_cycle_check.%s=%r is not a number in %s..%s — %s in use", where, k, v, lo, hi, out[k])
    return out


def settings_mark(product_dir: Path) -> str:
    """A short hash of the product's product.yaml as it is on disk ("" when it cannot be read)."""
    try:
        return hashlib.sha1((product_dir / "product.yaml").read_bytes()).hexdigest()[:12]
    except OSError:
        return ""


def due(data_dir: Path, slug: str, commit: str, cycles: int, mark: str = "") -> bool:
    """Is this cycle inside the window after a change of the commit — or, with `mark`, of the product's settings (28.11:
    a deny rule added to product.yaml cost the developers their tests for four hours; the code had not changed, so
    nothing was checked)? Counts it when it is (idempotent per call: one call per finished cycle). The very first call
    for a product only remembers the commit — there is nothing to compare with; so does the first call that brings a mark."""
    if not commit:
        return False
    f = data_dir / STATE_DIR / f"{slug}.json"
    try:
        state = json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    except (OSError, ValueError):
        state = None
    if state is None:
        new = {"commit": commit, "checked": cycles}                 # nothing to compare with: remembered, not checked
        verdict = False
    elif state.get("commit") != commit:
        new, verdict = {"commit": commit, "checked": 1, "before": state.get("commit"), "settings": mark or state.get("settings")}, True
    elif mark and not state.get("settings"):
        new, verdict = {**state, "settings": mark}, int(state.get("checked") or 0) < cycles     # remembered; the window goes on as it was
        if verdict:
            new["checked"] = int(state.get("checked") or 0) + 1
    elif mark and state.get("settings") != mark:
        new, verdict = {**state, "checked": 1, "settings": mark, "settings_before": state.get("settings")}, True
    elif int(state.get("checked") or 0) < cycles:
        new, verdict = {**state, "checked": int(state.get("checked") or 0) + 1}, True
    else:
        return False
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(new), encoding="utf-8")
    tmp.replace(f)
    return verdict


def findings(cyc: Any, result: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    """What is wrong with the finished cycle `cyc`; [] = green. Reads the cycle's counters and the events of its store."""
    out: list[str] = []
    store = cyc.store
    started = next((c["started"] for c in store.cycles(limit=200) if c["cycle_id"] == cyc.cycle_id), None)
    events = [e for e in store.events(since=started, limit=100000)] if started else []
    # 1. idle attempts: one task refused over and over, or taken over and over
    refused: dict[str, int] = {}
    for x in getattr(cyc, "_not_started", []):
        refused[x["task"]] = refused.get(x["task"], 0) + 1
    for tid, n in sorted(refused.items(), key=lambda kv: -kv[1]):
        if n > cfg["refusals_per_task"]:
            out.append(f"{tid} was refused {n} times in one cycle (limit {cfg['refusals_per_task']})")
    for tid, n in sorted(getattr(cyc, "picks", {}).items(), key=lambda kv: -kv[1]):
        if n > cfg["picks_per_task"]:
            out.append(f"{tid} was taken by a lane {n} times (limit {cfg['picks_per_task']})")
    # 2. run slots without a run
    idle = sum(getattr(cyc, "picks", {}).values()) - int(getattr(cyc, "n_executed", 0))
    if idle > cfg["idle_picks"]:
        out.append(f"the seat lanes took tasks {idle} times more than they carried through (limit {cfg['idle_picks']})")
    # 3. ready tasks nobody took and nobody explained
    if not getattr(cyc, "n_executed", 0) and "execute" in (result.get("phases") or []):
        try:
            waiting = [t for t in cyc.work_waiting() if t not in refused]
        except Exception as exc:  # noqa: BLE001 — the check reports, it does not fail the cycle
            waiting = []
            log.debug("first cycle check: work_waiting failed: %s", exc)
        if waiting:
            out.append(f"no task was executed although {len(waiting)} could start and none has a reason on record: {', '.join(waiting[:5])}")
    # 4. spend
    if float(result.get("spent_usd") or 0.0) < 0:
        out.append(f"the cycle reports a spend of {result.get('spent_usd')}")
    # 5. errors of the phases and of the runs
    errs = [e for e in events if e["kind"] == "phase.error"]
    if errs:
        out.append(f"{len(errs)} phase error(s): " + "; ".join(f"{e['subject']}: {str(e['detail'])[:120]}" for e in errs[:3]))
    runs = [e for e in events if e["kind"] == "run"]
    failed = [e for e in runs if isinstance(e["detail"], dict) and e["detail"].get("ok") is False]
    if len(runs) >= cfg["min_runs"] and len(failed) / len(runs) > cfg["fail_share"]:
        out.append(f"{len(failed)} of {len(runs)} runs failed (limit {cfg['fail_share']:.0%})")
    # 6. what the cycle left behind
    leases = [r[0] for r in store.db.execute("SELECT task_id FROM claims").fetchall()]
    claimed = [t["id"] for t in store.tasks(["claimed"])]
    if leases or claimed:
        out.append(f"left after the cycle: leases {sorted(leases)[:5]}, claimed {claimed[:5]}")
    # 7. waiting for a run slot
    waits = getattr(cyc.lanes.gate, "max_wait", {})
    if waits.get("main", 0.0) > cfg["main_wait_s"]:
        out.append(f"the PO waited {waits['main']:.0f}s for a run slot (limit {cfg['main_wait_s']:g}s)")
    if waits.get("review", 0.0) > cfg["review_wait_s"]:
        out.append(f"a review waited {waits['review']:.0f}s for a run slot (limit {cfg['review_wait_s']:g}s)")
    if waits.get("execute", 0.0) > cfg["execute_wait_s"]:
        out.append(f"a seat lane waited {waits['execute']:.0f}s for a run slot (limit {cfg['execute_wait_s']:g}s)")
    # 8. executor runs that carried nothing through
    exec_runs = [e for e in runs if e["actor"] in EXEC_ROLES]
    done = int(getattr(cyc, "n_executed", 0))
    if len(exec_runs) >= cfg["min_runs"] and (len(exec_runs) - done) / len(exec_runs) > cfg["no_result_share"]:
        out.append(f"{len(exec_runs) - done} of {len(exec_runs)} executor runs carried no task through (limit {cfg['no_result_share']:.0%})")
    partial = [e for e in events if e["kind"] == "task.transition" and isinstance(e["detail"], dict)
               and str(e["detail"].get("note") or "").startswith("partial")]
    if len(exec_runs) >= cfg["min_runs"] and len(partial) / len(exec_runs) > cfg["partial_share"]:
        out.append(f"{len(partial)} of {len(exec_runs)} executor runs ended «partial» (limit {cfg['partial_share']:.0%}): "
                   + ", ".join(sorted({e["subject"] for e in partial})[:6]))
    # 9. tasks walked into work and back without a run
    back: dict[str, int] = {}
    ran: dict[str, int] = {}
    for e in events:
        d = e["detail"] if isinstance(e["detail"], dict) else {}
        if e["kind"] == "task.transition" and d.get("from") == "in_progress" and d.get("to") == "ready":
            back[e["subject"]] = back.get(e["subject"], 0) + 1
        elif e["kind"] == "task.run_crashed":
            ran[e["subject"]] = ran.get(e["subject"], 0) + 1
        elif e["kind"] == "run":
            tid = str(e["subject"]).rsplit("-", 2)[-2:]
            ran["-".join(tid)] = ran.get("-".join(tid), 0) + 1
    churned = sorted(((n - ran.get(t, 0), t) for t, n in back.items() if n - ran.get(t, 0) > cfg["churn_per_task"]), reverse=True)
    if churned:
        out.append(f"{len(churned)} task(s) went in_progress → ready without a run more than {cfg['churn_per_task']} time(s): "
                   + ", ".join(f"{t} ×{n}" for n, t in churned[:5]))
    # 10. runs ended by a signal, finished work called a dead end, a long cycle, a long tail with no executor run
    signalled = [e for e in runs if isinstance(e["detail"], dict) and SIGNAL_RE.search(str(e["detail"].get("error") or ""))]
    if len(signalled) > cfg["signalled_runs"]:
        out.append(f"{len(signalled)} run(s) were ended by a signal (limit {cfg['signalled_runs']}): " + ", ".join(str(e["subject"]) for e in signalled[:5]))
    wrong_dead = []
    for e in events:
        if e["kind"] == "task.dependency_dead" and isinstance(e["detail"], dict):
            for dep in e["detail"].get("dead") or []:
                d = store.task(str(dep))
                if d and d.get("state") in FINISHED_STATES:
                    wrong_dead.append(f"{e['subject']} ← {dep} ({d.get('state')})")
    if len(wrong_dead) > cfg["dead_finished"]:
        out.append(f"{len(wrong_dead)} task(s) were told their dependency can never be met although it is finished work "
                   f"(limit {cfg['dead_finished']}): " + ", ".join(wrong_dead[:5]))
    t0, now = _when(started), datetime.now(timezone.utc)
    if t0 is not None:
        length = (now - t0).total_seconds()
        if length / 60 > cfg["cycle_min"]:
            out.append(f"the cycle ran {length / 60:.0f} minutes (limit {cfg['cycle_min']:g})")
        last = max((x for x in (_when(e.get("ts")) for e in exec_runs) if x is not None), default=None)
        tail = (now - last).total_seconds() if last is not None else 0.0
        if length >= IDLE_TAIL_MIN_CYCLE_S and tail / length > cfg["idle_tail_share"]:
            out.append(f"the last executor run ended {tail / 60:.0f} minutes before the cycle did — {tail / length:.0%} of the cycle with "
                       f"no seat at work (limit {cfg['idle_tail_share']:.0%})")
    # 11. the load of the server
    load = server_load()
    if load is not None and load[0] > cfg["load_per_core"]:
        out.append(f"the server's 5-minute load is {load[1]:.1f} on {load[2]} cores = {load[0]:.1f} per core (limit {cfg['load_per_core']:g})")
    return out


def _when(ts: Any) -> datetime | None:
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def server_load() -> tuple[float, float, int] | None:
    """(5-minute load per core, the load, the cores) of this machine; None where the system does not say."""
    try:
        import os
        cores = os.cpu_count() or 1
        five = os.getloadavg()[1]
        return five / cores, five, cores
    except (OSError, AttributeError):
        return None


def check(cyc: Any, result: dict[str, Any]) -> dict[str, Any] | None:
    """Called once at the end of `Cycle.run`. None = not due (off, a dry run, not the first cycle on this commit)."""
    cfg = settings(cyc.st.raw, cyc.p.raw, cyc.p.slug)
    if not cfg["enabled"] or getattr(cyc, "dry_run", False):
        return None
    commit = flow.code_version(cyc.st.root)
    if not due(cyc.st.data_dir, cyc.p.slug, commit, int(cfg["cycles"]), settings_mark(cyc.p.dir)):
        return None
    found = findings(cyc, result, cfg)
    verdict = "RED" if found else "GREEN"
    line = (f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {cyc.p.slug} {cyc.cycle_id} {commit[:7]} {verdict} "
            f"runs={result.get('runs')} executed={getattr(cyc, 'n_executed', 0)} spent={result.get('spent_usd')}"
            + (" | " + " | ".join(found[:FINDINGS_IN_LINE]) + (f" | +{len(found) - FINDINGS_IN_LINE} more" if len(found) > FINDINGS_IN_LINE else "") if found else ""))
    f = cyc.st.data_dir / LOG_FILE
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(line.replace("\n", " ") + "\n")
    cyc.store.event(EVENT, cyc.cycle_id, "dispatcher", {"commit": commit[:12], "verdict": verdict, "findings": [x[:300] for x in found[:20]]})
    log.log(logging.WARNING if found else logging.INFO, "%s: first cycle on %s: %s%s", cyc.p.slug, commit[:7], verdict,
            (" — " + "; ".join(found[:FINDINGS_IN_LINE])) if found else "")
    return {"verdict": verdict, "findings": found, "commit": commit}
