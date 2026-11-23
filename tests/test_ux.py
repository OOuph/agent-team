"""Persona runs judge the reader's experience: scores are recorded, successes are not filed, low scores become value gaps."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
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


# ------------------------------------------------------------------ schedule: by the clock, not by the cycle count

GOOD = {"first_impression": "clear", "steps_to_understand": 1, "interest": 4, "usefulness": 4, "would_return": "yes",
        "would_share": "maybe", "why": "answers my question", "wish": "nothing"}


def _age_mark(p: C.Product, hours: float, **over) -> dict:
    """Move the product's persona mark `hours` into the past (as if the last run started then)."""
    m = {**ux.persona_mark(p.dir), **over}
    m["started"] = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    if m.get("last_ok"):
        m["last_ok"] = m["started"]
    ux.write_persona_mark(p.dir, m)
    return m


def test_personas_run_on_schedule_in_a_store_with_60_cycles(tmp_path: Path) -> None:
    """04.11–22.11 the personas ran on no product: the schedule was `len(store.cycles()) % every == 1` and
    `Store.cycles()` returns at most 50 rows, so after a product's 50th cycle the condition was false for good."""
    st = _settings(tmp_path)
    p = _product(st)
    s = Store(p.dir)
    for i in range(60):
        s.cycle_start(f"old-{i:02d}")
        s.cycle_finish(f"old-{i:02d}", ["collect"], 0.0)
    assert len(s.cycles()) == 50 and len(s.cycles()) % 12 != 1                # the old condition can never hold here again
    s.close()
    assert st.cycle["persona_every_h"] == 12 and ux.persona_every_h(st.cycle) == 12

    r = Runner([GOOD] * 6)
    Cycle(st, p, r, "c61").run(["collect"])                                   # never ran → due at once
    assert [c[0] for c in r.calls] == ["persona", "persona"]
    m = ux.persona_mark(p.dir)
    assert m["cycle"] == "c61" and m["scenarios"] == ["s0", "s1"] and m["ok"] == 2 and m["finished"] and m["last_ok"] == m["started"]

    Cycle(st, p, r, "c62").run(["collect"])                                   # an hour later: not due
    assert len(r.calls) == 2
    _age_mark(p, 11.5)
    Cycle(st, p, r, "c63").run(["collect"])                                   # 11.5 h: still not due
    assert len(r.calls) == 2
    _age_mark(p, 12.1)
    Cycle(st, p, r, "c64").run(["collect"])                                   # 12 h passed: due, the rotation goes on
    assert len(r.calls) == 4
    m = ux.persona_mark(p.dir)
    assert m["cycle"] == "c64" and m["scenarios"] == ["s2", "s0"] and m["next"] == 1
    s = Store(p.dir)
    assert len([e for e in s.events(limit=200) if e["kind"] == "persona.run"]) == 2
    assert not [e for e in s.events(limit=200) if e["kind"] == "phase.error"]
    s.close()

    st.raw["cycle"]["persona_every_h"] = 0                                    # 0 switches the personas off
    _age_mark(p, 100)
    Cycle(st, p, r, "c65").run(["collect"])
    assert len(r.calls) == 4


def test_persona_run_without_a_result_is_retried_sooner_and_dry_run_leaves_no_mark(tmp_path: Path) -> None:
    from team.runner import RunResult
    st = _settings(tmp_path)
    p = _product(st)

    class Failing:
        calls = 0

        def run(self, role, prompt, schema, **kw):
            Failing.calls += 1
            return RunResult(False, None, None, 0.0, 0, "timeout")

    Cycle(st, p, Failing(), "d1", dry_run=True).run(["collect"])              # dry run: nothing runs, no mark, due again
    assert Failing.calls == 0 and ux.persona_mark(p.dir) == {} and ux.personas_due(p.dir, 12)
    Cycle(st, p, Failing(), "c1").run(["collect"])
    m = ux.persona_mark(p.dir)
    assert Failing.calls == 2 and m["ok"] == 0 and m["finished"] and not m["last_ok"]
    now = datetime.now(timezone.utc)
    assert not ux.personas_due(p.dir, 12, now + timedelta(hours=2), retry_h=3)     # not every cycle
    assert ux.personas_due(p.dir, 12, now + timedelta(hours=3, minutes=1), retry_h=3)   # but well before the 12 h
    assert not ux.personas_due(p.dir, 2, now + timedelta(hours=1), retry_h=3)       # the retry never waits longer than the period
    assert ux.personas_due(p.dir, 2, now + timedelta(hours=2, minutes=1), retry_h=3)
    (p.dir / ux.PERSONA_MARK_FILE).write_text("{broken", encoding="utf-8")    # an unreadable mark reads as «never ran»
    assert ux.persona_mark(p.dir) == {} and ux.personas_due(p.dir, 12)


def test_persona_every_h_reads_the_deprecated_key_as_hours(caplog) -> None:
    assert ux.persona_every_h({}) == 12
    assert ux.persona_every_h({"persona_every_h": 6, "persona_every_n_cycles": 24}) == 6      # the new key wins
    assert ux.persona_every_h({"persona_every_h": "soon"}) == 12
    ux._legacy_warned = False
    with caplog.at_level("WARNING", logger="team.ux"):
        assert ux.persona_every_h({"persona_every_n_cycles": 8}) == 8
        assert ux.persona_every_h({"persona_every_n_cycles": 0}) == 0
    assert caplog.text.count("deprecated") == 1                               # one warning per process
    assert "persona_every_n_cycles" not in C.DEFAULTS["cycle"]
