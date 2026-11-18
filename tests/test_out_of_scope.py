"""T-0167: files in the diff outside the task's declared scope are flagged in the PR facts block (warn-only)."""
from pathlib import Path

from team import friction, gitops, prfacts
from team.cycle import Cycle

from test_store_cycle import FakeRunner, _product, _settings

SCOPE = "The module team/prfacts.py and team/cycle.py, a new test, SCHEME.md, CHANGELOG.md."


def test_extra_file_listed_in_facts():
    md, _ = prfacts.build("", ["team/prfacts.py", "team/other.py", "tests/test_x.py"], None, {}, scope=SCOPE)
    assert "out-of-scope files: `team/other.py`" in md


def test_inside_scope_has_no_line():
    files = ["team/prfacts.py", "team/cycle.py", "tests/test_new.py", "SCHEME.md", "CHANGELOG.md"]
    md, _ = prfacts.build("", files, None, {}, scope=SCOPE)
    assert "out-of-scope" not in md
    assert prfacts.out_of_scope("no paths here", files) == []


def test_friction_record_written_and_not_blocking(tmp_path: Path, monkeypatch):
    st = _settings(tmp_path)
    p = _product(st)
    cy = Cycle(st, p, FakeRunner(), "c1")
    t = cy.store.create_task(title="t", type_="defect", problem="p", scope=SCOPE, acceptance=["a"], goal="G1", created_by="test")
    cy.store.transition(t["id"], "ready", "po")
    bodies = []
    monkeypatch.setattr(gitops, "push_branch", lambda repo, tid: "abc")
    monkeypatch.setattr(gitops, "changed_files", lambda repo, base: ["team/prfacts.py", "team/extra.py"])
    monkeypatch.setattr(gitops, "tests_modified_or_deleted", lambda repo, base: [])
    monkeypatch.setattr(gitops, "diff_stat", lambda repo, base: "")
    monkeypatch.setattr(gitops, "open_pull_request", lambda url, tid, base, title, body: bodies.append(body) or "https://github.com/o/r/pull/1")
    cy.release_as_pull_request(cy.store.task(t["id"]), tmp_path, {"summary": "s"})
    assert bodies and "out-of-scope files: `team/extra.py`" in bodies[0]
    recs = friction.collect_product(p, cy.store)
    assert any(r["kind"] == "reported" and "team/extra.py" in r["summary"] for r in recs)
