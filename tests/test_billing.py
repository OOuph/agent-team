"""Plan limits, usage credits and the shared $100/day cap (owner 04.11)."""
from __future__ import annotations

from pathlib import Path

from team import billing
from team import config as C

DAY = 1793952000.0          # 2026-11-06 08:00 UTC


def _st(tmp_path: Path) -> C.Settings:
    return C.Settings(dict(C.DEFAULTS), tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")


def _info(util: float, ovs: str = "allowed", using: bool = False, status: str = "allowed") -> dict:
    return {"status": status, "resetsAt": 1794045600, "rateLimitType": "seven_day", "overageStatus": ovs,
            "isUsingOverage": using, "overageResetsAt": 1796083200,
            "unifiedWindows": {"seven_day": {"utilization": util, "status": status}}}


def test_limit_info_from_events() -> None:
    assert billing.limit_info([{"type": "x"}, {"rate_limit_info": {"a": 1}}, {"type": "result"}]) == {"a": 1}
    assert billing.limit_info([{"type": "result"}]) is None


def test_overage_notified_once_and_credit_spend(tmp_path: Path) -> None:
    st, sent = _st(tmp_path), []
    assert billing.observe(st, _info(0.73), "r1", sent.append, now=DAY, cost=1.0) == []
    assert billing.observe(st, _info(1.0, using=True), "r2", sent.append, now=DAY + 60, cost=2.0) == ["overage_started"]
    assert billing.observe(st, _info(1.0, using=True), "r3", sent.append, now=DAY + 120, cost=2.0) == []
    assert len(sent) == 1 and "кредитах" in sent[0]
    s = billing.load(st)
    assert s["spend"]["2026-11-06"] == 5.0 and s["credit_spend"]["2026-11-06"] == 4.0
    assert billing.paused(st, now=DAY + 200) is None


def test_shared_daily_cap(tmp_path: Path) -> None:
    st, sent = _st(tmp_path), []
    st.raw["billing"] = {"daily_cap_usd_all": 10.0}
    billing.observe(st, _info(0.5), "a", sent.append, now=DAY, cost=6.0)
    assert billing.paused(st, now=DAY + 1) is None and billing.remaining_today(st, now=DAY + 1) == 4.0
    assert billing.observe(st, None, "b", sent.append, now=DAY + 2, cost=4.5) == ["daily_cap"]
    assert "shared daily spend cap" in (billing.paused(st, now=DAY + 3) or "")
    assert billing.paused(st, now=DAY + 86400 + 1) is None              # next UTC day
    assert len(sent) == 1 and "$10" in sent[0]


def test_default_cap_is_100(tmp_path: Path) -> None:
    assert billing.daily_cap(_st(tmp_path)) == 100.0


def test_exhausted_pauses_until_reset_and_credits_warning(tmp_path: Path) -> None:
    st, sent = _st(tmp_path), []
    assert billing.observe(st, _info(0.92, ovs="rejected"), "a", sent.append, now=DAY) == ["credits_unavailable"]
    out = billing.observe(st, _info(1.0, ovs="rejected", status="rejected"), "b", sent.append, now=DAY + 5)
    assert out == ["exhausted"] and "exhausted" in (billing.paused(st, now=DAY + 10) or "")
    assert billing.paused(st, now=1796083200 + 1) is None
