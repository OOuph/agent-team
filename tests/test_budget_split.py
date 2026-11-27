"""A run that hit its budget sends the task to the PO at once (27.11, ai-battles T-0610: a game designer's document
ran into `error_max_budget_usd` three times in a row, 06:30–08:04, before it was blocked — about $13 and 94 minutes for
nothing; the same task with the same scope cannot fit the second time either). `cycle.escalation.budget_split` (off
by default): after the FIRST such run the task is blocked with a question to the PO — split it or narrow it — and is
not started again until the PO answers."""
from __future__ import annotations

from pathlib import Path

from team import config as C
from team import gitops
from team.cycle import Cycle
from team.runner import RunResult
from team.store import Store

from test_playtest import _game, _settings as _base

OVER = RunResult(False, None, {"subtype": "error_max_budget_usd", "is_error": True}, 3.4, 9, "error_max_budget_usd", duration_s=300.0)


class R:
    def __init__(self, result=OVER):
        self.result, self.calls = result, []

    def run(self, role, prompt, schema, **kw):
        self.calls.append(role)
        return self.result


def _setup(tmp_path: Path, on: bool, role: str):
    st = _base(tmp_path)
    st.raw["cycle"] = {**st.raw["cycle"], "escalation": {"budget_split": on}}
    p = _game(st)
    (p.repo_dir / "GAME-DESIGN.md").write_text("# GDD\n")
    gitops.git(p.repo_dir, "add", "-A"); gitops.git(p.repo_dir, "commit", "-q", "-m", "gdd")
    s = Store(p.dir)
    t = s.create_task(title="The Tower: the whole GDD", type_="design", problem="p", scope="design/gdd/26-tower.md", acceptance=["a"], goal="M0",
                      created_by="po", role=role, state="ready")
    s.close()
    return st, p, t["id"]


def test_default_is_off_three_attempts_as_before(tmp_path: Path):
    st, p, tid = _setup(tmp_path, False, "game-designer")
    assert C.DEFAULTS["cycle"]["escalation"]["budget_split"] is False
    r = R()
    for i in range(3):
        c = Cycle(st, p, r, f"c{i}"); c.run(["execute"]); c.store.close()
    s = Store(p.dir)
    assert len(r.calls) == 3 and s.task(tid)["state"] == "blocked" and not s.questions(["open"])
    s.close()


def test_the_first_run_over_budget_goes_to_the_po_and_is_not_started_again(tmp_path: Path):
    for role in ("game-designer", "developer"):
        st, p, tid = _setup(tmp_path / role, True, role)
        r = R()
        for i in range(3):
            c = Cycle(st, p, r, f"c{i}"); c.run(["execute"]); c.store.close()
        s = Store(p.dir)
        t = s.task(tid)
        assert len(r.calls) == 1, role                                    # one run, not three
        assert t["state"] == "blocked" and str(t.get("blocked_by")).startswith("Q-") and "run budget" in t["history"][-1]["note"]
        q = [q for q in s.questions(["open"]) if q["to"] == "po"]
        assert len(q) == 1 and tid in q[0]["blocks"] and "split" in q[0]["question"].lower() and "$" in q[0]["question"]
        assert any(e["kind"] == "task.budget_split" and e["subject"] == tid for e in s.events())
        assert t["results"][-1]["status"] == "error" and not t.get("attempts")
        s.close()


def test_another_failure_keeps_its_three_attempts(tmp_path: Path):
    st, p, tid = _setup(tmp_path, True, "game-designer")
    r = R(RunResult(False, None, None, 0.2, 1, "no structured_output in the result", duration_s=5.0))
    for i in range(2):
        c = Cycle(st, p, r, f"c{i}"); c.run(["execute"]); c.store.close()
    s = Store(p.dir)
    assert len(r.calls) == 2 and s.task(tid)["state"] == "ready" and s.task(tid)["attempts"] == 2
    s.close()
