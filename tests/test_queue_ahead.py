"""The PO cuts tasks ahead (rework slice 9, owner 26.11).

Measured on ai-battles 26.11: three developer seats with refill take a task every few minutes, the PO proposed at most
three tasks per run and ran only when something new arrived — 1–2 ready tasks stood against three seats. After slice 1
a PO run costs about 40 % less ($3.71 → $2.07–2.47), so it may cut more per run. `cycle.queue` (off by default):
`ahead` — the PO is woken when the ready queue of a role with seats is below `target_ready` (not more often than
`wake_every_min`), is told the state of the queue, and may propose `max_per_run` tasks in one run."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from team import config as C
from team.cycle import Cycle
from team.runner import RunResult
from team.store import Store

from test_store_cycle import _product, _settings as _base


def _settings(tmp_path: Path, queue: dict | None) -> C.Settings:
    st = _base(tmp_path)
    if queue is not None:
        st.raw["cycle"] = {**st.raw["cycle"], "queue": queue}
    return st


class PO:
    def __init__(self, n_tasks: int = 0):
        self.n, self.prompts, self.schemas = n_tasks, [], []

    def run(self, role, prompt, schema, **kw):
        self.prompts.append(prompt)
        self.schemas.append(schema)
        tasks = [{"title": f"Task {i}", "type": "defect", "problem": "p", "scope": "s", "acceptance": ["a"], "negative_cases": [], "goal": "G1",
                  "priority": "P2", "size": "S", "role": "developer"} for i in range(self.n)]
        out = {"decisions": [], "answers": [], "owner_questions": [], "no_new_tasks": not tasks, "digest_md": "d", "ready_tasks": tasks}
        return RunResult(True, out, {"total_cost_usd": 0.1}, 0.1, 3, None, session_id="s", duration_s=1.0)


def _busy(s: Store, ready: int) -> None:
    """One task in progress (the team is not idle) and `ready` ready developer tasks."""
    t = s.create_task(title="running", type_="defect", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po", state="ready")
    s.transition(t["id"], "in_progress", "d", force=True, assignee_run="r")
    for i in range(ready):
        s.create_task(title=f"ready {i}", type_="defect", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po", state="ready")


def test_off_by_default(tmp_path: Path):
    st = _settings(tmp_path, None)
    p = _product(st)
    assert p.queue_cfg == {"ahead": False, "target_ready": 6, "max_per_run": 6, "wake_every_min": 30, "cut_on_design": False}
    s = Store(p.dir); _busy(s, 1); s.close()
    r = PO()
    c = Cycle(st, p, r, "c1"); c.phase_triage(); c.store.close()
    assert r.prompts == []                                              # nothing new and the team is busy: the PO is not run, as before


def test_a_low_queue_wakes_the_po_and_it_may_cut_more(tmp_path: Path):
    st = _settings(tmp_path, {"ahead": True, "target_ready": 5, "max_per_run": 6})
    p = _product(st)
    s = Store(p.dir); _busy(s, 2); s.close()
    r = PO(n_tasks=5)
    c = Cycle(st, p, r, "c1"); c.phase_triage(); c.store.close()
    assert len(r.prompts) == 1
    pr = r.prompts[0]
    assert "Propose at most 6 Ready tasks" in pr and "READY QUEUE" in pr and "developer: 2 ready" in pr and "at least 5" in pr
    assert r.schemas[0]["properties"]["ready_tasks"]["maxItems"] == 6    # the schema lets the six through
    s = Store(p.dir)
    assert len(s.tasks(["ready"])) == 7                                 # 2 + 5 cut ahead
    assert any(e["kind"] == "queue.low" for e in s.events())
    s.close()


def test_a_full_queue_does_not_wake_the_po(tmp_path: Path):
    st = _settings(tmp_path, {"ahead": True, "target_ready": 3})
    p = _product(st)
    s = Store(p.dir); _busy(s, 3); s.close()
    r = PO()
    c = Cycle(st, p, r, "c1"); c.phase_triage(); c.store.close()
    assert r.prompts == []


def test_the_wake_is_not_repeated_every_cycle(tmp_path: Path):
    st = _settings(tmp_path, {"ahead": True, "target_ready": 5, "wake_every_min": 30})
    p = _product(st)
    s = Store(p.dir); _busy(s, 1); s.close()
    r = PO(n_tasks=0)                                                   # the PO has nothing certain to add
    c = Cycle(st, p, r, "c1"); c.phase_triage(); c.store.close()
    c = Cycle(st, p, r, "c2"); c.phase_triage(); c.store.close()
    assert len(r.prompts) == 1                                          # the second cycle, minutes later: not asked again
    c = Cycle(st, p, r, "c3")
    assert c.queue_wake_due(datetime.now(timezone.utc) + timedelta(minutes=31))
    c.store.close()


def test_default_schema_and_limit_stay_when_off(tmp_path: Path):
    st = _settings(tmp_path, {"ahead": False, "max_per_run": 9})
    p = _product(st)
    r = PO(n_tasks=1)
    c = Cycle(st, p, r, "c1"); c.phase_triage(); c.store.close()        # the team is idle: the PO runs
    assert "Propose at most 3 Ready tasks" in r.prompts[0] and "READY QUEUE" not in r.prompts[0]
    assert r.schemas[0]["properties"]["ready_tasks"]["maxItems"] == 3


def test_bad_values_fall_back(tmp_path: Path):
    p = _product(_settings(tmp_path, {"ahead": "yes", "target_ready": 0, "max_per_run": 99, "wake_every_min": "soon"}))
    assert p.queue_cfg == {"ahead": False, "target_ready": 6, "max_per_run": 6, "wake_every_min": 30, "cut_on_design": False}
