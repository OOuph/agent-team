"""Stuck goals (owner 11.11): failures counted over a chain of tasks → PO replans with 3 alternatives → probes →
a working probe goes back to the PO, none → the owner. Replays the Luz T-0117 → T-0130 shape without models."""
from __future__ import annotations

from pathlib import Path

from team import replan
from team.cycle import Cycle
from team.runner import RunResult
from team.store import Store

from test_store_cycle import FakeRunner, _product, _settings


def _partial(n):
    return {"status": "partial", "summary": f"OMIE still 404 at 0{n}:11Z", "changes": [], "how_to_verify": [], "self_check": []}


def _alts(framing=True):
    return [{"title": f"alt {i}", "approach": f"approach {i}", "differs_from_tried": "another file",
             "changes_source_or_framing": framing and i == 0, "probe_check": f"fetch file {i} and compare the PT column"} for i in range(3)]


class ScriptRunner(FakeRunner):
    """Developer: partial with a timestamped summary (the repeated-partial guard does not see it as the same); probe 2 works."""

    def __init__(self, replans=None):
        super().__init__()
        self.replans = replans or []

    def run(self, role, prompt, schema, *, cwd, **kw):
        self.calls.append(role)
        if role == "po":
            out = {"decisions": [], "answers": [], "owner_questions": [], "no_new_tasks": True, "digest_md": "ok", "ready_tasks": [],
                   "replans": self.replans}
        elif role == "developer" and "# PROBE" in prompt:
            works = "Probe 2/3" in prompt
            out = {"status": "done", "summary": "common file has the PT column" if works else "also 404", "changes": [], "how_to_verify": [],
                   "self_check": [{"criterion": "works", "met": works, "evidence": "curl output"}]}
        elif role == "developer":
            out = _partial(self.calls.count("developer"))
        else:
            out = {"summary_md": "n/a", "observations": [], "task_effects": []}
        return RunResult(True, out, {"total_cost_usd": 0.1}, 0.1, 3, None, session_id="s", duration_s=1.0)


def _mk(s, title, **kw):
    return s.create_task(title=title, type_="defect", problem=kw.pop("problem", "prices missing"), scope="s", acceptance=["prices on the site"],
                         goal="G1", created_by="po", state="ready", role="developer", **kw)


def test_chain_and_failures_over_recreated_tasks(tmp_path: Path):
    s = Store(tmp_path / "p")
    a = _mk(s, "OMIE 404")
    s.add_result(a["id"], {"status": "partial", "summary": "404"})
    b = _mk(s, f"OMIE still 404 (replaces {a['id']})")
    s.add_result(b["id"], {"status": "partial", "summary": "404 again"})
    c = _mk(s, "Find the real file")
    c["continues"] = [b["id"]]
    s.save_task(c)
    s.add_result(c["id"], {"status": "error", "summary": "run failed"})
    assert [x["id"] for x in replan.chain(s, s.task(c["id"]))] == [c["id"], b["id"], a["id"]]
    assert replan.chain_failures(s, s.task(c["id"])) == 3
    assert replan.needs_attention(s, s.task(c["id"]), 3) == "replan"
    assert replan.needs_attention(s, s.task(b["id"]), 3) is None          # b alone has 2
    assert len(replan.tried(s, s.task(c["id"]))) == 3


def test_validate_requires_three_and_a_source_change():
    assert replan.validate({"diagnosis": "d", "alternatives": _alts()}) == []
    assert any("source" in x for x in replan.validate({"diagnosis": "d", "alternatives": _alts(framing=False)}))
    assert any("2 alternatives" in x for x in replan.validate({"diagnosis": "d", "alternatives": _alts()[:2]}))


def test_full_replan_flow_working_probe(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    s = Store(p.dir)
    t = _mk(s, "OMIE 404 for tomorrow")
    r = ScriptRunner()
    for i in range(1, 6):
        Cycle(st, p, r, f"c{i}").phase_execute()
    # partials with different timestamps: the old repeated-partial guard does not fire; the chain counter stops at 3
    assert r.calls.count("developer") == 3
    t = s.task(t["id"])
    assert t["state"] == "blocked" and t["replan"]["state"] == "asked"
    # the PO sees the stuck goal with what was tried
    c = Cycle(st, p, r, "c6")
    block = replan.prompt_block(s, c.replan_cfg())
    assert t["id"] in block and "OMIE still 404 at 03:11Z" in block
    # an invalid replan (no source change) is refused and the task stays asked
    r.replans = [{"task_id": t["id"], "diagnosis": "the file moved", "alternatives": _alts(framing=False)}]
    c.phase_triage()
    assert s.task(t["id"])["replan"]["state"] == "asked"
    # a valid one creates 3 probes
    r.replans = [{"task_id": t["id"], "diagnosis": "the file moved", "alternatives": _alts()}]
    Cycle(st, p, r, "c7").phase_triage()
    rp = s.task(t["id"])["replan"]
    assert rp["state"] == "probing" and len(rp["probes"]) == 3
    assert all(s.task(x)["probe_of"] == t["id"] and s.task(x)["state"] == "ready" for x in rp["probes"])
    for i in range(8, 11):
        Cycle(st, p, r, f"c{i}").phase_execute()
    assert [s.task(x)["state"] for x in rp["probes"]] == ["done"] * 3
    assert [bool(s.task(x).get("probe_works")) for x in rp["probes"]] == [False, True, False]
    r.replans = []
    Cycle(st, p, r, "c11").phase_triage()
    t = s.task(t["id"])
    assert t["state"] == "rejected" and t["replan"]["state"] == "done" and t["replan"]["winner"] == rp["probes"][1]
    q = [x for x in s.questions(["open"]) if x["to"] == "po" and t["id"] in x["question"]]
    assert q and "continues" in q[0]["question"]
    # the implementation task continuing the replanned one starts with a clean counter
    impl = _mk(s, "Use the common OMIE file")
    impl["continues"] = [t["id"]]
    s.save_task(impl)
    assert replan.chain_failures(s, impl) == 0
    s.add_result(impl["id"], {"status": "partial", "summary": "x"}); s.add_result(impl["id"], {"status": "partial", "summary": "y"})
    s.add_result(impl["id"], {"status": "partial", "summary": "z"})
    assert replan.needs_attention(s, s.task(impl["id"]), 3) == "owner"      # one replan per chain


class NoProbeWorks(ScriptRunner):
    def run(self, role, prompt, schema, *, cwd, **kw):
        if role == "developer" and "# PROBE" in prompt:
            self.calls.append(role)
            return RunResult(True, {"status": "done", "summary": "404 too", "changes": [], "how_to_verify": [],
                                    "self_check": [{"criterion": "works", "met": False, "evidence": "404"}]},
                             {"total_cost_usd": 0.1}, 0.1, 3, None, session_id="s", duration_s=1.0)
        return super().run(role, prompt, schema, cwd=cwd, **kw)


def test_no_probe_works_goes_to_owner_and_answer_restarts_once(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    s = Store(p.dir)
    t = _mk(s, "OMIE 404")
    r = NoProbeWorks()
    for i in range(1, 5):                     # 3 failed runs; the 4th start is stopped by the chain counter
        Cycle(st, p, r, f"c{i}").phase_execute()
    r.replans = [{"task_id": t["id"], "diagnosis": "d", "alternatives": _alts()}]
    Cycle(st, p, r, "c4").phase_triage()
    r.replans = []
    for i in range(5, 8):
        Cycle(st, p, r, f"c{i}").phase_execute()
    st.raw["deputy"] = {**dict(st.raw.get("deputy") or {}), "enabled": False}
    Cycle(st, p, r, "c8").phase_triage()
    t = s.task(t["id"])
    assert t["state"] == "waiting_owner" and t["replan"]["state"] == "exhausted"
    q = s.task(t["id"])["replan"]["question"]
    assert "none of the 3 probes" in [x for x in s.questions(["open"]) if x["id"] == q][0]["question"]
    s.answer(q, "drop tomorrow's prices, show today's only", "owner")
    n = r.calls.count("developer")
    Cycle(st, p, r, "c9").phase_execute()
    assert r.calls.count("developer") == n + 1                                 # the owner's direction runs
    t = s.task(t["id"])
    assert t["replan"]["state"] == "owner" and "drop tomorrow's prices" in t["scope"]
