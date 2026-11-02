"""Tests for team.seocheck: a mini site as dict url -> (status, headers, body); no network."""
from __future__ import annotations

import json

import pytest

from team import seocheck as sc

BASE = "https://luz.example.test"
PAGES = ["/", "/pt/sobre/", "/en/about/"]

ROBOTS_OK = "User-agent: *\nAllow: /\n\nSitemap: https://luz.example.test/sitemap.xml\n"
ROBOTS_BLOCK_GPT = "User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n\nSitemap: https://luz.example.test/sitemap.xml\n"


def sitemap(locs: list[str]) -> str:
    body = "".join(f"<url><loc>{u}</loc></url>" for u in locs)
    return f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'


def page(path: str, *, title: str | None = None, description: str | None = "A sixty-character description of this page for search engines ok.",
         canonical: str | None = None, lang: str | None = "pt", h1: int = 1, jsonld: str | None = '{"@context":"https://schema.org","@type":"WebPage"}',
         hreflang: dict[str, str] | None = None, og: bool = True, robots: str | None = None) -> str:
    url = BASE + path
    title = f"Page {path} — Luz" if title is None else title
    canonical = url if canonical is None else canonical
    hreflang = {"pt": BASE + "/pt/", "en": BASE + "/en/"} if hreflang is None else hreflang
    head = []
    if title:
        head.append(f"<title>{title}</title>")
    if description is not None:
        head.append(f'<meta name="description" content="{description}">')
    if canonical:
        head.append(f'<link rel="canonical" href="{canonical}">')
    for code, href in hreflang.items():
        head.append(f'<link rel="alternate" hreflang="{code}" href="{href}">')
    if og:
        head.append(f'<meta property="og:title" content="{title}"><meta property="og:description" content="{description or "x"}">')
    if robots:
        head.append(f'<meta name="robots" content="{robots}">')
    if jsonld is not None:
        head.append(f'<script type="application/ld+json">{jsonld}</script>')
    lang_attr = f' lang="{lang}"' if lang else ""
    body = "".join("<h1>Heading</h1>" for _ in range(h1)) + "<p>text</p>"
    return f"<!doctype html><html{lang_attr}><head>{''.join(head)}</head><body>{body}</body></html>"


def mini_site(**overrides: str | tuple) -> dict[str, tuple[int, dict, str]]:
    site: dict[str, tuple[int, dict, str]] = {
        BASE + "/robots.txt": (200, {}, ROBOTS_OK),
        BASE + "/sitemap.xml": (200, {}, sitemap([BASE + p for p in PAGES])),
        BASE + "/llms.txt": (200, {}, f"# Luz\n\n- [About]({BASE}/en/about/)\n"),
        BASE + "/feed.xml": (200, {}, '<rss version="2.0"><channel><title>Luz</title><item><title>x</title></item></channel></rss>'),
    }
    for p in PAGES:
        site[BASE + p] = (200, {"content-type": "text/html"}, page(p))
    for k, v in overrides.items():
        site[k] = v if isinstance(v, tuple) else (200, {}, v)
    return site


def make_fetch(site: dict[str, tuple[int, dict, str]], calls: list[str] | None = None):
    def fetch(url: str) -> tuple[int, dict, str]:
        if calls is not None:
            calls.append(url)
        return site.get(url, (404, {}, "not found"))
    return fetch


def failed_ids(cards: list[dict]) -> set[str]:
    return {c["title"] for c in cards if c["classification"] == "reproducible_defect"}


# ---------------------------------------------------------------------------------------------------- happy path

def test_all_good_one_success_card():
    calls: list[str] = []
    cards = sc.check_site(BASE, make_fetch(mini_site(), calls), max_pages=8, langs=["pt", "en"])
    assert len(cards) == 1
    c = cards[0]
    assert c["classification"] == "success"
    assert c["source"] == "seocheck"
    assert c["title"].endswith("checks passed")
    assert c["confidence"] == "high"
    assert len(c["fingerprint"]) == 12
    assert BASE + "/__seo_probe_404__/" in calls
    assert calls.count(BASE + "/pt/sobre/") == 1            # cached: sitemap sample and page checks share one fetch


def test_report_table_and_card_format():
    data = sc.report(BASE, make_fetch(mini_site()), langs=["pt", "en"])
    ids = {c["id"] for c in data["checks"]}
    assert {"reachable", "robots", "sitemap", "llms", "title", "description", "canonical", "lang", "h1", "jsonld",
            "hreflang", "og", "noindex", "title_dup", "feed", "notfound"} <= ids
    assert data["failed"] == 0 and data["passed"] == len(data["checks"])
    assert len(data["pages"]) == 3 and data["skipped"] == []
    assert all(c["ok"] for c in data["checks"])
    json.dumps(data)                                           # serialisable for metrics/seo-<date>.json
    for card in data["cards"]:
        assert set(card) == {"title", "classification", "source", "url", "expected", "actual", "evidence", "confidence", "fingerprint"}
        assert len(card["title"]) <= 140 and all(len(e) <= 200 for e in card["evidence"])


# ---------------------------------------------------------------------------------------------------- defects

def test_missing_description():
    site = mini_site(**{BASE + "/pt/sobre/": page("/pt/sobre/", description=None)})
    cards = sc.check_site(BASE, make_fetch(site), langs=["pt", "en"])
    defects = [c for c in cards if c["classification"] == "reproducible_defect"]
    assert len(defects) == 1
    d = defects[0]
    assert "description missing" in d["title"] and d["url"] == BASE + "/pt/sobre/"
    assert d["fingerprint"] == sc.fingerprint("description", "/pt/sobre/")
    assert d["fingerprint"] != sc.fingerprint("description", "/en/about/")
    assert not any(c["classification"] == "success" for c in cards)


def test_canonical_mismatch():
    site = mini_site(**{BASE + "/en/about/": page("/en/about/", canonical=BASE + "/en/")})
    cards = sc.check_site(BASE, make_fetch(site), langs=["pt", "en"])
    assert [c["title"] for c in cards] == ["canonical mismatch: /en/about/"]
    assert "/en/" in cards[0]["actual"]


def test_canonical_ignores_query_on_page_url():
    site = mini_site(**{BASE + "/en/about/": page("/en/about/", canonical=BASE + "/en/about/?utm=1")})
    cards = sc.check_site(BASE, make_fetch(site), langs=["pt", "en"])
    assert cards[0]["classification"] == "success"


def test_robots_blocks_gptbot():
    site = mini_site(**{BASE + "/robots.txt": ROBOTS_BLOCK_GPT})
    cards = sc.check_site(BASE, make_fetch(site), langs=["pt", "en"])
    assert len(cards) == 1
    assert cards[0]["title"] == "robots.txt blocks GPTBot"
    assert cards[0]["url"] == BASE + "/robots.txt"
    assert cards[0]["fingerprint"] == sc.fingerprint("robots", "/robots.txt")


def test_robots_star_disallow_blocks_all_and_sitemap_line_required():
    groups = sc.parse_robots("User-agent: *\nDisallow: /\n")
    assert all(sc.robots_blocks(groups, b) for b in sc.BOTS)
    groups = sc.parse_robots("User-agent: Googlebot\nUser-agent: bingbot\nDisallow: /private/\n\nUser-agent: *\nDisallow:\n")
    assert not any(sc.robots_blocks(groups, b) for b in sc.BOTS)
    site = mini_site(**{BASE + "/robots.txt": "User-agent: *\nAllow: /\n"})
    cards = sc.check_site(BASE, make_fetch(site), langs=["pt", "en"])
    assert [c["title"] for c in cards] == ["robots.txt without Sitemap:"]


def test_sitemap_foreign_host():
    site = mini_site(**{BASE + "/sitemap.xml": sitemap([BASE + "/", "https://other.example.test/x/"])})
    cards = sc.check_site(BASE, make_fetch(site), langs=["pt", "en"])
    assert [c["title"] for c in cards] == ["sitemap.xml lists URLs of another host"]
    assert any("other.example.test" in e for e in cards[0]["evidence"])


def test_sitemap_invalid_xml_and_dead_page():
    site = mini_site(**{BASE + "/sitemap.xml": "<urlset><url><loc>x</url>"})
    cards = sc.check_site(BASE, make_fetch(site), langs=["pt", "en"])
    assert [c["title"] for c in cards] == ["sitemap.xml is not valid XML"]
    site = mini_site(**{BASE + "/sitemap.xml": sitemap([BASE + "/", BASE + "/gone/"])})
    cards = sc.check_site(BASE, make_fetch(site), langs=["pt", "en"])
    assert [c["title"] for c in cards] == ["sitemap.xml: 1 listed page(s) do not answer 200"]


def test_hreflang_missing():
    site = mini_site(**{BASE + "/": page("/", hreflang={"pt": BASE + "/pt/"})})
    cards = sc.check_site(BASE, make_fetch(site), langs=["pt", "en"])
    assert [c["title"] for c in cards] == ["hreflang missing en: /"]
    # without expected langs the check is not run at all
    data = sc.report(BASE, make_fetch(site))
    assert data["failed"] == 0 and not any(c["id"] == "hreflang" for c in data["checks"])


def test_go_paths_are_skipped():
    go = BASE + "/go/partner/"
    site = mini_site(**{BASE + "/sitemap.xml": sitemap([BASE + p for p in PAGES] + [go]),
                        go: '<html><head><meta name="robots" content="noindex"><title>go</title></head><body></body></html>'})
    data = sc.report(BASE, make_fetch(site), langs=["pt", "en"])
    assert data["failed"] == 0
    assert not any(c["path"].startswith("/go/") for c in data["checks"])
    assert any(p["path"] == "/go/partner/" and p.get("skipped") for p in data["pages"])


def test_noindex_outside_go_is_a_defect():
    site = mini_site(**{BASE + "/en/about/": page("/en/about/", robots="noindex, follow")})
    cards = sc.check_site(BASE, make_fetch(site), langs=["pt", "en"])
    assert [c["title"] for c in cards] == ["noindex on page: /en/about/"]


def test_404_probe_returns_200():
    site = mini_site()
    fetch_inner = make_fetch(site)

    def fetch(url: str):
        if "__seo_probe_404__" in url:
            return 200, {}, "<html><title>catch-all</title></html>"
        return fetch_inner(url)

    cards = sc.check_site(BASE, fetch, langs=["pt", "en"])
    assert [c["title"] for c in cards] == ["unknown path answers 200, not 404"]
    assert cards[0]["fingerprint"] == sc.fingerprint("notfound", sc.PROBE_404_PATH)


# ---------------------------------------------------------------------------------------------------- softer cases

def test_no_jsonld_and_duplicate_titles():
    site = mini_site(**{BASE + p: page(p, title="Same title everywhere", jsonld=None) for p in PAGES})
    cards = sc.check_site(BASE, make_fetch(site), langs=["pt", "en"])
    titles = [c["title"] for c in cards]
    assert "no JSON-LD: /" in titles and "no JSON-LD: /pt/sobre/" in titles
    assert "same <title> on every sampled page" in titles
    assert all(c["classification"] == "reproducible_defect" for c in cards)


def test_title_length_h1_lang_og():
    site = mini_site(**{BASE + "/": page("/", title="Short", h1=2, lang=None, og=False)})
    cards = sc.check_site(BASE, make_fetch(site), langs=["pt", "en"])
    titles = {c["title"] for c in cards}
    assert titles == {"title length 5: /", "2 h1 on page: /", "html lang missing: /", "missing og:title, og:description: /"}


def test_feed_absent_is_not_a_defect_but_empty_feed_is():
    site = mini_site(**{BASE + "/feed.xml": (404, {}, "")})
    data = sc.report(BASE, make_fetch(site), langs=["pt", "en"])
    feed = next(c for c in data["checks"] if c["id"] == "feed")
    assert feed["ok"] and feed["actual"] == "no feed"
    site = mini_site(**{BASE + "/feed.xml": '<rss version="2.0"><channel><title>Luz</title></channel></rss>'})
    cards = sc.check_site(BASE, make_fetch(site), langs=["pt", "en"])
    assert [c["title"] for c in cards] == ["feed.xml has no entries"]


def test_llms_without_site_link():
    site = mini_site(**{BASE + "/llms.txt": "# Luz\nSee https://github.com/x/y\n"})
    cards = sc.check_site(BASE, make_fetch(site), langs=["pt", "en"])
    assert [c["title"] for c in cards] == ["llms.txt has no absolute link to the site"]


def test_home_unreachable_single_card():
    def fetch(url: str):
        raise TimeoutError("timed out")

    cards = sc.check_site(BASE, fetch)
    assert len(cards) == 1
    assert cards[0]["classification"] == "reproducible_defect" and cards[0]["title"].startswith("unreachable")
    assert "TimeoutError" in cards[0]["actual"]


def test_fetch_error_on_one_page_is_skipped_not_fatal():
    site = mini_site()
    inner = make_fetch(site)

    def fetch(url: str):
        if url.endswith("/en/about/"):
            raise ConnectionError("reset")
        return inner(url)

    data = sc.report(BASE, fetch, langs=["pt", "en"])
    assert any(s["url"].endswith("/en/about/") and "ConnectionError" in s["error"] for s in data["skipped"])
    assert any(p["path"] == "/en/about/" and p.get("skipped") for p in data["pages"])
    assert not any(c["path"] == "/en/about/" for c in data["checks"])
    # the sitemap check still reports the page as not answering 200
    assert any(c["id"] == "sitemap" and not c["ok"] for c in data["checks"])


def test_max_pages_limits_sample():
    locs = [BASE + f"/p{i}/" for i in range(20)]
    site = mini_site(**{BASE + "/sitemap.xml": sitemap([BASE + "/"] + locs)})
    for u in locs:
        site[u] = (200, {}, page(u.removeprefix(BASE)))
    calls: list[str] = []
    data = sc.report(BASE, make_fetch(site, calls), max_pages=3, langs=["pt", "en"])
    assert len(data["pages"]) == 4                              # home + 3
    assert sum(1 for u in calls if "/p" in u) == 3


def test_cli_json(capsys, monkeypatch):
    inner = make_fetch(mini_site())
    monkeypatch.setattr(sc, "default_fetch", lambda url, **kw: inner(url))   # the CLI builds its fetch from default_fetch
    rc = sc.main([BASE, "--langs", "pt,en", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["failed"] == 0 and out["cards"][0]["classification"] == "success"
