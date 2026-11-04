"""Analyst sees every recent day of the metrics, compactly (ops 04.11, release-radar Q-0009)."""
from __future__ import annotations

import json
from pathlib import Path

from team import prompts


def _day(humans: int, returns: int) -> dict:
    return {"requests_raw": 1000, "status": {"2xx": 900}, "humans": {"requests": humans * 2, "sources": humans, "engaged": 1, "returns": returns},
            "agents": {"requests": 5, "sources": 1, "by_family": {"curl": 5}}, "crawlers": {"requests": 400, "sources": 9, "by_bot": {"Googlebot": 40}},
            "scanners": {"requests": 3, "sources": 1},
            "targets": {"/go/telegram/": {"requests": 2, "human": {"sources": 1}, "agent": {"sources": 0}, "crawler": {"requests": 1}}, "/feed.xml": {"requests": 0}},
            "top_pages": [{"path": f"/p{i}", "requests": 100 - i} for i in range(20)] + [{"path": "/x" * 500, "requests": 1}],
            "referers": [{"host": "t.me", "requests": 3}], "by_src": {"bluesky": {"requests": 4, "human_sources": 1, "engaged_sources": 0}}}


def test_digest_keeps_newest_days_and_is_compact(tmp_path: Path) -> None:
    old = {"generated": "2026-11-03T07:00:00+00:00", "targets": ["/go/telegram/", "/feed.xml"], "days": {"2026-11-02": _day(43, 0), "2026-11-03": _day(1, 0)}}
    new = {"generated": "2026-11-04T07:00:00+00:00", "targets": ["/go/telegram/", "/feed.xml"], "days": {"2026-11-02": _day(43, 0), "2026-11-03": _day(18, 2), "2026-11-04": _day(5, 1)}}
    (tmp_path / "2026-11-03.json").write_text(json.dumps(old))
    (tmp_path / "2026-11-04.json").write_text(json.dumps(new))
    out = prompts.metrics_digest(sorted(tmp_path.glob("*.json")))
    assert out.index("## 2026-11-04") < out.index("## 2026-11-03") < out.index("## 2026-11-02")
    assert "sources 18, engaged 1, returns 2" in out                 # the newest file wins for a day
    assert "/go/telegram/ req 2 · human src 1" in out and "/feed.xml" not in out
    assert "bluesky req 4 human 1" in out and "/p5" not in out and len(out) < 4000
    assert prompts.metrics_digest([]) == "(no metrics files yet)"
