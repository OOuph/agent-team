from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from team import friction, retro

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def _rec(i, kind, role, ts):
    return {"id": f"F-{i:04d}", "kind": kind, "role": role, "ts": ts.isoformat()}


def _task(tid, fid, merged):
    return {"id": tid, "scope": "team/roles/reviewer.md", "problem": f"Friction: {fid}. rule", "merged_at": merged.isoformat()}


def test_came_back_too_early_held():
    old = NOW - timedelta(days=10)
    recs = [_rec(1, "denied", "reviewer", old - timedelta(days=1)), _rec(2, "denied", "reviewer", NOW - timedelta(days=2)),
            _rec(3, "slow", "reviewer", old - timedelta(days=1)),
            _rec(4, "late", "reviewer", NOW - timedelta(hours=5))]
    merged = [_task("T-1", "F-0001", old), _task("T-2", "F-0003", old), _task("T-3", "F-0004", NOW - timedelta(hours=10))]
    md = retro.rule_effectiveness(recs, merged, now=NOW)
    lines = {l.split()[1]: l for l in md.splitlines() if l.startswith("- T-")}
    assert "before 1, after 1" in lines["T-1"] and "came back" in lines["T-1"] and "replace or remove" in lines["T-1"]
    assert "held" in lines["T-2"] and "came back" not in lines["T-2"]
    assert "too early" in lines["T-3"] and "came back" not in lines["T-3"]


def test_prompt_tells_to_replace(tmp_path):
    st = SimpleNamespace(root=tmp_path)
    reg = friction.Register(tmp_path / "reg")
    p = retro.retro_prompt(st, reg, [], [], "## Rule effectiveness\n- x came back")
    assert "replacing or removing" in p and "not adding another" in p
