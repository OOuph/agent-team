"""`team cost`: spend per accepted task and prompt-cache share, read from the run folders and the backlog (owner 25.11)."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from team import config as C
from team import cost
from team.store import Store

NOW = datetime(2026, 11, 25, 12, 0, tzinfo=timezone.utc)


def _settings(tmp_path: Path) -> C.Settings:
    return C.Settings(dict(C.DEFAULTS), tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")


def _product(st: C.Settings, slug: str = "p1") -> C.Product:
    d = st.products_dir / slug
    for sub in ("backlog", "questions", "observations", "reviews", "digests"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    return C.Product(slug, d, {**C.PRODUCT_DEFAULTS, "slug": slug, "host": f"{slug}.example", "name": slug}, st)


def _run(p: C.Product, cycle: str, name: str, role: str, usd: float | None, *, ok: bool = True, age_days: float = 1.0,
         usage: dict | None = None, result: bool = True) -> None:
    d = p.runs_dir / cycle / name
    d.mkdir(parents=True, exist_ok=True)
    finished = (NOW - timedelta(days=age_days)).isoformat()
    (d / "run.json").write_text(json.dumps({"run_id": f"{cycle}-{name}", "role": role, "ok": ok, "cost_usd": usd,
                                            "num_turns": 3, "finished": finished}), encoding="utf-8")
    lines = [{"type": "assistant", "message": {"model": "m", "usage": {"input_tokens": 10, "cache_creation_input_tokens": 100,
                                                                      "cache_read_input_tokens": 0}}}]
    if result:
        lines.append({"type": "result", "total_cost_usd": usd, "usage": usage or {"input_tokens": 100, "cache_creation_input_tokens": 300,
                                                                                 "cache_read_input_tokens": 600, "output_tokens": 50}})
    (d / "events.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
    ts = (NOW - timedelta(days=age_days)).timestamp()
    os.utime(d / "run.json", (ts, ts))


def _task(s: Store, state: str) -> str:
    t = s.create_task(title=f"task {state}", type_="content", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po",
                      state="ready")
    path = s.dir / "backlog" / f"{t['id']}.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("state: ready", f"state: {state}"), encoding="utf-8")
    return t["id"]


def test_cost_per_accepted_task_counts_failed_work_in_the_numerator(tmp_path: Path) -> None:
    st = _settings(tmp_path)
    p = _product(st)
    s = Store(p.dir)
    a, b, c = _task(s, "accepted"), _task(s, "done"), _task(s, "rejected")
    _run(p, "20261124T1000", f"dev-{a}", "developer", 1.0)
    _run(p, "20261124T1000", f"review-{a}", "reviewer", 0.2)
    _run(p, "20261124T1100", f"dev-{b}", "developer", 0.5, ok=False)       # a failed attempt, then a redo
    _run(p, "20261124T1200", f"dev-{b}", "developer", 0.5)
    _run(p, "20261124T1300", f"dev-{c}", "developer", 2.0)                 # rejected work is still paid for
    _run(p, "20261124T1000", "po", "po", 0.8)                              # no task: overhead
    r = cost.collect(st, [p], days=7, now=NOW)
    t = r["tasks"]
    assert t["accepted"] == 2 and t["touched"] == 3
    assert abs(t["task_spend"] - 4.2) < 1e-9
    assert abs(t["per_accepted"] - 2.1) < 1e-9                             # (1.2 + 1.0 + 2.0) / 2: the rejected task's 2.0 is inside
    assert abs(t["per_accepted_all_in"] - 2.5) < 1e-9                      # with the PO overhead: 5.0 / 2
    assert abs(t["accepted_median"] - 1.1) < 1e-9                          # own cost of the two accepted tasks: 1.2 and 1.0
    assert abs(r["total"] - 5.0) < 1e-9
    dev = r["roles"]["developer"]
    assert dev["runs"] == 4 and dev["failed"] == 1 and abs(dev["usd"] - 4.0) < 1e-9


def test_no_accepted_tasks_gives_no_ratio(tmp_path: Path) -> None:
    st = _settings(tmp_path)
    p = _product(st)
    c = _task(Store(p.dir), "rejected")
    _run(p, "20261124T1300", f"dev-{c}", "developer", 2.0)
    r = cost.collect(st, [p], days=7, now=NOW)
    assert r["tasks"]["accepted"] == 0 and r["tasks"]["per_accepted"] is None and r["tasks"]["per_accepted_all_in"] is None
    text = cost.render(r)
    assert "accepted tasks: 0" in text and "$2.00" in text


def test_window_and_cache_share(tmp_path: Path) -> None:
    st = _settings(tmp_path)
    p = _product(st)
    _run(p, "20261124T1000", "po", "po", 1.0)
    _run(p, "20261110T1000", "po", "po", 9.0, age_days=15)                 # outside the 7-day window
    _run(p, "20261124T1100", "po", "po", 0.5, result=False)               # killed run: no result event, cost still booked
    r = cost.collect(st, [p], days=7, now=NOW)
    po = r["roles"]["po"]
    assert po["runs"] == 2 and abs(po["usd"] - 1.5) < 1e-9
    assert po["with_usage"] == 1
    assert abs(po["cache_read_share"] - 0.6) < 1e-9                        # 600 / (100 + 300 + 600)
    assert abs(po["first_call_cold_share"] - 1.0) < 1e-9                   # the first call read nothing from the cache
    assert abs(r["cache_read_share"] - 0.6) < 1e-9


def test_unknown_cost_is_reported_not_counted_as_zero(tmp_path: Path) -> None:
    st = _settings(tmp_path)
    p = _product(st)
    _run(p, "20261124T1000", "po", "po", None)
    r = cost.collect(st, [p], days=7, now=NOW)
    assert r["roles"]["po"]["unknown_cost"] == 1 and r["unknown_cost"] == 1
    assert "cost unknown" in cost.render(r)


def test_json_is_serialisable(tmp_path: Path) -> None:
    st = _settings(tmp_path)
    p = _product(st)
    _run(p, "20261124T1000", "po", "po", 1.0)
    json.dumps(cost.collect(st, [p], days=7, now=NOW))
