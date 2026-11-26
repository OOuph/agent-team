"""`needs_context` (rework slice 2a, owner 26.11, point 19 of the guide review: «агент без нужных данных останавливается и
эскалирует PO; у PO либо есть ответ, либо он спрашивает владельца»).

A role that lacks what the task needs returns `status: needs_context` with the list `missing`. With
`cycle.escalation.needs_context: true` the dispatcher turns it into ONE question to the PO, the task waits for the answer
and the attempt is not counted as a failure; the answer travels to the role with the task. Off (default): the old
`blocked` + `question` path."""
from __future__ import annotations

import json
from pathlib import Path

from team import config as C
from team import gitops, prompts, replan
from team.cycle import Cycle
from team.runner import RunResult
from team.store import Store

SCHEMAS = Path(C.__file__).parent / "schemas"


def _settings(tmp_path: Path, on: bool) -> C.Settings:
    raw = C._merge(C.DEFAULTS, {"cycle": {"review_after_min": 0, "persona_every_h": 0, "digest_hour_utc": 99,
                                          "escalation": {"needs_context": on}}})
    return C.Settings(raw, tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")


def _product(st: C.Settings) -> C.Product:
    d = st.products_dir / "demo"
    d.mkdir(parents=True)
    (d / "product.yaml").write_text("slug: demo\nname: Demo\nhost: ''\nrepo:\n  url: ''\n  branch: main\n")
    (d / "VISION.md").write_text("# Demo\n\nGoals: G1 people.\n")
    p = C.load_product(st, d)
    repo = gitops.init_local(p.repo_dir)
    (repo / "README.md").write_text("static site\n")
    gitops.git(repo, "add", "-A"); gitops.git(repo, "commit", "-q", "-m", "init")
    return p


class Runner:
    """The developer answers with the scripted results, one per run; prompts are kept."""

    def __init__(self, *results: dict):
        self.results, self.prompts = list(results), []

    def run(self, role, prompt, schema, *, cwd, **kw):
        self.prompts.append(prompt)
        out = self.results.pop(0)
        if out.get("status") == "done":
            (Path(cwd) / "a.txt").write_text("x")
            gitops.git(Path(cwd), "add", "-A"); gitops.git(Path(cwd), "commit", "-q", "-m", "T-0001: a")
        return RunResult(True, out, {"total_cost_usd": 0.1}, 0.1, 3, None, session_id="s", duration_s=1.0)


def _res(status: str, **kw) -> dict:
    return {"status": status, "summary": kw.pop("summary", "s"), "changes": [], "how_to_verify": [], "self_check": [], **kw}


def _run(st, p, s, r, tid, n):
    s.claim(tid, "developer", f"run-{n}")
    s.transition(tid, "claimed", "d", force=True)
    s.transition(tid, "in_progress", "d")                 # as `Cycle.execute_task` does before the role runs
    assert Cycle(st, p, r, f"c{n}").exec_developer(s.task(tid), f"run-{n}")


def _task(s: Store) -> dict:
    return s.create_task(title="Price table", type_="feature", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po", state="ready")


def test_schemas_know_needs_context():
    tr = json.loads((SCHEMAS / "task-result.schema.json").read_text())
    assert "needs_context" in tr["properties"]["status"]["enum"] and tr["properties"]["missing"]["type"] == "array"
    ds = json.loads((SCHEMAS / "design-spec.schema.json").read_text())
    assert ds["properties"]["missing"]["type"] == "array"
    assert C.DEFAULTS["cycle"]["escalation"]["needs_context"] is False


def test_needs_context_becomes_one_po_question_and_is_not_a_failure(tmp_path: Path):
    st = _settings(tmp_path, True)
    p = _product(st)
    s = Store(p.dir)
    t = _task(s)
    r = Runner(_res("needs_context", missing=["the currency of the prices", "where the price list lives"]))
    _run(st, p, s, r, t["id"], 1)
    t = s.task(t["id"])
    qs = s.questions(["open"])
    assert len(qs) == 1 and qs[0]["to"] == "po" and qs[0]["from"] == "developer" and t["id"] in qs[0]["blocks"]
    assert "the currency of the prices" in qs[0]["question"] and "where the price list lives" in qs[0]["question"]
    assert "owner_questions" in qs[0]["question"]                      # the PO is told where an owner-only point goes
    assert t["state"] == "blocked" and t["blocked_by"] == qs[0]["id"]
    assert t["results"][-1]["status"] == "needs_context"
    assert replan.failures(t) == 0 and not t.get("attempts")           # the task waits; it did not fail
    assert any(e["kind"] == "task.needs_context" for e in s.events())
    s.close()


def test_answer_travels_to_the_role_and_the_task_goes_on(tmp_path: Path):
    st = _settings(tmp_path, True)
    p = _product(st)
    s = Store(p.dir)
    t = _task(s)
    r = Runner(_res("needs_context", missing=["the currency of the prices"]), _res("done", summary="table added"))
    _run(st, p, s, r, t["id"], 1)
    q = s.questions(["open"])[0]
    s.answer(q["id"], "Euro, VISION §2 says the audience is in the EU.", "po")
    assert s.task(t["id"])["state"] == "ready"
    _run(st, p, s, r, t["id"], 2)
    assert "ANSWERS TO THIS TASK'S QUESTIONS" in r.prompts[1] and "Euro, VISION §2" in r.prompts[1]
    assert "ANSWERS TO THIS TASK'S QUESTIONS" not in r.prompts[0]
    assert s.task(t["id"])["state"] in ("released", "review")
    s.close()


def test_old_blocked_with_a_question_takes_the_same_path_when_on(tmp_path: Path):
    st = _settings(tmp_path, True)
    p = _product(st)
    s = Store(p.dir)
    t = _task(s)
    _run(st, p, s, Runner(_res("blocked", question="Which currency?")), t["id"], 1)
    t = s.task(t["id"])
    assert t["results"][-1]["status"] == "needs_context" and t["results"][-1]["missing"] == ["Which currency?"]
    assert replan.failures(t) == 0 and "Which currency?" in s.questions(["open"])[0]["question"]
    s.close()


def test_off_is_the_old_blocked_path(tmp_path: Path):
    st = _settings(tmp_path, False)
    p = _product(st)
    s = Store(p.dir)
    t = _task(s)
    r = Runner(_res("needs_context", missing=["the currency", "the price list"]), _res("done"))
    _run(st, p, s, r, t["id"], 1)
    t = s.task(t["id"])
    q = s.questions(["open"])[0]
    assert t["results"][-1]["status"] == "blocked" and replan.failures(t) == 1          # counted, as a blocked result always was
    assert q["question"] == "the currency; the price list" and t["state"] == "blocked"
    assert not any(e["kind"] == "task.needs_context" for e in s.events())
    s.answer(q["id"], "Euro", "po")
    _run(st, p, s, r, t["id"], 2)
    assert "ANSWERS TO THIS TASK'S QUESTIONS" not in r.prompts[1]                         # the prompt is the old one
    s.close()


def test_owner_answer_reaches_the_role_too(tmp_path: Path):
    st = _settings(tmp_path, True)
    p = _product(st)
    s = Store(p.dir)
    t = _task(s)
    _run(st, p, s, Runner(_res("needs_context", missing=["may we show prices at all"])), t["id"], 1)
    q = s.questions(["open"])[0]
    cy = Cycle(st, p, Runner(), "c2")
    cy.apply_po({"decisions": [], "answers": [{"question_id": q["id"], "answer": "Asked the owner; wait."}],
                 "owner_questions": [{"question": "May the site show prices?", "why_owner": "a promise to users", "task_id": t["id"]}],
                 "ready_tasks": [], "no_new_tasks": True, "digest_md": "d"}, [])
    t2 = s.task(t["id"])
    assert t2["state"] == "waiting_owner"                              # the task waits for the owner, it is not restarted to guess
    oq = next(x for x in s.questions(["open"]) if x["to"] == "owner")
    s.answer(oq["id"], "Yes, with VAT.", "owner")
    assert s.task(t["id"])["state"] == "ready"
    block = prompts.answers_block(cy.task_answers(s.task(t["id"])))
    assert "Yes, with VAT." in block and "answered_by: owner" in block
    s.close()


def test_role_cards_do_not_tell_roles_to_ask_or_wait_for_confirmation():
    roles = Path(C.__file__).parent / "roles"
    for name in ("developer", "designer", "game-designer", "tester", "reviewer", "analyst", "marketer"):
        text = (roles / f"{name}.md").read_text(encoding="utf-8").lower()
        for phrase in ("ask the po", "ask the owner", "ask the user", "clarify with", "wait for confirmation", "wait for approval", "wait for the owner"):
            assert phrase not in text, f"{name}.md: «{phrase}»"
    for name in ("developer", "designer", "game-designer"):
        assert "needs_context" in (roles / f"{name}.md").read_text(encoding="utf-8"), name
    assert "needs_context" in (roles / "po.md").read_text(encoding="utf-8")
