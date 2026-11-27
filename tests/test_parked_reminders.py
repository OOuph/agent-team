"""`cycle.queue.remind_parked`: tasks that leave their state only by somebody's decision are named to that somebody.

ai-battles 27.11: one `proposed` task for ten hours (only the PO's `promote_tasks` starts it) and five `deferred` ones
for 2–6 days (put on hold by the owner's word; the PO has no right to raise them)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from team import cycle as cycle_mod
from team.cycle import Cycle, daily_digest
from team.store import Store

from cycle_invariants import run_cycle
from test_parallel_lanes import LaneRunner, _product, _settings, _task

ON = {"cycle": {"queue": {"remind_parked": True}}}


def _park(p, state: str, hours: float, note: str, title: str) -> str:
    tid = _task(p, "developer", title)
    s = Store(p.dir)
    s.transition(tid, state, "owner" if state == "deferred" else "dispatcher", note, force=True)
    t = s.task(tid)
    t["history"][-1]["ts"] = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    s.save_task(t)
    s.close()
    return tid


def _setup(tmp_path: Path, over: dict):
    st = _settings(tmp_path, over)
    p = _product(st)
    old = _park(p, "proposed", 30.0, "hypothesis rule: no evidence from people", "a full test series of every mode")
    fresh = _park(p, "proposed", 2.0, "hypothesis rule: no evidence from people", "proposed an hour ago")
    held = _park(p, "deferred", 137.0, "владелец 21.11: мобильная разработка на паузе", "Телефон: полный экран на Android и iPhone")
    return st, p, old, fresh, held


def test_off_by_default_nothing_is_said(tmp_path: Path):
    st, p, old, fresh, held = _setup(tmp_path, {})
    r = LaneRunner()
    cyc = Cycle(st, p, r, "c1")
    assert cyc.proposed_block() == "" and cycle_mod.parked_digest_lines(p, cyc.store) == []
    assert "На паузе по слову владельца" not in daily_digest(p, cyc.store, "2026-11-28")
    cyc.store.close()


def test_the_po_is_told_about_old_proposed_tasks_and_not_about_deferred_ones(tmp_path: Path):
    st, p, old, fresh, held = _setup(tmp_path, ON)
    r = LaneRunner()
    cyc = Cycle(st, p, r, "c1")
    run_cycle(cyc, ["triage"])
    block = r.prompts["po"][0].split("PROPOSED TASKS NOBODY HAS PROMOTED")[1].split("\n# ")[0]      # the block itself, up to the next heading
    assert old in block and fresh not in block and held not in block and "promote_tasks" in block
    cyc.store.close()


def test_the_owner_is_told_about_deferred_tasks_in_the_daily_digest(tmp_path: Path):
    st, p, old, fresh, held = _setup(tmp_path, ON)
    s = Store(p.dir)
    text = daily_digest(p, s, "2026-11-28")
    s.close()
    assert "На паузе по слову владельца (1); вернуть может только владелец или оператор:" in text
    line = [ln for ln in text.splitlines() if ln.startswith(f"- {held}")][0]
    assert "6 дн." in line and "мобильная разработка на паузе" in line and old not in text.split("На паузе")[1]
