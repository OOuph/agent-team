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


def test_po_duplicate_tasks_skipped_and_close_tasks(tmp_path: Path):
    from team import cycle as C
    st = _settings(tmp_path)
    p = _product(st)
    s = Store(p.dir)
    base = {"type": "marketing", "problem": "p", "scope": "s", "acceptance": ["a"], "negative_cases": [], "goal": "G1",
            "priority": "P2", "size": "S", "role": "marketer"}
    cy = Cycle(st, p, FakeRunner(), "c1")
    cy.apply_po({"ready_tasks": [{**base, "title": "Daily post 03.11 14:05 UTC: prices", "not_before": "2026-11-03T14:05:00Z"}],
                 "digest_md": "d"}, [])
    assert len(s.tasks(["ready"])) == 1
    first = s.tasks(["ready"])[0]
    # same slot under another title, same title under another slot, exact title: none creates a task
    cy.apply_po({"ready_tasks": [
        {**base, "title": "Post 03.11 (starts 14:05 UTC): prices for 04.11", "not_before": "2026-11-03T14:05:00+00:00"},
        {**base, "title": "daily post 03.11 14:05 UTC — prices"},
        {**base, "title": "Daily post 03.11 14:05 UTC: prices", "not_before": "2026-11-03T14:05:00Z"},
        {**base, "title": "Morning post 04.11", "not_before": "2026-11-04T07:00:00Z"},        # a new slot: created
    ], "digest_md": "d"}, [])
    ready = s.tasks(["ready"])
    assert [t["title"] for t in ready] == [first["title"], "Morning post 04.11"]
    assert sum(1 for e in s.events() if e["kind"] == "task.duplicate_skipped") == 3
    assert "не создал дубли" in (p.dir / "digests" / "c1.md").read_text(encoding="utf-8")
    # a developer task with the same not_before is a different slot
    cy.apply_po({"ready_tasks": [{**base, "role": "developer", "type": "feature", "title": "Verify build", "not_before": "2026-11-03T14:05:00Z"}],
                 "digest_md": "d"}, [])
    assert len(s.tasks(["ready"])) == 3
    # close_tasks → rejected; an in_progress task is not closable
    cy.apply_po({"close_tasks": [{"task_id": first["id"], "reason": "duplicate"}], "digest_md": "d"}, [])
    assert s.task(first["id"])["state"] == "rejected"
    assert C.duplicate_task(s.tasks(C.OPEN_TASK_STATES), {**base, "title": first["title"]}) is None
    s.close()


def test_allowed_hosts_by_role(tmp_path: Path):
    st = _settings(tmp_path)
    st.raw["fetch"] = {"extra_hosts_by_role": {"reviewer": ["github.com", "raw.githubusercontent.com"]}}
    p = _product(st)
    cy = Cycle(st, p, FakeRunner(), "c1")
    assert "raw.githubusercontent.com" in cy._allowed_hosts("reviewer").split(",")
    assert "raw.githubusercontent.com" not in cy._allowed_hosts("developer").split(",")
    assert cy._allowed_hosts(None).split(",")[-1] == "dev.to"


def test_execute_slots_per_role(tmp_path: Path, monkeypatch):
    st = _settings(tmp_path)
    st.raw["cycle"]["execute_slots"] = {"developer": 1, "marketer": 1}
    p = _product(st)
    s = Store(p.dir)
    base = dict(type_="feature", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po", state="ready")
    d1 = s.create_task(title="dev P1", priority="P1", role="developer", **base)
    d2 = s.create_task(title="dev P2", priority="P2", role="developer", **base)
    m = s.create_task(title="post", priority="P3", role="marketer", **base)
    cy = Cycle(st, p, FakeRunner(), "c1")
    started = []
    monkeypatch.setattr(cy, "execute_task", lambda t: started.append(t["id"]) or True)
    cy.phase_execute()
    assert started == [d1["id"], m["id"]]          # one developer seat (P1 first), one marketer seat; dev P2 waits
    s.close()


def test_content_without_post_guard(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    s = Store(p.dir)
    cy = Cycle(st, p, FakeRunner(), "c1")
    m = s.create_task(title="post", type_="marketing", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po", state="ready", role="marketer")
    d = s.create_task(title="code", type_="feature", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po", state="ready")
    assert cy.content_without_post(m) and not cy.content_without_post(d)
    s.record_post("telegram", "1", "https://t.me/x/1", m["id"])
    assert not cy.content_without_post(m)
    s.close()


def test_deputy_due_and_apply(tmp_path: Path):
    from team import deputy
    st = _settings(tmp_path)
    st.raw["deputy"] = {"enabled": True, "after_h": 0}
    (st.root / "DEPUTY.md").write_text("# mandate\n", encoding="utf-8")
    p = _product(st)
    s = Store(p.dir)
    t = s.create_task(title="post", type_="marketing", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po", state="ready", role="marketer")
    q = s.ask(from_="po", to="owner", question="May we add channel X?", task_id=t["id"])
    assert s.task(t["id"])["state"] == "waiting_owner"
    q2 = s.ask(from_="po", to="po", question="internal", task_id=None)
    due = deputy.due_questions(st, s)
    assert [x["id"] for x in due] == [q["id"]]
    assert deputy.enabled(st)
    txt = deputy.deputy_prompt(p, s, due, deputy.mandate(st))
    assert "MANDATE" in txt and q["id"] in txt and "unavailable" in txt
    n = deputy.apply(s, {"answers": [{"question_id": q["id"], "kind": "unavailable", "answer": "No new channels this week; proceed with the listed ones."},
                                     {"question_id": "Q-9999", "kind": "info", "answer": "x"}]}, due)
    assert n == 1
    qq = s.questions(["answered"])[0]
    assert qq["answered_by"] == "deputy" and qq["answer"].startswith("[unavailable]")
    assert s.task(t["id"])["state"] == "ready"          # the waiting task is released
    assert deputy.due_questions(st, s) == []
    s.close()


def test_canary_runs_dry_on_a_state_copy(tmp_path: Path):
    """The canary must not touch the live store: it copies the product dir, runs --dry-run, and leaves no trace."""
    st = _settings(tmp_path)
    (st.root / "team.yaml").write_text("paths:\n  products_dir: products\n  data_dir: data\n  repos_dir: data/repos\n", encoding="utf-8")
    p = _product(st)
    s = Store(p.dir)
    t = s.create_task(title="live task", type_="feature", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po", state="ready")
    n_events = len(s.events())
    s.close()
    cy = Cycle(st, p, FakeRunner(), "c9")
    ok, note = cy.canary(Path(__file__).resolve().parent.parent, p.slug)
    assert ok, note
    assert "dry-run" in note and "state copy" in note
    s = Store(p.dir)
    assert s.task(t["id"])["state"] == "ready"                 # the live task was not claimed
    assert len(s.events()) == n_events                          # nothing written to the live store
    assert not (st.data_dir / "canary").exists() or not any((st.data_dir / "canary").iterdir())
    s.close()


def test_deputy_skips_in_dry_run(tmp_path: Path):
    from team import deputy
    st = _settings(tmp_path)
    st.raw["deputy"] = {"enabled": True, "after_h": 0}
    (st.root / "DEPUTY.md").write_text("# mandate\n", encoding="utf-8")
    p = _product(st)
    s = Store(p.dir)
    s.ask(from_="po", to="owner", question="anything?")
    s.close()
    cy = Cycle(st, p, FakeRunner(), "c1", dry_run=True)
    assert deputy.run_for_product(cy) == 0 and cy.runner.calls == []
