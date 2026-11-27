"""The PO cuts tasks as soon as a design document is merged (27.11, ai-battles: every hand-over «document → developer
tasks» cost a whole cycle, and a cycle is about an hour — the game designer's proposals became observations, and the PO
read them only in the triage of the NEXT cycle). `cycle.queue.cut_on_design` (off by default): when a task of type
`design` or of the game designer or designer is merged inside the execute phase, the PO runs at once; with refill the
seats take what it cuts in the same cycle."""
from __future__ import annotations

from pathlib import Path

from team import config as C
from team.cycle import Cycle
from team.store import Store

from test_flow import _dev_files
from test_parallel_lanes import LaneRunner, _events, _product, _settings, _state, _task

PO_CUTS = {"decisions": [], "answers": [], "owner_questions": [], "no_new_tasks": False, "digest_md": "d",
           "ready_tasks": [{"title": "Tower: server rules from the new GDD part", "type": "milestone", "problem": "p", "scope": "s", "acceptance": ["a"],
                            "negative_cases": [], "goal": "G1", "priority": "P1", "size": "S", "role": "developer"}]}


def _run(tmp_path: Path, on: bool):
    st = _settings(tmp_path, {"cycle": {"flow": {"refill": True, "refill_while_running": True}, "queue": {"cut_on_design": on}}})
    p = _product(st, extra="profile: game\n")
    gd = _task(p, "game-designer", "design/gdd/26-tower.md: part 1", type_="design") if False else None
    s = Store(p.dir)
    t = s.create_task(title="design/gdd/26-tower.md: part 1", type_="design", problem="p", scope="design/gdd/", acceptance=["a"], goal="G1",
                      created_by="po", role="game-designer", state="ready")
    s.close()
    r = LaneRunner()
    _dev_files(r)
    r.answers["po"] = dict(PO_CUTS)
    cyc = Cycle(st, p, r, "c1"); cyc.run(["execute"]); cyc.store.close()
    return p, r, t["id"]


def test_default_is_off_the_po_does_not_run_inside_the_execute_phase(tmp_path: Path):
    p, r, gd = _run(tmp_path, False)
    assert C.DEFAULTS["cycle"]["queue"]["cut_on_design"] is False
    assert r.calls == ["game-designer"] and not _events(p, "triage.cut")


def test_on_the_po_runs_after_the_merged_document_and_its_task_is_done_in_the_same_cycle(tmp_path: Path):
    p, r, gd = _run(tmp_path, True)
    assert r.calls[:2] == ["game-designer", "po"] and "developer" in r.calls
    assert "# JUST MERGED" in r.prompts["po"][0] and gd in r.prompts["po"][0]
    s = Store(p.dir)
    new = next(t for t in s.tasks() if t["title"].startswith("Tower: server rules"))
    assert new["state"] in ("released", "accepted")                     # cut and carried through without waiting for the next cycle
    ev = _events(p, "triage.cut")
    assert len(ev) == 1 and ev[0]["subject"] == gd and ev[0]["detail"]["created"] == 1
    s.close()


def test_the_po_is_not_run_for_every_document_without_end(tmp_path: Path):
    st = _settings(tmp_path, {"cycle": {"flow": {"refill": True}, "queue": {"cut_on_design": True}}})
    p = _product(st, extra="profile: game\n")
    s = Store(p.dir)
    for i in range(5):
        s.create_task(title=f"design/gdd/part-{i}.md", type_="design", problem="p", scope="design/gdd/", acceptance=["a"], goal="G1",
                      created_by="po", role="game-designer", state="ready")
    s.close()
    r = LaneRunner()
    r.work["game-designer"] = lambda cwd, n: {"summary": "s", "files": [{"path": f"design/gdd/part-{n}.md", "content": f"# part {n}\n"}], "balance_changes": [],
                                              "proposed_tasks": [], "open_questions": []}
    cyc = Cycle(st, p, r, "c1"); cyc.run(["execute"]); cyc.store.close()
    assert r.calls.count("po") <= Cycle.CUT_MAX_PER_CYCLE
