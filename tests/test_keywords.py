"""Keyword data for the analyst (owner 28.11, team/keywords.py): DataForSEO and Bing are called by the dispatcher within
the product's ceiling; the analyst gets tables, never keys; a task is not `done` without its deliverable. No network:
every HTTP call goes to a fake."""
from __future__ import annotations

import json
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

from team import config as C
from team import gitops, keywords
from team.cycle import Cycle, analyst_not_done, required_outputs
from team.runner import RunResult
from team.store import Store

ENV = {"DATAFORSEO_LOGIN": "login-secret-1", "DATAFORSEO_PASSWORD": "pw-secret-2", "BING_WEBMASTER_API_KEY": "bingkeysecret3"}


class FakeAPI:
    """DataForSEO + Bing answers; counts paid posts; the balance falls by each task's cost."""

    def __init__(self, balance: float = 49.6, cost: float = 0.09, bing: dict[str, int] | None = None):
        self.balance, self.cost = balance, cost
        self.calls: list[tuple[str, str]] = []
        self.bing = bing if bing is not None else {"premier league corners": 120, "championship corners": 0}

    def __call__(self, method: str, url: str, body: Any, headers: dict) -> Any:
        self.calls.append((method, url))
        if "appendix/user_data" in url:
            return {"status_code": 20000, "tasks": [{"status_code": 20000, "result": [{"money": {"balance": self.balance}}]}]}
        if "search_volume/live" in url:
            self.balance -= self.cost
            kws = body[0]["keywords"]
            res = [{"keyword": k, "search_volume": 10 * (i + 1) if body[0].get("location_code") else 100 * (i + 1), "cpc": None,
                    "competition": "LOW", "monthly_searches": [{"year": 2026, "month": 10, "search_volume": 1},
                                                               {"year": 2025, "month": 11, "search_volume": 1}]}
                   for i, k in enumerate(kws)]
            return {"status_code": 20000, "cost": self.cost, "tasks": [{"status_code": 20000, "cost": self.cost, "result": res}]}
        if "ranked_keywords/live" in url:
            self.balance -= 0.02
            items = [{"keyword_data": {"keyword": "championship corners stats", "keyword_info": {"search_volume": 320}},
                      "ranked_serp_element": {"serp_item": {"rank_group": 3, "relative_url": "/corners/"}}}]
            return {"status_code": 20000, "cost": 0.02, "tasks": [{"status_code": 20000, "cost": 0.02, "result": [{"items": items}]}]}
        if "ssl.bing.com" in url:
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            assert q["apikey"] == [ENV["BING_WEBMASTER_API_KEY"]]
            phrase = q["q"][0]
            if "GetRelatedKeywords" in url:
                return {"d": [{"Query": phrase + " stats", "Impressions": 7, "BroadImpressions": 9},
                              {"Query": phrase + " zero", "Impressions": 0, "BroadImpressions": 1}]}
            return {"d": {"Query": phrase, "Impressions": self.bing.get(phrase, 0), "BroadImpressions": 2 * self.bing.get(phrase, 0)}}
        raise AssertionError(f"unexpected call {url}")

    def paid(self) -> int:
        return sum(1 for _, u in self.calls if "/live" in u)


def _cfg(**over: Any) -> dict[str, Any]:
    return keywords.settings({"keywords": {"enabled": True, "dataforseo_cap_usd": 10, **over}})


def _req(cfg: dict[str, Any], **over: Any) -> dict[str, Any]:
    raw = {"keywords": ["Premier League corners", "championship corners", "premier league corners"], "locations": ["gb", "all"], **over}
    return keywords.normalize(raw, cfg)[0]


def test_normalize_drops_what_the_apis_refuse():
    cfg = _cfg()
    req, notes = keywords.normalize({"keywords": ["BTTS stats", "btts stats", "corners, cards!", "x " * 12, ""],
                                     "locations": ["GB", "mars"], "sources": ["ranked_keywords", "bing_keyword", "telepathy"],
                                     "competitors": ["https://www.soccerstats.com/latest.asp", "not a domain"]}, cfg)
    assert req["keywords"] == ["btts stats"] and req["locations"] == ["gb"]
    assert req["sources"] == ["ranked_keywords", "bing_keyword"] and req["competitors"] == ["soccerstats.com"]
    assert any("mars" in n for n in notes) and any("telepathy" in n for n in notes) and any("not a domain" in n for n in notes)
    assert keywords.normalize({"keywords": ["a"], "sources": ["ranked_keywords"]}, cfg)[0]["sources"] == []
    assert keywords.estimate_usd(_req(cfg), cfg) == pytest.approx(0.18)       # one volume request per location


def test_fetch_books_the_cost_and_writes_tables(tmp_path: Path):
    cfg, api, s = _cfg(), FakeAPI(), Store(tmp_path / "p")
    req = _req(cfg)
    res = keywords.fetch(req, cfg, out_dir=tmp_path / "kw", store=s, task_id="T-1", env=ENV, http=api, sleep=lambda _: None)
    assert res["volume"]["gb"]["premier league corners"]["volume"] == 10 and res["volume"]["all"]["championship corners"]["volume"] == 200
    assert res["volume"]["gb"]["premier league corners"]["months"] == "2025-11…2026-10"
    assert api.paid() == 2 and s.api_spent("dataforseo") == pytest.approx(0.18) and s.api_calls("dataforseo") == 2
    assert [x["balance_before"] for x in res["spend"]] == [pytest.approx(49.6), pytest.approx(49.51)]
    assert res["bing"]["gb"]["premier league corners"] == {"impressions": 120, "broad": 240}
    # related only for gb and only for a phrase with impressions
    assert list(res["bing_related"]["gb"]) == ["premier league corners"]
    assert res["bing_related"]["gb"]["premier league corners"] == [{"query": "premier league corners stats", "impressions": 7, "broad": 9}]
    md = (tmp_path / "kw" / f"T-1-{res['key']}.md").read_text()
    assert "| premier league corners | 10 | 100 |" in md and "DataForSEO" in md and "Bing Webmaster impressions" in md
    assert "120 (240)" in md and "ceiling $10.00" in md
    for f in (tmp_path / "kw").rglob("*"):
        if f.is_file():
            txt = f.read_text()
            assert not any(v in txt for v in ENV.values()), f


def test_a_second_fetch_of_the_same_request_pays_nothing(tmp_path: Path):
    cfg, api, s = _cfg(), FakeAPI(), Store(tmp_path / "p")
    req = _req(cfg)
    keywords.fetch(req, cfg, out_dir=tmp_path / "kw", store=s, task_id="T-1", env=ENV, http=api, sleep=lambda _: None)
    n = len(api.calls)
    res = keywords.fetch(req, cfg, out_dir=tmp_path / "kw", store=s, task_id="T-1", env=ENV, http=api, sleep=lambda _: None)
    assert len(api.calls) == n and s.api_calls("dataforseo") == 2 and res["spent_usd"] == 0
    assert res["volume"]["gb"]["premier league corners"]["volume"] == 10       # from the checkpoint files


def test_the_ceiling_refuses_the_call_that_would_pass_it(tmp_path: Path):
    cfg, api, s = _cfg(dataforseo_cap_usd=0.1), FakeAPI(), Store(tmp_path / "p")
    res = keywords.fetch(_req(cfg), cfg, out_dir=tmp_path / "kw", store=s, task_id="T-1", env=ENV, http=api, sleep=lambda _: None)
    assert api.paid() == 1 and s.api_spent("dataforseo") == pytest.approx(0.09)
    assert "ceiling of this product is $0.10" in res["refused"] and "all" not in res["volume"]
    assert any("not called" in e["error"] and e["what"] == "volume all" for e in res["errors"])
    assert res["bing"]["gb"]                                                   # the free part still runs
    zero = _cfg(dataforseo_cap_usd=0)
    res0 = keywords.fetch(_req(zero, keywords=["btts stats"]), zero, out_dir=tmp_path / "kw0", store=Store(tmp_path / "p0"), task_id="T-2",
                          env=ENV, http=FakeAPI(), sleep=lambda _: None)
    assert res0["refused"] and not res0["volume"]


def test_the_balance_refuses_a_call_it_cannot_pay(tmp_path: Path):
    cfg, api, s = _cfg(), FakeAPI(balance=0.05), Store(tmp_path / "p")
    res = keywords.fetch(_req(cfg, sources=["google_ads_volume"]), cfg, out_dir=tmp_path / "kw", store=s, task_id="T-1", env=ENV, http=api)
    assert api.paid() == 0 and s.api_calls("dataforseo") == 0 and "balance $0.0500" in res["refused"]
    assert not keywords.has_data(res)


def test_no_credentials_no_call_and_the_key_never_shows(tmp_path: Path):
    cfg, api, s = _cfg(), FakeAPI(), Store(tmp_path / "p")
    res = keywords.fetch(_req(cfg), cfg, out_dir=tmp_path / "kw", store=s, task_id="T-1", env={}, http=api)
    assert api.calls == [] and "DATAFORSEO_LOGIN" in res["refused"]
    assert any("BING_WEBMASTER_API_KEY is not set" in e["error"] for e in res["errors"])

    def boom(method, url, body, headers):
        raise OSError(f"connection reset while reading {url}")
    res = keywords.fetch(_req(cfg, sources=["bing_keyword"]), cfg, out_dir=tmp_path / "kw2", store=s, task_id="T-1", env=ENV, http=boom,
                         sleep=lambda _: None)
    blob = json.dumps(res) + (tmp_path / "kw2" / f"T-1-{res['key']}.md").read_text()
    assert "connection reset" in blob and ENV["BING_WEBMASTER_API_KEY"] not in blob and "apikey=***" in blob


def test_ranked_keywords_per_competitor_and_location(tmp_path: Path):
    cfg, api, s = _cfg(), FakeAPI(), Store(tmp_path / "p")
    req, _ = keywords.normalize({"sources": ["ranked_keywords"], "competitors": ["soccerstats.com"], "locations": ["gb", "all"],
                                 "include": ["corners", "btts"]}, cfg)
    res = keywords.fetch(req, cfg, out_dir=tmp_path / "kw", store=s, task_id="T-1", env=ENV, http=api)
    assert res["ranked"] == [{"keyword": "championship corners stats", "location": "United Kingdom", "volume": 320,
                              "competitor": "soccerstats.com", "position": 3, "url": "/corners/"}]
    assert any("'all'" in e["error"] for e in res["errors"]) and s.api_spent("dataforseo") == pytest.approx(0.02)
    body = keywords._ranked_body("x.com", cfg["locations"]["gb"], "en", 200, ["corners", "btts"])
    assert body["filters"] == [["keyword_data.keyword", "like", "%corners%"], "or", ["keyword_data.keyword", "like", "%btts%"]]


def test_role_processes_never_get_the_keys(tmp_path: Path, monkeypatch):
    from team import runner as R
    seen: dict[str, str] = {}

    def popen(argv, cwd, env, **kw):
        seen.update(env)
        raise FileNotFoundError
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(R.subprocess, "Popen", popen)
    rr = R.Runner({"binary": "claude", "auth": "login"}, {"analyst": {"description": "d", "prompt": "p"}}).run(
        "analyst", "hi", None, max_usd=1, cwd=tmp_path)
    assert not rr.ok and seen and not any(k in seen for k in ENV)
    from team import cycle as CY
    assert all(k.startswith(CY.SECRET_ENV_PREFIXES) for k in ENV)


# ------------------------------------------------------------------ the analyst task in a cycle


T68_SUMMARY = ("**День 27.11.2026.** | Discovery | нет данных |\n**T-0068 (keyword-map.md):** в этом запуске я не делал. У аналитика нет "
               "сети, а автодополнение Google, PAA, Bing и SERP снять нечем. Задача нужна исполнителю с доступом в сеть.")


def test_the_t0068_report_is_not_a_delivery():
    kw = keywords.settings({})
    t = {"scope": "products/hittheline/seo/keyword-map.md. For each page type …", "acceptance": ["keyword-map.md covers every page type"]}
    assert required_outputs(t, keywords.settings({"keywords": {"outputs": ["seo/"]}})) == ["seo/keyword-map.md"]
    assert "не делал" in analyst_not_done({"summary_md": T68_SUMMARY}, t, kw, [], kw_enabled=False)
    assert analyst_not_done({"summary_md": "x", "task_status": "needs_data", "missing": ["volumes"]}, t, kw, [], kw_enabled=True) == "status needs_data: volumes"
    assert "seo/keyword-map.md" in analyst_not_done({"summary_md": "A long and complete answer. " * 10, "task_status": "done"}, t, kw, [], kw_enabled=True)
    ok = [{"path": "seo/keyword-map.md", "content": "# map"}]
    assert analyst_not_done({"summary_md": "done", "task_status": "done"}, t, kw, ok, kw_enabled=True) is None
    assert analyst_not_done({"summary_md": "Metrics for 02.11: 55 human sources, 11 engaged. " * 3}, {"scope": "measure"}, kw, [], kw_enabled=False) is None


class AnalystRunner:
    """Scripted analyst reports, one per run; the prompts are kept."""

    def __init__(self, reports: list[dict[str, Any]]):
        self.reports, self.prompts = list(reports), []

    def run(self, role, prompt, schema, *, cwd, **kw):
        assert role == "analyst"
        self.prompts.append(prompt)
        return RunResult(True, self.reports.pop(0), {"total_cost_usd": 0.1}, 0.1, 3, None, session_id="s", duration_s=1.0)


def _product(tmp_path: Path, keywords_yaml: str = "") -> tuple[C.Settings, C.Product]:
    raw = C._merge(C.DEFAULTS, {"cycle": {"review_after_min": 0, "persona_every_h": 0, "digest_hour_utc": 99}})
    st = C.Settings(raw, tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")
    d = st.products_dir / "demo"
    d.mkdir(parents=True)
    (d / "product.yaml").write_text("slug: demo\nname: Demo\nhost: ''\nrepo:\n  url: ''\n  branch: main\n" + keywords_yaml)
    (d / "VISION.md").write_text("# Demo\n\nGoals: G1 people.\n")
    p = C.load_product(st, d)
    repo = gitops.init_local(p.repo_dir)
    (repo / "README.md").write_text("site\n")
    gitops.git(repo, "add", "-A"); gitops.git(repo, "commit", "-q", "-m", "init")
    return st, p


KW_ON = "keywords:\n  enabled: true\n  dataforseo_cap_usd: 10\n"
MAP = "# Keyword map\n\n| page type | query | UK volume |\n|---|---|---|\n| team × metric | premier league corners | 10 |\n"


def _task(p: C.Product, **extra: Any) -> dict[str, Any]:
    s = Store(p.dir)
    t = s.create_task(title="Keyword map", type_="research", problem="no target queries", scope="seo/keyword-map.md per page type",
                      acceptance=["keyword-map.md covers every page type", "no invented search volumes"], goal="G1", created_by="test")
    t.update(role="analyst", **extra)
    s.save_task(t)
    s.transition(t["id"], "ready", "owner")
    return s.task(t["id"])


def test_a_no_network_report_blocks_the_task_with_a_question(tmp_path: Path):
    st, p = _product(tmp_path)
    t = _task(p)
    cy = Cycle(st, p, AnalystRunner([{"summary_md": T68_SUMMARY, "observations": []}]), "c1")
    assert cy.exec_analyst_task(t, "c1-exec")
    s = Store(p.dir)
    assert s.task(t["id"])["state"] == "blocked"
    q = s.questions(["open"])[0]
    assert q["to"] == "po" and "did not deliver" in q["question"] and "keywords.enabled" in q["question"]


def test_the_task_data_reaches_the_prompt_and_the_map_is_merged(tmp_path: Path, monkeypatch):
    st, p = _product(tmp_path, KW_ON)
    api = FakeAPI()
    monkeypatch.setattr(keywords, "_http", api)
    monkeypatch.setattr(keywords.time, "sleep", lambda _: None)
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    t = _task(p, data_request={"keywords": ["premier league corners", "championship corners"], "locations": ["gb", "us", "ie", "all"]})
    r = AnalystRunner([{"summary_md": "Map written from the DataForSEO and Bing tables. " * 4, "observations": [], "task_status": "done",
                        "files": [{"path": "seo/keyword-map.md", "content": MAP}, {"path": "public/index.html", "content": "x"}]}])
    cy = Cycle(st, p, r, "c1")
    assert cy.exec_analyst_task(t, "c1-exec")
    s = Store(p.dir)
    t2 = s.task(t["id"])
    assert t2["state"] == "done", t2["history"][-1]
    assert t2["results"][-1]["changes"] == ["seo/keyword-map.md"] and t2["released_sha"]
    assert t2["data"]["spent_usd"] == pytest.approx(0.36)                       # 4 locations × $0.09
    assert MAP.strip() in gitops.git(p.repo_dir, "show", f"{p.repo_branch}:seo/keyword-map.md")
    assert "public/index.html" not in gitops.git(p.repo_dir, "ls-files")        # outside keywords.outputs: refused
    pr = r.prompts[0]
    assert "# DATA FETCHED BY THE DISPATCHER FOR THIS TASK" in pr and "| premier league corners | 10 | 10 | 10 | 100 |" in pr
    assert not any(v in pr for v in ENV.values()) and "$0.36 spent of the product's ceiling $10.00" in pr
    assert s.api_spent("dataforseo") == pytest.approx(0.36)
    from team.cycle import daily_digest
    assert "DataForSEO $0.36 из потолка $10.00" in daily_digest(p, s, "2026-11-28")


def test_the_analyst_asks_for_data_and_is_run_again(tmp_path: Path, monkeypatch):
    st, p = _product(tmp_path, KW_ON)
    api = FakeAPI()
    monkeypatch.setattr(keywords, "_http", api)
    monkeypatch.setattr(keywords.time, "sleep", lambda _: None)
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    t = _task(p)
    r = AnalystRunner([
        {"summary_md": "I need volumes first.", "observations": [], "task_status": "needs_data",
         "data_request": {"keywords": ["btts stats", "ligue 2 corners"], "locations": ["gb"], "sources": ["google_ads_volume", "bing_keyword"]}},
        {"summary_md": "Map from the fetched tables. " * 6, "observations": [], "task_status": "done",
         "files": [{"path": "seo/keyword-map.md", "content": MAP}]}])
    cy = Cycle(st, p, r, "c1")
    assert cy.exec_analyst_task(t, "c1-exec")
    t2 = Store(p.dir).task(t["id"])
    assert t2["state"] == "done" and len(r.prompts) == 2 and t2["data_request"]["keywords"] == ["btts stats", "ligue 2 corners"]
    assert "rounds left: 2" in r.prompts[0] and "| btts stats | 10 |" in r.prompts[1] and "rounds left: 1" in r.prompts[1]
    assert api.paid() == 1


def test_a_refused_purchase_blocks_for_the_owner(tmp_path: Path, monkeypatch):
    st, p = _product(tmp_path, "keywords:\n  enabled: true\n  dataforseo_cap_usd: 0.05\n")
    api = FakeAPI()
    monkeypatch.setattr(keywords, "_http", api)
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    t = _task(p, data_request={"keywords": ["btts stats"], "locations": ["gb"], "sources": ["google_ads_volume"]})
    r = AnalystRunner([])
    assert Cycle(st, p, r, "c1").exec_analyst_task(t, "c1-exec")
    s = Store(p.dir)
    assert s.task(t["id"])["state"] == "waiting_owner" and r.prompts == [] and api.paid() == 0      # a question to the owner parks it
    q = s.questions(["open"])[0]
    assert q["to"] == "owner" and "ceiling of this product is $0.05" in q["question"] and "dataforseo_cap_usd" in q["question"]


def test_needs_data_without_keyword_data_blocks(tmp_path: Path):
    st, p = _product(tmp_path)
    t = _task(p)
    r = AnalystRunner([{"summary_md": "Volumes needed.", "observations": [], "task_status": "needs_data", "missing": ["UK volumes"],
                        "data_request": {"keywords": ["btts stats"]}}])
    assert Cycle(st, p, r, "c1").exec_analyst_task(t, "c1-exec")
    s = Store(p.dir)
    assert s.task(t["id"])["state"] == "blocked" and len(r.prompts) == 1
    assert "External keyword data (DataForSEO, Bing Webmaster) is OFF" in r.prompts[0]
    assert "UK volumes" in s.questions(["open"])[0]["question"]


def test_status_shows_the_keyword_spend(tmp_path: Path, capsys):
    st, p = _product(tmp_path, KW_ON)
    s = Store(p.dir)
    s.book_api_spend("dataforseo", "keywords_data/google_ads/search_volume/live", "T-1", 0.09, 49.6, "volume gb")
    assert keywords.spend_line(s, keywords.settings(p.raw)) == "keyword data: DataForSEO $0.09 of ceiling $10.00 (1 paid call(s))"
    assert keywords.spend_line(Store(tmp_path / "other"), keywords.settings({})) is None
