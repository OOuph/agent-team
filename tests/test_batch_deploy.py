"""Batch deploy: one switch of the live host per cycle (owner 25.11: «чтобы выкладка шла пачкой, раз в цикл, а не после
каждой задачи»; team/cycle.py `deploy_batch`, team/lanes.py invariant 5).

Measured on ai-battles 25.11: 52 deploys by the dispatcher (67 with the operator's), one after every merge; each holds
the live host exclusively for 0.5–5 minutes and cuts the matches of live players; 17 of them waited for play-tests, 124
minutes in all, with the repository lock held. Here: merges go on as before, the host is switched once on the last
merged commit, a task is «deployed» and reviewed only after that, a failed batch finds its culprit through the per-task
path, a merged commit never waits longer than `deploy.batch_max_wait_min`, and what a dead cycle left is deployed first
by the next one. No network, no models: `servicedeploy.run` is replaced by a recorder."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from team import config as C
from team import gitops, servicedeploy
from team.cycle import DEPLOY_ENV_RETRIES, DEPLOY_PENDING, Cycle
from team.runner import RunResult
from team.store import Store

from test_servicedeploy import _game, _settings

DONE = {"status": "done", "summary": "done", "changes": ["x"], "how_to_verify": ["x"], "self_check": [{"criterion": "f", "met": True, "evidence": "e"}]}
PASS = {"verdict": "pass", "steps": [], "criteria": [{"criterion": "works", "met": True, "evidence": "read"}], "summary": "read. fine."}
BATCH = {"mode": "service", "host": "local", "batch": True}


class Dev:
    """Every developer run commits a file named after its task; a task titled «broken» also commits BROKEN."""

    def __init__(self):
        self.calls: list[str] = []

    def run(self, role, prompt, schema, *, max_usd, cwd, **kw):
        self.calls.append(role)
        if role == "developer":
            cwd = Path(cwd)
            (cwd / f"{cwd.name}.ts").write_text("work\n")
            if "broken" in prompt:
                (cwd / "BROKEN").write_text("x\n")
            gitops.git(cwd, "add", "-A"); gitops.git(cwd, "commit", "-q", "-m", f"dev: {cwd.name}")
            return RunResult(True, dict(DONE), {"total_cost_usd": 0.1}, 0.1, 2, None, session_id="s", duration_s=1.0)
        return RunResult(True, dict(PASS), {"total_cost_usd": 0.1}, 0.1, 2, None, session_id="s", duration_s=1.0)


class Host:
    """Stands in for deploy-service.sh: records the commits it was asked for; a commit that holds BROKEN fails its
    health check (exit 20), `exit_code` makes every deploy fail that way (30 = the push to the game host)."""

    def __init__(self, p: C.Product, exit_code: int = 0):
        self.p, self.exit_code, self.calls, self.live = p, exit_code, [], ""

    def __call__(self, product, *, sha="", log_path=None, **k):
        self.calls.append(sha)
        broken = gitops.git(self.p.repo_dir, "ls-tree", "-r", "--name-only", sha).split().count("BROKEN") > 0
        code = self.exit_code or (20 if broken else 0)
        if code:
            return servicedeploy.DeployResult(False, sha=sha, exit_code=code, error="health check failed" if code == 20 else "push failed", output="…")
        self.live = sha
        return servicedeploy.DeployResult(True, sha=sha, url=product.site_base + "/", port=8102, colour="b", duration_s=30.0)


def _setup(tmp_path: Path, monkeypatch, n: int, deploy: dict | None = None, titles: list[str] | None = None, slots: int = 0):
    st = _settings(tmp_path)
    st.raw["cycle"]["execute_slots"] = {"developer": slots or n}
    p = _game(st, deploy if deploy is not None else dict(BATCH))
    s = Store(p.dir)
    ids = [s.create_task(title=(titles or [f"task {i}"] * n)[i] if titles else f"task {i}", type_="milestone", problem="x", scope="src/", acceptance=["a"], goal="M0",
                         created_by="po", role="developer", state="ready")["id"] for i in range(n)]
    s.close()
    host = Host(p)
    monkeypatch.setattr(servicedeploy, "run", host)
    return st, p, ids, host


def _t(p: C.Product, tid: str) -> dict:
    s = Store(p.dir)
    try:
        return s.task(tid)
    finally:
        s.close()


def _ev(p: C.Product, kind: str) -> list[dict]:
    s = Store(p.dir)
    try:
        return [e for e in reversed(s.events()) if e["kind"] == kind]
    finally:
        s.close()


def test_settings(tmp_path: Path, caplog):
    p = _game(_settings(tmp_path), {"mode": "service", "host": "local"})
    assert p.deploy_batch is False and p.deploy_batch_max_wait_min == C.DEPLOY_BATCH_MAX_WAIT_MIN and "after every merge" in servicedeploy.describe(p)
    p.raw["deploy"] = {"mode": "service", "batch": True, "batch_max_wait_min": 20}
    assert p.deploy_batch and p.deploy_batch_max_wait_min == 20 and "batch: once per cycle" in servicedeploy.describe(p)
    with caplog.at_level("WARNING"):
        p.raw["deploy"] = {"mode": "service", "batch": "yes", "batch_max_wait_min": 0}
        assert p.deploy_batch is False and p.deploy_batch_max_wait_min == C.DEPLOY_BATCH_MAX_WAIT_MIN
    assert "deploy.batch='yes'" in caplog.text and "batch_max_wait_min=0" in caplog.text


def test_without_batch_every_merge_is_deployed(tmp_path: Path, monkeypatch):
    st, p, ids, host = _setup(tmp_path, monkeypatch, 3, {"mode": "service", "host": "local"})
    cyc = Cycle(st, p, Dev(), "c1")
    cyc.run(["execute"])
    cyc.store.close()
    assert len(host.calls) == 3 and [_t(p, i)["deployed_sha"] for i in ids] == host.calls and not _ev(p, "deploy.queued")


def test_one_deploy_per_cycle_on_the_last_merged_commit(tmp_path: Path, monkeypatch):
    st, p, ids, host = _setup(tmp_path, monkeypatch, 3)
    cyc = Cycle(st, p, Dev(), "c1")
    cyc.run(["execute"])
    cyc.store.close()
    head = gitops.head(p.repo_dir)
    assert host.calls == [head] and host.live == head                         # one switch of the host, on the last merge
    for tid in ids:
        t = _t(p, tid)
        assert t["state"] == "released" and t["deployed_sha"] == head and t["deployed_at"] and not t.get(DEPLOY_PENDING), tid
    assert _t(p, ids[-1])["released_sha"] == head and _t(p, ids[0])["released_sha"] != head      # each task keeps its own merge commit
    ok = _ev(p, "deploy.ok")
    assert len(ok) == 1 and ok[0]["subject"] == "c1" and ok[0]["detail"]["batch"] == 3 and ok[0]["detail"]["tasks"] == ids
    assert "end of the execute phase" in ok[0]["detail"]["why"] and len(_ev(p, "deploy.queued")) == 3
    assert (st.data_dir / "work" / "fleets" / "c1").glob("deploy-batch-*.log")


def test_a_task_is_reviewed_only_after_the_batch_that_made_it_live(tmp_path: Path, monkeypatch):
    """The cycle died before its batch (here: the batch is switched off for the first cycle). The next cycle deploys
    first, then reviews; while the host cannot be reached the tasks stay pending and nobody plays an old release."""
    st, p, ids, host = _setup(tmp_path, monkeypatch, 2)
    cyc = Cycle(st, p, Dev(), "c1")
    monkeypatch.setattr(cyc, "deploy_batch", lambda why: False)
    cyc.run(["execute"])
    cyc.store.close()
    assert host.calls == [] and all(_t(p, i)[DEPLOY_PENDING] and "deployed_sha" not in _t(p, i) for i in ids)
    # the host is unreachable: nothing is reviewed, one observation, the tasks wait
    host.exit_code = servicedeploy.EXIT_PUSH_FAILED
    r = Dev()
    cyc = Cycle(st, p, r, "c2")
    cyc.run(["review"])
    obs = [o for o in cyc.store.observations("new") if "deploy" in o["title"]]
    cyc.store.close()
    assert r.calls == [] and len(host.calls) == 1 and len(obs) == 1 and obs[0]["classification"] == "environment_error"
    assert all(_t(p, i)[DEPLOY_PENDING] and _t(p, i)["deploy_failures"] == 1 and _t(p, i)["state"] == "released" for i in ids)
    assert _ev(p, "deploy.failed")[0]["detail"]["blamed"] is False
    # the host is back: the batch goes first, then both tasks are reviewed
    host.exit_code = 0
    r = Dev()
    cyc = Cycle(st, p, r, "c3")
    cyc.run(["review"])
    cyc.store.close()
    head = gitops.head(p.repo_dir)
    assert host.calls[-1] == head and len(host.calls) == 2 and len(r.calls) == 2
    assert all(_t(p, i)["deployed_sha"] == head and _t(p, i)["state"] == "accepted" for i in ids)
    assert "left by the previous cycle" in _ev(p, "deploy.ok")[0]["detail"]["why"]


def test_a_failed_batch_finds_the_task_that_broke_the_release(tmp_path: Path, monkeypatch):
    st, p, ids, host = _setup(tmp_path, monkeypatch, 3, titles=["good one", "broken one", "good two"])
    cyc = Cycle(st, p, Dev(), "c1")
    cyc.run(["execute"])
    qs = cyc.store.questions(["open"])
    cyc.store.close()
    a, b, c = (_t(p, i) for i in ids)
    head = gitops.head(p.repo_dir)
    # the batch on the head failed; then each task's own merge in merge order — the head itself is not deployed twice
    assert host.calls == [head, a["released_sha"], b["released_sha"]] and c["released_sha"] == head
    assert a["state"] == "released" and a["deployed_sha"] == a["released_sha"] and host.live == a["released_sha"]      # the last good release is live
    assert b["state"] == "blocked" and c["state"] == "blocked" and "deployed_sha" not in b and "deployed_sha" not in c
    assert not any(t.get(DEPLOY_PENDING) for t in (a, b, c))
    assert sorted(q["task"] for q in qs) == sorted([ids[1], ids[2]]) and all("did not deploy" in q["question"] for q in qs)
    failed = _ev(p, "deploy.failed")
    assert failed[0]["subject"] == "c1" and failed[0]["detail"]["batch"] == 3 and failed[0]["detail"]["blamed"] is True


def test_a_host_that_stays_unreachable_ends_in_the_per_task_path(tmp_path: Path, monkeypatch):
    st, p, ids, host = _setup(tmp_path, monkeypatch, 1)
    host.exit_code = servicedeploy.EXIT_PUSH_FAILED
    cyc = Cycle(st, p, Dev(), "c1")
    cyc.run(["execute"])
    for n in range(2, DEPLOY_ENV_RETRIES + 1):
        assert _t(p, ids[0])["state"] == "released" and _t(p, ids[0])["deploy_failures"] == n - 1
        cyc.deploy_batch("again")
    cyc.store.close()
    t = _t(p, ids[0])
    assert t["state"] == "blocked" and not t.get(DEPLOY_PENDING) and len(host.calls) == DEPLOY_ENV_RETRIES


def test_a_merged_commit_does_not_wait_longer_than_the_limit(tmp_path: Path, monkeypatch):
    st, p, ids, host = _setup(tmp_path, monkeypatch, 2, slots=1)
    # an earlier cycle merged the first task 50 minutes ago and never deployed it (limit: 45)
    cyc = Cycle(st, p, Dev(), "c1")
    monkeypatch.setattr(cyc, "deploy_batch", lambda why: False)
    cyc.run(["execute"])
    old = cyc.store.task(ids[0])
    old[DEPLOY_PENDING] = (datetime.now(timezone.utc) - timedelta(minutes=50)).isoformat(timespec="seconds")
    cyc.store.save_task(old)
    cyc.store.close()
    cyc = Cycle(st, p, Dev(), "c2")
    assert cyc.deploy_waiting_s() == pytest.approx(3000, abs=30)
    cyc.run(["execute"])                                                         # the second task merges → the batch goes at once, in the lane
    cyc.store.close()
    ok = _ev(p, "deploy.ok")
    assert len(host.calls) == 1 and len(ok) == 1 and "waited 50 min" in ok[0]["detail"]["why"] and ok[0]["detail"]["batch"] == 2
    assert ok[0]["detail"]["waited_s"] >= 2990


def test_the_timer_deploys_when_nothing_else_is_merged(tmp_path: Path, monkeypatch):
    st, p, ids, host = _setup(tmp_path, monkeypatch, 1)
    cyc = Cycle(st, p, Dev(), "c1")
    real = cyc.deploy_batch
    monkeypatch.setattr(cyc, "deploy_batch", lambda why: False)
    cyc.run(["execute"])                                                         # merged, pending, the cycle's own batch did not happen
    monkeypatch.setattr(cyc, "deploy_batch", real)
    cyc._arm_batch_timer(0.0)                                                    # the first merge that waits arms it; fires after a second
    cyc._arm_batch_timer(0.0)                                                    # one timer per cycle
    deadline = time.monotonic() + 15
    while not host.calls and time.monotonic() < deadline:
        time.sleep(0.1)
    time.sleep(0.3)
    assert len(host.calls) == 1 and _t(p, ids[0])["deployed_sha"] == gitops.head(p.repo_dir)
    cyc._cancel_batch_timer()
    cyc.store.close()


def test_a_manual_deploy_does_not_disturb_the_batch_and_nothing_pending_deploys_nothing(tmp_path: Path, monkeypatch):
    st, p, ids, host = _setup(tmp_path, monkeypatch, 1)
    cyc = Cycle(st, p, Dev(), "c1")
    assert cyc.deploy_batch("nothing merged") is False and host.calls == []
    cyc.run(["execute"])
    assert len(host.calls) == 1
    assert cyc.deploy_batch("nothing new") is False and len(host.calls) == 1     # no pending task: the host is left alone
    cyc.store.close()
    dry = Cycle(st, p, None, "c2", dry_run=True)
    assert dry.deploy_batch("dry") is False
    dry.store.close()
