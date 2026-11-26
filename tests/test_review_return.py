"""Three unproven reviews send the task back to work with a named reason (rework slice 2b, owner 26.11).

Measured on ai-battles: 21 of 29 blocked tasks on 26.11 were «inconclusive 3×» — blocked for good, with a PO question
that produced a NEW «proof» task (32 such tasks in the backlog). With `cycle.escalation.review_return: true` the third
`inconclusive` returns the SAME task to its role with the reason word from the fixed list and what to do about it; a
reason that is the environment's (lock, server, tooling) gives the review another try instead. After
`REVIEW_RETURN_MAX` returns the old rule applies (blocked + a PO question). Off (default): the old rule from the start."""
from __future__ import annotations

from pathlib import Path

from team import config as C
from team import cycle as cyc
from team import prompts
from team.cycle import Cycle
from team.runner import RunResult
from team.store import Store


def _setup(tmp_path: Path, on: bool) -> tuple[C.Settings, C.Product, Store, str]:
    raw = C._merge(C.DEFAULTS, {"cycle": {"escalation": {"review_return": on}}})
    st = C.Settings(raw, tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")
    d = st.products_dir / "game"
    d.mkdir(parents=True)
    (d / "product.yaml").write_text("slug: game\nname: Game\nhost: ''\nprofile: game\nrepo:\n  url: ''\n  branch: main\n")
    (d / "VISION.md").write_text("# Game\n")
    p = C.load_product(st, d)
    s = Store(p.dir)
    t = s.create_task(title="Cup counter", type_="feature", problem="p", scope="s", acceptance=["the counter shows 24"], goal="G1",
                      created_by="po", state="ready")
    s.transition(t["id"], "released", "dispatcher", force=True, released_sha="abc", released_at="2026-11-26T08:00:00+00:00")
    return st, p, s, t["id"]


def _verdict(reason: str | None = "no_test_for_criterion", text: str = "no spec covers the counter") -> RunResult:
    out = {"verdict": "inconclusive", "steps": [], "criteria": [{"criterion": "the counter shows 24", "met": False, "evidence": text}],
           "summary": "could not prove", "first_failure": text}
    if reason:
        out["inconclusive_reason"] = reason
    return RunResult(True, out, {"total_cost_usd": 0.1}, 0.1, 2, None, session_id="s", duration_s=1.0)


def _review(st, p, s, tid, rr, n=1):
    for i in range(n):
        c = Cycle(st, p, None, f"c{i}")
        c.apply_review(s.task(tid), rr)
        c.store.close()


def test_default_is_off_and_blocks_as_before(tmp_path: Path):
    st, p, s, tid = _setup(tmp_path, False)
    assert C.DEFAULTS["cycle"]["escalation"]["review_return"] is False
    _review(st, p, s, tid, _verdict(), 3)
    t = s.task(tid)
    assert t["state"] == "blocked" and t["blocked_by"] == "review" and not t.get("review_returns")
    assert len([q for q in s.questions(["open"]) if q["to"] == "po"]) == 1
    s.close()


def test_third_inconclusive_returns_the_same_task_with_the_named_reason(tmp_path: Path):
    st, p, s, tid = _setup(tmp_path, True)
    _review(st, p, s, tid, _verdict(), 2)
    assert s.task(tid)["state"] == "released"                         # two are not three
    _review(st, p, s, tid, _verdict(), 1)
    t = s.task(tid)
    assert t["state"] == "in_progress" and t["review_returns"] == 1 and t["review_inconclusive"] == 0
    assert t["history"][-1]["note"].startswith("returned from review: unproven 3× [no_test_for_criterion]")
    assert not s.questions(["open"]) and len(s.tasks()) == 1          # no PO question, no new task
    lr = t["last_review"]
    assert lr["return_reason"] == "no_test_for_criterion" and "no spec covers the counter" in lr["first_failure"]
    assert cyc.REVIEW_RETURN_TODO["no_test_for_criterion"] in lr["first_failure"]
    ev = [e for e in s.events() if e["kind"] == "review.returned"]
    assert len(ev) == 1 and ev[0]["detail"]["reason"] == "no_test_for_criterion"
    txt = prompts.developer_prompt(p, t, tmp_path, "task/x", t.get("results") or [], t.get("last_review"))
    assert "REVIEW THAT FAILED" in txt and "no_test_for_criterion" in txt
    s.close()


def test_every_reason_of_the_fixed_list_has_its_instruction():
    from team.play import INCONCLUSIVE_REASONS
    assert set(cyc.REVIEW_RETURN_TODO) | set(cyc.REVIEW_RETRY_REASONS) == set(INCONCLUSIVE_REASONS)
    assert not set(cyc.REVIEW_RETURN_TODO) & set(cyc.REVIEW_RETRY_REASONS)


def test_environment_reason_retries_the_review_and_does_not_blame_the_role(tmp_path: Path):
    st, p, s, tid = _setup(tmp_path, True)
    _review(st, p, s, tid, _verdict("lock_timeout", "no e2e host lock within 600 s"), 3)
    t = s.task(tid)
    assert t["state"] == "released" and t["review_env_retries"] == 1 and t["review_inconclusive"] == cyc.INCONCLUSIVE_LIMIT - 1
    assert not t.get("review_returns") and not s.questions(["open"])
    assert any(e["kind"] == "review.retry" and e["detail"]["reason"] == "lock_timeout" for e in s.events())
    _review(st, p, s, tid, _verdict("lock_timeout", "no e2e host lock within 600 s"), cyc.REVIEW_ENV_RETRIES)
    t = s.task(tid)                                                    # the environment stays broken: the old rule, the PO decides
    assert t["state"] == "blocked" and "[lock_timeout]" in t["history"][-1]["note"]
    assert len([q for q in s.questions(["open"]) if q["to"] == "po"]) == 1
    s.close()


def test_after_the_last_return_the_old_rule_applies(tmp_path: Path):
    st, p, s, tid = _setup(tmp_path, True)
    for n in range(cyc.REVIEW_RETURN_MAX):
        _review(st, p, s, tid, _verdict(), 3)
        assert s.task(tid)["state"] == "in_progress" and s.task(tid)["review_returns"] == n + 1
        s.transition(tid, "released", "dispatcher", force=True)        # the role worked on it and it was merged again
    _review(st, p, s, tid, _verdict(), 3)
    t = s.task(tid)
    assert t["state"] == "blocked" and t["blocked_by"] == "review"
    q = [q for q in s.questions(["open"]) if q["to"] == "po"]
    assert len(q) == 1 and f"returned to work {cyc.REVIEW_RETURN_MAX} time(s)" in q[0]["question"]
    s.close()


def test_review_that_failed_to_run_three_times_is_retried_as_environment(tmp_path: Path):
    st, p, s, tid = _setup(tmp_path, True)
    bad = RunResult(False, None, {}, 0.1, 2, "timeout", session_id="s", duration_s=1500.0)
    _review(st, p, s, tid, bad, 3)
    t = s.task(tid)
    assert t["state"] == "released" and t["review_env_retries"] == 1
    _review(st, p, s, tid, bad, cyc.REVIEW_ENV_RETRIES)
    assert s.task(tid)["state"] == "blocked"
    s.close()
