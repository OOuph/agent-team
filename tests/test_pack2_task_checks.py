"""Pack 2 of 28.11, pokerbr К1 and К2: a task's own text is read when it is created; a private repository; a review by
hand of a task with nothing merged."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from team import taskcheck
from team.cycle import Cycle
from team.store import Store

from test_parallel_lanes import PO_NOTHING, LaneRunner, _commit, _events, _product, _released, _settings, _state

REPO = Path(__file__).resolve().parent.parent
ON = {"cycle": {"escalation": {"task_checks": True, "review_needs_merge": True}}}
TASK = {"title": "Landing page of the ranking", "type": "defect", "problem": "the ranking has no page", "scope": "public/", "goal": "G1", "role": "developer", "priority": "P2", "size": "S",
        "acceptance": ["npm test passes", "the page answers 200 on the live site https://fleets.example/ranking", "the title is in Portuguese"]}


def test_which_criteria_only_the_deployed_product_shows():
    acc = ["npm test passes", "The page answers 200 on the live site", "после выкладки страница открывается", "visible at placar.bymachines.ai/x",
           "the README names the command"]
    assert taskcheck.after_deploy(acc, "placar.bymachines.ai") == acc[1:4]
    assert taskcheck.after_deploy(["unit tests cover the reducer", "a delivery note is in the summary"], "x.example") == []
    assert taskcheck.after_deploy(None) == [] and taskcheck.developer_note([]) == ""


def test_a_role_without_tools_and_a_task_that_asks_for_the_network():
    task = {"title": "Keyword volumes", "problem": "fetch the search volumes from the API for 40 phrases", "scope": "", "acceptance": ["a table in the report"]}
    assert "has no tools and no network" in taskcheck.toolless_warning("analyst", task)
    assert taskcheck.toolless_warning("developer", task) == ""                              # it has a shell
    assert taskcheck.toolless_warning("analyst", {**task, "data_request": {"keywords": ["poker"]}}) == ""      # the dispatcher fetches it (#584)
    assert taskcheck.toolless_warning("analyst", {"title": "Read the week's metrics", "problem": "which page lost visits", "acceptance": []}) == ""


def _po_creates(tmp_path: Path, over: dict):
    st = _settings(tmp_path, over)
    p = _product(st)
    r = LaneRunner()
    cyc = Cycle(st, p, r, "c1")
    research = {**TASK, "title": "Keyword volumes", "role": "analyst", "type": "research", "problem": "download the volumes from https://api.example/v3",
                "acceptance": ["a table of 40 phrases"]}
    cyc._apply_po({**PO_NOTHING, "ready_tasks": [dict(TASK), research], "digest_md": "two tasks"}, [])
    tasks = {t["title"]: t for t in cyc.store.tasks()}
    return st, p, cyc, tasks


def test_the_executor_is_told_which_criteria_are_the_reviewers(tmp_path: Path):
    """К1: three partial runs of T-0038 — the developer waited for a site that shows nothing before the merge."""
    st, p, cyc, tasks = _po_creates(tmp_path, ON)
    t = tasks["Landing page of the ranking"]
    assert t["after_deploy"] == ["the page answers 200 on the live site https://fleets.example/ranking"]
    digest = (p.dir / "digests" / "c1.md").read_text(encoding="utf-8")
    assert "Проверка новых задач диспетчером" in digest and t["id"] in digest and tasks["Keyword volumes"]["id"] in digest
    assert [e["subject"] for e in _events(p, "task.toolless")] == [tasks["Keyword volumes"]["id"]]
    cyc.store.transition(t["id"], "ready", "po", force=True)
    r = LaneRunner()
    cyc2 = Cycle(st, p, r, "c2")
    cyc.store.close()
    cyc2.run(["execute"])
    cyc2.store.close()
    prompt = next(x for x in r.prompts["developer"] if "Landing page of the ranking" in x)
    assert "# CRITERIA CHECKED AFTER THE DEPLOY (not yours to prove)" in prompt and "- the page answers 200 on the live site" in prompt
    assert "do not report `partial` because of" in prompt


def test_off_by_default_nothing_is_read_and_nothing_is_said(tmp_path: Path):
    st, p, cyc, tasks = _po_creates(tmp_path, {})
    assert "after_deploy" not in tasks["Landing page of the ranking"]
    assert "Проверка новых задач" not in (p.dir / "digests" / "c1.md").read_text(encoding="utf-8") and _events(p, "task.toolless") == []
    cyc.store.close()


def _cli(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(REPO), "TEAM_ROOT": str(root)}
    return subprocess.run([sys.executable, "-m", "team.cli", *args], cwd=root, env=env, capture_output=True, text=True, timeout=60)


def _root(tmp_path: Path, on: bool):
    extra = "cycle:\n  escalation:\n    task_checks: true\n    review_needs_merge: true\n" if on else ""
    (tmp_path / "team.yaml").write_text("paths:\n  products_dir: products\n  data_dir: data\n  repos_dir: data/repos\n" + extra, encoding="utf-8")
    st = _settings(tmp_path)
    return st, _product(st)


def test_team_task_new_warns_and_a_review_by_hand_needs_a_merge_or_a_reason(tmp_path: Path):
    st, p = _root(tmp_path, on=True)
    cp = _cli(tmp_path, "task", "fleets", "new", "--title", "Landing", "--ready", "--by", "operator",
              "--acceptance", "npm test passes", "--acceptance", "the page answers 200 on the live site")
    tid = cp.stdout.strip()
    assert cp.returncode == 0 and "can be checked only on the deployed product" in cp.stderr and "the reviewer's, after the deploy" in cp.stderr
    assert _state(p, tid)["after_deploy"] == ["the page answers 200 on the live site"]
    cp = _cli(tmp_path, "task", "fleets", "new", "--title", "Volumes", "--role", "analyst", "--problem", "fetch https://api.example/x", "--by", "operator")
    assert cp.returncode == 0 and "has no tools and no network" in cp.stderr
    # К1: `team task move --force --to review` of a task with nothing merged went through, and the reviewer had no commit
    cp = _cli(tmp_path, "task", "fleets", "move", "--id", tid, "--to", "review", "--force")
    assert cp.returncode == 2 and "has no merged commit" in cp.stderr and _state(p, tid)["state"] == "ready"
    cp = _cli(tmp_path, "task", "fleets", "move", "--id", tid, "--to", "review", "--force", "--note", "checked by hand on the staging host")
    assert cp.returncode == 0 and _state(p, tid)["state"] == "review"
    merged = _released(p)
    assert _cli(tmp_path, "task", "fleets", "move", "--id", merged, "--to", "review", "--force").returncode == 0      # a merged task moves as before


def test_with_the_switches_off_the_command_only_warns(tmp_path: Path):
    st, p = _root(tmp_path, on=False)
    cp = _cli(tmp_path, "task", "fleets", "new", "--title", "Landing", "--ready", "--acceptance", "the page answers 200 on the live site")
    tid = cp.stdout.strip()
    assert cp.returncode == 0 and "BEFORE the merge" in cp.stderr and "after_deploy" not in _state(p, tid)
    assert _cli(tmp_path, "task", "fleets", "move", "--id", tid, "--to", "review", "--force").returncode == 0


def test_a_private_repository_gives_every_review_the_checkout(tmp_path: Path):
    """К1: the reviewer's only way to the files was raw.githubusercontent, which answers 404 for a private repository."""
    seen = {}
    for name, private in (("private", True), ("public", False)):
        st = _settings(tmp_path / name)
        p = _product(st)
        p.raw["repo"] = {**(p.raw.get("repo") or {}), "url": "https://github.com/example/fleets.git", "private": private}
        p.raw["host"] = "fleets.example"                              # a product with a live site: not `repo_only` by itself
        _commit(p.repo_dir, "public/index.html", "<h1>ranking</h1>\n", "T: page")
        tid = _released(p)
        s = Store(p.dir)
        t = s.task(tid)
        t["results"][-1]["how_to_verify"] = ["open https://fleets.example/ranking"]
        s.save_task(t)
        s.close()
        r = LaneRunner()
        cyc = Cycle(st, p, r, "c1")
        assert p.repo_private is private
        cyc.review_task(cyc.store.task(tid))
        cyc.store.close()
        seen[name] = (r.prompts["reviewer"][0], r.cwds["reviewer"][0])
    assert "is PRIVATE" in seen["private"][0] and "# READ-ONLY CHECKOUT" in seen["private"][0] and seen["private"][1].name == "checkout"
    assert "is PRIVATE" not in seen["public"][0] and "# READ-ONLY CHECKOUT" not in seen["public"][0]
