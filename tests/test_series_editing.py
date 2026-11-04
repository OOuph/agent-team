"""Series mode: tasks of the daily series get the rubric, Opus, three rounds and a numeric bar enforced in code."""
from __future__ import annotations

from pathlib import Path

from team import config as C
from team import editorial


def _st(tmp_path: Path) -> C.Settings:
    st = C.Settings(dict(C.DEFAULTS), tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")
    (tmp_path / "docs" / "notes").mkdir(parents=True)
    (tmp_path / "docs" / "notes" / "series-editorial-rubric.md").write_text("# rubric\n- no AI tells", encoding="utf-8")
    return st


def test_series_detection_and_rubric(tmp_path: Path) -> None:
    st = _st(tmp_path)
    s = editorial.series_mode(st, {"title": "Pivots 4/7, 08.11 10:00 UTC (dev.to): it grew out of Errata"})
    assert s and s["editor_model"] == "opus" and s["rounds"] == 3 and "no AI tells" in s["rubric"]
    assert editorial.series_mode(st, {"title": "Telegram post 05.11"}) is None
    st.raw["editorial"] = {"series": {"enabled": False}}
    assert editorial.series_mode(st, {"title": "Pivots 1/7"}) is None


def test_bar_enforced(tmp_path: Path) -> None:
    s = editorial.series_mode(_st(tmp_path), {"title": "Pivots 1/7"})
    good = {"verdict": "approve", "scores": {a: 5 for a in editorial.AXES}, "final_text": "x"}
    assert editorial.enforce_series_bar(good, s, last_round=False)["verdict"] == "approve"
    weak = {"verdict": "approve", "scores": {**{a: 5 for a in editorial.AXES}, "tone": 3}, "final_text": "x"}
    r = editorial.enforce_series_bar(weak, s, last_round=False)
    assert r["verdict"] == "revise" and r["final_text"] is None and "tone" in r["issues"][-1]
    assert editorial.enforce_series_bar(weak, s, last_round=True)["verdict"] == "reject"
    mean_low = {"verdict": "approve", "scores": {a: 4 for a in editorial.AXES}}
    assert editorial.enforce_series_bar(mean_low, s, last_round=False)["verdict"] == "revise"
    assert editorial.enforce_series_bar({"verdict": "revise"}, s, last_round=True)["verdict"] == "reject"
    missing = {"verdict": "approve", "scores": {"hook": 5}}
    assert editorial.enforce_series_bar(missing, s, last_round=False)["verdict"] == "revise"


def test_series_prompt_has_rubric_and_formats(tmp_path: Path) -> None:
    st = _st(tmp_path)
    d = st.products_dir / "agent-team-diary"
    d.mkdir(parents=True)
    (d / "VISION.md").write_text("# diary\n## Tone\nliving story", encoding="utf-8")
    p = C.Product("agent-team-diary", d, {**C.PRODUCT_DEFAULTS, "slug": "agent-team-diary"}, st)
    t = {"id": "T-1", "title": "Pivots 1/7"}
    s = editorial.series_mode(st, t)
    out = editorial.editor_prompt(p, t, [("a.json", {"channel": "telegram", "text": "x"})], [], [], 2, ["fix hook"], series=s)
    assert "SERIES RUBRIC" in out and "up to 4000" in out and "ROUND 1" in out and "Telegram ≤ 600" not in out
