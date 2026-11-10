"""Google Search Console for the products (owner 10.11): what Google indexed and what it shows people.

Once a day per product with a host: Search Analytics for the last ``days`` days (totals by day, top pages, top queries,
filtered to the product's host inside a domain property) and URL Inspection for a rotating sample of sitemap URLs
(``inspect_per_day`` a day, each URL at most once in ``reinspect_days``). Output: ``products/<slug>/metrics/gsc.json``
and ``gsc.md``; the PO and the analyst read ``gsc.md`` next to the log metrics.

Auth: a service-account JSON key (``gsc.key_file``, outside git), added in Search Console as a restricted user. The JWT is
signed with the ``openssl`` CLI, so there is no Google library dependency. Search data lags 2–3 days behind.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("team.gsc")

SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
API = "https://www.googleapis.com/webmasters/v3"
INSPECT_API = "https://searchconsole.googleapis.com/v1/urlInspection/index:inspect"
DEFAULTS = {"enabled": False, "key_file": "secrets/gsc.json", "days": 28, "inspect_per_day": 20, "reinspect_days": 7,
            "top_pages": 25, "top_queries": 25}

HttpFn = Callable[[str, str, dict | None, dict], dict]     # (method, url, json body, headers) -> json


def settings(st: Any) -> dict[str, Any]:
    return {**DEFAULTS, **dict(st.raw.get("gsc") or {})}


def _b64(x: bytes) -> bytes:
    return base64.urlsafe_b64encode(x).rstrip(b"=")


def _http(method: str, url: str, body: dict | None, headers: dict) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", **headers} if data else dict(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def access_token(key_file: Path) -> str:
    """Service-account JWT → OAuth access token. The private key touches disk only as a 0600 temp file for openssl."""
    k = json.loads(key_file.read_text(encoding="utf-8"))
    now = int(time.time())
    head = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    claims = _b64(json.dumps({"iss": k["client_email"], "scope": SCOPE, "aud": k["token_uri"], "iat": now, "exp": now + 3600}).encode())
    fd, kp = tempfile.mkstemp(prefix="gsc-", suffix=".pem")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(k["private_key"])
        sig = subprocess.run(["openssl", "dgst", "-sha256", "-sign", kp], input=head + b"." + claims,
                             capture_output=True, check=True).stdout
    finally:
        os.unlink(kp)
    form = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                   "assertion": (head + b"." + claims + b"." + _b64(sig)).decode()}).encode()
    with urllib.request.urlopen(k["token_uri"], form, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))["access_token"]


def property_for(host: str, sites: list[str]) -> str | None:
    """The most specific property covering ``host``: a URL-prefix property for it, else the longest matching sc-domain."""
    host = host.lower()
    best, best_len = None, -1
    for s in sites:
        if s.startswith("sc-domain:"):
            d = s[len("sc-domain:"):].lower()
            if (host == d or host.endswith("." + d)) and len(d) > best_len:
                best, best_len = s, len(d)
        else:
            h = urllib.parse.urlparse(s).hostname or ""
            if h.lower() == host and 10_000 > best_len:
                best, best_len = s, 10_000
    return best


def host_filter(host: str) -> list[dict]:
    return [{"filters": [{"dimension": "page", "operator": "contains", "expression": f"://{host}/"}]}]


def query(http: HttpFn, tok: str, prop: str, host: str, start: str, end: str, dims: list[str], limit: int) -> list[dict]:
    url = f"{API}/sites/{urllib.parse.quote(prop, safe='')}/searchAnalytics/query"
    body = {"startDate": start, "endDate": end, "dimensions": dims, "rowLimit": limit, "dimensionFilterGroups": host_filter(host)}
    return list(http("POST", url, body, {"Authorization": f"Bearer {tok}"}).get("rows") or [])


def sitemap_urls(site_base: str, fetch: Callable[[str], str] | None = None, cap: int = 5000) -> list[str]:
    """URLs from /sitemap.xml (one level of sitemap index followed)."""
    def _get(u: str) -> str:
        req = urllib.request.Request(u, headers={"User-Agent": "agent-team gsc (dispatcher)"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read().decode("utf-8", "replace")
    fetch = fetch or _get
    try:
        xml = fetch(site_base.rstrip("/") + "/sitemap.xml")
    except Exception as exc:  # noqa: BLE001
        log.info("sitemap not available for %s: %s", site_base, exc)
        return []
    locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)
    if "<sitemapindex" in xml:
        out: list[str] = []
        for sm in locs[:20]:
            try:
                out += re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", fetch(sm))
            except Exception as exc:  # noqa: BLE001
                log.info("child sitemap %s failed: %s", sm, exc)
        locs = out
    return locs[:cap]


def pick_for_inspection(urls: list[str], state: dict[str, Any], n: int, reinspect_days: int, now: datetime) -> list[str]:
    """Never-inspected URLs first (in sitemap order, home page first), then the oldest inspections past ``reinspect_days``."""
    cutoff = (now - timedelta(days=reinspect_days)).isoformat()
    fresh = [u for u in urls if u not in state]
    stale = sorted((u for u in urls if u in state and str(state[u].get("checked") or "") < cutoff), key=lambda u: state[u].get("checked") or "")
    return (fresh + stale)[:max(0, n)]


def inspect(http: HttpFn, tok: str, prop: str, url: str) -> dict[str, Any]:
    r = http("POST", INSPECT_API, {"inspectionUrl": url, "siteUrl": prop}, {"Authorization": f"Bearer {tok}"})
    idx = ((r.get("inspectionResult") or {}).get("indexStatusResult") or {})
    return {"verdict": idx.get("verdict"), "coverage": idx.get("coverageState"), "last_crawl": idx.get("lastCrawlTime"),
            "google_canonical": idx.get("googleCanonical"), "user_canonical": idx.get("userCanonical"),
            "robots": idx.get("robotsTxtState"), "fetch": idx.get("pageFetchState")}


def _row(r: dict, keys: list[str]) -> dict[str, Any]:
    return {**dict(zip(keys, r.get("keys") or [])), "clicks": int(r.get("clicks") or 0), "impressions": int(r.get("impressions") or 0),
            "ctr": round(float(r.get("ctr") or 0), 4), "position": round(float(r.get("position") or 0), 1)}


def collect(product: Any, cfg: dict[str, Any], tok: str, sites: list[str], *, http: HttpFn = _http,
            fetch: Callable[[str], str] | None = None, now: datetime | None = None) -> dict[str, Any] | None:
    now = now or datetime.now(timezone.utc)
    host = str(product.host or urllib.parse.urlparse(product.site_base).hostname or "")
    if not host:
        return None
    prop = property_for(host, sites)
    if not prop:
        log.warning("%s: no Search Console property covers %s (sites: %s)", product.slug, host, ", ".join(sites))
        return {"host": host, "property": None, "error": "no property covers this host; add it in Search Console"}
    end = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    start = (now - timedelta(days=int(cfg["days"]))).strftime("%Y-%m-%d")
    out: dict[str, Any] = {"host": host, "property": prop, "start": start, "end": end, "generated": now.isoformat(timespec="seconds")}
    log.info("%s: Search Analytics %s %s..%s", product.slug, prop, start, end)
    out["by_date"] = [_row(r, ["date"]) for r in query(http, tok, prop, host, start, end, ["date"], 1000)]
    out["pages"] = [_row(r, ["page"]) for r in query(http, tok, prop, host, start, end, ["page"], int(cfg["top_pages"]))]
    out["queries"] = [_row(r, ["query"]) for r in query(http, tok, prop, host, start, end, ["query"], int(cfg["top_queries"]))]
    out["totals"] = {k: sum(r[k] for r in out["by_date"]) for k in ("clicks", "impressions")}

    mdir = product.dir / "metrics"
    mdir.mkdir(parents=True, exist_ok=True)
    state_f = mdir / "gsc-inspect.json"
    state: dict[str, Any] = json.loads(state_f.read_text(encoding="utf-8")) if state_f.exists() else {}
    urls = sitemap_urls(product.site_base or f"https://{host}", fetch)
    todo = pick_for_inspection(urls, state, int(cfg["inspect_per_day"]), int(cfg["reinspect_days"]), now)
    n_ok = 0
    for u in todo:
        try:
            state[u] = {**inspect(http, tok, prop, u), "checked": now.isoformat(timespec="seconds")}
            n_ok += 1
        except Exception as exc:  # noqa: BLE001 — quota or a single bad URL must not lose the rest
            log.warning("%s: inspection of %s failed: %s", product.slug, u, exc)
            if "429" in str(exc) or "quota" in str(exc).lower():
                break
    state = {u: v for u, v in state.items() if u in set(urls)} if urls else state     # pages gone from the sitemap drop out
    state_f.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    cov: dict[str, int] = {}
    for v in state.values():
        cov[str(v.get("coverage") or "unknown")] = cov.get(str(v.get("coverage") or "unknown"), 0) + 1
    out["inspection"] = {"sitemap_urls": len(urls), "inspected": len(state), "inspected_today": n_ok, "coverage": cov,
                         "not_indexed_examples": [{"url": u, "coverage": v.get("coverage")} for u, v in state.items()
                                                  if v.get("verdict") != "PASS"][:10]}
    (mdir / "gsc.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    (mdir / "gsc.md").write_text(gsc_md(out), encoding="utf-8")
    log.info("%s: gsc clicks=%d impressions=%d inspected=%d/%d (+%d today)", product.slug, out["totals"]["clicks"],
             out["totals"]["impressions"], len(state), len(urls), n_ok)
    return out


def gsc_md(d: dict[str, Any]) -> str:
    if not d.get("property"):
        return f"# Google Search Console — {d.get('host')}\n\n{d.get('error')}\n"
    t, ins = d.get("totals") or {}, d.get("inspection") or {}
    lines = [f"# Google Search Console — {d['host']} ({d['start']} … {d['end']}, data lags 2–3 days)", "",
             f"Clicks from Google: {t.get('clicks', 0)} · impressions: {t.get('impressions', 0)} (days with data: {len(d.get('by_date') or [])})",
             "", f"## Index (URL Inspection, sample of the sitemap: {ins.get('inspected', 0)} of {ins.get('sitemap_urls', 0)} URLs checked)"]
    for k, v in sorted((ins.get("coverage") or {}).items(), key=lambda kv: -kv[1]):
        lines.append(f"- {k}: {v}")
    if ins.get("not_indexed_examples"):
        lines += ["", "Not indexed (examples):"] + [f"- {x['url']} — {x['coverage']}" for x in ins["not_indexed_examples"]]
    if d.get("by_date"):
        lines += ["", "## By day (date · clicks · impressions · avg position)"]
        lines += [f"- {r['date']} · {r['clicks']} · {r['impressions']} · {r['position']}" for r in d["by_date"][-14:]]
    if d.get("pages"):
        lines += ["", "## Top pages (page · clicks · impressions · position)"]
        lines += [f"- {r['page']} · {r['clicks']} · {r['impressions']} · {r['position']}" for r in d["pages"]]
    if d.get("queries"):
        lines += ["", "## Top queries (query · clicks · impressions · position)"]
        lines += [f"- {r['query']} · {r['clicks']} · {r['impressions']} · {r['position']}" for r in d["queries"]]
    if not d.get("by_date"):
        lines += ["", "No search impressions yet for this host in the window."]
    return "\n".join(lines) + "\n"


def run_all(st: Any, products: list[Any]) -> int:
    cfg = settings(st)
    key = Path(cfg["key_file"])
    key = key if key.is_absolute() else st.root / key
    if not key.exists():
        log.error("gsc key file %s not found", key)
        return 1
    tok = access_token(key)
    sites = [s["siteUrl"] for s in _http("GET", f"{API}/sites", None, {"Authorization": f"Bearer {tok}"}).get("siteEntry") or []]
    log.info("gsc: properties visible to the service account: %s", ", ".join(sites) or "(none)")
    rc = 0
    for p in products:
        try:
            collect(p, cfg, tok, sites)
        except Exception as exc:  # noqa: BLE001
            log.exception("%s: gsc collection failed: %s", p.slug, exc)
            rc = 1
    return rc
