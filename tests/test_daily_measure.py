"""The day is measured, not every task; tasks close without waiting for the analyst (rework slice 11, owner 26.11:
«как будто аналитик каждый день по 4 раза излишен. И мы заливаем фичи такими пачками, что влияние отдельной фичи
посчитать невозможно»).

ai-battles 26.11: four analyst runs in a row ended at the role's cap, and because a task left `accepted` only after a
successful analyst run, 149 tasks stood there. `cycle.measure.by_day` (off by default):
  * a task accepted more than `measure_after_h` ago goes to `done` by itself (effect `not_measured`) whether the analyst
    ran, failed or was not due; a task with no user metric of its own (quality, defect, design, documents, tests,
    proofs) does not wait at all;
  * the analyst reads the day — goals, the funnel, and the effect of a release or a theme (`theme_effects`); a single
    task is named only when it has its own before/after; `task_effects` is optional;
  * a failed analyst run is not repeated in the same window."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from team import config as C
from team import prompts
from team.cycle import Cycle, needs_measure
from team.runner import RunResult
from team.store import Store

from test_po_prompt_compact import _product, _settings

SCHEMAS = Path(C.__file__).parent / "schemas"
OLD = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds")
NEW = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")


def _setup(tmp_path: Path, on: bool, **cycle) -> tuple[C.Settings, C.Product, Store]:
    st = _settings(tmp_path)
    p = _product(st, cycle={"measure": {"by_day": on}, **cycle})
    (p.dir / "metrics").mkdir(exist_ok=True)
    (p.dir / "metrics" / "2026-11-26.json").write_text("{}")
    (p.dir / "digests").mkdir(exist_ok=True)
    return st, p, Store(p.dir)


def _acc(s: Store, title: str, when: str, type_: str = "feature", role: str = "developer", **fields) -> str:
    t = s.create_task(title=title, type_=type_, problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po", state="ready", role=role)
    s.transition(t["id"], "accepted", "reviewer", force=True, accepted_at=when, **fields)
    return t["id"]


class Analyst:
    def __init__(self, ok: bool = True, out: dict | None = None):
        self.ok, self.out, self.n, self.prompts = ok, out, 0, []

    def run(self, role, prompt, schema, **kw):
        self.n += 1
        self.prompts.append(prompt)
        if not self.ok:
            return RunResult(False, None, {"subtype": "error_max_budget_usd"}, 1.1, 2, "error_max_budget_usd", duration_s=60.0)
        return RunResult(True, self.out or {"summary_md": "day", "observations": []}, {}, 0.3, 2, None, session_id="s", duration_s=1.0)


def test_schema_task_effects_optional_theme_effects_added():
    d = json.loads((SCHEMAS / "analyst-report.schema.json").read_text())
    assert "task_effects" not in d["required"] and "summary_md" in d["required"]
    te = d["properties"]["theme_effects"]["items"]["properties"]
    assert {"theme", "task_ids", "effect", "evidence"} <= set(te)
    assert C.DEFAULTS["cycle"]["measure"] == {"by_day": False}


def test_which_tasks_have_a_metric_of_their_own():
    assert needs_measure({"type": "feature", "role": "developer", "title": "Challenge link button"})
    for t in ({"type": "quality"}, {"type": "defect"}, {"type": "design"}, {"type": "feature", "role": "game-designer", "title": "design/gdd/21-cups.md"},
              {"type": "feature", "title": "E2E: cups spec follows the new rows"}, {"type": "feature", "title": "Arena proof for T-0263"},
              {"type": "milestone", "title": "Unit tests for the corridor step"}, {"type": "feature", "title": "DESIGN.md §31: the car table"}):
        assert not needs_measure(t), t


def test_off_tasks_wait_for_a_successful_analyst_as_before(tmp_path: Path):
    st, p, s = _setup(tmp_path, False)
    a = _acc(s, "Feature", OLD)
    r = Analyst(ok=False)
    for i in range(2):
        c = Cycle(st, p, r, f"c{i}"); c.phase_measure(); c.store.close()
    assert s.task(a)["state"] == "accepted" and r.n == 2              # the failed run is repeated every cycle, nothing closes
    s.close()


def test_on_tasks_close_whatever_happened_to_the_analyst(tmp_path: Path):
    st, p, s = _setup(tmp_path, True)
    old_feature, new_feature = _acc(s, "Old feature", OLD), _acc(s, "New feature", NEW)
    new_defect, new_doc = _acc(s, "Fix overlap", NEW, type_="defect"), _acc(s, "design/gdd/x.md", NEW, role="game-designer")
    r = Analyst(ok=False)
    c = Cycle(st, p, r, "c1"); c.phase_measure(); c.store.close()
    t = s.task(old_feature)
    assert t["state"] == "done" and t["effect"] == "not_measured"       # older than measure_after_h: closed by itself
    assert s.task(new_feature)["state"] == "accepted"                   # a feature waits its day
    assert s.task(new_defect)["state"] == "done" and s.task(new_defect)["effect"] == "not_applicable"
    assert s.task(new_doc)["state"] == "done"
    s.close()


def test_a_failed_analyst_is_not_run_again_in_the_same_window(tmp_path: Path):
    st, p, s = _setup(tmp_path, True)
    r = Analyst(ok=False)
    for i in range(3):
        c = Cycle(st, p, r, f"c{i}"); c.phase_measure(); c.store.close()
    assert r.n == 1 and any(e["kind"] == "analyst.failed" for e in s.events())
    c = Cycle(st, p, r, "c9")
    assert c.analyst_due(datetime.now(timezone.utc) + timedelta(hours=24))    # the next window tries once more
    c.store.close(); s.close()


def test_theme_effects_reach_the_tasks_and_task_effects_still_work(tmp_path: Path):
    st, p, s = _setup(tmp_path, True)
    a, b, c_ = _acc(s, "Link slice 1", OLD), _acc(s, "Link slice 2", OLD), _acc(s, "Other", OLD)
    out = {"summary_md": "day", "observations": [],
           "theme_effects": [{"theme": "Challenge link", "task_ids": [a, b], "effect": "positive", "evidence": "share 0 → 14 a day"}],
           "task_effects": [{"task_id": c_, "effect": "none", "evidence": "returns 21 → 21"}]}
    cy = Cycle(st, p, Analyst(out=out), "c1"); cy.phase_measure(); cy.store.close()
    assert s.task(a)["effect"] == "positive" and "Challenge link" in s.task(a)["history"][-2]["note"] and s.task(b)["state"] == "done"
    assert s.task(c_)["effect"] == "none"
    s.close()


def test_prompt_groups_the_day_by_release_and_goal_when_on(tmp_path: Path):
    st, p, s = _setup(tmp_path, True)
    a = _acc(s, "Link slice 1", NEW, deployed_sha="aaaaaaaaaaaa1111", deployed_at=NEW)
    b = _acc(s, "Link slice 2", NEW, deployed_sha="aaaaaaaaaaaa1111", deployed_at=NEW)
    r = Analyst()
    cy = Cycle(st, p, r, "c1"); cy.phase_measure(); cy.store.close()
    pr = r.prompts[0]
    assert "# RELEASES" in pr and "aaaaaaaaaaaa" in pr and a in pr and b in pr and "theme_effects" in pr
    st2, p2, s2 = _setup(tmp_path / "off", False)
    _acc(s2, "x", NEW)
    r2 = Analyst()
    cy = Cycle(st2, p2, r2, "c1"); cy.phase_measure(); cy.store.close()
    assert "# RELEASES" not in r2.prompts[0]
    s.close(); s2.close()


def test_measure_hours_follow_the_product(tmp_path: Path):
    st, p, s = _setup(tmp_path, True, measure_every_h=24, measure_after_h=48)
    a = _acc(s, "Feature", OLD)                                         # 30 h old, the product says 48
    cy = Cycle(st, p, Analyst(), "c1")
    assert cy.cycle_hours("measure_every_h", 6) == 24 and cy.cycle_hours("measure_after_h", 24) == 48
    cy.phase_measure(); cy.store.close()
    assert s.task(a)["state"] == "accepted"
    s.close()


def test_card_says_day_and_themes():
    text = " ".join((Path(C.__file__).parent / "roles" / "analyst.md").read_text(encoding="utf-8").split())
    assert "theme_effects" in text and "влияние отдельной фичи посчитать невозможно" in text
