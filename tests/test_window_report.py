"""`team window`: one yardstick for any two windows (28.11: «how do we compare yesterday and today?»)."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from team import windowreport as W
from team.store import Store

from test_parallel_lanes import _product, _settings

REPO = Path(__file__).resolve().parent.parent


def _fill(tmp_path: Path):
    (tmp_path / "team.yaml").write_text("paths:\n  products_dir: products\n  data_dir: data\n  repos_dir: data/repos\n", encoding="utf-8")
    st = _settings(tmp_path)
    p = _product(st)
    Store(p.dir).close()                                               # creates the tables
    db = sqlite3.connect(p.dir / "team.db")
    def ev(ts, kind, subject, actor, detail):
        db.execute("INSERT INTO events(ts, kind, subject, actor, detail) VALUES(?,?,?,?,?)", (ts + "+00:00", kind, subject, actor, json.dumps(detail)))
    def tr(ts, task, frm, to, note="", actor="dispatcher"):
        ev(ts, "task.transition", task, actor, {"from": frm, "to": to, "note": note})
    # the window 28.11 00:00–04:00: one cycle 00:00–01:00 and one 02:00–03:20
    for cid, a, b in (("c1", "2026-11-28T00:00:00", "2026-11-28T01:00:00"), ("c2", "2026-11-28T02:00:00", "2026-11-28T03:20:00")):
        db.execute("INSERT INTO cycles(cycle_id, started, finished, phases, spent_usd) VALUES(?,?,?,?,?)", (cid, a + "+00:00", b + "+00:00", "[]", 1.0))
    tr("2026-11-28T00:05:00", "T-0001", "ready", "claimed"); tr("2026-11-28T00:30:00", "T-0001", "in_progress", "released", "merged abc")
    tr("2026-11-28T00:50:00", "T-0001", "released", "accepted", "ok", "reviewer")
    tr("2026-11-28T00:06:00", "T-0002", "ready", "claimed"); tr("2026-11-28T00:40:00", "T-0002", "in_progress", "in_progress", "partial — continues next cycle")
    tr("2026-11-28T02:05:00", "T-0003", "ready", "claimed"); tr("2026-11-28T02:45:00", "T-0003", "in_progress", "ready", "run failed: no result event (exit 143): ")
    ev("2026-11-28T00:30:00", "run", "c1-dev-T-0001", "developer", {"ok": True, "cost": 1.0, "error": ""})
    ev("2026-11-28T00:40:00", "run", "c1-dev-T-0002", "developer", {"ok": True, "cost": 1.0, "error": ""})
    ev("2026-11-28T02:45:00", "run", "c2-dev-T-0003", "developer", {"ok": False, "cost": 0.5, "error": "no result event (exit 143): "})
    ev("2026-11-28T00:20:00", "run", "c1-po", "po", {"ok": True, "cost": 2.0, "error": ""})         # not an executor
    ev("2026-11-28T02:10:00", "task.dependency_dead", "T-0004", "dispatcher", {"dead": ["T-0001"]})
    tr("2026-11-27T12:00:00", "T-0900", "released", "accepted", "another window", "reviewer")
    for ts, usd in (("2026-11-28T00:30:00", 1.0), ("2026-11-28T02:45:00", 0.5), ("2026-11-27T12:00:00", 9.0)):
        db.execute("INSERT INTO spend(day, run_id, role, usd, reported, ts) VALUES(?,?,?,?,?,?)", (ts[:10], "r", "developer", usd, 1, ts + "+00:00"))
    db.commit(); db.close()
    (st.data_dir / "logs").mkdir(parents=True, exist_ok=True)
    (st.data_dir / "cron.log").write_text(
        "2026-11-28 00:40:00,000 INFO [1 exec-developer-1] team.lanes: fleets: lane exec-developer-1 ended after 2000s\n"      # idle 20 min of cycle 1
        "2026-11-28 01:00:00,000 INFO [1 exec-developer-2] team.lanes: fleets: lane exec-developer-2 ended after 3000s\n"      # worked to the end
        "2026-11-28 00:50:00,000 INFO [1 exec-developer-1] team.lanes: other: lane exec-developer-1 ended after 10s\n"         # another product
        "2026-11-28 02:50:00,000 INFO [1 exec-developer-1] team.lanes: fleets: lane exec-developer-1 ended after 2700s\n",     # idle 30 min of cycle 2; seat 2 had no lane: 80 min
        encoding="utf-8")
    (st.data_dir / "logs" / "stall-watch.log").write_text(
        '2026-11-28T00:10:00Z INFO what=heartbeat x load="10.00 9 8" mem\n2026-11-28T00:20:00Z INFO what=heartbeat x load="30.00 9 8" mem\n'
        '2026-11-28T01:30:00Z INFO what=heartbeat x load="99.00 9 8" mem\n2026-11-28T02:30:00Z INFO what=heartbeat x load="20.00 9 8" mem\n', encoding="utf-8")
    return st, p


def test_the_measures_of_a_window(tmp_path: Path):
    st, p = _fill(tmp_path)
    m = W.measure(p.dir, st.data_dir, p.slug, W.parse_when("2026-11-28T00:00:00"), W.parse_when("2026-11-28T04:00:00"), seats=2)
    assert (m["accepted"], m["merged"], m["taken"]) == (1, 1, 3)
    assert (m["exec_runs"], m["failed_runs"], m["partial"], m["partial_share"], m["signalled"], m["dead_deps"]) == (3, 1, 1, 33.3, 1, 1)
    assert (m["cycles"], m["cycle_med_min"], m["cycle_max_min"]) == (2, 70.0, 80.0)
    assert m["idle_min"] == 20 + 0 + 30 + 80 and m["idle_share"] == round(100 * 130 / (2 * 60 + 2 * 80), 1)
    assert m["merge_to_accept_min"] == 20.0 and m["spent_usd"] == 1.5
    assert (m["load_med"], m["load_max"]) == (20.0, 30.0)               # the sample between the cycles (99) is not the cycles' load
    empty = W.measure(p.dir, st.data_dir, p.slug, W.parse_when("2026-11-20T00:00:00"), W.parse_when("2026-11-20T04:00:00"), seats=2)
    assert empty["accepted"] == 0 and empty["cycles"] == 0 and empty["idle_min"] is None and empty["partial_share"] is None


def test_the_command_prints_two_windows_side_by_side(tmp_path: Path):
    st, p = _fill(tmp_path)
    env = {**os.environ, "PYTHONPATH": str(REPO), "TEAM_ROOT": str(tmp_path)}
    cp = subprocess.run([sys.executable, "-m", "team.cli", "window", "fleets", "--from", "2026-11-28T00:00", "--to", "2026-11-28T04:00",
                         "--vs-from", "2026-11-27T10:00", "--vs-to", "2026-11-27T14:00", "--seats", "2"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0, cp.stderr
    assert "| Принято задач | 1 | 1 | +0 |" in cp.stdout and "| Доля «partial» | 33.3% | — | — |" in cp.stdout
    assert "| Простой мест (developer) внутри циклов | 130 мин | — | — |" in cp.stdout and "| Расход | 1.5 $ | 9 $ | -7.5 |" in cp.stdout
    bad = subprocess.run([sys.executable, "-m", "team.cli", "window", "fleets"], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60)
    assert bad.returncode == 2
