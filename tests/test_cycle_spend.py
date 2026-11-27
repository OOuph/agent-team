"""A cycle's spend is the sum of its own runs (27.11): the difference of `spent_today()` at its end and its start went
negative for a cycle that crossed midnight UTC — ai-battles `20261126T2350` showed «$-345.13», its real spend was 12.64."""
from __future__ import annotations

from pathlib import Path

from team.cycle import Cycle
from team.store import Store

from test_store_cycle import FakeRunner, _product, _settings


def test_spent_by_cycle_sums_the_runs_of_that_cycle_whatever_the_day(tmp_path: Path):
    s = Store(tmp_path / "p")
    s.db.execute("INSERT INTO spend(day, run_id, role, usd, reported, ts) VALUES('2026-11-26','20261126T2350-po','po',2.5,1,'2026-11-26T23:52:00+00:00')")
    s.db.execute("INSERT INTO spend(day, run_id, role, usd, reported, ts) VALUES('2026-11-27','20261126T2350-dev-T-0001','developer',1.25,1,'2026-11-27T00:10:00+00:00')")
    s.db.execute("INSERT INTO spend(day, run_id, role, usd, reported, ts) VALUES('2026-11-26','20261126T2310-po','po',300.0,1,'2026-11-26T23:12:00+00:00')")
    s.db.execute("INSERT INTO spend(day, run_id, role, usd, reported, ts) VALUES('2026-11-27','20261126T23500-x','po',9.0,1,'2026-11-27T00:11:00+00:00')")
    s.db.commit()
    assert s.spent_by_cycle("20261126T2350") == 3.75                      # both days, only this cycle's runs
    assert s.spent_by_cycle("20261126T9999") == 0.0
    assert s.spent_by_cycle("c_1%") == 0.0                                # no pattern characters leak into the match
    s.close()


def test_a_cycle_over_midnight_reports_what_it_spent(tmp_path: Path, monkeypatch):
    st = _settings(tmp_path)
    p = _product(st)
    s = Store(p.dir)
    s.db.execute("INSERT INTO spend(day, run_id, role, usd, reported, ts) VALUES('2000-01-01','old-run','po',345.0,1,'2000-01-01T23:00:00+00:00')")
    s.db.commit()
    calls = {"n": 0}
    real = Store.spent_today

    def spent_today(self):                                                # the day turned over while the cycle ran
        calls["n"] += 1
        return 345.0 if calls["n"] == 1 else real(self)

    monkeypatch.setattr(Store, "spent_today", spent_today)
    res = Cycle(st, p, FakeRunner(), "c1").run()
    assert res["spent_usd"] >= 0 and abs(res["spent_usd"] - s.spent_by_cycle("c1")) < 1e-6 and res["spent_usd"] > 0
    row = s.db.execute("SELECT spent_usd FROM cycles WHERE cycle_id='c1'").fetchone()
    assert abs(row[0] - res["spent_usd"]) < 1e-6
    s.close()
