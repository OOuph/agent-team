from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from team import gsc

SITES = ["sc-domain:formachines.ai", "sc-domain:luz.bymachines.ai", "sc-domain:bymachines.ai"]


def test_property_for_picks_most_specific():
    assert gsc.property_for("agentsmd.formachines.ai", SITES) == "sc-domain:formachines.ai"
    assert gsc.property_for("formachines.ai", SITES) == "sc-domain:formachines.ai"
    assert gsc.property_for("luz.bymachines.ai", SITES) == "sc-domain:luz.bymachines.ai"
    assert gsc.property_for("example.com", SITES) is None
    assert gsc.property_for("notformachines.ai", SITES) is None
    assert gsc.property_for("x.example.com", ["sc-domain:example.com", "https://x.example.com/"]) == "https://x.example.com/"


def test_pick_new_first_then_stale():
    now = datetime(2026, 11, 10, tzinfo=timezone.utc)
    state = {"a": {"checked": "2026-11-01T00:00:00+00:00"}, "b": {"checked": "2026-11-09T00:00:00+00:00"}}
    picked = gsc.pick_for_inspection(["a", "b", "c", "d"], state, 3, 7, now)
    assert sorted(picked[:2]) == ["c", "d"] and picked[2] == "a"            # new first, then stale; fresh "b" skipped
    assert picked == gsc.pick_for_inspection(["a", "b", "c", "d"], state, 3, 7, now)   # same day → same sample
    urls = [f"u{i}" for i in range(1000)]
    assert gsc.pick_for_inspection(urls, {}, 50, 7, now) != urls[:50]       # not sitemap order


def test_collect_writes_md_and_filters_host(tmp_path: Path):
    calls = []

    def http(method, url, body, headers):
        calls.append((url, body))
        if "searchAnalytics" in url:
            dims = body["dimensions"]
            if dims == ["date"]:
                return {"rows": [{"keys": ["2026-11-08"], "clicks": 2, "impressions": 40, "ctr": 0.05, "position": 12.3}]}
            if dims == ["page"]:
                return {"rows": [{"keys": ["https://agentsmd.formachines.ai/repo/x/"], "clicks": 2, "impressions": 30, "position": 9}]}
            return {"rows": [{"keys": ["agents.md tracker"], "clicks": 1, "impressions": 5, "position": 4}]}
        return {"inspectionResult": {"indexStatusResult": {"verdict": "NEUTRAL", "coverageState": "Discovered - currently not indexed"}}}

    sm = "<urlset><url><loc>https://agentsmd.formachines.ai/</loc></url><url><loc>https://agentsmd.formachines.ai/repo/x/</loc></url></urlset>"
    p = SimpleNamespace(slug="agentsmd-tracker", host="agentsmd.formachines.ai", site_base="https://agentsmd.formachines.ai", dir=tmp_path)
    cfg = {**gsc.DEFAULTS, "inspect_per_day": 1}
    out = gsc.collect(p, cfg, "tok", SITES, http=http, fetch=lambda u: sm, now=datetime(2026, 11, 10, tzinfo=timezone.utc))
    assert out["totals"] == {"clicks": 2, "impressions": 40}
    assert out["inspection"]["inspected_today"] == 1 and out["inspection"]["sitemap_urls"] == 2
    q = [b for u, b in calls if "searchAnalytics" in u][0]
    assert q["dimensionFilterGroups"][0]["filters"][0]["expression"] == "://agentsmd.formachines.ai/"
    md = (tmp_path / "metrics" / "gsc.md").read_text()
    assert "Clicks from Google: 2" in md and "Discovered - currently not indexed: 1" in md and "agents.md tracker" in md


def test_collect_without_property(tmp_path: Path):
    p = SimpleNamespace(slug="x", host="example.com", site_base="https://example.com", dir=tmp_path)
    out = gsc.collect(p, dict(gsc.DEFAULTS), "tok", SITES, http=lambda *a: {}, fetch=lambda u: "")
    assert out["property"] is None and "no property" in out["error"]
