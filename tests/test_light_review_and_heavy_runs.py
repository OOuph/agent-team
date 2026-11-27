"""Two rules about the load of the team's server (27.11, an 8-core server at a load of 28–31):

`cycle.review_tests.docs_outside_slots` — the review of a commit that changes documents only is a 12-second read; it
waited 19 minutes for a run slot behind reviews that build and test. It goes beside the lanes.

`cycle.heavy_runs` — a load test or a simulation of hundreds of battles started inside a developer's run takes the
processor from every other run. The commands are denied to the run and the role is told to hand the run to the operator."""
from __future__ import annotations

import threading
from pathlib import Path

from team import config as C
from team import gitops, lanes
from team.cycle import Cycle
from team.runner import RunResult
from team.store import Store

from cycle_invariants import game_like, run_cycle
from test_parallel_lanes import PASS, WAIT_S, LaneRunner, _commit, _events, _product, _settings, _state

LONG = "The change is in the repository and its tests; every acceptance criterion is listed with the command that shows it, one by one."
DOCS = {"cycle": {"review_tests": {"skip_for_docs": True, "docs_outside_slots": True}}}


def _merged(p, path: str) -> str:
    """A task merged the way the dispatcher merges: its change on a branch, a merge commit on the main branch."""
    repo = p.repo_dir
    main = gitops.git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    gitops.git(repo, "checkout", "-q", "-b", f"task-{path.replace('/', '-')}")
    _commit(repo, path, "text\n", f"change {path}")
    gitops.git(repo, "checkout", "-q", main)
    gitops.git(repo, "merge", "-q", "--no-ff", "-m", f"Merge {path}", f"task-{path.replace('/', '-')}")
    sha = gitops.head(repo)
    s = Store(p.dir)
    t = s.create_task(title=f"merged {path}", type_="design", problem="p", scope="docs/", acceptance=["reads well"], goal="G1", created_by="po",
                      role="designer", state="ready")
    s.transition(t["id"], "released", "dispatcher", "merged", force=True, released_at="2020-01-01T00:00:00+00:00", released_sha=sha,
                 results=[{"status": "done", "summary": LONG}])
    s.close()
    return t["id"]


def test_which_review_is_light(tmp_path: Path):
    st = _settings(tmp_path, DOCS)
    p = _product(st)
    doc, code = _merged(p, "design/gdd/tower.md"), _merged(p, "src/tower.ts")
    cyc = Cycle(st, p, LaneRunner(), "c1")
    assert cyc.light_review(cyc.store.task(doc)) is True and cyc.light_review(cyc.store.task(code)) is False
    cyc.store.close()
    st2 = _settings(tmp_path / "off", {"cycle": {"review_tests": {"skip_for_docs": True}}})
    p2 = _product(st2)
    doc2 = _merged(p2, "design/gdd/tower.md")
    off = Cycle(st2, p2, LaneRunner(), "c1")
    assert off.light_review(off.store.task(doc2)) is False             # the switch is off: a slot, as before
    off.store.close()


def test_a_lane_outside_the_slots_runs_while_every_slot_is_taken():
    ls = lanes.Lanes(lanes.settings({"parallel": {"enabled": True, "max_runs": 1}}, {}, "t"))
    release, busy, got = threading.Event(), threading.Event(), threading.Event()
    ls.submit("execute", lanes.LANE_EXECUTE, "exec", lambda: (busy.set(), release.wait(WAIT_S)))
    assert busy.wait(WAIT_S)
    ls.submit("review", lanes.LANE_REVIEW, "review-doc", got.set, outside=True)
    assert got.wait(2.0)                                               # the only slot is held, the light lane ran all the same
    waits = threading.Event()
    ls.submit("review", lanes.LANE_REVIEW, "review-code", waits.set)
    assert not waits.wait(0.5)                                         # an ordinary review waits for the slot
    release.set()
    assert ls.join() == [] and waits.is_set() and ls.gate.at_work() == 0


def test_the_review_of_a_document_does_not_wait_for_the_seats(tmp_path: Path):
    """The whole cycle on the game's settings: every executor slot is held by long runs, and the document is accepted
    before the first of them ends."""
    tight = {"cycle": {"parallel": {"max_runs": 3, "review_max_runs": 1, "review_reserve": 0}}}     # three developer seats take all three slots
    st = _settings(tmp_path, C._merge(C._merge(game_like(), DOCS), tight))
    p = _product(st)
    doc = _merged(p, "design/gdd/tower.md")
    from test_parallel_lanes import _task
    for i in range(6):
        _task(p, "developer", f"dev {i}")
    r = LaneRunner()
    done_at: dict[str, float] = {}
    import time

    def dev(cwd: Path, n: int):
        time.sleep(1.5)
        _commit(cwd, f"src/part-{cwd.name}.ts", "part\n")

    def rev(cwd: Path, n: int):
        done_at.setdefault("review", time.monotonic())                # the first review: the document's (later ones are of the tasks merged in the cycle)
        return dict(PASS)
    r.work.update({"developer": dev, "reviewer": rev})
    cyc = Cycle(st, p, r, "c1")
    run_cycle(cyc, ["review", "execute"])
    cyc.store.close()
    first_dev_end = min(b for role, a, b in r.spans if role == "developer")
    assert _state(p, doc)["state"] == "accepted" and done_at["review"] < first_dev_end
    assert len(_events(p, "review.tests_skipped")) == 1


class Capture:
    def __init__(self):
        self.kw, self.prompts = [], []

    def run(self, role, prompt, schema, **kw):
        self.kw.append(kw); self.prompts.append(prompt)
        return RunResult(True, {"summary_md": "x"}, {"total_cost_usd": 0.1}, 0.1, 1, None, session_id="s")


def test_heavy_runs_are_denied_to_the_named_roles_and_the_role_is_told_what_to_do(tmp_path: Path):
    deny = ["Bash(npx tsx tests/load/*)", "Bash(npm run sim:*)"]
    st = _settings(tmp_path, {"cycle": {"heavy_runs": {"deny": deny, "roles": ["developer", "game-designer"]}}})
    p = _product(st)
    assert p.heavy_runs_cfg == {"deny": deny, "roles": ["developer", "game-designer"]}
    r = Capture()
    cyc = Cycle(st, p, r, "c1")
    cyc.role_run("developer", "do the task", None, cwd=tmp_path / "w", label="dev")
    cyc.role_run("reviewer", "read it", None, cwd=tmp_path / "w", label="rev")
    cyc.store.close()
    assert r.kw[0]["disallowed_tools"] == deny and "HEAVY RUNS ARE NOT STARTED FROM THIS RUN" in r.prompts[0]
    assert "`Bash(npx tsx tests/load/*)`" in r.prompts[0] and "the operator starts it" in r.prompts[0]
    assert "disallowed_tools" not in r.kw[1] and "HEAVY RUNS" not in r.prompts[1]      # not a role the rule names


def test_heavy_runs_off_by_default_and_bad_values_are_ignored(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    assert C.HEAVY_RUNS_DEFAULTS == {"deny": [], "roles": []} and p.heavy_runs_cfg == {"deny": [], "roles": []}
    r = Capture()
    cyc = Cycle(st, p, r, "c1")
    cyc.role_run("developer", "do the task", None, cwd=tmp_path / "w", label="dev")
    cyc.store.close()
    assert "disallowed_tools" not in r.kw[0] and "HEAVY RUNS" not in r.prompts[0]
    p2 = _product(_settings(tmp_path / "b", {"cycle": {"heavy_runs": {"deny": "everything", "roles": [1, 2]}}}))
    assert p2.heavy_runs_cfg == {"deny": [], "roles": []}
