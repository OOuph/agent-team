"""The deputy sees what the owner has put on record and leaves him the questions about his own words (27.11).

ai-battles Q-0236: both sides of the question were the owner's words, the deciding VISION section lay past the 9000
characters the prompt carried, decisions.md and the owner's observations were not in the prompt at all — and the deputy
«decided for now» against a decision of the day before."""
from __future__ import annotations

from pathlib import Path

from team import deputy
from team.cycle import Cycle
from team.runner import RunResult
from team.store import Store

from test_parallel_lanes import _product, _settings

LATE = "## Машина по описанию создаётся всегда (владелец, 26–27.11.2026)\nОтказа игроку нет: машина выдаётся всегда.\n"
VISION = "# VISION\n\nGoals: G1.\n\n## Что это\n" + "вода " * 9000 + "\n\n" + LATE + "\n## Стек\nnode\n"


class OneAnswer:
    """A stand-in runner: the deputy answers every question with `kind`."""

    def __init__(self, kind: str):
        self.kind, self.prompts, self.calls = kind, [], []

    def run(self, role, prompt, schema, **kw):
        self.calls.append(role)
        self.prompts.append(prompt)
        ids = [ln.split('"')[3] for ln in prompt.splitlines() if ln.strip().startswith('"id": "Q-')]
        return RunResult(ok=True, structured={"answers": [{"question_id": i, "kind": self.kind, "answer": "Both are the owner's words: VISION «создаётся всегда» and his 26.11 note."} for i in ids]},
                         cost_usd=0.1, num_turns=1, session_id="s", error=None, raw={})


def _setup(tmp_path: Path, on: bool):
    st = _settings(tmp_path)
    st.raw["deputy"] = {"enabled": True, "after_h": 0}
    (st.root / "DEPUTY.md").write_text("# mandate\n", encoding="utf-8")
    p = _product(st, extra="deputy:\n  owner_words: true\n" if on else "")
    (p.dir / "VISION.md").write_text(VISION, encoding="utf-8")
    (p.dir / "decisions.md").write_text("# решения\n\n" + "- старое решение\n" * 2000 + "- 2026-11-27 — владелец: вариант B вопроса отозван, машина выдаётся всегда.\n", encoding="utf-8")
    s = Store(p.dir)
    s.add_observation({"title": "Такого быть не должно!", "actual": "Игроку отказали в машине.", "source": "owner", "classification": "defect"})
    s.add_observation({"title": "a role's note", "actual": "not the owner", "source": "developer", "classification": "defect"})
    t = s.create_task(title="rescue rule", type_="design", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po", state="ready", role="designer")
    q = s.ask(from_="game-designer", to="owner", question="Rescue never fails vs the corridor promise: A, B or C?", task_id=t["id"])
    s.close()
    return st, p, t["id"], q["id"]


def test_default_is_off_and_the_prompt_is_as_before(tmp_path: Path):
    st, p, tid, qid = _setup(tmp_path, on=False)
    assert deputy.owner_words(st, p) is False
    s = Store(p.dir)
    txt = deputy.deputy_prompt(p, s, deputy.due_questions(st, s), "m", owner=deputy.owner_words(st, p))
    s.close()
    assert "создаётся всегда" not in txt and "DECISIONS THE OWNER" not in txt and "owner_only" not in txt


def test_the_prompt_carries_the_owners_sections_decisions_and_observations(tmp_path: Path):
    st, p, tid, qid = _setup(tmp_path, on=True)
    assert deputy.owner_words(st, p) is True
    s = Store(p.dir)
    txt = deputy.deputy_prompt(p, s, deputy.due_questions(st, s), "m", owner=True)
    s.close()
    assert "Отказа игроку нет: машина выдаётся всегда." in txt                     # a section that began past character 9000
    assert "вариант B вопроса отозван" in txt and "older decisions are not shown" in txt
    assert "Такого быть не должно!" in txt and "a role's note" not in txt
    assert "owner_only" in txt
    assert len(txt) < 9000 + deputy.VISION_OWNER_CHARS + deputy.DECISIONS_CHARS + 30000


def test_the_size_limits_hold():
    big = "# V\n\n" + "".join(f"## Раздел {i} (владелец)\n" + "x" * 30000 + "\n" for i in range(4)) + "## Прочее\n" + "y" * 50000
    out = deputy.vision_for_deputy(big)
    assert len(out) < deputy.VISION_OWNER_CHARS + deputy.VISION_REST_CHARS + 2000 and "Раздел 3 (владелец)" in out and "not shown here" in out


def test_a_question_about_the_owners_words_stays_open(tmp_path: Path):
    st, p, tid, qid = _setup(tmp_path, on=True)
    r = OneAnswer("owner_only")
    cyc = Cycle(st, p, r, "c1")
    assert deputy.run_for_product(cyc) == 0                                           # nothing was answered
    q = [x for x in cyc.store.questions() if x["id"] == qid][0]
    assert q["state"] == "open" and q.get("answered_by") is None and q["deputy_left"]["why"].startswith("Both are the owner's words")
    assert cyc.store.task(tid)["state"] == "waiting_owner"
    assert [e["subject"] for e in cyc.store.events() if e["kind"] == "deputy.left_to_owner"] == [qid]
    assert "Отказа игроку нет" in r.prompts[0]
    assert deputy.run_for_product(cyc) == 0 and len(r.calls) == 1                     # and the deputy is not asked again
    cyc.store.answer(qid, "A: машина выдаётся всегда", "owner")                       # the owner's answer still releases the task
    assert cyc.store.task(tid)["state"] == "ready"
    cyc.store.close()


def test_any_other_answer_is_written_as_before(tmp_path: Path):
    st, p, tid, qid = _setup(tmp_path, on=True)
    cyc = Cycle(st, p, OneAnswer("decided"), "c1")
    assert deputy.run_for_product(cyc) == 1
    assert cyc.store.task(tid)["state"] == "ready"
    cyc.store.close()
