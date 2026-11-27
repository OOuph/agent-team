"""Contracts of the roles: what a role's request holds and how big it is (audit of 27.11.2026, defects №4, №5, №6, №13).

A scenario checks the scheduler. These check what the role is GIVEN when the scheduler starts it: on a store the size
of the game's (600 tasks) no request is over its limit and none grows with the backlog; what the task refers to — a
file of the repository, an observation, a question — is in the request or in the run's working folder; the deputy has
the owner's recorded words. One whole cycle on the stand-in runner per case, on «every switch on, seats and slots of
ai-battles»."""
from __future__ import annotations

from pathlib import Path

import pytest

from team import config as C
from team import deputy
from team.cycle import Cycle
from team.store import Store

from cycle_invariants import game_like, run_cycle, violations
from test_parallel_lanes import LaneRunner, _commit, _product, _released, _settings, _task

# ------------------------------------------------------------------ parameters
BIG = 600                         # finished tasks in the big store (ai-battles on 27.11: 626 tasks, 477 of them done)
BIG_OBS = 150                     # observations already triaged
LONG = ("The change is in src/engine and its tests; every acceptance criterion is listed with the command that shows it. " * 12).strip()
PROMPT_LIMIT = {                  # characters; a request over its limit fails the contract (27.11: the analyst's was 816 KB)
    "po": 220_000, "analyst": 120_000, "developer": 60_000, "designer": 60_000, "game-designer": 120_000,
    "reviewer": 40_000, "tester": 40_000, "deputy": 110_000,
}
GROWTH_LIMIT = {                  # characters a request may grow from the small store to the big one
    "po": 60_000, "analyst": 40_000, "developer": 3_000, "designer": 3_000, "reviewer": 3_000, "tester": 3_000,
    "game-designer": 9_000,       # its block of observations is capped (measured 27.11: 10 579 characters at 150 and at 450 observations)
}
ROLES_CHECKED = ("po", "developer", "designer", "game-designer", "reviewer", "analyst")
PHASES = ["review", "triage", "execute", "measure"]


def _fill(p, n_done: int, n_obs: int) -> None:
    s = Store(p.dir)
    for i in range(n_done):
        t = s.create_task(title=f"finished work number {i}: a full sentence as a title, as the PO writes them", type_="defect", problem=LONG[:600],
                          scope="src/", acceptance=["works", "is tested"], goal="G1", created_by="po", role="developer", state="ready")
        s.transition(t["id"], "done", "dispatcher", "closed", force=True, results=[{"status": "done", "summary": LONG, "changes": ["src/x.ts"]}],
                     accepted_at="2026-11-20T00:00:00+00:00")
    for i in range(n_obs):
        s.add_observation({"title": f"observation {i}", "actual": LONG[:500], "source": "analyst", "classification": "defect", "fingerprint": f"fp{i}"})
    s.close()


def _one_cycle(tmp_path: Path, n_done: int, n_obs: int):
    st = _settings(tmp_path, game_like())
    p = _product(st)
    _fill(p, n_done, n_obs)
    _released(p)
    for role in ("developer", "designer", "game-designer", "analyst"):
        _task(p, role, f"the {role}'s task of this cycle")
    r = LaneRunner()
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, PHASES, limit_s=150)
    cyc.store.close()
    assert violations(cyc, p, res, r) == []
    return {role: max(len(x) for x in ps) for role, ps in r.prompts.items()}, r


@pytest.mark.timeout_s(400)
def test_no_request_is_over_its_limit_or_grows_with_the_backlog(tmp_path: Path):
    small, _ = _one_cycle(tmp_path / "small", 5, 5)
    big, _ = _one_cycle(tmp_path / "big", BIG, BIG_OBS)
    print("\nrequest sizes, characters (small store → big store):", {k: (small.get(k), big[k]) for k in sorted(big)})
    for role in ROLES_CHECKED:
        assert role in big, f"the cycle did not start the {role}: the contract was not checked"
    for role, n in sorted(big.items()):
        assert n <= PROMPT_LIMIT.get(role, 60_000), f"the {role}'s request is {n} characters on a store of {BIG} tasks (limit {PROMPT_LIMIT.get(role, 60_000)})"
        if role in small and role in GROWTH_LIMIT:
            assert n - small[role] <= GROWTH_LIMIT[role], f"the {role}'s request grew by {n - small[role]} characters with the backlog (limit {GROWTH_LIMIT[role]})"



def test_what_the_task_refers_to_is_in_the_request_or_in_the_working_folder(tmp_path: Path):
    """№5 (the game designer started in an empty folder) and №13 (the task named OBS-… and the role never saw its text)."""
    st = _settings(tmp_path, game_like())
    p = _product(st)
    s = Store(p.dir)
    obs = s.add_observation({"title": "the label is cut", "actual": "On a 360 px screen the label «Garage» loses its last two letters.", "source": "tester",
                             "classification": "defect"})
    q = s.ask(from_="developer", to="po", question="Which of the two layouts?")
    s.answer(q["id"], "The second layout: one column under 400 px.", "po")
    s.close()
    ids = {role: _task(p, role, f"{role}: fix what {obs['id']} describes, as decided in {q['id']}; see README.md") for role in ("developer", "designer", "game-designer")}
    r = LaneRunner()
    seen_files: dict[str, bool] = {}

    def look(role):
        def work(cwd: Path, n: int):
            seen_files[role] = (Path(cwd) / "README.md").exists()      # the file the task names, in the folder the run starts in
            if role != "game-designer":
                _commit(cwd, f"src/{role}.ts", "x\n")
        return work
    for role in ids:
        r.work[role] = look(role)
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, ["execute"])
    cyc.store.close()
    for role in ids:
        prompt = r.prompts[role][0]
        assert "loses its last two letters" in prompt, f"the {role}'s request does not hold the text of {obs['id']}"
        assert "one column under 400 px" in prompt, f"the {role}'s request does not hold the answer to {q['id']}"
        assert seen_files.get(role) is True, f"the {role} was started in a folder without the repository's files"


def test_the_deputy_gets_the_owners_words(tmp_path: Path):
    """№6 (Q-0236): the deputy decided against a recorded decision of the owner it had never been shown."""
    over = C._merge(game_like(), {"deputy": {"enabled": True, "after_h": 0, "owner_words": True}})
    st = _settings(tmp_path, over)
    (st.root / "DEPUTY.md").write_text("# mandate\n", encoding="utf-8")
    p = _product(st)
    (p.dir / "VISION.md").write_text("# VISION\n\nGoals: G1.\n\n## Что это\n" + "вода " * 9000 + "\n\n## Машина создаётся всегда (владелец, 27.11.2026)\nОтказа игроку нет.\n",
                                     encoding="utf-8")
    (p.dir / "decisions.md").write_text("# решения\n\n- 2026-11-27 — владелец: вариант B отозван.\n", encoding="utf-8")
    s = Store(p.dir)
    s.add_observation({"title": "Такого быть не должно!", "actual": "Игроку отказали в машине.", "source": "owner", "classification": "defect"})
    s.ask(from_="game-designer", to="owner", question="A, B or C?")
    s.close()
    r = LaneRunner()
    r.answers["deputy"] = {"answers": []}
    cyc = Cycle(st, p, r, "c1")
    run_cycle(cyc, ["triage"])
    cyc.store.close()
    prompt = r.prompts["deputy"][0]
    for words in ("Отказа игроку нет.", "вариант B отозван", "Такого быть не должно!", "owner_only"):
        assert words in prompt, f"the deputy's request does not hold «{words}»"
    assert len(prompt) <= PROMPT_LIMIT["deputy"]
