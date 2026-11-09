"""Voice of the user (weekly research → pains with quotes) and the hypothesis rule (a feature needs real evidence)."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from test_store_cycle import FakeRunner, _product, _settings

from team import config as C
from team import cycle as cyc
from team import hypothesis, voice
from team.cycle import Cycle
from team.runner import RunResult

REPORT = {
    "sources_searched": [{"where": "reddit.com/r/portugal", "query": "preço luz hora", "useful": True}],
    "pains": [
        {"id": "P1", "who": "EV owner in Lisbon", "pain": "does not know when charging is cheapest",
         "quotes": [{"url": "https://www.reddit.com/r/portugal/comments/abc", "quote": "a que horas carrego mais barato?", "date": "2026-10-30"}],
         "frequency": "several", "workarounds": "ERSE simulator", "fit": "home page shows the cheapest 3 hours"},
        {"id": "P2", "who": "someone", "pain": "no source given", "quotes": [{"url": "not a link", "quote": "x", "date": "2026-10-01"}],
         "frequency": "one", "workarounds": "-", "fit": "none"},
    ],
    "hypotheses": [{"id": "H1", "pain_ids": ["P1"], "change": "an EV charging line on the first screen",
                    "expected_effect": "more returns", "metric": "returns"}],
    "summary_md": "Люди спрашивают, когда заряжать машину.",
}


def _hyp(evidence, read_on=None):
    return {"who": "EV owner", "pain": "when to charge", "evidence": evidence, "change": "EV line", "expected_effect": "returns up",
            "metric": "returns", "min_sample": 20, "read_on": read_on or (date.today() + timedelta(days=7)).isoformat(),
            "score": {"reach": 3, "pain": 4, "confidence": 3, "ease": 4}}


def _feature(**kw):
    return {"title": "EV line on home", "type": "feature", "problem": "p", "scope": "public/index.html", "acceptance": ["/ shows it"],
            "negative_cases": [], "goal": "G2", "priority": "P2", "size": "S", "role": "developer", **kw}


def test_report_files_cards_and_po_block(tmp_path: Path):
    d = tmp_path / "prod"
    voice.save(d, REPORT, "2026-11-07", "2026-11-07T18:00:00+00:00")
    assert (d / "voice" / "2026-11-07.md").read_text(encoding="utf-8").count("voice:2026-11-07:P1") == 1
    cards = voice.to_observations(REPORT, "luz", "2026-11-07")
    assert len(cards) == 1                                       # P2 has no URL → no card
    c = cards[0]
    assert c["classification"] == "voice" and c["evidence_id"] == "voice:2026-11-07:P1" and "reddit.com" in c["url"]
    assert "H1" in c["actual"]
    block = voice.prompt_block(d)
    assert "voice:2026-11-07:P1" in block and "a que horas" in block and "H1" in block
    assert voice.known_evidence_ids(d) == {"voice:2026-11-07:P1", "voice:2026-11-07:P2"}
    assert "no voice-of-user report" in voice.prompt_block(tmp_path / "empty")


def test_due_weekly(tmp_path: Path):
    d = tmp_path / "prod"
    assert voice.due(d, 168)
    now = datetime(2026, 11, 7, 18, tzinfo=timezone.utc)
    voice.save(d, REPORT, "2026-11-07", now.isoformat())
    assert not voice.due(d, 168, now + timedelta(days=6))
    assert voice.due(d, 168, now + timedelta(days=7))
    assert not voice.due(d, 0, now + timedelta(days=30))        # 0 = off
    voice.mark_attempt(d, now + timedelta(days=7))
    assert not voice.due(d, 168, now + timedelta(days=7, hours=23))   # a started or failed run is not retried for a day
    assert voice.due(d, 168, now + timedelta(days=8))


def test_one_voice_run_per_cycle(tmp_path: Path):
    assert voice.claim_cycle(tmp_path, "c1") and not voice.claim_cycle(tmp_path, "c1") and voice.claim_cycle(tmp_path, "c2")


def test_topics_default_and_override():
    assert voice.topics_for({"slug": "luz-amanha"})[0].startswith("preço")
    assert voice.topics_for({"slug": "x", "voice_topics": ["a", " ", "b"]}) == ["a", "b"]
    assert voice.topics_for({"slug": "x", "name": "Demo"}) == ["Demo"]


def test_rule_needs_real_evidence():
    t = {"type": "feature", "created_by": "po", "state": "ready"}
    kw = dict(voice_ids={"voice:2026-11-07:P1"}, observations={"OBS-0001": {"classification": "ux_friction"},
                                                                "OBS-0002": {"classification": "feedback"}},
              site_bases=["https://luz.example"], today=date.today())
    assert hypothesis.check(t, **kw) == ["no hypothesis"]
    for ev in (["OBS-0001"], ["metric:returns"], ["https://luz.example/d/"], ["voice:2026-11-01:P9"]):
        probs = hypothesis.check({**t, "hypothesis": _hyp(ev)}, **kw)
        assert any("real people" in p for p in probs), ev       # persona, metric alone, our own site, unknown pain id
    for ev in (["voice:2026-11-07:P1"], ["OBS-0002"], ["https://github.com/o/r/issues/1", "metric:returns"]):
        assert hypothesis.check({**t, "hypothesis": _hyp(ev)}, **kw) == [], ev
    past = hypothesis.check({**t, "hypothesis": _hyp(["OBS-0002"], read_on="2026-01-01")}, **kw)
    assert any("not in the future" in p for p in past)
    assert hypothesis.check({**t, "type": "defect"}, **kw) == []          # outside the rule
    assert hypothesis.check({**t, "created_by": "retro"}, **kw) == []     # the retro's evidence is the friction register
    assert hypothesis.score(_hyp([])) == round(3 * 4 * 3 * 4 / 625 * 100, 1)


def test_po_feature_without_evidence_goes_to_proposed_and_promote(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    voice.save(p.dir, REPORT, "2026-11-07", "2026-11-07T18:00:00+00:00")
    cy = Cycle(st, p, FakeRunner(), "c1")
    cy.apply_po({"ready_tasks": [_feature(), _feature(title="Backed", hypothesis=_hyp(["voice:2026-11-07:P1"]))], "digest_md": "d"}, [])
    by = {t["title"]: t for t in cy.store.tasks()}
    assert by["EV line on home"]["state"] == "proposed" and by["Backed"]["state"] == "ready"
    assert "Правило гипотез" in (p.dir / "digests" / "c1.md").read_text(encoding="utf-8")
    tid = by["EV line on home"]["id"]
    cy.apply_po({"promote_tasks": [{"task_id": tid, "hypothesis": _hyp(["persona said so"])}], "digest_md": "d"}, [])
    assert cy.store.task(tid)["state"] == "proposed"
    cy.apply_po({"promote_tasks": [{"task_id": tid, "hypothesis": _hyp(["voice:2026-11-07:P1"])}], "digest_md": "d"}, [])
    assert cy.store.task(tid)["state"] == "ready" and cy.store.task(tid)["hypothesis"]["evidence"] == ["voice:2026-11-07:P1"]


def test_execute_moves_unbacked_ready_feature_back(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    r = FakeRunner()
    cy = Cycle(st, p, r, "c1")
    t = cy.store.create_task(title="old idea", type_="feature", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po", state="ready")
    cy.phase_execute()
    assert cy.store.task(t["id"])["state"] == "proposed" and "developer" not in r.calls


def test_readout_card_once(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    cy = Cycle(st, p, FakeRunner(), "c1")
    t = cy.store.create_task(title="done one", type_="feature", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po", state="accepted")
    t["hypothesis"] = _hyp(["OBS-0002"], read_on="2026-11-01")
    cy.store.save_task(t)
    cy.hand_over_readouts()
    cy.hand_over_readouts()
    cards = [o for o in cy.store.observations() if o.get("classification") == hypothesis.READOUT_CLASS]
    assert len(cards) == 1 and cards[0]["task"] == t["id"]


def test_phase_voice_runs_weekly_and_files_cards(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)

    class R(FakeRunner):
        def run(self, role, prompt, schema, **kw):
            self.calls.append(role)
            assert role == "voice" and "TOPICS TO SEARCH" in prompt and kw.get("allowed_tools") == ["WebSearch", "WebFetch"]
            return RunResult(True, json.loads(json.dumps(REPORT)), {}, 0.5, 5, None)

    r = R()
    d2 = st.products_dir / "other"
    d2.mkdir()
    (d2 / "product.yaml").write_text("slug: other\nname: Other\nhost: ''\nrepo:\n  url: ''\n  branch: main\n")
    (d2 / "VISION.md").write_text("# Other\n")
    p2 = C.load_product(st, d2)
    Cycle(st, p, r, "c1").phase_voice()
    Cycle(st, p2, r, "c1").phase_voice()                        # one voice run per cycle: the other product waits
    Cycle(st, p, r, "c2").phase_voice()                         # a week has not passed: no second run
    assert r.calls == ["voice"] and len(voice.reports(p.dir)) == 1 and not voice.reports(p2.dir)
    assert [o["classification"] for o in Cycle(st, p, r, "c3").store.observations()] == ["voice"]


def test_schemas_and_phase_order():
    assert "voice" in cyc.PHASES and cyc.PHASES.index("voice") < cyc.PHASES.index("digest")
    s = json.loads((cyc.SCHEMAS / "po-decision.schema.json").read_text(encoding="utf-8"))
    assert "hypothesis" in s["properties"]["ready_tasks"]["items"]["properties"] and "promote_tasks" in s["properties"]
    json.loads((cyc.SCHEMAS / "voice-report.schema.json").read_text(encoding="utf-8"))


def test_quality_type_outside_rule_and_po_woken_for_idle_developer(tmp_path: Path):
    from team import store as S
    assert "quality" in S.TASK_TYPES
    s = json.loads((cyc.SCHEMAS / "po-decision.schema.json").read_text(encoding="utf-8"))
    assert "quality" in s["properties"]["ready_tasks"]["items"]["properties"]["type"]["enum"]
    assert hypothesis.check({"type": "quality", "created_by": "po", "state": "ready"}, voice_ids=set(), observations={},
                            site_bases=[], today=date.today()) == []
    st = _settings(tmp_path)
    p = _product(st)
    calls = []

    class R(FakeRunner):
        def run(self, role, prompt, schema, **kw):
            calls.append((role, prompt))
            return RunResult(True, {"ready_tasks": [], "digest_md": "d"}, {}, 0.1, 1, None)

    cy = Cycle(st, p, R(), "c1")
    cy.store.create_task(title="post", type_="content", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po",
                         state="ready", role="marketer")
    cy.phase_triage()                                            # only a marketer task: the developer is idle → PO woken
    assert [c[0] for c in calls] == ["po"] and "developer has no ready work" in calls[0][1]
    Cycle(st, p, R(), "c2").phase_triage()                       # within 12 h: not woken again
    assert len(calls) == 1
    old = datetime.now(timezone.utc) - timedelta(hours=13)
    (p.dir / Cycle.DEV_IDLE_FILE).write_text(old.isoformat(), encoding="utf-8")
    Cycle(st, p, R(), "c3").phase_triage()
    assert len(calls) == 2
    assert voice.EVERY_H_DEFAULT == 84
