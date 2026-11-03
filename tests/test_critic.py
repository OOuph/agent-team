"""External critic reports → observation cards per product; repeats attach; unreadable or finding-less reports are not lost."""
from __future__ import annotations

from pathlib import Path

from team import config as C
from team import critic
from team.store import Store

SAMPLE = (Path(__file__).parent / "fixtures_critic_sample.md").read_text(encoding="utf-8")


def _settings(tmp_path: Path) -> C.Settings:
    st = C.Settings(dict(C.DEFAULTS), tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")
    st.raw["external_critic"] = {"enabled": True, "dir": str(tmp_path / "reports"), "default_product": "agent-team", "author": "critic"}
    (tmp_path / "reports").mkdir()
    return st


def _product(st: C.Settings, slug: str, host: str) -> C.Product:
    d = st.products_dir / slug
    for sub in ("backlog", "questions", "observations", "reviews"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    (d / "VISION.md").write_text(f"# {slug}\n", encoding="utf-8")
    return C.Product(slug, d, {**C.PRODUCT_DEFAULTS, "slug": slug, "host": host}, st)


def test_parse_sample_report() -> None:
    hosts = {"releases.formachines.ai": "release-radar", "luz.bymachines.ai": "luz-amanha"}
    fs = critic.parse_report(SAMPLE, hosts)
    ids = [f.id for f in fs]
    assert ids[:3] == ["TXT-RR-01", "TXT-AT-01", "TXT-LU-01"] and "TXT-DI-02" in ids and "TXT-FW-01" in ids
    by = {f.id: f for f in fs}
    assert by["TXT-RR-01"].slug == "release-radar" and by["TXT-RR-01"].priority == "P2"
    assert by["TXT-RR-01"].url == "https://releases.formachines.ai/" and by["TXT-RR-01"].heading == "Release Radar"
    assert by["TXT-DI-02"].slug == "agent-team-diary" and by["TXT-DI-02"].body.startswith("На `/try/`")
    assert by["TXT-FW-01"].slug == "agent-team"
    assert "Что закрывать первым" not in by["TXT-FW-01"].body       # the next heading ends the finding


def test_ingest_routes_and_dedupes(tmp_path: Path) -> None:
    st = _settings(tmp_path)
    rr = _product(st, "release-radar", "releases.formachines.ai")
    fw = _product(st, "agent-team", "")
    (tmp_path / "reports" / "r1.md").write_text(SAMPLE, encoding="utf-8")
    s_rr, s_fw = Store(rr.dir), Store(fw.dir)
    assert critic.ingest(st, rr, s_rr, [rr, fw]) == 1
    o = s_rr.observations()[0]
    assert o["classification"] == "feedback" and o["source"] == "external-critic" and o["fingerprint"] == "critic:TXT-RR-01"
    assert o["title"].startswith("[критик TXT-RR-01 P2]") and o["report"] == "r1.md"
    assert critic.ingest(st, rr, s_rr, [rr, fw]) == 0                      # already seen for this product
    assert critic.ingest(st, fw, s_fw, [rr, fw]) == 1                      # TXT-FW-01
    # the same finding id in a later report attaches to the live card
    (tmp_path / "reports" / "r2.md").write_text("## Release Radar\n\n**TXT-RR-01 · P1 · same thing again**\n\nStill there.\n", encoding="utf-8")
    assert critic.ingest(st, rr, s_rr, [rr, fw]) == 1
    obs = s_rr.observations()
    assert len(obs) == 1 and obs[0].get("repeat_count") == 1
    # a report without findings → one card to the default product only
    (tmp_path / "reports" / "r3.md").write_text("# Общий обзор\n\nНичего структурного.\n", encoding="utf-8")
    assert critic.ingest(st, rr, s_rr, [rr, fw]) == 0
    assert critic.ingest(st, fw, s_fw, [rr, fw]) == 1
    assert s_fw.observations()[-1]["fingerprint"] == "critic:report:r3.md"
    seen = critic.load_seen(st)
    assert sorted(seen["r1.md"]) == ["agent-team", "release-radar"]
    s_rr.close(); s_fw.close()


def test_disabled_or_missing_dir(tmp_path: Path) -> None:
    st = _settings(tmp_path)
    st.raw["external_critic"]["enabled"] = False
    p = _product(st, "release-radar", "releases.formachines.ai")
    s = Store(p.dir)
    assert critic.ingest(st, p, s, [p]) == 0
    st.raw["external_critic"] = {"enabled": True, "dir": str(tmp_path / "nope")}
    assert critic.ingest(st, p, s, [p]) == 0
    s.close()
