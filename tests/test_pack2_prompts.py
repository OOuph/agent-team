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
