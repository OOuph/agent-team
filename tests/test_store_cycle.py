"""Store state machine + a full dispatcher cycle with a fake runner and a local git repository (no network, no models)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from team import config as C
from team import gitops
from team.cycle import Cycle
from team.runner import RunResult
from team.store import Store, TRANSITIONS


def test_task_state_machine(tmp_path: Path):
    s = Store(tmp_path / "p")
    t = s.create_task(title="t", type_="feature", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="test")
    assert t["state"] == "proposed" and t["id"] == "T-0001"
    with pytest.raises(ValueError):
        s.transition(t["id"], "released", "x")
    s.transition(t["id"], "ready", "po")
    assert s.claim(t["id"], "developer", "run-1")
    assert not s.claim(t["id"], "developer", "run-2")      # second worker is refused
    s.transition(t["id"], "claimed", "d"); s.transition(t["id"], "in_progress", "d")
    s.transition(t["id"], "released", "d", released_sha="abc"); s.transition(t["id"], "accepted", "r")
    s.transition(t["id"], "measured", "a"); s.transition(t["id"], "done", "a")
    h = s.task(t["id"])["history"]
    assert [x["to"] for x in h] == ["proposed", "ready", "claimed", "in_progress", "released", "accepted", "measured", "done"]
    assert all(set(v) <= set(TRANSITIONS) for v in TRANSITIONS.values())
    q = s.ask(from_="developer", to="owner", question="which colour?", task_id=None)
    assert q["state"] == "open"
    s.answer(q["id"], "blue", "owner")
    assert s.questions(["answered"])[0]["answer"] == "blue"
    s.book_spend("r1", "po", 0.5, True); s.book_spend("r2", "dev", 0.25, False)
    assert s.spent_today() == 0.75
    s.create_task(title="second", type_="defect", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="t")
    assert s.tasks()[1]["id"] == "T-0002"


def test_observation_dedup(tmp_path: Path):
    s = Store(tmp_path / "p")
    a = s.add_observation({"title": "x", "classification": "reproducible_defect", "fingerprint": "f1", "source": "t"})
    b = s.add_observation({"title": "x again", "classification": "reproducible_defect", "fingerprint": "f1", "source": "t"})
    assert a["id"] == b["id"] and b["repeat_count"] == 1
    assert len(s.observations()) == 1


class FakeRunner:
    """Scripted answers per role; the developer actually writes a file and commits so git paths are exercised."""

    def __init__(self):
        self.calls: list[str] = []

    def run(self, role, prompt, schema, *, max_usd, cwd, allowed_tools=None, env_extra=None, run_dir=None, run_id="", max_turns=None, **kw):
        self.calls.append(role)
        if role == "po":
            out = {"decisions": [], "answers": [], "owner_questions": [], "no_new_tasks": False, "digest_md": "ok",
                   "ready_tasks": [{"title": "Add about page", "type": "content", "problem": "no about", "scope": "public/about.html",
                                    "acceptance": ["/about.html returns 200 and says AI"], "negative_cases": [], "goal": "G1",
                                    "priority": "P2", "size": "S", "role": "developer"}]}
        elif role == "developer":
            (Path(cwd) / "public").mkdir(exist_ok=True)
            (Path(cwd) / "public" / "about.html").write_text("<p>made by AI agents</p>")
            gitops.git(Path(cwd), "add", "-A"); gitops.git(Path(cwd), "commit", "-q", "-m", "T-0001: about page")
            out = {"status": "done", "summary": "added", "changes": ["public/about.html"], "how_to_verify": ["/about.html"],
                   "self_check": [{"criterion": "200", "met": True, "evidence": "file exists"}], "proposals": ["add a sitemap"]}
        elif role == "reviewer":
            out = {"verdict": "pass", "steps": [{"url": "x", "status": 200, "note": "ok"}],
                   "criteria": [{"criterion": "200", "met": True, "evidence": "seen"}], "summary": "fine. really."}
        else:
            out = {"summary_md": "n/a", "observations": [], "task_effects": []}
        return RunResult(True, out, {"total_cost_usd": 0.1}, 0.1, 3, None, session_id="s", duration_s=1.0)


def _settings(tmp_path: Path) -> C.Settings:
    raw = C._merge(C.DEFAULTS, {"cycle": {"review_after_min": 0, "persona_every_n_cycles": 0, "digest_hour_utc": 99}})
    st = C.Settings(raw, tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")
    return st


def _product(st: C.Settings, slug: str = "demo") -> C.Product:
    d = st.products_dir / slug
    d.mkdir(parents=True)
    (d / "product.yaml").write_text("slug: demo\nname: Demo\nhost: ''\nrepo:\n  url: ''\n  branch: main\n")
    (d / "VISION.md").write_text("# Demo\n\nGoals: G1 people.\n")
    p = C.load_product(st, d)
    repo = gitops.init_local(p.repo_dir)
    (repo / "README.md").write_text("static site, public/ is served\n")
    gitops.git(repo, "add", "-A"); gitops.git(repo, "commit", "-q", "-m", "init")
    return p


def test_full_cycle_idle_to_released_to_accepted(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    r = FakeRunner()
    res = Cycle(st, p, r, "c1").run()
    assert "triage" in res["phases"] and "execute" in res["phases"]
    s = Store(p.dir)
    t = s.tasks()[0]
    assert t["state"] == "released" and t.get("released_sha"), t
    assert gitops.git(p.repo_dir, "log", "--oneline", "-3").count("\n") >= 2        # merge commit on main
    assert any(o["source"] == "developer" for o in s.observations())             # proposal became an observation
    # next cycle: review passes → accepted; PO not re-run because the team is busy and nothing new… except the proposal
    res2 = Cycle(st, p, r, "c2").run()
    t = s.task(t["id"])
    assert t["state"] == "accepted", t["history"]
    assert r.calls.count("reviewer") == 1
    assert s.spent_today() > 0


def test_stop_file_and_budget(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    p.stop_file.write_text("stop")
    assert Cycle(st, p, FakeRunner(), "c1").run().get("stopped")
    p.stop_file.unlink()
    s = Store(p.dir)
    s.book_spend("x", "po", 25.0, True)        # over the $20 cap
    r = FakeRunner()
    Cycle(st, p, r, "c2").run()
    assert r.calls == []                        # no model run above the cap
    assert any(e["kind"] == "budget.stop" for e in s.events())


def test_future_dated_title_not_claimed(tmp_path: Path, monkeypatch):
    from datetime import datetime, timezone
    from team import cycle as C
    st = _settings(tmp_path)
    p = _product(st)
    s = Store(p.dir)
    t = s.create_task(title="Daily post 03.11", type_="marketing", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po", state="ready")
    monkeypatch.setattr(C, "_now", lambda: datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc))
    assert C.future_dated_title("Daily post 03.11", C._now())
    assert C.future_dated_title("Daily post 03.11.2026", C._now())
    assert C.future_dated_title("Пост 04.11: что видно", C._now())
    assert not C.future_dated_title("Daily post 02.11", C._now())
    assert C.future_dated_title("Post 02.11.2027 planning", C._now())              # explicit later year counts
    assert not C.future_dated_title("Python 3.12 release post", C._now())       # version, not a date
    assert not C.future_dated_title("v1.12 and 2.1.4 shipped", C._now())
    assert not C.future_dated_title("Daily post 01.11.2026", C._now())
    assert C.future_dated_title("Post 02.01", datetime(2026, 12, 20, tzinfo=timezone.utc))      # year rollover
    assert not C.future_dated_title("Post 02.01.2026", datetime(2026, 12, 20, tzinfo=timezone.utc))
    cy = Cycle(st, p, FakeRunner(), "c1")
    called = []
    monkeypatch.setattr(cy, "execute_task", lambda t: called.append(t) or True)
    cy.phase_execute()
    assert not called and s.task(t["id"])["state"] == "ready"
    assert any(o.get("task") == t["id"] for o in s.observations())
    s.close()


def test_task_blocked_twice_same_question_not_restarted(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    s = Store(p.dir)
    t = s.create_task(title="post", type_="marketing", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po", state="ready")
    q1 = s.ask(from_="marketer", to="po", question="data not ready?", task_id=t["id"], blocks=[t["id"]])
    s.answer(q1["id"], "retry", "po")
    assert s.task(t["id"])["state"] == "ready"
    q2 = {**q1, "id": "Q-9999"}
    s.transition(t["id"], "blocked", "marketer", "again", force=True)
    from team.store import _write
    q2.update(state="open", answer=None)
    _write(s._path("Q", "Q-9999"), q2)
    s.answer("Q-9999", "retry", "po")
    assert s.task(t["id"])["state"] == "blocked"
    s.close()
