"""The text of what a task refers to travels with it (27.11, ai-battles T-0624: the task said «his eight decisions are
in observation OBS-2063 word for word», the game designer could not see an observation, asked — Q-0246 — and the answer
cost a block and a cycle). `cycle.escalation.references` (off by default): the observations and questions a task names
(OBS-…, Q-… in its text, its `observations` and `links`) are put into the role's prompt, within a size limit."""
from __future__ import annotations

from pathlib import Path

from team import config as C
from team import gitops, prompts
from team.cycle import Cycle
from team.runner import RunResult
from team.store import Store

from test_playtest import _game, _settings as _base


def _setup(tmp_path: Path, on: bool):
    st = _base(tmp_path)
    st.raw["cycle"] = {**st.raw["cycle"], "escalation": {"references": on}}
    p = _game(st)
    (p.repo_dir / "GAME-DESIGN.md").write_text("# GDD\n")
    gitops.git(p.repo_dir, "add", "-A"); gitops.git(p.repo_dir, "commit", "-q", "-m", "gdd")
    s = Store(p.dir)
    o = s.add_observation({"title": "Owner 27.11: The Tower approved, eight decisions", "classification": "need_hypothesis", "source": "owner",
                           "actual": "DECISION-TEXT: 1. Tower 2. weekly reset " + "detail " * 400, "fingerprint": "tower"})
    q = s.ask(from_="game-designer", to="po", question="Which reset day?")
    s.answer(q["id"], "ANSWER-TEXT: Monday 00:00 UTC", "po")
    return st, p, s, o["id"], q["id"]


def test_references_block_finds_ids_in_text_observations_and_links(tmp_path: Path):
    st, p, s, oid, qid = _setup(tmp_path, True)
    t = {"id": "T-0001", "title": "Tower GDD part 1", "problem": f"The decisions are in observation {oid} word for word.", "scope": "s",
         "acceptance": [f"agrees with the answer to {qid}"], "observations": ["OBS-9999"], "links": []}
    block = prompts.references_block(s, t)
    assert "# WHAT THIS TASK REFERS TO" in block and "DECISION-TEXT" in block and "ANSWER-TEXT" in block and "Which reset day?" in block
    assert "OBS-9999" in block and "not found" in block                       # a dead reference is said, not hidden
    assert len(block) <= prompts.REFS_TOTAL_CHARS + 600 and "[cut" in block     # one long card is cut, the block stays bounded
    assert prompts.references_block(s, {"id": "T-0002", "title": "no refs", "problem": "p"}) == ""
    assert prompts.task_refs({"problem": f"{oid}, {oid} and again {oid}"}) == [oid]
    s.close()


class GD:
    def __init__(self):
        self.prompts = {}

    def run(self, role, prompt, schema, **kw):
        self.prompts[role] = prompt
        out = {"summary": "written", "files": [{"path": "design/gdd/26-tower.md", "content": "# Tower\n"}]}
        return RunResult(True, out, {}, 0.3, 4, None, session_id="s", duration_s=1.0)


def _task(s: Store, oid: str, role: str) -> str:
    t = s.create_task(title="Tower GDD part 1", type_="design", problem=f"His eight decisions are in observation {oid} word for word.", scope="design/gdd/",
                      acceptance=["a"], goal="M0", created_by="po", role=role, state="ready")
    return t["id"]


def test_the_game_designer_gets_the_observation_text_when_on(tmp_path: Path):
    st, p, s, oid, qid = _setup(tmp_path, True)
    _task(s, oid, "game-designer"); s.close()
    r = GD()
    c = Cycle(st, p, r, "c1"); c.run(["execute"]); c.store.close()
    assert "DECISION-TEXT" in r.prompts["game-designer"] and "# WHAT THIS TASK REFERS TO" in r.prompts["game-designer"]


def test_off_the_prompt_is_the_old_one(tmp_path: Path):
    st, p, s, oid, qid = _setup(tmp_path, False)
    assert C.DEFAULTS["cycle"]["escalation"]["references"] is False
    _task(s, oid, "game-designer"); s.close()
    r = GD()
    c = Cycle(st, p, r, "c1"); c.run(["execute"]); c.store.close()
    assert "DECISION-TEXT" not in r.prompts["game-designer"]


def test_the_developer_prompt_takes_the_block(tmp_path: Path):
    st, p, s, oid, qid = _setup(tmp_path, True)
    t = s.task(_task(s, oid, "developer"))
    pr = prompts.developer_prompt(p, t, tmp_path, "task/x", [], None, refs=prompts.references_block(s, t))
    assert pr.index("# TASK") < pr.index("# WHAT THIS TASK REFERS TO") and "DECISION-TEXT" in pr
    s.close()
