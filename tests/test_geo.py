"""Tests for team.geo: prompt, query list, metrics summary, observation cards, schema validity of a sample result."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import jsonschema
import yaml

from team import geo

ROOT = Path(__file__).resolve().parent.parent
QUERIES = ["which npm packages had breaking changes this week", "does express 5 have breaking changes", "preço da luz amanhã por hora Portugal"]


def sample_result() -> dict:
    return {
        "queries": [
            {"query": QUERIES[0], "engine_note": "WebSearch, 10 results", "our_site_cited": True, "our_url": "https://releases.formachines.ai/",
             "position": 3, "competitors": ["github.com", "npmjs.com"], "our_page_exists": True, "note": ""},
            {"query": QUERIES[1], "engine_note": "WebSearch, 10 results", "our_site_cited": False, "our_url": None,
             "position": None, "competitors": ["expressjs.com", "github.com", "stackoverflow.com"], "our_page_exists": False, "note": "no /pkg/npm/express/ page"},
            {"query": QUERIES[2], "engine_note": "not_observable", "our_site_cited": False, "our_url": None,
             "position": None, "competitors": [], "our_page_exists": False, "note": "search tool returned an error"},
        ],
        "summary_md": "1 из 3 вопросов цитирует нас.",
        "observations": [{"title": "404 on /pkg/npm/express/", "classification": "reproducible_defect",
                          "url": "https://releases.formachines.ai/pkg/npm/express/", "actual": "HTTP 404"}],
    }


def test_prompt_contains_all_queries_and_site():
    p = geo.build_prompt("Release Radar", "https://releases.formachines.ai", QUERIES, "| Разработчик | «что сломалось» |")
    for q in QUERIES:
        assert q in p
    assert "releases.formachines.ai" in p and "Разработчик" in p and "not_observable" in p


def test_queries_for_product_yaml_or_default():
    assert geo.queries_for({"slug": "release-radar", "geo_queries": ["a", " b ", "a"]}) == ["a", "b"]
    assert geo.queries_for({"slug": "agentsmd-tracker"}) == geo.DEFAULT_QUERIES["agentsmd-tracker"]
    assert geo.queries_for({"slug": "unknown"}) == []
    assert len(geo.queries_for({"slug": "x", "geo_queries": [str(i) for i in range(20)]})) == geo.MAX_QUERIES


def test_product_yaml_geo_queries_present():
    for slug in ("release-radar", "agentsmd-tracker", "luz-amanha"):
        raw = yaml.safe_load((ROOT / "products" / slug / "product.yaml").read_text(encoding="utf-8"))
        qs = geo.queries_for(raw)
        assert len(qs) == 5 and qs == raw["geo_queries"]
    luz = geo.queries_for(yaml.safe_load((ROOT / "products" / "luz-amanha" / "product.yaml").read_text(encoding="utf-8")))
    assert sum("portugal" in q.lower() and ("amanhã" in q.lower() or "omie" in q.lower()) for q in luz) == 3


def test_to_metrics_counts_cited():
    m = geo.to_metrics(sample_result(), "2026-11-02")
    assert m["date"] == "2026-11-02" and m["total"] == 3 and m["cited"] == 1 and m["observable"] == 2 and m["pages_missing"] == 2
    assert m["top_competitors"][0] == "github.com"
    assert [q["cited"] for q in m["queries"]] == [True, False, False] and m["queries"][2]["observable"] is False
    assert geo.to_metrics({}, "d")["total"] == 0


def test_to_observations_fingerprint_and_class():
    cards = geo.to_observations(sample_result(), "release-radar")
    by_q = {c.get("query"): c for c in cards if c.get("query")}
    assert by_q[QUERIES[0]]["classification"] == "success"
    gap = by_q[QUERIES[1]]
    assert gap["classification"] == "need_hypothesis" and gap["title"].startswith("GEO: create a page for query")
    assert "expressjs.com" in gap["actual"] and gap["source"] == "geo-probe"
    assert gap["fingerprint"] == hashlib.sha1(f"geo|{QUERIES[1]}".encode()).hexdigest()[:12] == geo.fingerprint(QUERIES[1])
    assert QUERIES[2] not in by_q                                   # not_observable → no card
    defects = [c for c in cards if c["classification"] == "reproducible_defect"]
    assert len(defects) == 1 and defects[0]["url"].endswith("/pkg/npm/express/")
    # page exists but not cited → still a hypothesis, different title
    r = sample_result(); r["queries"][1]["our_page_exists"] = True
    assert geo.to_observations(r, "x")[1]["title"].startswith("GEO: page exists but not cited")


def test_sample_result_matches_schema():
    schema = json.loads((ROOT / "team" / "schemas" / "geo-report.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(sample_result(), schema)
    bad = sample_result(); bad["queries"][0]["extra"] = 1
    try:
        jsonschema.validate(bad, schema)
    except jsonschema.ValidationError:
        pass
    else:
        raise AssertionError("additionalProperties must be rejected")


def test_role_card_allows_web_tools():
    from team.runner import DISALLOWED_DEFAULT, front_matter
    meta, body = front_matter((ROOT / "team" / "roles" / "geo-probe.md").read_text(encoding="utf-8"))
    assert meta["name"] == "geo-probe" and set(meta["tools"]) == {"WebSearch", "WebFetch"} and "not_observable" in body
    assert {"WebSearch", "WebFetch"} <= set(DISALLOWED_DEFAULT)     # the runner still blocks them by default: see the report
