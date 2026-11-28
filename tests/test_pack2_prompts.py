"""Pack 2 of 28.11, the three small ones: what was merged after an observation (pokerbr К5), the files a merged commit
changed for the reviewer, the waiting hint in the developer's card."""
from __future__ import annotations

from pathlib import Path

from team import gitops, prompts
from team.cycle import Cycle
from team.store import Store

import test_po_prompt_compact as po
from test_parallel_lanes import LaneRunner, _commit, _product, _released, _settings

OBS = {"title": "the page has no canonical link", "classification": "reproducible_defect", "source": "reviewer", "actual": "no <link rel=canonical>"}


def _obs_and_merges(s: Store) -> tuple[dict, str, str]:
    card = s.add_observation(dict(OBS))
    s.update_observation(card["id"], observed_at="2026-11-27T22:00:00+00:00")
    card = [o for o in s.observations() if o["id"] == card["id"]][0]
    before = s.create_task(title="merged before the observation", type_="defect", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po", state="ready")
    s.transition(before["id"], "released", "dispatcher", force=True, released_at="2026-11-27T21:00:00+00:00")
    after = s.create_task(title="canonical link added to every page", type_="defect", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po", state="ready")
    s.transition(after["id"], "accepted", "reviewer", force=True, released_at="2026-11-28T03:10:00+00:00", accepted_at="2026-11-28T03:40:00+00:00")
    return card, before["id"], after["id"]


def test_an_observation_names_what_was_merged_after_it(tmp_path: Path) -> None:
    """28.11, pokerbr К5: a task was cut from an observation that a merge of the night had already answered."""
    p = po._product(po._settings(tmp_path, {"merged_since": True}))
    s = Store(p.dir)
    card, before, after = _obs_and_merges(s)
    txt = prompts.po_prompt(p, s, **{**po._kw(), "observations": [card]})
    block = txt.split("# NEW OBSERVATIONS", 1)[1].split("# LATEST METRICS", 1)[0]
    assert "merged_after_this_observation" in block and f"{after} canonical link added to every page (merged 2026-11-28T03:10)" in block
    assert before not in block and "check whether one of them" in block
    quiet = s.add_observation({**OBS, "title": "seen just now", "fingerprint": "x"})
    block = prompts.po_prompt(p, s, **{**po._kw(), "observations": [quiet]}).split("# NEW OBSERVATIONS", 1)[1]
    assert "merged_after_this_observation" not in block.split("# LATEST METRICS", 1)[0]       # nothing merged since: nothing added
    s.close()


def test_off_by_default_the_card_travels_as_before(tmp_path: Path) -> None:
    p = po._product(po._settings(tmp_path))
    s = Store(p.dir)
    card, _, after = _obs_and_merges(s)
    assert p.po_prompt_cfg["merged_since"] is False
    block = prompts.po_prompt(p, s, **{**po._kw(), "observations": [card]}).split("# NEW OBSERVATIONS", 1)[1].split("# LATEST METRICS", 1)[0]
    assert "merged_after_this_observation" not in block and after not in block
    s.close()


def _review_prompt(tmp_path: Path, over: dict) -> str:
    st = _settings(tmp_path, over)
    p = _product(st)
    _commit(p.repo_dir, "src/tower.ts", "export const floors = 10\n", "T: tower floors")
    tid = _released(p)
    r = LaneRunner()
    cyc = Cycle(st, p, r, "c1")
    cyc.review_task(cyc.store.task(tid))
    cyc.store.close()
    return (r.prompts.get("reviewer") or r.prompts.get("tester") or [""])[0]


def test_the_reviewer_is_given_the_files_the_commit_changed(tmp_path: Path) -> None:
    """28.11: reviewers and testers of the game spent 13 turns in three cycles on `git show --stat`, which they may not run."""
    prompt = _review_prompt(tmp_path / "on", {"cycle": {"review_tests": {"changed_files": True}}})
    assert "# FILES THE MERGED COMMIT CHANGED" in prompt and "src/tower.ts" in prompt and "1 file changed" in prompt
    assert "# FILES THE MERGED COMMIT CHANGED" not in _review_prompt(tmp_path / "off", {})


def test_the_developers_card_says_how_to_wait_without_monitor() -> None:
    card = (Path(prompts.__file__).parent / "roles" / "developer.md").read_text(encoding="utf-8")
    assert "the `Monitor` tool is not available to you" in card and 'until grep -q "^exit="' in card


# ------------------------------------------------------------------ «wait for a task» in the PO's answer

def _blocked_with_a_question(p):
    s = Store(p.dir)
    other = s.create_task(title="the ruling the tower waits for", type_="design", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po",
                          role="game-designer", state="ready")
    waits = s.create_task(title="tower floor built by the ruling", type_="feature", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po", state="ready")
    q = s.ask(from_="developer", to="po", question="Which ruling of the tower applies?", task_id=waits["id"])
    s.close()
    return other["id"], waits["id"], q["id"]


def test_the_po_answers_wait_for_a_task_and_the_task_starts_when_it_is_met(tmp_path: Path) -> None:
    """28.11: the PO put T-0690 and T-0742 of the game off to 12:30 and 14:00 — hours it guessed — when they waited for the
    acceptance of another task. With `cycle.dependencies.in_answers` the answer names the task and the wait ends with it."""
    st = _settings(tmp_path, {"cycle": {"dependencies": {"in_answers": True}}})
    p = _product(st)
    other, waits, qid = _blocked_with_a_question(p)
    cyc = Cycle(st, p, LaneRunner(), "c1")
    assert cyc.store.task(waits)["state"] == "blocked"
    cyc._apply_po({"answers": [{"question_id": qid, "answer": "Wait for the ruling.", "wait_for": [other, waits, "T-9999"]}]}, [])
    t = cyc.store.task(waits)
    assert t["state"] == "ready" and t["depends_on"] == [other] and not t.get("not_before")      # itself and an unknown id are left out
    assert f"waits for {other}" in t["history"][-1]["note"]
    assert cyc.startable(t) is None                                    # not yet: its dependency is not finished
    cyc.store.transition(other, "accepted", "reviewer", force=True, accepted_at="2026-11-28T12:00:00+00:00", released_at="2026-11-28T11:00:00+00:00")
    assert cyc.startable(cyc.store.task(waits)) is not None and cyc.startable(cyc.store.task(waits)) is not False
    cyc.store.close()
    from test_parallel_lanes import _events
    assert [e["detail"]["wait_for"] for e in _events(p, "task.wait_for")] == [[other]]


def test_wait_for_is_ignored_while_the_switch_is_off_and_the_prompt_says_nothing(tmp_path: Path) -> None:
    st = _settings(tmp_path)
    p = _product(st)
    other, waits, qid = _blocked_with_a_question(p)
    cyc = Cycle(st, p, LaneRunner(), "c1")
    cyc._apply_po({"answers": [{"question_id": qid, "answer": "Wait.", "wait_for": [other]}]}, [])
    t = cyc.store.task(waits)
    assert t["state"] == "ready" and t["depends_on"] == []
    cyc.store.close()


def test_the_po_is_told_about_wait_for_only_when_the_switch_is_on(tmp_path: Path) -> None:
    from test_parallel_lanes import PO_NOTHING
    seen = {}
    for name, over in (("on", {"cycle": {"dependencies": {"in_answers": True}}}), ("off", {})):
        st = _settings(tmp_path / name, over)
        p = _product(st)
        _blocked_with_a_question(p)
        r = LaneRunner()
        r.answers["po"] = dict(PO_NOTHING)
        cyc = Cycle(st, p, r, "c1")
        cyc.run(["triage"])
        cyc.store.close()
        seen[name] = (r.prompts.get("po") or [""])[0]
    assert seen["on"] and seen["off"]
    assert "# WAIT FOR A TASK" in seen["on"] and "# WAIT FOR A TASK" not in seen["off"]
