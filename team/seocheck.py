"""Programmatic SEO/GEO check of a static site: one pass per dispatcher cycle, no model, stdlib only (Python >= 3.12).

Input: ``site_base`` (``https://luz.bymachines.ai``), ``fetch(url) -> (status, headers, body)`` (injected, so tests
run without a network), ``max_pages`` (home + up to ``max_pages`` sitemap pages), ``langs`` (expected hreflang codes).

Output of :func:`check_site`: dispatcher observation cards, one per FAILED check (``reproducible_defect``) and one
``success`` card "N checks passed" when nothing failed. :func:`report` returns the full table (every check, every
page, skipped fetches) for ``metrics/seo-<date>.json``.

Checks (``id`` in the report):
  reachable  - home answers 200 (otherwise one ``unreachable`` card and nothing else is checked)
  robots     - /robots.txt 200, no ``Disallow: /`` for the AI/search bots (own group or ``*``), has ``Sitemap:``
  sitemap    - /sitemap.xml 200, valid XML, >= 1 <loc>, all loc on the site host, first ``max_pages`` pages answer 200
  llms       - /llms.txt 200 with >= 1 absolute link to the site
  per page   - title (10-70 chars) · description (50-160) · canonical == page URL without query · html lang ·
               exactly one h1 · JSON-LD present and parses · hreflang per expected lang · og:title + og:description ·
               no noindex (paths under /go/ are skipped entirely)
  title_dup  - the sampled pages do not all share one title
  feed       - /feed.xml 200 and XML with >= 1 item/entry (404 is "no feed", not a defect)
  notfound   - a random path answers 404, not 200

CLI::

    python3 -m team.seocheck https://host [--max-pages 8] [--langs pt,en] [--json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

log = logging.getLogger("team.seocheck")

# ------------------------------------------------------------------ parameters

SOURCE = "seocheck"
FINGERPRINT_SCOPE = "seo"
BOTS = ("GPTBot", "ClaudeBot", "PerplexityBot", "Googlebot", "bingbot")
TITLE_LEN = (10, 70)
DESCRIPTION_LEN = (50, 160)
SKIP_PREFIXES = ("/go/",)
PROBE_404_PATH = "/__seo_probe_404__/"
ROBOTS_PATH = "/robots.txt"
SITEMAP_PATH = "/sitemap.xml"
LLMS_PATH = "/llms.txt"
FEED_PATH = "/feed.xml"
DEFAULT_MAX_PAGES = 8
DEFAULT_TIMEOUT = 20.0
DEFAULT_UA = "agent-team/seocheck"
EVIDENCE_MAX = 200
TITLE_MAX = 140
TEXT_MAX = 300
URL_RE = re.compile(r"https?://[^\s<>()\[\]\"']+")

Fetch = Callable[[str], tuple[int, dict, Any]]


# ------------------------------------------------------------------ fetch

def default_fetch(url: str, timeout: float = DEFAULT_TIMEOUT, ua: str = DEFAULT_UA) -> tuple[int, dict, str]:
    """GET ``url``; HTTP errors come back as their status, any other failure raises."""
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "text/html, application/xml, text/plain, */*;q=0.5"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read().decode("utf-8", "replace")


# ------------------------------------------------------------------ html parsing

@dataclass
class PageInfo:
    title: str | None = None
    lang: str | None = None
    description: str | None = None
    canonical: str | None = None
    robots: str | None = None
    og: dict[str, str] = field(default_factory=dict)
    hreflang: dict[str, str] = field(default_factory=dict)
    h1: int = 0
    jsonld: list[str] = field(default_factory=list)
    jsonld_errors: list[str] = field(default_factory=list)


class _HeadParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.info = PageInfo()
        self._in_title = False
        self._title_parts: list[str] = []
        self._in_jsonld = False
        self._jsonld_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        tag = tag.lower()
        if tag == "html" and self.info.lang is None:
            self.info.lang = a.get("lang")
        elif tag == "title":
            self._in_title = True
            self._title_parts = []
        elif tag == "h1":
            self.info.h1 += 1
        elif tag == "meta":
            name = a.get("name", "").lower()
            prop = a.get("property", "").lower()
            content = a.get("content", "")
            if name == "description" and self.info.description is None:
                self.info.description = content
            elif name == "robots" and self.info.robots is None:
                self.info.robots = content
            elif prop.startswith("og:") and prop not in self.info.og:
                self.info.og[prop] = content
            elif name.startswith("og:") and name not in self.info.og:
                self.info.og[name] = content
        elif tag == "link":
            rel = a.get("rel", "").lower().split()
            href = a.get("href", "")
            if "canonical" in rel and self.info.canonical is None:
                self.info.canonical = href
            elif "alternate" in rel and a.get("hreflang"):
                self.info.hreflang[a["hreflang"].lower()] = href
        elif tag == "script" and a.get("type", "").lower().strip() == "application/ld+json":
            self._in_jsonld = True
            self._jsonld_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "title" and self._in_title:
            self._in_title = False
            if self.info.title is None:
                self.info.title = " ".join("".join(self._title_parts).split())
        elif tag == "script" and self._in_jsonld:
            self._in_jsonld = False
            raw = "".join(self._jsonld_parts).strip()
            try:
                json.loads(raw)
                self.info.jsonld.append(raw)
            except json.JSONDecodeError as e:
                self.info.jsonld_errors.append(f"{e.msg} at pos {e.pos}")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)
        if self._in_jsonld:
            self._jsonld_parts.append(data)


def parse_page(html: str) -> PageInfo:
    p = _HeadParser()
    p.feed(html)
    p.close()
    return p.info


# ------------------------------------------------------------------ robots / xml helpers

def parse_robots(text: str) -> dict[str, list[str]]:
    """``{user-agent (lower): [disallow paths]}``; several UA lines in a row share one group."""
    groups: dict[str, list[str]] = {}
    current: list[str] = []
    last_was_ua = False
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if not last_was_ua:
                current = []
            current.append(value.lower())
            groups.setdefault(value.lower(), [])
            last_was_ua = True
            continue
        last_was_ua = False
        if key == "disallow":
            for ua in current:
                groups.setdefault(ua, []).append(value)
    return groups


def robots_blocks(groups: dict[str, list[str]], bot: str) -> bool:
    """True when ``bot`` falls into a group with ``Disallow: /``; a bot without its own group inherits ``*``."""
    group = groups.get(bot.lower())
    if group is None:
        group = groups.get("*", [])
    return any(d.strip() == "/" for d in group)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap(xml_text: str) -> tuple[str, list[str], str | None]:
    """Returns (kind, locs, error): kind is ``urlset`` | ``sitemapindex`` | ``other``."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return "other", [], f"XML parse error: {e}"
    kind = _local(root.tag)
    locs = [(el.text or "").strip() for el in root.iter() if _local(el.tag) == "loc"]
    return (kind if kind in ("urlset", "sitemapindex") else "other"), [u for u in locs if u], None


def feed_entries(xml_text: str) -> tuple[int, str | None]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return 0, f"XML parse error: {e}"
    return sum(1 for el in root.iter() if _local(el.tag) in ("item", "entry")), None


def _as_text(body: Any) -> str:
    if body is None:
        return ""
    if isinstance(body, bytes):
        return body.decode("utf-8", "replace")
    return str(body)


def _norm_url(url: str) -> str:
    s = urlsplit(url)
    path = s.path or "/"
    return f"{s.scheme.lower()}://{(s.hostname or '').lower()}{path}"


def fingerprint(check_id: str, path: str) -> str:
    return hashlib.sha1(f"{FINGERPRINT_SCOPE}|{check_id}|{path}".encode()).hexdigest()[:12]


def _cut(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ------------------------------------------------------------------ checker

class Checker:
    def __init__(self, site_base: str, fetch: Fetch, max_pages: int = DEFAULT_MAX_PAGES, langs: list[str] | None = None):
        self.base = site_base.rstrip("/")
        self.host = (urlsplit(self.base).hostname or "").lower()
        self.fetch = fetch
        self.max_pages = max_pages
        self.langs = [l.lower() for l in (langs or [])]
        self.checks: list[dict[str, Any]] = []
        self.pages: list[dict[str, Any]] = []
        self.skipped: list[dict[str, str]] = []
        self._cache: dict[str, tuple[int, dict, str] | None] = {}

    # ---- plumbing
    def url(self, path: str) -> str:
        return urljoin(self.base + "/", path.lstrip("/")) if not path.startswith("http") else path

    def get(self, url: str) -> tuple[int, dict, str] | None:
        """Cached fetch; a raised error is recorded in ``skipped`` and returns None."""
        if url in self._cache:
            return self._cache[url]
        try:
            status, headers, body = self.fetch(url)
            res: tuple[int, dict, str] | None = (int(status), dict(headers or {}), _as_text(body))
            log.debug("GET %s -> %s (%d bytes)", url, status, len(res[2]))
        except Exception as e:  # noqa: BLE001 - any transport failure is a skipped fetch, not a crash
            log.warning("fetch failed %s: %s: %s", url, type(e).__name__, e)
            self.skipped.append({"url": url, "error": f"{type(e).__name__}: {e}"[:EVIDENCE_MAX]})
            res = None
        self._cache[url] = res
        return res

    def add(self, check_id: str, path: str, ok: bool, expected: str, actual: str, evidence: list[str] | None = None,
            title: str | None = None) -> None:
        self.checks.append({"id": check_id, "path": path, "ok": bool(ok), "expected": _cut(expected, TEXT_MAX),
                            "actual": _cut(actual, TEXT_MAX), "evidence": [_cut(e, EVIDENCE_MAX) for e in (evidence or [])][:8],
                            "title": _cut(title or f"{check_id}: {path}", TITLE_MAX)})
        log.info("%s %s %s — %s", "PASS" if ok else "FAIL", check_id, path, actual)

    # ---- top level
    def run(self) -> dict[str, Any]:
        log.info("seocheck %s max_pages=%d langs=%s", self.base, self.max_pages, self.langs)
        home = self.get(self.base + "/")
        if home is None or home[0] != 200:
            actual = f"HTTP {home[0]}" if home else (self.skipped[-1]["error"] if self.skipped else "no response")
            self.add("reachable", "/", False, "home answers 200", actual, [self.base + "/"], title="unreachable: home does not answer 200")
            return self.result()
        self.add("reachable", "/", True, "home answers 200", "HTTP 200", [self.base + "/"])
        self.check_robots()
        sitemap_pages = self.check_sitemap()
        self.check_llms()
        self.check_pages(sitemap_pages)
        self.check_feed()
        self.check_notfound()
        return self.result()

    def result(self) -> dict[str, Any]:
        failed = [c for c in self.checks if not c["ok"]]
        return {"site": self.base, "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "max_pages": self.max_pages, "langs": self.langs, "passed": len(self.checks) - len(failed),
                "failed": len(failed), "checks": self.checks, "pages": self.pages, "skipped": self.skipped}

    # ---- robots
    def check_robots(self) -> None:
        url = self.url(ROBOTS_PATH)
        r = self.get(url)
        if r is None or r[0] != 200:
            self.add("robots", ROBOTS_PATH, False, "robots.txt answers 200", f"HTTP {r[0]}" if r else "fetch failed", [url],
                     title="robots.txt missing")
            return
        groups = parse_robots(r[2])
        blocked = [b for b in BOTS if robots_blocks(groups, b)]
        has_sitemap = any(l.strip().lower().startswith("sitemap:") for l in r[2].splitlines())
        ev = [url] + [l.strip() for l in r[2].splitlines() if l.strip()][:6]
        if blocked:
            self.add("robots", ROBOTS_PATH, False, f"no Disallow: / for {', '.join(BOTS)}",
                     f"Disallow: / applies to {', '.join(blocked)}", ev, title=f"robots.txt blocks {', '.join(blocked)}")
        elif not has_sitemap:
            self.add("robots", ROBOTS_PATH, False, "robots.txt names Sitemap:", "no Sitemap: line", ev, title="robots.txt without Sitemap:")
        else:
            self.add("robots", ROBOTS_PATH, True, "200, bots allowed, Sitemap: present", "ok", ev)

    # ---- sitemap
    def check_sitemap(self) -> list[str]:
        url = self.url(SITEMAP_PATH)
        r = self.get(url)
        if r is None or r[0] != 200:
            self.add("sitemap", SITEMAP_PATH, False, "sitemap.xml answers 200", f"HTTP {r[0]}" if r else "fetch failed", [url],
                     title="sitemap.xml missing")
            return []
        kind, locs, err = parse_sitemap(r[2])
        if err:
            self.add("sitemap", SITEMAP_PATH, False, "sitemap.xml is valid XML", err, [url], title="sitemap.xml is not valid XML")
            return []
        if kind == "sitemapindex":                       # follow the first child sitemap on the site host
            children = [u for u in locs if (urlsplit(u).hostname or "").lower() == self.host]
            locs, err = [], None
            for child in children[:3]:
                cr = self.get(child)
                if cr is None or cr[0] != 200:
                    continue
                _, child_locs, child_err = parse_sitemap(cr[2])
                if child_err:
                    err = f"{child}: {child_err}"
                    continue
                locs.extend(child_locs)
            if err and not locs:
                self.add("sitemap", SITEMAP_PATH, False, "child sitemaps are valid XML", err, [url], title="sitemap index child is not valid XML")
                return []
        if not locs:
            self.add("sitemap", SITEMAP_PATH, False, ">= 1 <loc>", "no <loc> entries", [url], title="sitemap.xml has no URLs")
            return []
        foreign = [u for u in locs if (urlsplit(u).hostname or "").lower() != self.host]
        if foreign:
            self.add("sitemap", SITEMAP_PATH, False, f"all <loc> on {self.host}", f"{len(foreign)} of {len(locs)} loc on another host",
                     [url] + foreign[:5], title="sitemap.xml lists URLs of another host")
            return [u for u in locs if u not in foreign]
        sample = []
        for u in locs:
            if u not in sample and _norm_url(u) != _norm_url(self.base + "/"):
                sample.append(u)
            if len(sample) >= self.max_pages:
                break
        bad = []
        for u in sample:
            pr = self.get(u)
            if pr is None:
                bad.append(f"{u} -> fetch failed")
            elif pr[0] != 200:
                bad.append(f"{u} -> HTTP {pr[0]}")
        if bad:
            self.add("sitemap", SITEMAP_PATH, False, f"first {len(sample)} sitemap pages answer 200", f"{len(bad)} do not", [url] + bad[:5],
                     title=f"sitemap.xml: {len(bad)} listed page(s) do not answer 200")
        else:
            self.add("sitemap", SITEMAP_PATH, True, "200, valid XML, same host, sampled pages 200", f"{len(locs)} loc, {len(sample)} sampled", [url])
        return sample

    # ---- llms.txt
    def check_llms(self) -> None:
        url = self.url(LLMS_PATH)
        r = self.get(url)
        if r is None or r[0] != 200:
            self.add("llms", LLMS_PATH, False, "llms.txt answers 200", f"HTTP {r[0]}" if r else "fetch failed", [url], title="llms.txt missing")
            return
        links = [l.rstrip(".,;:") for l in URL_RE.findall(r[2])]
        own = [l for l in links if (urlsplit(l).hostname or "").lower() == self.host]
        if not own:
            self.add("llms", LLMS_PATH, False, f">= 1 absolute link to {self.host}", f"{len(links)} links, none to the site", [url],
                     title="llms.txt has no absolute link to the site")
        else:
            self.add("llms", LLMS_PATH, True, "200 with site links", f"{len(own)} site links", [url, own[0]])

    # ---- pages
    def check_pages(self, sitemap_pages: list[str]) -> None:
        urls = [self.base + "/"] + [u for u in sitemap_pages if _norm_url(u) != _norm_url(self.base + "/")]
        titles: list[str] = []
        checked_paths: list[str] = []
        for u in urls[: self.max_pages + 1]:
            path = urlsplit(u).path or "/"
            if path.startswith(SKIP_PREFIXES):
                self.pages.append({"url": u, "path": path, "skipped": "path under " + "/".join(SKIP_PREFIXES)})
                continue
            r = self.get(u)
            if r is None:
                self.pages.append({"url": u, "path": path, "skipped": "fetch failed"})
                continue
            if r[0] != 200:
                self.pages.append({"url": u, "path": path, "skipped": f"HTTP {r[0]}"})
                continue
            info = parse_page(r[2])
            self.check_page(u, path, info)
            checked_paths.append(path)
            if info.title:
                titles.append(info.title)
            self.pages.append({"url": u, "path": path, "title": info.title, "lang": info.lang, "canonical": info.canonical,
                               "description_len": len(info.description or ""), "h1": info.h1, "jsonld": len(info.jsonld),
                               "hreflang": sorted(info.hreflang), "og": sorted(info.og), "robots": info.robots})
        if len(checked_paths) > 1:
            if titles and len(titles) == len(checked_paths) and len(set(titles)) == 1:
                self.add("title_dup", "/", False, "pages have distinct titles", f"all {len(titles)} sampled pages share one title",
                         [f"title: {titles[0]}"] + checked_paths[:5], title="same <title> on every sampled page")
            else:
                self.add("title_dup", "/", True, "pages have distinct titles", f"{len(set(titles))} distinct titles on {len(checked_paths)} pages",
                         checked_paths[:5])

    def check_page(self, url: str, path: str, info: PageInfo) -> None:
        ev = [url]
        # title
        if info.title is None or not info.title:
            self.add("title", path, False, f"<title> {TITLE_LEN[0]}-{TITLE_LEN[1]} chars", "no <title>", ev, title=f"title missing: {path}")
        elif not TITLE_LEN[0] <= len(info.title) <= TITLE_LEN[1]:
            self.add("title", path, False, f"<title> {TITLE_LEN[0]}-{TITLE_LEN[1]} chars", f"{len(info.title)} chars: {info.title}", ev,
                     title=f"title length {len(info.title)}: {path}")
        else:
            self.add("title", path, True, f"<title> {TITLE_LEN[0]}-{TITLE_LEN[1]} chars", f"{len(info.title)} chars", ev)
        # description
        d = info.description
        if d is None:
            self.add("description", path, False, f"meta description {DESCRIPTION_LEN[0]}-{DESCRIPTION_LEN[1]} chars", "no meta description", ev,
                     title=f"meta description missing: {path}")
        elif not DESCRIPTION_LEN[0] <= len(d) <= DESCRIPTION_LEN[1]:
            self.add("description", path, False, f"meta description {DESCRIPTION_LEN[0]}-{DESCRIPTION_LEN[1]} chars", f"{len(d)} chars: {d}", ev,
                     title=f"meta description length {len(d)}: {path}")
        else:
            self.add("description", path, True, f"meta description {DESCRIPTION_LEN[0]}-{DESCRIPTION_LEN[1]} chars", f"{len(d)} chars", ev)
        # canonical
        want = _norm_url(url)
        if not info.canonical:
            self.add("canonical", path, False, f"rel=canonical == {want}", "no rel=canonical", ev, title=f"canonical missing: {path}")
        elif _norm_url(urljoin(url, info.canonical)) != want:
            self.add("canonical", path, False, f"rel=canonical == {want}", f"canonical = {info.canonical}", ev + [f"canonical: {info.canonical}"],
                     title=f"canonical mismatch: {path}")
        else:
            self.add("canonical", path, True, "rel=canonical == page URL", "ok", ev)
        # lang
        if not info.lang:
            self.add("lang", path, False, "<html lang> set", "no lang attribute", ev, title=f"html lang missing: {path}")
        else:
            self.add("lang", path, True, "<html lang> set", f"lang={info.lang}", ev)
        # h1
        if info.h1 != 1:
            self.add("h1", path, False, "exactly one <h1>", f"{info.h1} <h1>", ev, title=f"{info.h1} h1 on page: {path}")
        else:
            self.add("h1", path, True, "exactly one <h1>", "1 <h1>", ev)
        # json-ld
        if info.jsonld:
            self.add("jsonld", path, True, "JSON-LD present and parses", f"{len(info.jsonld)} block(s)", ev)
        elif info.jsonld_errors:
            self.add("jsonld", path, False, "JSON-LD parses", f"invalid JSON-LD: {info.jsonld_errors[0]}", ev, title=f"JSON-LD does not parse: {path}")
        else:
            self.add("jsonld", path, False, "JSON-LD present", "no JSON-LD block", ev, title=f"no JSON-LD: {path}")
        # hreflang
        if self.langs:
            have = {k.split("-", 1)[0] for k in info.hreflang}
            missing = [l for l in self.langs if l not in have and l not in info.hreflang]
            if missing:
                self.add("hreflang", path, False, f"hreflang for {', '.join(self.langs)}", f"missing {', '.join(missing)}; present: {', '.join(sorted(info.hreflang)) or 'none'}",
                         ev, title=f"hreflang missing {', '.join(missing)}: {path}")
            else:
                self.add("hreflang", path, True, f"hreflang for {', '.join(self.langs)}", "ok", ev)
        # open graph
        missing_og = [k for k in ("og:title", "og:description") if not info.og.get(k)]
        if missing_og:
            self.add("og", path, False, "og:title and og:description set", f"missing {', '.join(missing_og)}", ev,
                     title=f"missing {', '.join(missing_og)}: {path}")
        else:
            self.add("og", path, True, "og:title and og:description set", "ok", ev)
        # noindex
        robots = (info.robots or "").lower()
        if "noindex" in robots:
            self.add("noindex", path, False, "no noindex in meta robots", f"meta robots: {info.robots}", ev, title=f"noindex on page: {path}")
        else:
            self.add("noindex", path, True, "no noindex in meta robots", "ok", ev)

    # ---- feed
    def check_feed(self) -> None:
        url = self.url(FEED_PATH)
        r = self.get(url)
        if r is None:
            self.add("feed", FEED_PATH, True, "feed.xml 200 with entries, or absent", "fetch failed (skipped)", [url])
            return
        if r[0] == 404:
            self.add("feed", FEED_PATH, True, "feed.xml 200 with entries, or absent", "no feed", [url])
            return
        if r[0] != 200:
            self.add("feed", FEED_PATH, False, "feed.xml answers 200 or 404", f"HTTP {r[0]}", [url], title=f"feed.xml HTTP {r[0]}")
            return
        n, err = feed_entries(r[2])
        if err:
            self.add("feed", FEED_PATH, False, "feed.xml is valid XML", err, [url], title="feed.xml is not valid XML")
        elif n < 1:
            self.add("feed", FEED_PATH, False, ">= 1 item/entry", "0 entries", [url], title="feed.xml has no entries")
        else:
            self.add("feed", FEED_PATH, True, "200, valid XML, >= 1 entry", f"{n} entries", [url])

    # ---- 404 probe
    def check_notfound(self) -> None:
        url = self.url(PROBE_404_PATH)
        r = self.get(url)
        if r is None:
            self.add("notfound", PROBE_404_PATH, True, "unknown path answers 404", "fetch failed (skipped)", [url])
        elif r[0] in (404, 410):
            self.add("notfound", PROBE_404_PATH, True, "unknown path answers 404", f"HTTP {r[0]}", [url])
        else:
            self.add("notfound", PROBE_404_PATH, False, "unknown path answers 404", f"HTTP {r[0]}", [url],
                     title=f"unknown path answers {r[0]}, not 404")


# ------------------------------------------------------------------ public API

def report(site_base: str, fetch: Fetch, max_pages: int = DEFAULT_MAX_PAGES, langs: list[str] | None = None) -> dict[str, Any]:
    """Full check table for ``metrics/seo-<date>.json``; ``cards`` holds the dispatcher cards derived from it."""
    data = Checker(site_base, fetch, max_pages=max_pages, langs=langs).run()
    data["cards"] = cards_from_report(data)
    return data


def cards_from_report(data: dict[str, Any]) -> list[dict[str, Any]]:
    base = data["site"]
    cards: list[dict[str, Any]] = []
    for c in data["checks"]:
        if c["ok"]:
            continue
        cards.append({"title": _cut(c["title"], TITLE_MAX), "classification": "reproducible_defect", "source": SOURCE,
                      "url": urljoin(base + "/", c["path"].lstrip("/")), "expected": c["expected"], "actual": c["actual"],
                      "evidence": list(c["evidence"]), "confidence": "high", "fingerprint": fingerprint(c["id"], c["path"])})
    if not cards:
        n = len(data["checks"])
        cards.append({"title": f"{n} checks passed", "classification": "success", "source": SOURCE, "url": base + "/",
                      "expected": "all SEO/GEO checks pass", "actual": f"{n} checks passed on {len(data['pages'])} page(s)",
                      "evidence": [base + "/"], "confidence": "high", "fingerprint": fingerprint("all", "/")})
    return cards


def check_site(site_base: str, fetch: Fetch, max_pages: int = DEFAULT_MAX_PAGES, langs: list[str] | None = None) -> list[dict[str, Any]]:
    """Dispatcher cards: one ``reproducible_defect`` per failed check, or one ``success`` card."""
    return report(site_base, fetch, max_pages=max_pages, langs=langs)["cards"]


def report_md(data: dict[str, Any]) -> str:
    lines = [f"# seocheck {data['site']} — {data['generated']}", f"passed {data['passed']} · failed {data['failed']} · "
             f"pages {len(data['pages'])} · skipped fetches {len(data['skipped'])}", "", "| ok | check | path | actual |", "|---|---|---|---|"]
    for c in data["checks"]:
        lines.append(f"| {'✓' if c['ok'] else '✗'} | {c['id']} | {c['path']} | {c['actual'].replace('|', '/')} |")
    if data["skipped"]:
        lines += ["", "skipped:"] + [f"- {s['url']}: {s['error']}" for s in data["skipped"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python3 -m team.seocheck", description=__doc__.split("\n\n")[0])
    ap.add_argument("site_base")
    ap.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    ap.add_argument("--langs", default="", help="expected hreflang codes, comma-separated (pt,en)")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    ap.add_argument("--json", action="store_true", help="print the full report as JSON")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    langs = [l.strip() for l in a.langs.split(",") if l.strip()]
    data = report(a.site_base, lambda u: default_fetch(u, timeout=a.timeout), max_pages=a.max_pages, langs=langs)
    if a.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        print(report_md(data), end="")
        for c in data["cards"]:
            print(f"[{c['classification']}] {c['title']} — {c['url']}")
    return 1 if data["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
