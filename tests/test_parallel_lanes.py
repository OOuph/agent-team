"""Parallel lanes inside one product's cycle and one checkout per task (owner 21.11: «the absence of parallel work is a
huge blocker»; frictions F21/F29 of the ai-battles launch; team/lanes.py, SCHEME §3 «Параллельные дорожки»).

Measured on the first game (cycle 21.11 14:07 UTC): reviews 13 min, then the game designer 8 min, and only then the
developer got its P0 task — 22 minutes of waiting on an idle 4-core server. Here: the reviews run beside triage and
execution, every seat is its own lane, the developer and the designer work in two worktrees and both merge, a merge
conflict goes back to its author, the store loses nothing, the daily cap holds, a stopped run continues in its own
worktree, the layout before the worktrees is taken over, and `cycle.parallel.enabled: false` is the old sequential cycle.

Overlap is proved without a stopwatch wherever possible: a run of the fake runner waits until the other runs it must
overlap have started (with a generous timeout), so a sequential executor would hang there instead of passing slowly.
FakeRunner only — no network, no models."""
from __future__ import annotations

import copy
import logging
import os
import threading
import time
from pathlib import Path

import pytest

from team import cli, gitops, lanes, playtest, servicedeploy
from team import config as C
from team import cycle as cycle_mod
from team.cycle import INTERRUPTED, MERGE_CONFLICT, Cycle
from team.runner import RunResult
from team.store import Store

WAIT_S = 20.0            # how long a fake run waits for the runs it must overlap; a sequential executor fails here, not by timing
SLOTS = {"developer": 1, "designer": 1, "game-designer": 1, "analyst": 1, "marketer": 1}


# ------------------------------------------------------------------ fixtures

def _settings(tmp_path: Path, over: dict | None = None) -> C.Settings:
    base = copy.deepcopy(C.DEFAULTS)
    cyc = {"review_after_min": 0, "persona_every_h": 0, "digest_hour_utc": 99, "execute_slots": dict(SLOTS)}
    raw = C._merge(base, C._merge({"cycle": cyc}, over or {}))
    return C.Settings(raw, tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")


def _sequential(tmp_path: Path, over: dict | None = None) -> C.Settings:
    return _settings(tmp_path, C._merge({"cycle": {"parallel": {"enabled": False}}}, over or {}))


def _product(st: C.Settings, slug: str = "fleets", extra: str = "") -> C.Product:
    d = st.products_dir / slug
    d.mkdir(parents=True)
    (d / "product.yaml").write_text(f"slug: {slug}\nname: {slug}\nhost: ''\nrepo:\n  url: ''\n  branch: main\n  kind: service\n{extra}", encoding="utf-8")
    (d / "VISION.md").write_text("# VISION\n\nGoals: G1.\n", encoding="utf-8")
    p = C.load_product(st, d)
    repo = gitops.init_local(p.repo_dir)
    (repo / "README.md").write_text("line one\nline two\nline three\n", encoding="utf-8")
    (repo / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    gitops.git(repo, "add", "-A"); gitops.git(repo, "commit", "-q", "-m", "init")
    return p


def _task(p: C.Product, role: str = "developer", title: str = "", state: str = "ready", **kw) -> str:
    s = Store(p.dir)
    t = s.create_task(title=title or f"{role} task", type_="design" if role != "developer" else "defect", problem="p", scope="src/",
                      acceptance=["works"], goal="G1", created_by="po", role=role, state=state, **kw)
    s.close()
    return t["id"]


def _released(p: C.Product, role: str = "developer") -> str:
    s = Store(p.dir)
    t = s.create_task(title=f"released {role} task", type_="defect", problem="p", scope="src/", acceptance=["works"], goal="G1",
                      created_by="po", role=role, state="ready")
    s.transition(t["id"], "released", "dispatcher", "merged", force=True, released_at="2020-01-01T00:00:00+00:00", released_sha=gitops.head(p.repo_dir))
    s.add_result(t["id"], {"run_id": "r", "status": "done", "summary": "done", "changes": ["README.md"], "how_to_verify": [], "self_check": []})
    s.close()
    return t["id"]


def _commit(cwd: Path, rel: str, text: str, msg: str = "work") -> None:
    f = cwd / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    gitops.git(cwd, "add", "-A"); gitops.git(cwd, "commit", "-q", "-m", msg)


DONE = {"status": "done", "summary": "done", "changes": ["x"], "how_to_verify": ["x"], "self_check": [{"criterion": "works", "met": True, "evidence": "seen"}]}
PASS = {"verdict": "pass", "steps": [], "criteria": [{"criterion": "works", "met": True, "evidence": "read"}], "summary": "read. fine."}
FAIL = {"verdict": "fail", "steps": [], "criteria": [{"criterion": "works", "met": False, "evidence": "no"}], "first_failure": "it does not work", "summary": "no. broken."}
PO_NOTHING = {"decisions": [], "answers": [], "owner_questions": [], "no_new_tasks": True, "digest_md": "ok", "ready_tasks": []}


class LaneRunner:
    """A fake runner that knows who runs beside whom. `sleep`: seconds every run takes. `wait_peak`: a run does not
    return before that many runs were active at once. `wait_roles[role]`: a run of `role` does not return before every
    role in the set has started a run. `work[role](cwd, n)`: what the n-th run of the role does in its checkout (default:
    commit one file); it may return a RunResult to replace the answer. `cost`: None = a run spends its whole cap."""

    def __init__(self, sleep: float = 0.0, wait_peak: int = 0, wait_roles: dict[str, set[str]] | None = None, cost: float | None = 0.1):
        self.sleep, self.wait_peak, self.wait_roles, self.cost = sleep, wait_peak, wait_roles or {}, cost
        self.cv = threading.Condition()
        self.active, self.peak, self.waited_out = 0, 0, False
        self.calls: list[str] = []
        self.spans: list[tuple[str, float, float]] = []          # role, start, end (monotonic)
        self.cwds: dict[str, list[Path]] = {}
        self.prompts: dict[str, list[str]] = {}
        self.caps: list[float] = []
        self.env: dict[str, list[dict]] = {}
        self.threads: set[str] = set()
        self.work: dict[str, object] = {}
        self.answers: dict[str, dict] = {}

    def run(self, role, prompt, schema, *, max_usd, cwd, **kw):
        t0 = time.monotonic()
        with self.cv:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls.append(role)
            n = self.calls.count(role)
            self.cwds.setdefault(role, []).append(Path(cwd))
            self.prompts.setdefault(role, []).append(prompt)
            self.caps.append(float(max_usd))
            self.env.setdefault(role, []).append(dict(kw.get("env_extra") or {}))
            self.threads.add(threading.current_thread().name)
            self.cv.notify_all()
        try:
            with self.cv:
                if self.wait_peak and not self.cv.wait_for(lambda: self.peak >= self.wait_peak, timeout=WAIT_S):
                    self.waited_out = True
                need = self.wait_roles.get(role)
                if need and not self.cv.wait_for(lambda: need <= set(self.calls), timeout=WAIT_S):
                    self.waited_out = True
            if self.sleep:
                time.sleep(self.sleep)
            res = self.answer(role, Path(cwd), n)
        finally:
            with self.cv:
                self.active -= 1
                self.spans.append((role, t0, time.monotonic()))
                self.cv.notify_all()
        if isinstance(res, RunResult):
            return res
        cost = float(max_usd) if self.cost is None else self.cost
        return RunResult(True, res, {"total_cost_usd": cost}, cost, 2, None, session_id="s", duration_s=1.0)

    def answer(self, role: str, cwd: Path, n: int):
        if role in self.work:
            res = self.work[role](cwd, n)
            if res is not None:
                return res
        elif role in ("developer", "designer"):
            _commit(cwd, "DESIGN.md" if role == "designer" else "src/server.ts", f"{role} work {n}\n", f"{role}: work")
        if role in self.answers:
            return dict(self.answers[role])
        if role in ("developer", "designer"):
            return dict(DONE)
        if role == "game-designer":
            return {"summary": "rules v1", "files": [{"path": "GAME-DESIGN.md", "content": "# Game design\n\nRules v1.\n"}], "balance_changes": [],
                    "proposed_tasks": [], "open_questions": []}
        if role in ("reviewer", "tester"):
            return dict(PASS)
        if role == "po":
            return dict(PO_NOTHING)
        return {"summary_md": "n/a " * 40, "observations": [], "task_effects": []}

    def span(self, role: str) -> tuple[float, float]:
        return next((a, b) for r, a, b in self.spans if r == role)

    def overlap(self, a: str, b: str) -> bool:
        (a0, a1), (b0, b1) = self.span(a), self.span(b)
        return a0 < b1 and b0 < a1


def _killed(limit: int = 2700) -> RunResult:
    return RunResult(False, None, None, None, 0, f"timeout after {limit}s", duration_s=float(limit), killed="timeout")


def _state(p: C.Product, tid: str) -> dict:
    s = Store(p.dir)
    try:
        return s.task(tid)
    finally:
        s.close()


def _events(p: C.Product, kind: str) -> list[dict]:
    s = Store(p.dir)
    try:
        return [e for e in s.events() if e["kind"] == kind]
    finally:
        s.close()


def _main_files(p: C.Product) -> set[str]:
    return set(gitops.git(p.repo_dir, "ls-tree", "-r", "--name-only", "main").splitlines())


# ------------------------------------------------------------------ 1. the lanes overlap; `enabled: false` does not

def test_three_roles_run_at_once_and_wall_time_is_clearly_below_the_sum(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    ids = [_task(p, "developer"), _task(p, "designer"), _task(p, "game-designer")]
    r = LaneRunner(sleep=1.0, wait_peak=3)                 # no run returns before all three are in flight together
    cyc = Cycle(st, p, r, "c1")
    res = cyc.run(["execute"])
    cyc.store.close()
    assert not r.waited_out and r.peak == 3 and sorted(r.calls) == ["designer", "developer", "game-designer"]
    assert res == {"phases": ["execute"], "runs": 3, "spent_usd": pytest.approx(0.3)} and cyc.lanes.gate.peak == 3
    window = max(b for _, _, b in r.spans) - min(a for _, a, _ in r.spans)
    total = sum(b - a for _, a, b in r.spans)
    assert window < 0.8 * total, (window, total)           # the wall time of the three runs is clearly below their sum
    assert all(n.startswith("lane-fleets") for n in r.threads) and len(r.threads) == 3
    for tid in ids:
        assert _state(p, tid)["state"] == "released", tid
    assert {"src/server.ts", "DESIGN.md", "GAME-DESIGN.md"} <= _main_files(p)
    assert threading.active_count() == 1 or not [t for t in threading.enumerate() if t.name.startswith("lane-")]     # no lane thread outlives the cycle


def test_parallel_disabled_is_the_strictly_sequential_cycle(tmp_path: Path):
    st = _sequential(tmp_path)
    p = _product(st)
    ids = [_task(p, "developer"), _task(p, "designer"), _task(p, "game-designer")]
    r = LaneRunner(sleep=0.2)
    cyc = Cycle(st, p, r, "c1")
    assert not cyc.par.enabled and "sequential" in cyc.par.describe()
    res = cyc.run(["execute"])
    cyc.store.close()
    assert r.peak == 1 and r.calls == ["developer", "designer", "game-designer"]          # the order of the backlog, one after another
    assert r.threads == {threading.main_thread().name} and cyc.lanes.started == 0 and cyc.lanes.gate.peak == 0
    spans = sorted((a, b) for _, a, b in r.spans)
    assert all(spans[i][1] <= spans[i + 1][0] for i in range(len(spans) - 1))             # no two runs overlap
    window = spans[-1][1] - spans[0][0]
    assert window >= sum(b - a for a, b in spans)                                         # … so the wall time is at least the sum
    assert res["phases"] == ["execute"] and res["runs"] == 3
    for tid in ids:
        assert _state(p, tid)["state"] == "released", tid
    assert {"src/server.ts", "DESIGN.md", "GAME-DESIGN.md"} <= _main_files(p)             # the same result as the parallel cycle


def test_reviews_run_beside_triage_and_execution(tmp_path: Path):
    """The measured problem: the developer waited for every review. Here the PO's run does not return before the
    reviewer's has started, and the reviewer's does not return before the developer's has started: the review is in
    flight through the triage and into execution. A sequential executor would hang on the second wait."""
    st = _settings(tmp_path)
    p = _product(st)
    rel, dev = _released(p), _task(p, "developer")
    s = Store(p.dir)
    s.add_observation({"title": "status text overlaps", "classification": "ux_friction", "source": "tester", "fingerprint": "f-1"})    # wakes the PO
    s.close()
    r = LaneRunner(wait_roles={"po": {"reviewer"}, "reviewer": {"developer"}})
    cyc = Cycle(st, p, r, "c1")
    res = cyc.run(["review", "triage", "execute"])
    cyc.store.close()
    assert not r.waited_out and sorted(r.calls) == ["developer", "po", "reviewer"]
    assert r.overlap("reviewer", "po") and r.overlap("reviewer", "developer") and r.calls.index("po") < r.calls.index("developer")
    assert res["phases"] == ["review", "triage", "execute"] and res["runs"] == 3
    assert _state(p, rel)["state"] == "accepted" and _state(p, dev)["state"] == "released"
    # the same cycle, sequential: review, then triage, then execution
    st2 = _sequential(tmp_path / "seq")
    p2 = _product(st2)
    rel2, dev2 = _released(p2), _task(p2, "developer")
    s = Store(p2.dir)
    s.add_observation({"title": "status text overlaps", "classification": "ux_friction", "source": "tester", "fingerprint": "f-1"})
    s.close()
    r2 = LaneRunner()
    cyc2 = Cycle(st2, p2, r2, "c1")
    cyc2.run(["review", "triage", "execute"])
    cyc2.store.close()
    assert r2.calls == ["reviewer", "po", "developer"] and r2.peak == 1
    assert _state(p2, rel2)["state"] == "accepted" and _state(p2, dev2)["state"] == "released"


def test_a_task_the_review_sent_back_is_started_in_the_same_cycle_when_its_seat_is_free(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    rel = _released(p)
    r = LaneRunner()
    r.answers["reviewer"] = FAIL
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["review", "execute"])
    cyc.store.close()
    assert r.calls == ["reviewer", "developer"]            # the second pass of the execute phase picked it up
    t = _state(p, rel)
    assert t["state"] == "released" and t["review_fails"] == 1 and "REVIEW THAT FAILED" in r.prompts["developer"][0]


def test_a_dependency_finished_by_another_lane_is_started_in_the_same_cycle(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    (p.dir / "metrics").mkdir(exist_ok=True)
    an = _task(p, "analyst", "measure the lobby")
    dev = _task(p, "developer", "use the measurement", depends_on=[an])
    r = LaneRunner()
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["execute"])
    cyc.store.close()
    assert r.calls == ["analyst", "developer"] and _state(p, an)["state"] == "done" and _state(p, dev)["state"] == "released"


def test_tasks_of_one_role_run_one_after_another_unless_the_role_may_overlap(tmp_path: Path):
    # two developer seats, default settings: two tasks in one cycle, but one at a time (one lane per role)
    st = _settings(tmp_path / "a", {"cycle": {"execute_slots": {"developer": 2}}})
    p = _product(st)
    a, b = _task(p, "developer", "first"), _task(p, "developer", "second")
    r = LaneRunner(sleep=0.1)
    r.work["developer"] = lambda cwd, n: _commit(cwd, f"src/part{n}.ts", f"part {n}\n")
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["execute"])
    cyc.store.close()
    assert r.calls == ["developer", "developer"] and r.peak == 1
    assert _state(p, a)["state"] == _state(p, b)["state"] == "released" and {"src/part1.ts", "src/part2.ts"} <= _main_files(p)
    # the next step, not the default: `overlap_roles: [developer]` lets the two seats work at once — each task has its own
    # worktree, the merges are serialised, so nothing else is needed
    st = _settings(tmp_path / "b", {"cycle": {"execute_slots": {"developer": 2}, "parallel": {"overlap_roles": ["developer"]}}})
    p = _product(st)
    a, b, c = _task(p, "developer", "first"), _task(p, "developer", "second"), _task(p, "developer", "third")
    r = LaneRunner(wait_peak=2)
    r.work["developer"] = lambda cwd, n: _commit(cwd, f"src/part-{cwd.name}.ts", "part\n")
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["execute"])
    cyc.store.close()
    assert not r.waited_out and r.peak == 2 and r.calls == ["developer", "developer"]       # two seats: the third task waits
    assert len(set(r.cwds["developer"])) == 2
    assert _state(p, a)["state"] == _state(p, b)["state"] == "released" and _state(p, c)["state"] == "ready"
    assert {f"src/part-{a}.ts", f"src/part-{b}.ts"} <= _main_files(p)


def test_who_gets_picked_does_not_change(tmp_path: Path, monkeypatch):
    """One developer seat, one marketer seat: the P1 developer task and the post start, the P2 developer task waits —
    in both kinds of cycle; the sequential one also keeps the order."""
    for name, st in (("par", _settings(tmp_path / "par")), ("seq", _sequential(tmp_path / "seq"))):
        st.raw["cycle"]["execute_slots"] = {"developer": 1, "marketer": 1}
        p = _product(st)
        d1 = _task(p, "developer", "dev P1", priority="P1")
        _task(p, "developer", "dev P2", priority="P2")
        m = _task(p, "marketer", "post", priority="P3")
        _task(p, "designer", "no seat for this role")
        cy = Cycle(st, p, LaneRunner(), "c1")
        started: list[str] = []
        monkeypatch.setattr(cy, "execute_task", lambda t, started=started: started.append(t["id"]) or True)
        cy.phase_execute()
        cy.store.close()
        assert sorted(started) == sorted([d1, m]), name
        if name == "seq":
            assert started == [d1, m]


# ------------------------------------------------------------------ 2. one checkout per task

def test_developer_and_designer_work_in_two_worktrees_and_both_merge(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    dev, des = _task(p, "developer"), _task(p, "designer")
    seen: dict[str, dict] = {}

    def look(role):
        def work(cwd: Path, n: int):
            seen[role] = {"branch": gitops.current_branch(cwd), "clone_branch": gitops.current_branch(p.repo_dir),
                          "is_worktree": (cwd / ".git").is_file(), "readme": (cwd / "README.md").read_text(encoding="utf-8")}
            _commit(cwd, "DESIGN.md" if role == "designer" else "src/server.ts", f"{role} work\n", f"{role}: work")
        return work

    r = LaneRunner(wait_peak=2)
    r.work["developer"], r.work["designer"] = look("developer"), look("designer")
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["execute"])
    cyc.store.close()
    assert not r.waited_out and r.peak == 2
    wt_dev, wt_des = r.cwds["developer"][0], r.cwds["designer"][0]
    assert wt_dev == p.worktree_dir(dev) == st.data_dir / "work" / "fleets" / "wt" / dev and wt_des == p.worktree_dir(des) and wt_dev != wt_des
    assert seen["developer"] == {"branch": f"task/{dev}", "clone_branch": "main", "is_worktree": True, "readme": "line one\nline two\nline three\n"}
    assert seen["designer"]["branch"] == f"task/{des}" and seen["designer"]["is_worktree"]
    # the prompt names the task's own checkout and says what that means
    pr = r.prompts["developer"][0]
    assert f"`{wt_dev}` on branch `task/{dev}`" in pr and "the task's own git worktree" in pr and "do not use `git stash`" in pr
    # a service repository: each run is told a port of its own, so two servers started at once do not collide
    ports = {role: r.env[role][0]["TEAM_PORT"] for role in ("developer", "designer")}
    assert ports["developer"] != ports["designer"] and f"use port `{ports['developer']}`" in pr and f"PORT={ports['developer']}" in pr
    assert f"use port `{ports['designer']}`" in r.prompts["designer"][0]
    # both merged; the clone stands on the base branch with both changes; the checkouts and the branches are gone
    for tid in (dev, des):
        t = _state(p, tid)
        assert t["state"] == "released" and gitops.is_ancestor(p.repo_dir, t["results"][-1]["commit"], "main"), tid
    assert {"src/server.ts", "DESIGN.md"} <= _main_files(p) and gitops.current_branch(p.repo_dir) == "main" and not gitops.dirty(p.repo_dir)
    assert (p.repo_dir / "src" / "server.ts").exists() and (p.repo_dir / "DESIGN.md").exists()
    assert not wt_dev.exists() and not wt_des.exists() and list(gitops.worktrees(p.repo_dir)) == [p.repo_dir.resolve()]
    assert gitops.git(p.repo_dir, "branch", "--list", "task/*") == ""
    assert len(_events(p, "worktree.created")) == 2 and len(_events(p, "worktree.removed")) == 2
    assert gitops.git(p.repo_dir, "log", "--merges", "--format=%s", "main").count("released by the dispatcher") == 2


def test_a_merge_conflict_returns_the_second_task_to_its_author_without_crashing(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    dev, des = _task(p, "developer"), _task(p, "designer")
    r = LaneRunner(wait_peak=2)                             # both start from the same base and change the same line

    def change(who):
        def work(cwd: Path, n: int):
            if n == 1:
                _commit(cwd, "README.md", f"line one\nline two — {who}\nline three\n", f"{who}: line two")
                _commit(cwd, f"src/{who}.ts", f"{who}\n", f"{who}: own file")
            else:                                           # the reconciling run: both wordings, as the prompt asks
                _commit(cwd, "README.md", "line one\nline two — developer and designer\nline three\n", f"{who}: reconcile")
        return work

    r.work["developer"], r.work["designer"] = change("developer"), change("designer")
    cyc = Cycle(st, p, r, "c1")
    res = cyc.run(["execute"])
    cyc.store.close()
    assert not r.waited_out and res["phases"] == ["execute"] and not _events(p, "phase.error")           # the cycle did not crash
    states = {tid: _state(p, tid) for tid in (dev, des)}
    won = next(tid for tid, t in states.items() if t["state"] == "released")
    lost = next(tid for tid, t in states.items() if t["state"] == "in_progress")
    role = states[lost]["role"]
    other = "designer" if role == "developer" else "developer"
    t = states[lost]
    mc = t[MERGE_CONFLICT]
    assert mc["files"] == ["README.md"] and mc["count"] == 1 and mc["base"] == "main" and "CONFLICT (content)" in mc["text"]
    assert f"line two — {other}" in mc["text"]              # what the base branch did to those lines, for the author
    assert t["assignee_run"] is None and "merge conflict with main in README.md" in t["history"][-1]["note"] and not t.get("released_sha")
    # the base branch holds the winner only; the loser's work is intact on its branch, in its checkout
    assert f"line two — {other}" in (p.repo_dir / "README.md").read_text(encoding="utf-8") and not gitops.dirty(p.repo_dir)
    assert f"src/{other}.ts" in _main_files(p) and f"src/{role}.ts" not in _main_files(p)
    assert p.worktree_dir(lost).exists() and not p.worktree_dir(won).exists()
    assert gitops.git(p.repo_dir, "rev-list", "--count", f"main..task/{lost}") == "2"
    s = Store(p.dir)
    obs = [o for o in s.observations() if o.get("fingerprint") == f"merge-conflict-{lost}"]
    assert len(obs) == 1 and obs[0]["classification"] == "environment_error" and obs[0]["task"] == lost and "README.md" in obs[0]["title"]
    assert [e["detail"]["count"] for e in s.events() if e["kind"] == "merge.conflict"] == [1] and not s.questions(["open"])
    s.close()
    # next cycle: the author gets the conflict text; the dispatcher has NOT merged the base for it (F39: `-X ours` dropped the
    # other side without a trace) — the role merges the base itself, keeps both sides, and then the branch merges
    def reconcile(cwd, n):
        gitops.git(cwd, "merge", "--no-edit", "main", check=False)          # conflicts in README.md, left for the role
        _commit(cwd, "README.md", "line one\nline two — developer and designer\nline three\n", "reconcile: both sides kept")
    r2 = LaneRunner()
    r2.work[role] = reconcile
    cyc = Cycle(st, p, r2, "c2")
    cyc.run(["execute"])
    cyc.store.close()
    assert r2.calls == [role]
    pr = r2.prompts[role][0]
    assert "# MERGE CONFLICT — RECONCILE FIRST, DO NOT REDO THE TASK" in pr and "Conflicting files: `README.md`" in pr
    assert f"line two — {other}" in pr and "did NOT merge `main` into your branch" in pr and "git merge origin/main" in pr
    assert "kept YOUR side" not in pr
    t = _state(p, lost)
    assert t["state"] == "released" and not t.get(MERGE_CONFLICT)
    assert "line two — developer and designer" in (p.repo_dir / "README.md").read_text(encoding="utf-8")
    assert {"src/developer.ts", "src/designer.ts"} <= _main_files(p) and not p.worktree_dir(lost).exists()


def test_repeated_merge_conflicts_block_the_task_for_the_po(tmp_path: Path):
    st = _settings(tmp_path, {"cycle": {"parallel": {"max_merge_conflicts": 2}}})
    p = _product(st)
    dev = _task(p, "developer")
    r = LaneRunner()

    def work(cwd: Path, n: int):
        # the developer changes line two; before it reports, someone else's change of the same line lands on the base branch
        _commit(cwd, "README.md", f"line one\nline two — developer {n}\nline three\n", "developer: line two")
        _commit(p.repo_dir, "README.md", f"line one\nline two — owner {n}\nline three\n", "owner: line two")

    r.work["developer"] = work
    for i in (1, 2):
        cyc = Cycle(st, p, r, f"c{i}")
        assert cyc.run(["execute"])["phases"] == ["execute"]
        cyc.store.close()
    t = _state(p, dev)
    assert t["state"] == "blocked" and t[MERGE_CONFLICT]["count"] == 2 and "2 conflict(s) in a row" in t["history"][-1]["note"]
    s = Store(p.dir)
    q = s.questions(["open"])
    assert len(q) == 1 and q[0]["to"] == "po" and q[0]["blocks"] == [dev] and "conflicted with main 2 times in a row" in q[0]["question"]
    assert t["blocked_by"] == q[0]["id"] and "MERGE CONFLICT — RECONCILE FIRST" in r.prompts["developer"][1] and "owner 1" in r.prompts["developer"][1]
    assert len([o for o in s.observations() if o.get("fingerprint") == f"merge-conflict-{dev}"]) == 1       # one card, repeated
    s.close()


def test_a_game_designer_document_that_conflicts_is_written_again_on_the_new_base(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    _commit(p.repo_dir, "GAME-DESIGN.md", "# Game design\n\nRules v0.\n", "gdd v0")
    gd = _task(p, "game-designer")
    r = LaneRunner()

    def work(cwd: Path, n: int):
        if n == 1:                                          # while the role writes, another change of the document is merged
            _commit(p.repo_dir, "GAME-DESIGN.md", "# Game design\n\nRules v0, with the owner's note.\n", "owner: note")
        return None

    r.work["game-designer"] = work
    cyc = Cycle(st, p, r, "c1")
    assert cyc.run(["execute"])["phases"] == ["execute"]
    cyc.store.close()
    t = _state(p, gd)
    assert t["state"] == "ready" and t[MERGE_CONFLICT]["files"] == ["GAME-DESIGN.md"] and "runs again on the new main" in t["history"][-1]["note"]
    assert "owner's note" in (p.repo_dir / "GAME-DESIGN.md").read_text(encoding="utf-8")      # nothing was overwritten
    assert not p.worktree_dir(gd).exists() and gitops.git(p.repo_dir, "branch", "--list", "task/*") == ""
    cyc = Cycle(st, p, r, "c2")
    cyc.run(["execute"])
    cyc.store.close()
    assert "owner's note" in r.prompts["game-designer"][1]                                    # the second run read the new document
    t = _state(p, gd)
    assert t["state"] == "released" and not t.get(MERGE_CONFLICT) and "Rules v1." in (p.repo_dir / "GAME-DESIGN.md").read_text(encoding="utf-8")


def test_the_worktree_of_a_stopped_run_holds_the_wip_commit_and_the_next_cycle_continues_there(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    tid = _task(p, "developer")
    wt = p.worktree_dir(tid)
    seen: dict[str, object] = {}

    def work(cwd: Path, n: int):
        if n == 1:                                          # half of the work, nothing committed, an installed dependency; the limit stops it
            (cwd / "src").mkdir()
            (cwd / "src" / "server.ts").write_text("// half of the room logic\n", encoding="utf-8")
            (cwd / "node_modules" / "ws").mkdir(parents=True)
            (cwd / "node_modules" / "ws" / "index.js").write_text("module.exports = 1\n", encoding="utf-8")
            return _killed()
        seen.update(cwd=cwd, server=(cwd / "src" / "server.ts").read_text(encoding="utf-8"), branch=gitops.current_branch(cwd),
                    deps=(cwd / "node_modules" / "ws" / "index.js").exists(), head_subject=gitops.git(cwd, "log", "-1", "--format=%s"))
        _commit(cwd, "src/client.ts", "export const client = true\n", f"{tid}: finish the room")
        return None

    r = LaneRunner()
    r.work["developer"] = work
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["execute"])
    cyc.store.close()
    t = _state(p, tid)
    res = t["results"][-1]
    assert t["state"] == "in_progress" and t["continuations"] == 1 and res["status"] == INTERRUPTED and res["wip_files"] == 1
    wip = res["wip_commit"]
    # the checkout survived the stop and holds the wip commit; the clone and the base branch know nothing of it
    assert wt.is_dir() and gitops.head(wt) == wip and gitops.current_branch(wt) == f"task/{tid}" and not gitops.dirty(wt)
    assert gitops.git(wt, "log", "-1", "--format=%s") == f"wip: {tid} — run stopped by the time limit, continues next cycle"
    assert "src/server.ts" not in _main_files(p) and gitops.current_branch(p.repo_dir) == "main" and not (p.repo_dir / "src").exists()
    assert _events(p, "run.wip_saved")[0]["detail"]["commit"] == wip
    cyc = Cycle(st, p, r, "c2")
    cyc.run(["execute"])
    cyc.store.close()
    assert seen == {"cwd": wt, "server": "// half of the room logic\n", "branch": f"task/{tid}", "deps": True,
                    "head_subject": f"wip: {tid} — run stopped by the time limit, continues next cycle"}
    pr = r.prompts["developer"][1]
    assert "# CONTINUATION — DO NOT START OVER" in pr and wip[:10] in pr and "the same checkout it worked in" in pr and "src/server.ts" in pr
    t = _state(p, tid)
    assert t["state"] == "released" and {"src/server.ts", "src/client.ts"} <= _main_files(p) and gitops.is_ancestor(p.repo_dir, wip, "main")
    assert not wt.exists() and len(_events(p, "worktree.created")) == 1                  # created once, reused, removed at the merge


def test_a_product_mid_task_on_the_old_layout_continues_after_the_update(tmp_path: Path, caplog):
    """Before the worktrees `task/<id>` was checked out in the clone itself. A product updated in that state — a wip
    commit of a stopped run on the branch, leftovers of a failed run in the tree — goes on: the clone is parked on the
    base branch, the task gets a worktree of its branch, and the continuation finds the wip commit there."""
    st = _settings(tmp_path)
    p = _product(st)
    tid, other = _task(p, "developer"), _task(p, "designer")
    clone = p.repo_dir
    # the state the old code left: branch in the clone, the run stopped by the limit, its work saved as a wip commit
    assert gitops.start_task_branch(clone, tid, "main") == f"task/{tid}"
    (clone / "src").mkdir()
    (clone / "src" / "server.ts").write_text("// half of the room logic\n", encoding="utf-8")
    wip = gitops.save_wip(clone, tid, "run stopped by the time limit, continues next cycle")["commit"]
    (clone / "leftover.txt").write_text("scratch of a later failed run\n", encoding="utf-8")
    s = Store(p.dir)
    s.transition(tid, "claimed", "dispatcher", "old"); s.transition(tid, "in_progress", "dispatcher", "started")
    s.add_result(tid, {"run_id": "old-exec", "role": "developer", "status": INTERRUPTED, "killed": "timeout", "summary": "run stopped",
                       "duration_s": 2700, "limit_s": 2700, "wip_commit": wip, "wip_files": 1})
    s.transition(tid, "in_progress", "dispatcher", "continues next cycle", force=True, continuations=1, assignee_run=None)
    s.close()
    assert gitops.current_branch(clone) == f"task/{tid}" and not p.worktrees_dir.exists()
    seen: dict[str, object] = {}

    def work(cwd: Path, n: int):
        seen.update(cwd=cwd, branch=gitops.current_branch(cwd), server=(cwd / "src" / "server.ts").read_text(encoding="utf-8"),
                    leftover=(cwd / "leftover.txt").exists(), clone_branch=gitops.current_branch(clone))
        _commit(cwd, "src/client.ts", "export const client = true\n", f"{tid}: finish the room")

    r = LaneRunner()
    r.work["developer"] = work
    with caplog.at_level(logging.WARNING, logger="team.git"):
        cyc = Cycle(st, p, r, "c1")
        res = cyc.run(["execute"])
        cyc.store.close()
    assert res["phases"] == ["execute"] and sorted(r.calls) == ["designer", "developer"]
    assert seen == {"cwd": p.worktree_dir(tid), "branch": f"task/{tid}", "server": "// half of the room logic\n", "leftover": False, "clone_branch": "main"}
    assert any("parked on main" in m for m in caplog.messages)
    pr = r.prompts["developer"][0]
    assert "# CONTINUATION — DO NOT START OVER" in pr and wip[:10] in pr and "src/server.ts" in pr and "continuation 1 of 3" in pr
    for x in (tid, other):
        assert _state(p, x)["state"] == "released", x
    assert {"src/server.ts", "src/client.ts", "DESIGN.md"} <= _main_files(p) and "leftover.txt" not in _main_files(p)
    assert gitops.is_ancestor(clone, wip, "main") and gitops.current_branch(clone) == "main" and not gitops.dirty(clone)
    assert not (clone / "leftover.txt").exists() and not p.worktree_dir(tid).exists()


def test_worktrees_of_closed_tasks_are_swept_and_blocked_ones_wait(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    closed, blocked, live = _task(p, "developer", "closed"), _task(p, "developer", "blocked"), _task(p, "developer", "live", priority="P3")
    for tid in (closed, blocked, live):
        gitops.start_task_worktree(p.repo_dir, p.worktree_dir(tid), tid, "main")
        _commit(p.worktree_dir(tid), f"src/{tid}.ts", "x\n")
    (p.worktrees_dir / "T-9999").mkdir()                                  # a directory without a task
    s = Store(p.dir)
    s.transition(closed, "rejected", "po", "closed by PO", force=True)
    s.transition(blocked, "blocked", "dispatcher", "stuck", force=True)
    s.close()
    cyc = Cycle(st, p, None, "c1")
    assert cyc.sweep_worktrees() == [closed, "T-9999"]
    assert not p.worktree_dir(closed).exists() and p.worktree_dir(blocked).exists() and p.worktree_dir(live).exists()
    assert gitops.git(p.repo_dir, "rev-list", "--count", f"main..task/{closed}") == "1"      # the branch and its commit stay
    cyc.store.close()
    st.raw["cycle"]["parallel"] = {**st.raw["cycle"]["parallel"], "worktree_keep_h": 0}       # no patience for parked tasks
    cyc = Cycle(st, p, None, "c2")
    assert cyc.sweep_worktrees() == [blocked] and p.worktree_dir(live).exists()
    assert Cycle(st, p, None, "c3", dry_run=True).sweep_worktrees() == []
    cyc.store.close()
    assert [e["subject"] for e in _events(p, "worktree.removed")] == [blocked, "T-9999", closed]       # newest first


def test_worktree_helpers(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    clone, wt = p.repo_dir, p.worktree_dir("T-1")
    assert gitops.start_task_worktree(clone, wt, "T-1") == "task/T-1" and gitops.worktrees(clone)[wt.resolve()] == "task/T-1"
    # the index lock of a worktree is not under <worktree>/.git (that is a file): save_wip's stale-lock retry needs the real path
    lock = gitops._index_lock(wt)
    assert lock.name == "index.lock" and lock.parent.is_dir() and "worktrees" in lock.parts and gitops._index_lock(clone) == clone / ".git" / "index.lock"
    (wt / "a.txt").write_text("a\n", encoding="utf-8")
    assert gitops.save_wip(wt, "T-1", "partial result, continues next cycle")["files"] == 1
    # a directory that is not a worktree is replaced; a worktree left on another branch comes back to the task branch
    gitops.git(wt, "checkout", "-q", "--detach")
    (wt / "junk.txt").write_text("x\n", encoding="utf-8")
    assert gitops.start_task_worktree(clone, wt, "T-1") == "task/T-1" and gitops.current_branch(wt) == "task/T-1"
    assert (wt / "a.txt").exists() and not (wt / "junk.txt").exists()
    stray = p.worktree_dir("T-2")
    stray.mkdir(parents=True); (stray / "x").write_text("x")
    assert gitops.start_task_worktree(clone, stray, "T-2") == "task/T-2" and not (stray / "x").exists() and (stray / "README.md").exists()
    # a base that moved is merged into a reused branch; a new branch can start from an older commit
    old = gitops.head(clone)
    _commit(clone, "base.txt", "base\n", "base moved")
    gitops.start_task_worktree(clone, wt, "T-1")
    assert (wt / "base.txt").exists() and (wt / "a.txt").exists()
    gitops.start_task_worktree(clone, p.worktree_dir("T-3"), "T-3", start_point=old)
    assert not (p.worktree_dir("T-3") / "base.txt").exists()
    # merge: the worktree and the branch go; a conflict leaves everything in place and says which files
    sha = gitops.merge_and_push(clone, "T-1", "main", worktree=wt)
    assert sha == gitops.head(clone) and not wt.exists() and (clone / "a.txt").exists() and "task/T-1" not in gitops.git(clone, "branch")
    _commit(stray, "README.md", "theirs\n", "T-2: readme")
    _commit(clone, "README.md", "ours\n", "base: readme")
    before = gitops.head(clone)
    with pytest.raises(gitops.MergeConflict) as ei:
        gitops.merge_and_push(clone, "T-2", "main", worktree=stray)
    assert ei.value.files == ["README.md"] and "CONFLICT" in ei.value.text and "+ours" in ei.value.text
    assert gitops.head(clone) == before and not gitops.dirty(clone) and stray.exists() and gitops.current_branch(stray) == "task/T-2"
    assert gitops.remove_task_worktree(clone, stray, "T-2", delete_branch=True) and not gitops.remove_task_worktree(clone, stray)
    assert "task/T-2" not in gitops.git(clone, "branch")


# ------------------------------------------------------------------ 3. shared state

def test_store_writes_from_parallel_threads_lose_nothing(tmp_path: Path):
    s = Store(tmp_path / "p")
    shared = s.create_task(title="shared", type_="defect", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po", state="ready")["id"]
    n_threads, n_each = 6, 10
    start = threading.Barrier(n_threads)
    errors: list[BaseException] = []
    conns: set[int] = set()

    def worker(k: int) -> None:
        try:
            start.wait(timeout=WAIT_S)
            conns.add(id(s.db))
            for i in range(n_each):
                s.event("probe", f"{k}-{i}", "test", {"k": k, "i": i})
                s.add_observation({"title": f"obs {k}-{i}", "classification": "ux_friction", "source": "t", "fingerprint": f"fp-{k}-{i}"})
                s.add_observation({"title": "the same card", "classification": "ux_friction", "source": "t", "fingerprint": "fp-same", "run_id": f"{k}-{i}"})
                s.ask(from_="developer", to="po", question=f"question {k}-{i}?")
                s.create_task(title=f"task {k}-{i}", type_="defect", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po")
                s.add_result(shared, {"run_id": f"{k}-{i}", "status": "done"})
                s.add_review(shared, {"verdict": "pass"}, f"{k}-{i}")
                s.book_spend(f"{k}-{i}", "developer", 0.01, True)
                assert s.spent_today() > 0 and s.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] > 0      # direct reads from a lane
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            s.close_thread()

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    total = n_threads * n_each
    assert not errors, errors
    assert len(conns) == n_threads and id(s.db) not in conns              # every thread had its own connection
    assert len([e for e in s.events(limit=10_000) if e["kind"] == "probe"]) == total
    obs = s.observations()
    assert len(obs) == total + 1 and len({o["id"] for o in obs}) == total + 1
    same = next(o for o in obs if o.get("fingerprint") == "fp-same")
    assert same["repeat_count"] == total - 1 and len(same["repeats"]) == total - 1
    qs = s.questions()
    assert len(qs) == total and len({q["id"] for q in qs}) == total
    tasks = s.tasks()
    assert len(tasks) == total + 1 and len({t["id"] for t in tasks}) == total + 1
    assert sorted(r["run_id"] for r in s.task(shared)["results"]) == sorted(f"{k}-{i}" for k in range(n_threads) for i in range(n_each))
    assert len(list((s.dir / "reviews").glob("R-*.yaml"))) == total
    assert s.spent_today() == pytest.approx(total * 0.01) and s.db.execute("SELECT COUNT(*) FROM spend").fetchone()[0] == total
    assert not list(s.dir.rglob("*.tmp"))
    s.close()
    assert s._conns == []


def test_the_daily_cap_is_not_overshot_by_parallel_starts(tmp_path: Path):
    """$5 left for the day, three lanes start together, a run may spend up to $3 and spends all of it. Without the
    reservation each lane would see «$5 left» and the three would spend $9."""
    st = _settings(tmp_path, {"budget": {"max_usd_per_run": 3.0, "min_usd_per_run": 0.3}})
    p = _product(st, extra="budget_usd_per_day: 5\n")
    ids = [_task(p, "developer"), _task(p, "designer"), _task(p, "game-designer")]
    r = LaneRunner(wait_peak=2, cost=None)                  # two runs are in flight together before either is booked
    cyc = Cycle(st, p, r, "c1")
    res = cyc.run(["execute"])
    assert not r.waited_out and sorted(r.caps, reverse=True) == [3.0, 2.0] and len(r.calls) == 2     # the third found nothing left
    assert cyc.store.spent_today() == pytest.approx(5.0) and cyc.store.spent_today() <= p.daily_cap + 1e-9
    assert res["runs"] == 2 and res["spent_usd"] == pytest.approx(5.0) and cyc._reserved == {}
    stops = [e["detail"] for e in cyc.store.events() if e["kind"] == "budget.stop"]
    assert len(stops) == 1 and stops[0]["remaining"] == pytest.approx(0.0)
    cyc.store.close()
    states = sorted(_state(p, tid)["state"] for tid in ids)
    assert states == ["ready", "released", "released"]
    held = next(_state(p, tid) for tid in ids if _state(p, tid)["state"] == "ready")
    assert held["history"][-1]["note"] == "no run (budget or dry-run)"
    # the same, step by step: while two runs are in flight their caps are reserved, and a third start is refused at once
    hold = threading.Event()
    r = LaneRunner(cost=None)
    r.work["developer"] = r.work["designer"] = lambda cwd, n: hold.wait(WAIT_S) and None
    cyc = Cycle(st, _product(st, "third", extra="budget_usd_per_day: 5\n"), r, "c1")
    starts = [threading.Thread(target=cyc.role_run, args=(role, "x", None), kwargs={"cwd": tmp_path / "w" / role, "label": role})
              for role in ("developer", "designer")]
    for t in starts:
        t.start()
    with r.cv:
        assert r.cv.wait_for(lambda: r.active == 2, timeout=WAIT_S)
    assert sorted(cyc._reserved.values()) == [2.0, 3.0] and cyc.store.spent_today() == 0
    assert cyc.role_run("po", "x", None, cwd=tmp_path / "w" / "po", label="po") is None and r.calls.count("po") == 0
    stop = next(e["detail"] for e in cyc.store.events() if e["kind"] == "budget.stop")
    assert stop["reserved"] == pytest.approx(5.0) and stop["remaining"] == pytest.approx(0.0)
    hold.set()
    for t in starts:
        t.join()
    assert cyc._reserved == {} and cyc.store.spent_today() == pytest.approx(5.0) and cyc.n_runs == 2
    cyc.store.close()
    # a run that raises gives its reservation back
    cyc = Cycle(st, _product(st, "other", extra="budget_usd_per_day: 5\n"), r, "c1")

    class Boom:
        def run(self, *a, **k):
            raise RuntimeError("runner broke")

    cyc.runner = Boom()
    with pytest.raises(RuntimeError):
        cyc.role_run("po", "x", None, cwd=tmp_path / "w", label="po")
    assert cyc._reserved == {} and cyc.store.spent_today() == 0
    cyc.store.close()


def test_the_shared_billing_ledger_survives_parallel_writers(tmp_path: Path):
    """data/billing.json is shared by every run of every product: each write is a read-modify-write under its file lock."""
    from team import billing
    st = _settings(tmp_path)
    errors: list[BaseException] = []

    def book(k: int) -> None:
        try:
            for i in range(10):
                billing.observe(st, None, f"run-{k}-{i}", cost=0.1)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=book, args=(k,)) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and sum(billing.load(st)["spend"].values()) == pytest.approx(8.0)


def test_play_test_ports_are_not_handed_out_twice(tmp_path: Path):
    got: list[int] = []
    lock = threading.Lock()

    def take() -> None:
        for _ in range(25):
            port = playtest.free_port()
            with lock:
                got.append(port)

    threads = [threading.Thread(target=take) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(got) == 200 and len(set(got)) == 200


def test_log_lines_carry_the_lane(tmp_path: Path):
    lanes.install_log_lane()
    lanes.install_log_lane()                                # twice is once
    fmt, cfmt = logging.Formatter(cli.LOG_FORMAT), logging.Formatter(cli.CYCLE_LOG_FORMAT)
    lines: dict[str, tuple[str, str]] = {}

    def emit(key: str) -> None:
        rec = logging.getLogger("team.cycle").makeRecord("team.cycle", logging.INFO, __file__, 1, "fleets cycle c1: phases ['collect'], runs 1", (), None)
        lines[key] = (fmt.format(rec), cfmt.format(rec))

    emit("main")
    ex = lanes.Lanes(lanes.Config(), "fleets")
    ex.submit("review", lanes.LANE_REVIEW, "review-T-0042", emit, "lane")
    assert ex.join() == []
    pid = os.getpid()
    assert f" INFO [{pid}] team.cycle: fleets cycle c1: phases" in lines["main"][0]          # unchanged: watchdog.sh greps «cycle .*: phases»
    assert f" INFO [{pid} review-T-0042] team.cycle: " in lines["lane"][0]
    assert " INFO team.cycle: " in lines["main"][1] and " INFO [review-T-0042] team.cycle: " in lines["lane"][1]
    assert lanes.current_lane() == ""


# ------------------------------------------------------------------ 4. the gate, the live host, errors

def test_the_gate_bounds_the_lanes_and_keeps_a_slot_for_execution(tmp_path: Path):
    ex = lanes.Lanes(lanes.Config(max_runs=3, review_max_runs=2), "fleets")
    cv = threading.Condition()
    state = {"active": 0, "peak": 0, "reviews": 0, "review_peak": 0, "go": False}
    order: list[str] = []

    def work(kind: str, name: str) -> None:
        with cv:
            order.append(name)
            state["active"] += 1; state["peak"] = max(state["peak"], state["active"])
            if kind == lanes.LANE_REVIEW:
                state["reviews"] += 1; state["review_peak"] = max(state["review_peak"], state["reviews"])
            cv.notify_all()
            cv.wait_for(lambda: state["go"], timeout=WAIT_S)
            state["active"] -= 1
            if kind == lanes.LANE_REVIEW:
                state["reviews"] -= 1

    for i in range(5):                                      # five reviews are waiting before execution has anything to start
        ex.submit("review", lanes.LANE_REVIEW, f"review-{i}", work, lanes.LANE_REVIEW, f"review-{i}")
    with cv:
        assert cv.wait_for(lambda: state["active"] == 2, timeout=WAIT_S)
    time.sleep(0.1)
    assert state["active"] == 2                             # reviews never take the last slot …
    ex.submit("execute", lanes.LANE_EXECUTE, "exec-developer", work, lanes.LANE_EXECUTE, "exec-developer")
    with cv:
        assert cv.wait_for(lambda: "exec-developer" in order, timeout=WAIT_S)      # … so the developer starts at once
        assert state["active"] == 3 and order[:3].count("exec-developer") == 1
        state["go"] = True
        cv.notify_all()
    assert ex.join() == [] and state["peak"] == 3 and state["review_peak"] == 2 and ex.gate.peak == 3 and len(order) == 6
    # a freed slot goes to the main thread first, then to execution, then to a review — whoever asked first
    gate = lanes.RunGate(1)
    got: list[str] = []
    hold = threading.Event()

    def holder() -> None:
        with gate.slot(lanes.LANE_EXECUTE, "holder"):
            hold.wait(WAIT_S)

    def asker(kind: str) -> None:
        with gate.slot(kind, kind):
            got.append(kind)

    h = threading.Thread(target=holder); h.start()
    while gate.at_work() != 1:
        time.sleep(0.01)
    askers = []
    for kind in (lanes.LANE_REVIEW, lanes.LANE_EXECUTE, lanes.LANE_MAIN):
        t = threading.Thread(target=asker, args=(kind,)); t.start(); askers.append(t)
        while len(gate._waiting) != len(askers):
            time.sleep(0.01)
    hold.set()
    for t in [h, *askers]:
        t.join()
    assert got == [lanes.LANE_MAIN, lanes.LANE_EXECUTE, lanes.LANE_REVIEW]


def test_host_lock_lets_play_tests_share_and_gives_a_deploy_the_host_alone():
    host = lanes.HostLock()
    log_: list[str] = []
    cv = threading.Condition()
    flags = {"release_readers": False, "deploy_waiting": False}

    def reader(name: str, wait_release: bool) -> None:
        with host.shared():
            with cv:
                log_.append(f"{name}+"); cv.notify_all()
                if wait_release:
                    cv.wait_for(lambda: flags["release_readers"], timeout=WAIT_S)
                log_.append(f"{name}-"); cv.notify_all()

    def deploy() -> None:
        with host.exclusive():
            with cv:
                log_.append("deploy+"); log_.append("deploy-"); cv.notify_all()

    a, b = threading.Thread(target=reader, args=("a", True)), threading.Thread(target=reader, args=("b", True))
    a.start(); b.start()
    with cv:
        assert cv.wait_for(lambda: {"a+", "b+"} <= set(log_), timeout=WAIT_S)          # two play-tests share the host
    d = threading.Thread(target=deploy); d.start()
    while host._writers_waiting != 1:
        time.sleep(0.01)
    c = threading.Thread(target=reader, args=("c", False)); c.start()                  # a new play-test waits behind the waiting deploy
    time.sleep(0.1)
    with cv:
        assert "deploy+" not in log_ and "c+" not in log_
        flags["release_readers"] = True
        cv.notify_all()
    for t in (a, b, d, c):
        t.join()
    assert log_.index("deploy+") > max(log_.index("a-"), log_.index("b-")) and log_.index("c+") > log_.index("deploy-")
    with host.shared(False):                                # a local play-test server takes nothing
        with host.exclusive():
            pass


def _game(st: C.Settings, tmp_path: Path) -> C.Product:
    p = _product(st, extra="site_base: https://fleets.example.test\nprofile: game\nstage: M0\ndeploy:\n  mode: service\n  host: local\n")
    return p


def test_the_tester_does_not_play_the_live_host_while_a_deploy_switches_it(tmp_path: Path, monkeypatch):
    st = _settings(tmp_path)
    p = _game(st, tmp_path)
    rel, dev = _released(p), _task(p, "developer")
    marks: dict[str, float] = {}
    dev_done = threading.Event()
    checkout = tmp_path / "live-checkout"
    checkout.mkdir()
    monkeypatch.setattr(cycle_mod, "run_tests_for_review", lambda *a, **k: "$ npm test\nexit code: 0\nok")
    monkeypatch.setattr(playtest, "start", lambda product, repo, sha, work, **k: playtest.Server(
        base_url=product.site_base, port=None, dest=checkout, log_path=None, proc=None, local=False, notes=["deployed host used"]))

    def deploy(product, *, sha="", log_path=None, **k):
        marks["deploy_start"] = time.monotonic()
        time.sleep(0.05)
        marks["deploy_end"] = time.monotonic()
        return servicedeploy.DeployResult(ok=True, sha=sha, url=product.site_base, port=8102, colour="b")

    monkeypatch.setattr(servicedeploy, "run", deploy)
    r = LaneRunner(wait_roles={"tester": {"developer"}})

    def play(cwd: Path, n: int):
        marks["play_start"] = time.monotonic()
        assert dev_done.wait(WAIT_S)                        # the developer finishes and wants to deploy while the tester still plays
        time.sleep(0.4)
        marks["play_end"] = time.monotonic()
        return None

    def develop(cwd: Path, n: int):
        _commit(cwd, "src/server.ts", "work\n", "developer: work")
        dev_done.set()
        return None

    r.work["tester"], r.work["developer"] = play, develop
    cyc = Cycle(st, p, r, "c1")
    res = cyc.run(["review", "execute"])
    cyc.store.close()
    assert not r.waited_out and sorted(r.calls) == ["developer", "tester"] and res["phases"] == ["review", "execute"]
    assert r.overlap("tester", "developer")                                 # the play-test ran beside the developer …
    assert marks["play_start"] < marks["play_end"] <= marks["deploy_start"] < marks["deploy_end"]      # … and the deploy waited for it
    t = _state(p, dev)
    assert t["state"] == "released" and t["deployed_sha"] == t["released_sha"] and _state(p, rel)["state"] == "accepted"


def test_a_released_task_is_not_reviewed_while_its_deploy_is_in_flight(tmp_path: Path, monkeypatch):
    st = _settings(tmp_path)
    p = _game(st, tmp_path)
    dev = _task(p, "developer")
    in_deploy, release = threading.Event(), threading.Event()

    def deploy(product, *, sha="", log_path=None, **k):
        in_deploy.set()
        assert release.wait(WAIT_S)
        return servicedeploy.DeployResult(ok=True, sha=sha, url=product.site_base, port=8102, colour="b")

    monkeypatch.setattr(servicedeploy, "run", deploy)
    cyc = Cycle(st, p, LaneRunner(), "c1")
    reviewed: list[str] = []
    monkeypatch.setattr(cyc, "review_task", lambda t: reviewed.append(t["id"]))
    ex = threading.Thread(target=cyc.phase_execute)
    ex.start()
    try:
        assert in_deploy.wait(WAIT_S)
        assert _state(p, dev)["state"] == "released" and cyc.lanes.busy(dev)       # merged, released, and the deploy is running
        cyc._in_run = True                                  # as inside `run`: the review phase does not wait for the lanes
        cyc.phase_review()
        cyc._in_run = False
        assert reviewed == [] and cyc.lanes.started == 1                           # only the execute lane: no review was started
    finally:
        release.set()
        ex.join()
    assert not cyc.lanes.busy(dev)
    cyc.phase_review()
    cyc.store.close()
    assert reviewed == [dev]


def test_a_lane_that_raises_does_not_stop_the_other_lanes_or_the_cycle(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    dev, des = _task(p, "developer"), _task(p, "designer")
    rel = _released(p)
    r = LaneRunner(wait_peak=2)

    def broken(cwd: Path, n: int):
        raise RuntimeError("designer lane broke")

    r.work["designer"] = broken
    cyc = Cycle(st, p, r, "c1")
    res = cyc.run(["review", "execute", "measure"])
    cyc.store.close()
    assert res["phases"] == ["review", "measure"] and res["runs"] == 3            # execute is not reported as finished; the cycle went on
    assert _state(p, dev)["state"] == "released" and _state(p, rel)["state"] == "accepted"
    err = _events(p, "phase.error")
    assert len(err) == 1 and err[0]["subject"] == "execute" and "lane exec-designer: designer lane broke" in err[0]["detail"]
    s = Store(p.dir)
    assert s.cycles()[0]["phases"] == ["review", "measure"] and s.cycles()[0]["finished"]
    assert s.task(des)["state"] == "in_progress" and not s.db.execute("SELECT COUNT(*) FROM claims").fetchone()[0]    # as a raised executor always left it
    s.close()
    # a phase called on its own raises the lane's error, as the sequential phase would
    st2 = _settings(tmp_path / "direct")
    p2 = _product(st2)
    _task(p2, "designer")
    r2 = LaneRunner()
    r2.work["designer"] = broken
    cyc2 = Cycle(st2, p2, r2, "c1")
    with pytest.raises(RuntimeError, match="designer lane broke"):
        cyc2.phase_execute()
    cyc2.store.close()


# ------------------------------------------------------------------ 5. configuration and what the owner sees

def test_parallel_settings_defaults_overrides_and_bad_values(tmp_path: Path, caplog):
    st = _settings(tmp_path)
    p = _product(st)
    cfg = p.parallel
    assert (cfg.enabled, cfg.max_runs, cfg.review_max_runs, cfg.overlap_roles, cfg.max_merge_conflicts, cfg.worktree_keep_h) == (True, 3, 2, (), 2, 72.0)
    assert cfg.describe() == "parallel, up to 3 runs at once (reviews ≤ 2)"
    assert C.DEFAULTS["cycle"]["parallel"] == lanes.DEFAULTS and C.DEFAULTS["cycle"]["execute_slots"] if "execute_slots" in C.DEFAULTS["cycle"] else True
    # team.yaml, then product.yaml on top, key by key
    st.raw["cycle"]["parallel"] = {**lanes.DEFAULTS, "max_runs": 4}
    assert (p.parallel.max_runs, p.parallel.review_max_runs) == (4, 3)
    p.raw["cycle"] = {"every_min": 5, "parallel": {"max_runs": 2, "overlap_roles": ["developer"]}}
    assert (p.parallel.max_runs, p.parallel.review_max_runs, p.parallel.overlap_roles, p.parallel.enabled) == (2, 1, ("developer",), True)
    p.raw["cycle"] = {"parallel": False}                    # the short form
    assert not p.parallel.enabled and p.parallel.max_runs == 4
    p.raw["cycle"] = {"parallel": {"enabled": False, "max_runs": 1}}
    assert not p.parallel.enabled and p.parallel.review_max_runs == 1
    # nonsense is logged and replaced, never raised: a typo must not stop the cycle
    p.raw["cycle"] = {"parallel": {"max_runs": 99, "review_max_runs": "many", "enabled": "yes", "overlap_roles": "developer", "worktree_keep_h": -1,
                                   "max_merge_conflicts": 0, "typo": 1}}
    with caplog.at_level(logging.WARNING, logger="team.lanes"):
        cfg = p.parallel
    assert (cfg.enabled, cfg.max_runs, cfg.review_max_runs, cfg.overlap_roles, cfg.worktree_keep_h, cfg.max_merge_conflicts) == (
        True, lanes.MAX_RUNS_CEILING, lanes.MAX_RUNS_CEILING - 1, (), 72.0, 1)
    text = " | ".join(caplog.messages)
    for word in ("max_runs=99", "review_max_runs='many'", "enabled='yes'", "overlap_roles='developer'", "worktree_keep_h=-1", "unknown key(s) typo"):
        assert word in text, word
    p.raw["cycle"] = {"parallel": [1, 2]}
    assert p.parallel.max_runs == 4


def test_status_and_the_summary_line_stay_truthful(tmp_path: Path, monkeypatch, capsys, caplog):
    st = _settings(tmp_path)
    p = _product(st)
    _task(p, "developer"); _task(p, "designer")
    r = LaneRunner(wait_peak=2)
    with caplog.at_level(logging.DEBUG, logger="team.cycle"):
        cyc = Cycle(st, p, r, "c1")
        res = cyc.run(["execute"])
        cyc.store.close()
    assert res == {"phases": ["execute"], "runs": 2, "spent_usd": pytest.approx(0.2)}
    line = next(m for m in caplog.messages if "cycle c1: phases" in m)
    assert "phases ['execute'], runs 2, spent $0.20" in line and line.endswith("2 lane(s), up to 2 at once")
    assert "fleets cycle c1: lanes parallel, up to 3 runs at once (reviews ≤ 2)" in caplog.messages
    for rec in ("dev", "designer"):
        runs = list((p.runs_dir / "c1").glob(f"{rec}-*/run.json"))
        assert len(runs) == 1 and '"lane": "exec-' in runs[0].read_text(encoding="utf-8")
    monkeypatch.setattr(cli.C, "load", lambda *a, **k: st)
    assert cli.main(["status", p.slug]) == 0
    out = capsys.readouterr().out
    assert "lanes: parallel, up to 3 runs at once (reviews ≤ 2)" in out and "spent today $0.20" in out and "cycle c1: ['execute'] $0.20" in out
    assert "running" not in out.split("cycle c1:")[1].splitlines()[0]


# ------------------------------------------------------------------ 6. a repository with a remote; what the PO is told

def _with_remote(p: C.Product, tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    gitops.git(tmp_path, "clone", "-q", "--bare", str(p.repo_dir), str(bare))
    gitops.git(p.repo_dir, "remote", "add", "origin", str(bare))
    gitops.git(p.repo_dir, "fetch", "-q", "origin")
    p.raw["repo"]["url"] = str(bare)
    return bare


def test_worktrees_of_a_repository_with_a_remote_push_and_follow_origin(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    bare = _with_remote(p, tmp_path)
    clone = p.repo_dir
    dev, des = _task(p, "developer"), _task(p, "designer")
    r = LaneRunner(wait_peak=2)
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["execute"])
    cyc.store.close()
    assert not r.waited_out and _state(p, dev)["state"] == _state(p, des)["state"] == "released"
    on_origin = set(gitops.git(bare, "ls-tree", "-r", "--name-only", "main").splitlines())
    assert {"src/server.ts", "DESIGN.md"} <= on_origin and gitops.head(clone) == gitops.git(bare, "rev-parse", "main")
    assert gitops.git(bare, "branch", "--list", "task/*") == ""             # task branches are never pushed in merge mode
    # the owner pushes to origin: the next task starts from it, and the clone's own tree follows
    owner = tmp_path / "owner"
    gitops.git(tmp_path, "clone", "-q", str(bare), str(owner))
    gitops.git(owner, "config", "user.name", "owner"); gitops.git(owner, "config", "user.email", "owner@example.test")
    _commit(owner, "OWNER.md", "hotfix\n", "owner: hotfix")
    gitops.git(owner, "push", "-q", "origin", "main")
    second = _task(p, "developer", "second")
    seen: dict[str, bool] = {}

    def work(cwd: Path, n: int):
        seen["owner_file"] = (cwd / "OWNER.md").exists()
        _commit(cwd, "src/second.ts", "second\n", "developer: second")

    r = LaneRunner()
    r.work["developer"] = work
    cyc = Cycle(st, p, r, "c2")
    cyc.run(["execute"])
    cyc.store.close()
    assert seen == {"owner_file": True} and _state(p, second)["state"] == "released" and (clone / "OWNER.md").exists()
    assert {"OWNER.md", "src/second.ts"} <= set(gitops.git(bare, "ls-tree", "-r", "--name-only", "main").splitlines())
    # a push that origin refuses: the task is blocked as before, and the clone's base branch does not keep the unpushed merge
    hook = bare / "hooks" / "pre-receive"
    hook.parent.mkdir(exist_ok=True)
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    third = _task(p, "developer", "third")
    r = LaneRunner()
    r.work["developer"] = lambda cwd, n: _commit(cwd, "src/third.ts", "third\n", "developer: third")
    cyc = Cycle(st, p, r, "c3")
    assert cyc.run(["execute"])["phases"] == ["execute"]
    cyc.store.close()
    t = _state(p, third)
    assert t["state"] == "blocked" and "merge/push failed" in t["history"][-1]["note"]
    assert gitops.head(clone) == gitops.git(bare, "rev-parse", "main") and "src/third.ts" not in _main_files(p)
    assert p.worktree_dir(third).exists() and gitops.git(clone, "rev-list", "--count", f"origin/main..task/{third}") == "1"
    hook.unlink()
    s = Store(p.dir)
    s.transition(third, "ready", "po", "origin accepts pushes again", force=True)
    s.close()
    r = LaneRunner()
    r.work["developer"] = lambda cwd, n: None                               # nothing new to do: the commit is on the branch
    cyc = Cycle(st, p, r, "c4")
    cyc.run(["execute"])
    cyc.store.close()
    assert _state(p, third)["state"] == "released" and "src/third.ts" in gitops.git(bare, "ls-tree", "-r", "--name-only", "main")
    assert not p.worktree_dir(third).exists()


def test_a_run_profile_written_while_the_po_was_running_is_shown_to_it_next_time(tmp_path: Path):
    """Reviews run beside the triage now: a long review that ends while the PO is thinking was not in the PO's prompt.
    «Seen by the PO» therefore ends where its prompt was built — otherwise that line would never reach it."""
    st = _settings(tmp_path)
    p = _product(st)
    rel = _released(p)
    s = Store(p.dir)
    s.add_observation({"title": "status text overlaps", "classification": "ux_friction", "source": "tester", "fingerprint": "f-1"})
    s.close()
    box: dict[str, Cycle] = {}
    r = LaneRunner(wait_roles={"reviewer": {"po"}})

    def po(cwd: Path, n: int):
        if n == 1:                                          # the PO returns only after the review's profile is in the store
            deadline = time.monotonic() + WAIT_S
            while time.monotonic() < deadline and not [e for e in box["cyc"].store.events() if e["kind"] == "run.profile"]:
                time.sleep(0.02)
        return None

    r.work["po"] = po
    r.work["reviewer"] = lambda cwd, n: RunResult(True, dict(PASS), {"total_cost_usd": 0.1}, 0.1, 9, None, session_id="s", duration_s=420.0)
    box["cyc"] = cyc = Cycle(st, p, r, "c1")
    cyc.run(["review", "triage"])
    assert not r.waited_out and "# RUN TIME" not in r.prompts["po"][0]
    kinds = [e["kind"] for e in reversed(cyc.store.events())]
    assert kinds.index("run.profile") < kinds.index("run.profile_seen")      # written before the PO's run ended …
    cyc.store.close()
    s = Store(p.dir)
    s.add_observation({"title": "lobby is slow", "classification": "ux_friction", "source": "tester", "fingerprint": "f-2"})
    s.close()
    cyc = Cycle(st, p, r, "c2")
    cyc.run(["triage"])
    cyc.store.close()
    assert "# RUN TIME" in r.prompts["po"][1] and f"reviewer {rel}: 420 s" in r.prompts["po"][1]     # … and still shown at the next triage
    s = Store(p.dir)
    s.add_observation({"title": "third", "classification": "ux_friction", "source": "tester", "fingerprint": "f-3"})
    s.close()
    cyc = Cycle(st, p, r, "c3")
    cyc.run(["triage"])
    cyc.store.close()
    assert "# RUN TIME" not in r.prompts["po"][2]                           # once


def test_base_conflict_at_start_is_never_resolved_silently(tmp_path: Path):
    """ai-battles 21.11 (F39): a continuing task branch that conflicts with the base used to get `merge -X ours` at start: the
    base's side of the conflicting hunks vanished, the branch then merged cleanly and the base LOST the other task's lines
    (T-0038 erased what T-0068 and T-0071 had released). The branch now stays without the base and nothing is dropped."""
    repo = tmp_path / "repo"
    repo.mkdir()
    gitops.git(repo, "init", "-q", "-b", "main"); gitops.git(repo, "config", "user.email", "t@example.com"); gitops.git(repo, "config", "user.name", "t")
    _commit(repo, "style.css", "a{color:red}\n", "base")
    wt = tmp_path / "wt" / "T-1"
    br = gitops.start_task_worktree(repo, wt, "T-1")
    _commit(wt, "style.css", "a{color:red}\n.menu{display:flex}\n", "task: menu")                  # the task appends its block
    _commit(repo, "style.css", "a{color:red}\n.fullscreen{display:none}\n", "other task released")  # main appended another one
    assert gitops.start_task_worktree(repo, wt, "T-1") == br                                        # next attempt: conflict at start
    assert (wt / "style.css").read_text(encoding="utf-8") == "a{color:red}\n.menu{display:flex}\n"   # as the attempt left it
    assert gitops.git(wt, "status", "--porcelain") == ""                                            # no half-finished merge
    assert gitops.git(wt, "rev-list", "--count", "HEAD..main") == "1"                                # the base is NOT merged in
    try:
        gitops.merge_and_push(repo, "T-1", "main", worktree=wt)
        raise AssertionError("the merge must conflict")
    except gitops.MergeConflict as e:
        assert "style.css" in e.files
    assert ".fullscreen{display:none}" in (repo / "style.css").read_text(encoding="utf-8")          # main keeps the other task's lines


# ------------------------------------------------------------------ owner 23.11: the cap of one run follows the role's model

def test_the_cap_of_one_run_comes_from_run_caps_of_the_role(tmp_path: Path):
    """product.yaml `run_caps: {developer: 5}` replaces the $3 of `max_usd_per_run` for that role only; team.yaml
    `budget.run_caps` is the layer below; a wrong value is ignored; the daily cap still holds."""
    st = _sequential(tmp_path, {"budget": {"max_usd_per_run": 3.0, "min_usd_per_run": 0.3, "run_caps": {"designer": 1.5, "po": 4.0}}})
    p = _product(st, extra="budget_usd_per_day: 50\nrun_caps:\n  developer: 5\n  po: 6.5\n  tester: -1\n")
    assert p.run_cap("developer") == (5.0, "product.yaml run_caps.developer")
    assert p.run_cap("po") == (6.5, "product.yaml run_caps.po")                        # the product layer wins
    assert p.run_cap("designer") == (1.5, "team.yaml budget.run_caps.designer")
    assert p.run_cap("tester") is None and p.run_cap("game-designer") is None           # wrong value and no value: the caller's cap
    _task(p, "developer"); _task(p, "designer"); _task(p, "game-designer")
    r = LaneRunner(cost=0.1)
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["execute"])
    by_role = dict(zip(r.calls, r.caps))
    assert by_role == {"developer": 5.0, "designer": 1.5, "game-designer": 3.0}
    cyc.store.close()


def test_run_caps_never_lift_a_run_above_what_is_left_of_the_day(tmp_path: Path):
    st = _sequential(tmp_path, {"budget": {"max_usd_per_run": 3.0, "min_usd_per_run": 0.3}})
    p = _product(st, extra="budget_usd_per_day: 4\nrun_caps:\n  developer: 9\n")
    _task(p, "developer")
    r = LaneRunner(cost=0.1)
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["execute"])
    assert r.caps == [4.0]
    cyc.store.close()
