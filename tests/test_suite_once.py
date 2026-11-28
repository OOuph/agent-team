"""`cycle.review_tests.once_per_batch`: the test suite runs once for a batch of merged tasks (owner 27.11: «приоритетно
выполним все работы по оптимизации фреймворка»).

ai-battles 27.11: ~360 runs of the unit suite a day, each ~1000 CPU seconds — half of an 8-core server that stood at a
load of 28–31. About 113 of them were the same suite run again before every review. With the switch: one run on the head
of the main branch; a review of a task whose commit is in that head takes its result. A red head: every task is tested on
its own commit, as before, and the review of the one that broke the suite sends it back."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from team import config as C
from team import gitops
from team.cycle import Cycle
from team.store import Store

from cycle_invariants import SETTINGS, run_cycle, violations
from test_parallel_lanes import FAIL, PASS, LaneRunner, _commit, _events, _product, _settings, _state

ON = {"cycle": {"review_tests": {"once_per_batch": True}}}
OFF = {"cycle": {"review_tests": {"once_per_batch": False}}}
LONG = "The change is in the repository and its tests; every acceptance criterion is listed with the command that shows it, one by one."
BOTH = pytest.mark.parametrize("name", sorted(SETTINGS))


def _setup(tmp_path: Path, over: dict, monkeypatch, n: int = 3, broken_at: int | None = None):
    """A product whose «suite» writes a line into a file every time it runs and fails when the file BROKEN is in the
    checkout; `n` tasks, each merged as its own commit; task number `broken_at` adds BROKEN."""
    count = tmp_path / "suite-runs.txt"
    monkeypatch.setenv("SUITE_COUNT_FILE", str(count))
    st = _settings(tmp_path, over)
    p = _product(st, extra="repo_test_cmd_placeholder: 1\n")
    raw = (p.dir / "product.yaml").read_text(encoding="utf-8").replace("repo_test_cmd_placeholder: 1\n", "").replace(
        "  kind: service\n", "  kind: service\n  test_cmd: 'git rev-parse HEAD >> \"$SUITE_COUNT_FILE\"; test ! -f BROKEN'\n")
    (p.dir / "product.yaml").write_text(raw, encoding="utf-8")
    p = C.load_product(st, p.dir)
    assert p.test_cmd
    s = Store(p.dir)
    ids, shas = [], []
    for i in range(n):
        _commit(p.repo_dir, "BROKEN" if broken_at == i else f"src/part{i}.ts", f"part {i}\n", f"task {i}")
        sha = gitops.head(p.repo_dir)
        t = s.create_task(title=f"merged task {i}", type_="defect", problem="p", scope="src/", acceptance=["works"], goal="G1", created_by="po",
                          role="developer", state="ready")
        s.transition(t["id"], "released", "dispatcher", "merged", force=True, released_at="2020-01-01T00:00:00+00:00", released_sha=sha,
                     results=[{"status": "done", "summary": LONG}])
        ids.append(t["id"]); shas.append(sha)
    s.close()
    return st, p, ids, shas, count


def _suite_runs(count: Path) -> list[str]:
    return count.read_text(encoding="utf-8").split() if count.exists() else []


def _honest_reviewer(r: LaneRunner):
    """A reviewer that reads the test report in its request: a failing suite is a failed review."""
    def work(cwd: Path, n: int):
        prompt = r.prompts["reviewer"][n - 1]
        return dict(FAIL) if "exit code: 1" in prompt else dict(PASS)
    r.work["reviewer"] = work


def test_off_every_review_runs_the_suite(tmp_path: Path, monkeypatch):
    st, p, ids, shas, count = _setup(tmp_path, OFF, monkeypatch)
    r = LaneRunner(); _honest_reviewer(r)
    cyc = Cycle(st, p, r, "c1"); run_cycle(cyc, ["review"]); cyc.store.close()
    assert sorted(_suite_runs(count)) == sorted(shas) and all(_state(p, t)["state"] == "accepted" for t in ids)
    assert _events(p, "review.suite_run") == []


@BOTH
def test_a_green_batch_one_run_for_all_its_reviews(tmp_path: Path, monkeypatch, name: str):
    st, p, ids, shas, count = _setup(tmp_path, C._merge(SETTINGS[name], ON), monkeypatch)
    r = LaneRunner(); _honest_reviewer(r)
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, ["review"])
    cyc.store.close()
    assert violations(cyc, p, res, r) == []
    assert _suite_runs(count) == [shas[-1]]                            # one run, on the head
    assert all(_state(p, t)["state"] == "accepted" for t in ids)
    assert len(_events(p, "review.suite_run")) == 1 and sorted(e["subject"] for e in _events(p, "review.suite_reused")) == sorted(ids)
    assert all("the suite was run once on commit" in x and "exit code: 0" in x for x in r.prompts["reviewer"])
    # the next cycle of the chain, the same head: a fourth task is reviewed without a new run
    s = Store(p.dir)
    t = s.create_task(title="one more on the same head", type_="defect", problem="p", scope="src/", acceptance=["works"], goal="G1", created_by="po",
                      role="developer", state="ready")
    s.transition(t["id"], "released", "dispatcher", "merged", force=True, released_at="2020-01-01T00:00:00+00:00", released_sha=shas[1],
                 results=[{"status": "done", "summary": LONG}])
    s.close()
    cyc2 = Cycle(st, p, r, "c2"); run_cycle(cyc2, ["review"]); cyc2.store.close()
    assert _suite_runs(count) == [shas[-1]] and _state(p, t["id"])["state"] == "accepted"


@BOTH
def test_a_red_batch_every_task_is_tested_on_its_own_commit_and_the_culprit_goes_back(tmp_path: Path, monkeypatch, name: str):
    st, p, ids, shas, count = _setup(tmp_path, C._merge(SETTINGS[name], ON), monkeypatch, n=3, broken_at=1)
    r = LaneRunner(); _honest_reviewer(r)
    cyc = Cycle(st, p, r, "c1")
    res = run_cycle(cyc, ["review"])
    cyc.store.close()
    assert violations(cyc, p, res, r) == []
    runs = _suite_runs(count)
    assert runs.count(shas[-1]) == 2 and sorted(set(runs)) == sorted(shas) and len(runs) == 4      # the head once (red), then each task's own commit
    assert _state(p, ids[0])["state"] == "accepted"                    # merged before the break: green on its own commit
    assert _state(p, ids[1])["state"] == "in_progress" and _state(p, ids[1])["review_fails"] == 1      # the one that broke the suite goes back
    assert _state(p, ids[2])["state"] == "in_progress"                 # merged on top of the break: red on its own commit as well
    assert len(_events(p, "review.suite_run")) == 1 and _events(p, "review.suite_run")[0]["detail"]["passed"] is False
    assert sorted(e["subject"] for e in _events(p, "review.suite_red")) == sorted(ids)
    s = Store(p.dir)
    red = [o for o in s.observations() if str(o.get("title", "")).startswith("The test suite is red on the main branch")]
    s.close()
    assert len(red) == 1                                               # the PO is told once


def test_a_commit_that_is_not_in_the_head_gets_its_own_run(tmp_path: Path, monkeypatch):
    st, p, ids, shas, count = _setup(tmp_path, ON, monkeypatch, n=2)
    gitops.git(p.repo_dir, "reset", "-q", "--hard", shas[0])           # the head is the first task's commit; the second's is not in it
    r = LaneRunner(); _honest_reviewer(r)
    cyc = Cycle(st, p, r, "c1"); run_cycle(cyc, ["review"]); cyc.store.close()
    assert sorted(_suite_runs(count)) == sorted(shas)                  # the head for the first, its own commit for the second


def test_a_green_run_on_an_earlier_commit_stands_after_the_head_has_moved(tmp_path: Path, monkeypatch):
    """28.11, the first live cycle with the switch: the second review found the head moved by a merge and ran the whole
    suite again (692 s), though a green run on a commit that held its task was fifteen minutes old."""
    st, p, ids, shas, count = _setup(tmp_path, ON, monkeypatch)
    r = LaneRunner(); _honest_reviewer(r)
    cyc = Cycle(st, p, r, "c1"); run_cycle(cyc, ["review"]); cyc.store.close()
    assert _suite_runs(count) == [shas[-1]]
    _commit(p.repo_dir, "src/later.ts", "merged later\n", "another task merged meanwhile")      # the head moves
    s = Store(p.dir)
    old = s.create_task(title="its commit is in the green run", type_="defect", problem="p", scope="src/", acceptance=["works"], goal="G1",
                        created_by="po", role="developer", state="ready")
    s.transition(old["id"], "released", "dispatcher", "merged", force=True, released_at="2020-01-01T00:00:00+00:00", released_sha=shas[0],
                 results=[{"status": "done", "summary": LONG}])
    new = s.create_task(title="its commit is the new head", type_="defect", problem="p", scope="src/", acceptance=["works"], goal="G1",
                        created_by="po", role="developer", state="ready")
    s.transition(new["id"], "released", "dispatcher", "merged", force=True, released_at="2020-01-01T00:00:00+00:00",
                 released_sha=gitops.head(p.repo_dir), results=[{"status": "done", "summary": LONG}])
    s.close()
    cyc2 = Cycle(st, p, r, "c2"); run_cycle(cyc2, ["review"]); cyc2.store.close()
    runs = _suite_runs(count)
    assert runs == [shas[-1], gitops.head(p.repo_dir)]                 # one new run, for the task the old one does not contain
    assert _state(p, old["id"])["state"] == "accepted" and _state(p, new["id"])["state"] == "accepted"
    earlier = [e for e in _events(p, "review.suite_reused") if e["detail"].get("earlier")]
    assert old["id"] in [e["subject"] for e in earlier] and new["id"] not in [e["subject"] for e in earlier]



def test_the_tasks_unit_log_holds_the_whole_output_of_the_shared_run(tmp_path: Path, monkeypatch):
    """27.11, Q-0273 (review of T-0671): with the shared run the task's unit log got only the tail of the output that goes
    into the request, so `team-play unit --grep` could not find the lines of the new tests."""
    from team import cycle as cycle_mod
    monkeypatch.setattr(cycle_mod, "REVIEW_TEST_OUTPUT_CHARS", 200)                    # the request shows a short tail
    count = tmp_path / "suite-runs.txt"
    monkeypatch.setenv("SUITE_COUNT_FILE", str(count))
    st = _settings(tmp_path, ON)
    p = _product(st)
    raw = (p.dir / "product.yaml").read_text(encoding="utf-8").replace(
        "  kind: service\n", "  kind: service\n  test_cmd: 'echo FIRST-LINE-new-test-passes; for i in $(seq 1 400); do echo filler line $i; done; echo LAST-LINE'\n")
    (p.dir / "product.yaml").write_text(raw, encoding="utf-8")
    p = C.load_product(st, p.dir)
    _commit(p.repo_dir, "src/part.ts", "part\n", "task")
    s = Store(p.dir)
    t = s.create_task(title="merged task", type_="defect", problem="p", scope="src/", acceptance=["works"], goal="G1", created_by="po",
                      role="developer", state="ready")
    s.transition(t["id"], "released", "dispatcher", "merged", force=True, released_at="2020-01-01T00:00:00+00:00",
                 released_sha=gitops.head(p.repo_dir), results=[{"status": "done", "summary": LONG}])
    s.close()
    r = LaneRunner(); _honest_reviewer(r)
    cyc = Cycle(st, p, r, "c1"); run_cycle(cyc, ["review"]); cyc.store.close()
    assert "filler line 5\n" not in r.prompts["reviewer"][0] and "filler line 400" in r.prompts["reviewer"][0]      # the request: the tail
    unit_log = (p.dir / "reviews" / t["id"] / cycle_mod.UNIT_LOG_NAME).read_text(encoding="utf-8")
    assert "\nFIRST-LINE-new-test-passes\n" in unit_log and "filler line 5\n" in unit_log and "LAST-LINE" in unit_log   # the log: all of it


def test_the_dispatchers_own_test_run_takes_a_place_among_the_heavy_runs(tmp_path: Path, monkeypatch):
    """28.11: the roles' test runs were to be limited to a few at once — the dispatcher's runs before a review count in the
    same total, or the limit is not one."""
    from team import heavy
    over = C._merge(ON, {"cycle": {"heavy_runs": {"queue": ["Bash(npm test:*)"], "slots": 1}}})
    st, p, ids, shas, count = _setup(tmp_path, over, monkeypatch, n=1)
    held = heavy.take_slot(str(st.data_dir / "locks" / "heavy.lock"), 1, 5)         # a role's test run holds the only place
    assert held is not None
    seen = {}
    real = heavy.take_slot

    def spy(lock, slots, wait_s, on_wait=None):
        seen["asked"] = (Path(lock).name, slots)
        held.close()                                                   # the role's run ends while the dispatcher waits
        return real(lock, slots, wait_s, on_wait)
    monkeypatch.setattr(heavy, "take_slot", spy)
    r = LaneRunner(); _honest_reviewer(r)
    cyc = Cycle(st, p, r, "c1"); run_cycle(cyc, ["review"]); cyc.store.close()
    assert seen["asked"] == ("heavy.lock", 1) and _suite_runs(count) == [shas[-1]] and _state(p, ids[0])["state"] == "accepted"
    again = heavy.take_slot(str(st.data_dir / "locks" / "heavy.lock"), 1, 5)        # … and gave the place back
    assert again is not None
    again.close()
