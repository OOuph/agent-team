"""A task must not wait for ever, in silence, for a dependency that was rejected (hittheline 26.11, 20:07–21:48 UTC).

One PO answer closed T-0001…T-0008 «replaced by …» and created T-0012…T-0014 with `depends_on` on the tasks it had
just closed: the PO has no «change the task» action and cannot name a task of the same answer, so it named the old
numbers. `unmet_dependencies` counted a rejected dependency as unmet for ever and nothing said so."""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from team import config as C
from team.cycle import Cycle, dead_dependencies, unmet_dependencies
from team.store import Store

from test_parallel_lanes import LaneRunner, _commit, _product, _settings, _state, _task

ROOT = Path(C.__file__).parent


def _po(st, p, **res) -> Cycle:
    c = Cycle(st, p, None, "c1")
    c.apply_po({"decisions": [], "answers": [], "owner_questions": [], "no_new_tasks": False, "digest_md": "d", "ready_tasks": [], **res}, [])
    return c


def _rt(title: str, **kw) -> dict:
    return {"title": title, "type": "ops", "problem": "p", "scope": "s", "acceptance": ["a"], "negative_cases": [], "goal": "G1",
            "priority": "P1", "size": "S", "role": "developer", **kw}


def test_a_rejected_or_missing_dependency_is_said_once_and_the_task_does_not_start(tmp_path: Path, caplog):
    st = _settings(tmp_path)
    p = _product(st)
    old, tid = _task(p, "developer", "old generator"), _task(p, "developer", "new generator")
    s = Store(p.dir)
    s.transition(old, "rejected", "po", "closed by PO: replaced", force=True)
    t = s.task(tid); t["depends_on"] = [old, "T-9999"]; s.save_task(t)
    assert dead_dependencies(s, s.task(tid)) == [old, "T-9999"] and unmet_dependencies(s, s.task(tid)) == [old, "T-9999"]
    s.close()
    r = LaneRunner()
    with caplog.at_level(logging.WARNING):
        for i in range(3):
            c = Cycle(st, p, r, f"c{i}"); c.run(["execute"]); c.store.close()
    assert r.calls == [] and _state(p, tid)["state"] == "ready"
    assert any(tid in m and old in m and "rejected" in m for m in caplog.messages)                 # WARNING with both numbers
    s = Store(p.dir)
    ev = [e for e in s.events() if e["kind"] == "task.dependency_dead"]
    assert len(ev) == 1 and ev[0]["subject"] == tid and ev[0]["detail"]["dead"] == [old, "T-9999"]
    qs = [q for q in s.questions(["open"]) if q["to"] == "po" and tid in q["question"]]
    assert len(qs) == 1 and old in qs[0]["question"] and "edit_tasks" in qs[0]["question"]        # asked exactly once in three cycles
    s.close()


def test_the_po_can_point_the_dependency_elsewhere_and_the_task_starts(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    old, tid = _task(p, "developer", "old generator"), _task(p, "developer", "new generator")
    s = Store(p.dir)
    s.transition(old, "rejected", "po", "closed", force=True)
    t = s.task(tid); t["depends_on"] = [old]; s.save_task(t); s.close()
    c = _po(st, p, edit_tasks=[{"task_id": tid, "depends_on": []}]); c.store.close()
    assert _state(p, tid)["depends_on"] == [] and any("depends_on" in h["note"] for h in _state(p, tid)["history"])
    r = LaneRunner()
    r.work["developer"] = lambda cwd, n: _commit(cwd, "src/gen.ts", "x\n")
    c = Cycle(st, p, r, "c2"); c.run(["execute"]); c.store.close()
    assert r.calls == ["developer"]


def test_one_answer_that_closes_a_task_and_creates_its_successor_leaves_no_dead_end(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    collector, pages = _task(p, "developer", "Collector on the test key"), _task(p, "developer", "Pages (feature)")
    waiting = _task(p, "developer", "Sitemap")
    s = Store(p.dir)
    t = s.task(waiting); t["depends_on"] = [pages]; s.save_task(t); s.close()
    c = _po(st, p,
            close_tasks=[{"task_id": collector, "reason": "replaced: paid key", "replaced_by": "Collector on the paid key"},
                         {"task_id": pages, "reason": "replaced as ops"}],
            ready_tasks=[_rt("Collector on the paid key"),
                         _rt("Page generator", depends_on=[collector]),                     # the old number of what this answer closes
                         _rt("Championship set", depends_on=["Page generator"]),            # a task of the same answer, by its title
                         _rt("Markup", depends_on=[pages])])                                # closed with no replacement named
    c.store.close()
    s = Store(p.dir)
    by = {t["title"]: t for t in s.tasks()}
    new_collector = by["Collector on the paid key"]["id"]
    assert by["Collector on the test key"]["state"] == "rejected" and by["Collector on the test key"]["replaced_by"] == new_collector
    assert by["Page generator"]["depends_on"] == [new_collector]                           # moved to the replacement
    assert by["Championship set"]["depends_on"] == [by["Page generator"]["id"]]            # the title became the id
    assert by["Markup"]["depends_on"] == []                                                # nothing to wait for: dropped, and said
    assert by["Sitemap"]["depends_on"] == []                                               # an OLDER open task that waited for the closed one
    digest = (p.dir / "digests" / "c1.md").read_text(encoding="utf-8")
    assert "Markup" in digest and pages in digest
    assert not any(dead_dependencies(s, t) for t in s.tasks(["ready"]))
    assert len([e for e in s.events() if e["kind"] == "task.dependency_moved"]) == 1 and len([e for e in s.events() if e["kind"] == "task.dependency_dropped"]) == 2
    s.close()


def test_a_dependency_on_a_replaced_task_follows_the_replacement(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    old, new, tid = _task(p, "developer", "old"), _task(p, "developer", "new"), _task(p, "developer", "user")
    s = Store(p.dir)
    s.transition(old, "rejected", "po", "replaced", force=True, replaced_by=new)
    t = s.task(tid); t["depends_on"] = [old]; s.save_task(t)
    assert dead_dependencies(s, s.task(tid)) == [] and unmet_dependencies(s, s.task(tid)) == [old]       # waits for `new`, which is not done
    s.add_result(new, {"run_id": "r", "status": "done", "summary": "The generator writes every page of the set into public/, each with its number in the h1, and the 14 tests pass. " * 2})
    s.transition(new, "accepted", "reviewer", force=True)
    assert unmet_dependencies(s, s.task(tid)) == []
    s.close()


def test_schema_and_po_card_know_the_new_fields():
    import json
    props = json.loads((ROOT / "schemas" / "po-decision.schema.json").read_text())["properties"]
    assert "replaced_by" in props["close_tasks"]["items"]["properties"] and "edit_tasks" in props
    po = " ".join((ROOT / "roles" / "po.md").read_text(encoding="utf-8").split())
    assert "`replaced_by`" in po and "`edit_tasks`" in po and "title of a task of the same answer" in po


def test_site_onboarding_does_not_make_the_base_build_a_feature():
    card = " ".join((ROOT / "roles" / "onboarding.md").read_text(encoding="utf-8").split())
    site = card.split("## Profile site")[1].split("## Rules")[0]
    assert "`feature` (generator" not in site and "`ops` (repository, data collector, generator, pages, sitemap, markup)" in site
    plan = yaml.safe_load((ROOT / "templates" / "onboarding.sample-site.yaml").read_text(encoding="utf-8"))
    backlog = plan["plan"]["backlog"]
    assert backlog and all(t["type"] != "feature" for t in backlog), [t["title"] for t in backlog if t["type"] == "feature"]
