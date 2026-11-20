from datetime import datetime, timedelta, timezone

from team import retro

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)
MERGE = NOW - timedelta(days=5)


def _rec(i, kind, ts):
    return {"id": f"F-{i:04d}", "kind": kind, "role": "reviewer", "ts": ts.isoformat()}


def _task(tid, fid):
    return {"id": tid, "scope": "team/roles/reviewer.md", "problem": f"Friction: {fid}.", "merged_at": MERGE.isoformat()}


def test_recurring_kind_is_listed():
    recs = [_rec(1, "denied", MERGE - timedelta(days=1)), _rec(2, "denied", MERGE + timedelta(days=1))]
    md = retro.rules_that_did_not_help(recs, [_task("T-1", "F-0001")])
    assert md.startswith("## Rules that did not help")
    assert "- T-1: kind `denied` before 1, after 1" in md


def test_kind_that_did_not_recur_is_not_listed():
    recs = [_rec(1, "slow", MERGE - timedelta(days=1)), _rec(2, "slow", MERGE - timedelta(days=2))]
    md = retro.rules_that_did_not_help(recs, [_task("T-2", "F-0001")])
    assert "T-2" not in md and "- none" in md
