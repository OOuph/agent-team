"""A document-only change is reviewed without the test step (27.11, night analysis of ai-battles).

A review lane spent 548 of its 580 seconds outside the model: `npm ci` and the unit tests on a copy of the commit — also
for a task that changed one GDD text (T-0602: 47 minutes in the queue, 6.5 minutes of that step, 9 seconds of the
reviewer). `cycle.review_tests.skip_for_docs` (off by default): when the reviewed commit changes only documents, the
tests are not run and no play-test server is started; the reviewer reads the read-only checkout."""
from __future__ import annotations

from pathlib import Path

from team import config as C
from team import cycle as cycle_mod
from team import gitops
from team.cycle import Cycle
from team.runner import RunResult
from team.store import Store

from test_playtest import _game, _settings as _base


def _setup(tmp_path: Path, on: bool, files: dict[str, str], role: str = "game-designer") -> tuple[C.Settings, C.Product, str]:
    st = _base(tmp_path)
    st.raw["cycle"] = {**st.raw["cycle"], "review_tests": {"skip_for_docs": on}}
    p = _game(st)
    repo = p.repo_dir
    gitops.git(repo, "checkout", "-q", "-b", "task/T-0001")
    for path, text in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(text)
    gitops.git(repo, "add", "-A"); gitops.git(repo, "commit", "-q", "-m", "T-0001: change")
    gitops.git(repo, "checkout", "-q", "main")
    gitops.git(repo, "merge", "-q", "--no-ff", "-m", "Merge task/T-0001", "task/T-0001")
    s = Store(p.dir)
    t = s.create_task(title="design/gdd/21-3.md: rulings", type_="design", problem="x", scope="design/gdd/", acceptance=["the rulings are written"],
                      goal="M0", created_by="po", role=role, state="ready")
    s.transition(t["id"], "released", "dispatcher", "merged", force=True, released_at="2020-01-01T00:00:00+00:00",
                 released_sha=gitops.head(repo), repo_only=True)
    s.add_result(t["id"], {"run_id": "r", "status": "done", "summary": "written", "changes": list(files), "how_to_verify": ["read"], "self_check": []})
    s.close()
    return st, p, t["id"]


class Reviewer:
    def __init__(self):
        self.calls, self.prompt, self.cwd_has = [], "", None

    def run(self, role, prompt, schema, *, cwd, **kw):
        self.calls.append(role)
        self.prompt = prompt
        self.cwd_has = (Path(cwd) / "design" / "gdd" / "21-3.md").exists()
        out = {"verdict": "pass", "steps": [], "criteria": [{"criterion": "c", "met": True, "evidence": "read"}], "scenarios": [{"name": "a", "status": "passed", "evidence": "e"}],
               "summary": "the rulings are there. read them."}
        return RunResult(True, out, {}, 0.1, 3, None, session_id="s", duration_s=1.0)


def _count_tests(monkeypatch) -> list:
    ran = []
    real = cycle_mod.run_tests_for_review

    def spy(*a, **k):
        ran.append(a[1])
        return real(*a, **k)

    monkeypatch.setattr(cycle_mod, "run_tests_for_review", spy)
    return ran


def test_off_the_tests_run_for_a_document_as_before(tmp_path: Path, monkeypatch):
    st, p, tid = _setup(tmp_path, False, {"design/gdd/21-3.md": "# Rulings\n"})
    assert C.DEFAULTS["cycle"]["review_tests"] == {"skip_for_docs": False}
    ran = _count_tests(monkeypatch)
    r = Reviewer()
    c = Cycle(st, p, r, "c1"); c.run(["review"]); c.store.close()
    assert len(ran) == 1 and "TEST RUN (by the dispatcher)" in r.prompt


def test_on_a_document_only_change_is_reviewed_without_tests(tmp_path: Path, monkeypatch):
    st, p, tid = _setup(tmp_path, True, {"design/gdd/21-3.md": "# Rulings\n", "docs/notes.txt": "n\n"})
    ran = _count_tests(monkeypatch)
    r = Reviewer()
    c = Cycle(st, p, r, "c1"); c.run(["review"]); c.store.close()
    assert ran == [] and r.calls == ["reviewer"] and r.cwd_has is True       # no test step; the reviewer reads the checkout
    assert "not run: this commit changes documents only" in r.prompt
    s = Store(p.dir)
    assert s.task(tid)["state"] == "accepted"
    assert any(e["kind"] == "review.tests_skipped" and e["subject"] == tid for e in s.events())
    s.close()


def test_on_a_change_that_touches_anything_else_is_tested(tmp_path: Path, monkeypatch):
    for name, files in (("data", {"design/gdd/21-3.md": "# R\n", "design/balance/cups.json": "{}\n"}), ("code", {"src/x.ts": "x\n"})):
        st, p, tid = _setup(tmp_path / name, True, files)
        ran = _count_tests(monkeypatch)
        c = Cycle(st, p, Reviewer(), "c1"); c.run(["review"]); c.store.close()
        assert len(ran) == 1, name


def test_a_designers_document_is_not_play_tested(tmp_path: Path, monkeypatch):
    st, p, tid = _setup(tmp_path, True, {"design/gdd/21-3.md": "# Rulings\n", "DESIGN.md": "# Design\n"}, role="designer")
    started = []
    monkeypatch.setattr(cycle_mod.playtest, "start", lambda *a, **k: started.append(1))
    r = Reviewer()
    c = Cycle(st, p, r, "c1"); c.run(["review"]); c.store.close()
    assert started == [] and r.calls == ["reviewer"]


def test_docs_only_helper(tmp_path: Path):
    st, p, tid = _setup(tmp_path, True, {"design/gdd/21-3.md": "# R\n"})
    c = Cycle(st, p, None, "c1")
    assert c.docs_only_change(gitops.head(p.repo_dir), pre_merge=False) is True
    assert c.docs_only_change("0" * 40, pre_merge=False) is False                # an unknown commit: not sure → tested
    c.store.close()
