"""GEO probe: once a week a role asks AI search engines the product's control questions and records whether our site
is cited. This module is the pure part — prompt, query list, metrics summary, observation cards; the role run itself
lives in the cycle (see `build_prompt` for what the role receives and `to_*` for what the dispatcher keeps).

Configuration: `product.yaml: geo_queries` (a list of questions, exact wording) — missing → `DEFAULT_QUERIES[slug]`.
"""
from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

log = logging.getLogger("team.geo")

# the role card the prompt is built for; the schema the result is validated against
ROLE = "geo-probe"
SCHEMA = "geo-report"
MAX_QUERIES = 8                     # a weekly probe stays small: ≤ 3 tool calls per question in the role card
OBS_TITLE_MAX = 140
OBS_ACTUAL_MAX = 400
AUDIENCE_HEADING = "## Для кого"    # the VISION section handed to the role as the audience

# control questions per product when product.yaml has no `geo_queries` (exact wording, the language of the audience)
DEFAULT_QUERIES: dict[str, list[str]] = {
    "release-radar": [
        "which npm packages had breaking changes this week",
        "which pypi packages released breaking changes this week",
        "does express 5 have breaking changes",
        "what changed in the latest pydantic release",
        "daily feed of python and npm package releases with breaking changes",
    ],
    "agentsmd-tracker": [
        "how many github repos have AGENTS.md",
        "what is AGENTS.md adoption",
        "which popular open source projects have a CLAUDE.md file",
        "AGENTS.md vs .cursorrules vs copilot-instructions adoption statistics",
        "do big open source projects use AGENTS.md",
    ],
    "luz-amanha": [
        "preço da luz amanhã por hora Portugal",
        "horas mais baratas de eletricidade amanhã Portugal",
        "preço OMIE amanhã Portugal",
        "cheapest electricity hours tomorrow Portugal",
        "electricity price tomorrow Portugal per hour",
    ],
}


def queries_for(product_raw: dict[str, Any]) -> list[str]:
    """`product.yaml: geo_queries` (a non-empty list of strings) or the default for the slug; never more than MAX_QUERIES."""
    raw = product_raw.get("geo_queries")
    qs = [str(q).strip() for q in raw if str(q).strip()] if isinstance(raw, list) else []
    if not qs:
        qs = list(DEFAULT_QUERIES.get(str(product_raw.get("slug") or ""), []))
        log.debug("geo: no geo_queries in product.yaml for %s, default of %d questions", product_raw.get("slug"), len(qs))
    seen: set[str] = set()
    out: list[str] = []
    for q in qs:
        if q.lower() not in seen:
            seen.add(q.lower())
            out.append(q)
    if len(out) > MAX_QUERIES:
        log.warning("geo: %d questions, only the first %d are probed", len(out), MAX_QUERIES)
    return out[:MAX_QUERIES]


def audience_section(vision_md: str) -> str:
    """The `## Для кого` section of VISION.md (heading to the next `## `), '' when absent."""
    m = re.search(rf"^{re.escape(AUDIENCE_HEADING)}[^\n]*\n(.*?)(?=^## |\Z)", vision_md, re.S | re.M)
    return m.group(1).strip() if m else ""


def build_prompt(product_name: str, site_base: str, queries: list[str], audience_md: str) -> str:
    host = re.sub(r"^https?://", "", site_base).strip("/")
    q_lines = "\n".join(f"{i}. {q}" for i, q in enumerate(queries, 1))
    return (f"# GEO PROBE — product `{product_name}`\n\n"
            f"Our site: {site_base} (host `{host}`). A result or citation counts as ours only when it points at this host.\n\n"
            "# AUDIENCE (who asks these questions, from VISION)\n" + (audience_md.strip() or "(not given)")
            + "\n\n# CONTROL QUESTIONS (exact wording, this order, one record each)\n" + q_lines
            + "\n\nFor each question: search it with WebSearch as that user would, read the first results, open a page with WebFetch"
              " only when the list is not enough; record whether our host is cited (position, URL), the top domains cited"
              " instead (≤ 5), and whether our site has a page for this question. Nothing observable → `not_observable`."
              " Report in the geo-report schema.\n")


def to_metrics(result: dict[str, Any], date: str) -> dict[str, Any]:
    """A small daily-metrics record: how many questions cite us, and the per-question list for the week-over-week diff."""
    qs = [q for q in (result.get("queries") or []) if isinstance(q, dict)]
    cited = [q for q in qs if q.get("our_site_cited") is True]
    observable = [q for q in qs if str(q.get("engine_note", "")).strip().lower() != "not_observable"]
    comp: dict[str, int] = {}
    for q in qs:
        for d in q.get("competitors") or []:
            d = str(d).strip().lower()
            if d:
                comp[d] = comp.get(d, 0) + 1
    rec = {
        "date": date,
        "total": len(qs),
        "cited": len(cited),
        "observable": len(observable),
        "pages_missing": sum(1 for q in qs if q.get("our_page_exists") is False),
        "top_competitors": [d for d, _ in sorted(comp.items(), key=lambda kv: (-kv[1], kv[0]))[:5]],
        "queries": [{"query": str(q.get("query", "")), "cited": q.get("our_site_cited") is True, "position": q.get("position"),
                     "our_url": q.get("our_url"), "page_exists": q.get("our_page_exists") is True,
                     "competitors": [str(d) for d in (q.get("competitors") or [])][:5],
                     "observable": str(q.get("engine_note", "")).strip().lower() != "not_observable"} for q in qs],
    }
    log.info("geo %s: cited %d/%d (observable %d), pages missing %d", date, rec["cited"], rec["total"], rec["observable"], rec["pages_missing"])
    return rec


def fingerprint(query: str) -> str:
    """sha1-12 of ("geo", query): the same question gives the same card week after week, so the store counts repeats."""
    return hashlib.sha1(f"geo|{query.strip().lower()}".encode()).hexdigest()[:12]


def to_observations(result: dict[str, Any], product_slug: str) -> list[dict[str, Any]]:
    """One card per question, classified from the record (deterministic, independent of the role's own observations):
    not cited and no page → need_hypothesis "create a page for query …"; not cited, page exists → need_hypothesis
    (the page is not picked up); cited → success; not observable → no card. The role's own `reproducible_defect`
    observations are appended with a fingerprint of their URL."""
    cards: list[dict[str, Any]] = []
    for q in (result.get("queries") or []):
        if not isinstance(q, dict) or not str(q.get("query", "")).strip():
            continue
        query = str(q["query"]).strip()
        if str(q.get("engine_note", "")).strip().lower() == "not_observable":
            continue
        comps = ", ".join(str(d) for d in (q.get("competitors") or [])[:5]) or "none seen"
        note = str(q.get("note") or "").strip()
        if q.get("our_site_cited") is True:
            cls = "success"
            title = f"GEO: cited for query “{query}”"
            actual = f"position {q.get('position')}, url {q.get('our_url')}; also cited: {comps}"
        elif q.get("our_page_exists") is True:
            cls = "need_hypothesis"
            title = f"GEO: page exists but not cited for query “{query}”"
            actual = f"not cited; cited instead: {comps}"
        else:
            cls = "need_hypothesis"
            title = f"GEO: create a page for query “{query}”"
            actual = f"not cited and no page of ours answers it; cited instead: {comps}"
        if note:
            actual += f" — {note}"
        cards.append({"title": title[:OBS_TITLE_MAX], "classification": cls, "source": ROLE, "product": product_slug,
                      "query": query, "url": str(q.get("our_url") or ""), "evidence": f"competitors: {comps}"[:300],
                      "actual": actual[:OBS_ACTUAL_MAX], "fingerprint": fingerprint(query)})
    for o in (result.get("observations") or []):
        if isinstance(o, dict) and o.get("classification") == "reproducible_defect":
            url = str(o.get("url") or "")
            cards.append({"title": str(o.get("title", ""))[:OBS_TITLE_MAX], "classification": "reproducible_defect", "source": ROLE,
                          "product": product_slug, "url": url, "evidence": url[:300], "actual": str(o.get("actual", ""))[:OBS_ACTUAL_MAX],
                          "fingerprint": hashlib.sha1(f"geo|defect|{url or o.get('title', '')}".encode()).hexdigest()[:12]})
    log.debug("geo: %d observation cards for %s", len(cards), product_slug)
    return cards
