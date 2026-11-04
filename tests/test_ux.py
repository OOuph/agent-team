"""Persona runs judge the reader's experience: scores are recorded, successes are not filed, low scores become value gaps."""
from __future__ import annotations

import json
from pathlib import Path

from team import config as C
from team import ux
from team.cycle import Cycle
from team.store import Store


def _settings(tmp_path: Path) -> C.Settings:
    st = C.Settings(json.loads(json.dumps(C.DEFAULTS)), tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")
    st.raw["cycle"]["persona_scenarios_per_run"] = 2
    return st


def _product(st: C.Settings) -> C.Product:
    d = st.products_dir / "p1"
    for sub in ("backlog", "questions", "observations", "reviews", "digests", "personas", "scenarios"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    (d / "VISION.md").write_text("# p1\n", encoding="utf-8")
    (d / "personas" / "reader.md").write_text("---\nid: reader\ntask: know tomorrow's cheap hours\n---\nA reader.\n", encoding="utf-8")
    for i in range(3):
        (d / "scenarios" / f"s{i}.md").write_text(f"---\nid: s{i}\npersona: reader\nentry: https://p1.example/\nmax_steps: 3\n---\nFind the window.\n", encoding="utf-8")
    return C.Product("p1", d, {**C.PRODUCT_DEFAULTS, "slug": "p1", "host": "p1.example"}, st)


class Runner:
    def __init__(self, exps):
        self.exps, self.calls = list(exps), []

    def run(self, role, prompt, schema, **kw):
        from team.runner import RunResult
        self.calls.append((role, prompt))
        exp = self.exps.pop(0)
        out = {"outcome": "success", "steps": [], "summary": "s", "experience": exp,
               "observations": [{"title": "worked", "classification": "success", "url": "u", "expected": "e", "actual": "a"}]}
        return RunResult(True, out, None, 0.1, 3, None)


def test_persona_run_records_scores_and_files_value_gap(tmp_path: Path) -> None:
    st = _settings(tmp_path)
    p = _product(st)
    good = {"first_impression": "I see tomorrow's cheap hours at once", "steps_to_understand": 1, "interest": 4, "usefulness": 5,
            "would_return": "yes", "would_share": "maybe", "why": "answers my question", "wish": "a morning reminder"}
    bad = {"first_impression": "A list of numbers I cannot read", "steps_to_understand": 0, "interest": 2, "usefulness": 2,
           "would_return": "no", "would_share": "no", "why": "too technical", "wish": "say in one line when to run the washer"}
    r = Runner([good, bad])
    cy = Cycle(st, p, r, "c1")
    cy.run_personas()
    assert [c[0] for c in r.calls] == ["persona", "persona"]                  # two scenarios per run, not all three
    assert "JUDGE AS THIS PERSON" in r.calls[0][1]
    s = Store(p.dir)
    obs = s.observations()
    assert all(o["classification"] != "success" for o in obs)                 # successes are not filed
    gaps = [o for o in obs if o["classification"] == "value_gap"]
    assert len(gaps) == 1 and "interest 2/5" in gaps[0]["title"] and gaps[0]["expected"].startswith("say in one line")
    s.close()
    sm = ux.summary(p)
    assert sm["runs"] == 2 and sm["interest"] == 3.0 and sm["usefulness"] == 3.5 and sm["return_yes"] == 1
    assert "интерес 3.0/5" in ux.digest_line(p)
    blk = ux.prompt_block(p)
    assert "Reader experience" in blk and "washer" in blk and "Low scores" in blk


def test_po_prompt_carries_reader_experience(tmp_path: Path) -> None:
    from team import prompts
    st = _settings(tmp_path)
    p = _product(st)
    s = Store(p.dir)
    txt = prompts.po_prompt(p, s, cycle_id="c1", observations=[], questions=[], metrics_md="", spent_today=0, cap=20, max_ready=3, idle=False)
    assert "READER EXPERIENCE" in txt and "no persona experience scores yet" in txt
    s.close()
