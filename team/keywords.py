"""Keyword data for the analyst (owner 28.11): DataForSEO and Bing Webmaster, called by the dispatcher, never by a role.

The analyst runs without a shell, without the network and without `.env`. A research task that needs search demand
carries a ``data_request`` (phrases × locations, competitor domains); the dispatcher calls the APIs itself with the keys
from its own environment, writes the answers to ``products/<slug>/metrics/keywords/<task>-<key>.{json,md}`` and puts the
markdown tables into the analyst's prompt. The analyst may also return a ``data_request`` of its own: the dispatcher
fetches it and runs the analyst again (``max_rounds``).

Sources:
- DataForSEO Google Ads ``search_volume/live`` (one request per location, ≤ 1000 phrases each) — paid.
- DataForSEO Labs ``google/ranked_keywords/live`` (one request per competitor × location) — paid.
- Bing Webmaster ``GetKeyword`` and ``GetRelatedKeywords`` (impressions over a window that ends ``bing_lag_days`` ago) — free.
Search Console stays in team/gsc.py; its ``gsc.md`` is already in every analyst prompt.

Money: ``product.yaml`` ``keywords.dataforseo_cap_usd`` is the product's ceiling for all DataForSEO calls ever booked
(table ``api_spend`` of team.db). Before each paid call the dispatcher reads the balance (``/v3/appendix/user_data``,
free) and refuses the call when the estimate would pass the ceiling or the balance; the cost the API reports is booked.
Every answer is cached on disk by request (checkpoint): a crash or a rerun of the same request pays nothing again.

Secrets: credentials are read from ``os.environ`` here and nowhere else; they never enter a prompt, a file or a log line
(the Bing key travels in the query string, so every error text is scrubbed of it).
"""
from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

log = logging.getLogger("team.keywords")

DFS_API = "https://api.dataforseo.com/v3"
BING_API = "https://ssl.bing.com/webmaster/api.svc/json"
ENV_DFS_LOGIN, ENV_DFS_PASSWORD, ENV_BING_KEY = "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD", "BING_WEBMASTER_API_KEY"
SECRET_ENV = (ENV_DFS_LOGIN, ENV_DFS_PASSWORD, ENV_BING_KEY)

SOURCES = ("google_ads_volume", "ranked_keywords", "bing_keyword", "bing_related")
DFS_OK = 20000

DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "dataforseo_cap_usd": 0.0,          # the product's ceiling for every DataForSEO call ever booked; 0 → no paid call at all
    "language": "en",                   # DataForSEO language_code
    "locations": {                      # request name → DataForSEO location code (None = all locations) and Bing country/language
        "gb": {"name": "United Kingdom", "dataforseo": 2826, "bing_country": "gb", "bing_language": "en-GB"},
        "us": {"name": "United States", "dataforseo": 2840, "bing_country": "us", "bing_language": "en-US"},
        "ie": {"name": "Ireland", "dataforseo": 2372, "bing_country": "ie", "bing_language": "en-IE"},
        # Bing has no «English, every country»: empty country and language = all of Bing (checked 28.11, 557 777 vs gb 90 833)
        "all": {"name": "all locations", "dataforseo": None, "bing_country": "", "bing_language": ""},
    },
    "default_locations": ["gb", "us", "ie", "all"],
    "default_sources": ["google_ads_volume", "bing_keyword", "bing_related"],
    "max_keywords": 300,                # phrases per request (DataForSEO takes ≤ 1000 per task)
    "max_competitors": 6,
    "ranked_limit": 200,                # ranked_keywords items per competitor × location
    "bing_window_days": 30,
    "bing_lag_days": 3,
    "bing_interval_s": 1.5,             # at most one Bing call per this many seconds
    "bing_max_calls": 400,              # per request; the rest is reported as not called
    "bing_related_locations": ["gb"],   # GetRelatedKeywords only here, and only for phrases with impressions > 0
    "prices": {"search_volume": 0.09, "ranked_request": 0.012, "ranked_result": 0.00012},   # user_data price list, 28.11
    "outputs": ["seo/"],                # repository paths (prefixes) the analyst may write with `files`
    "max_rounds": 2,                    # data requests the analyst may add itself within one task run
    "analyst_cap_usd": 2.0,             # model cap of one analyst TASK run (the daily measurement keeps $1)
    "cache_days": 14,
    "prompt_chars": 24000,              # the data tables in the analyst's prompt
}

HttpFn = Callable[[str, str, Any, dict], Any]          # (method, url, json body or None, headers) -> parsed json


class BudgetRefused(Exception):
    """A paid call was not made: it would pass the product's ceiling or the account balance."""


class MissingCredentials(Exception):
    pass


def settings(raw: dict[str, Any]) -> dict[str, Any]:
    own = dict(raw.get("keywords") or {})
    cfg = {**DEFAULTS, **own}
    cfg["locations"] = {**DEFAULTS["locations"], **dict(own.get("locations") or {})}
    cfg["prices"] = {**DEFAULTS["prices"], **dict(own.get("prices") or {})}
    cfg["dataforseo_cap_usd"] = float(cfg.get("dataforseo_cap_usd") or 0.0)
    return cfg


def _http(method: str, url: str, body: Any, headers: dict) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json", **headers} if data is not None else dict(headers)
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def scrub(text: str, env: dict[str, str] | None = None) -> str:
    """Error text with every credential value replaced (the Bing key sits in the URL)."""
    env = os.environ if env is None else env
    s = str(text)
    for k in SECRET_ENV:
        v = env.get(k) or ""
        if len(v) >= 4:
            s = s.replace(v, "***")
            s = s.replace(urllib.parse.quote(v, safe=""), "***")
    return re.sub(r"(apikey=)[^&\s]+", r"\1***", s)


# ------------------------------------------------------------------ the request


_BAD_CHARS = re.compile(r"[!@%,*()^=<>{}\[\]|\\;:\"?~`]")


def clean_phrase(k: Any) -> str | None:
    """DataForSEO's rules for a keyword: ≤ 80 characters, ≤ 10 words, none of its forbidden symbols."""
    s = " ".join(str(k or "").lower().split())
    if not s or len(s) > 80 or len(s.split()) > 10 or _BAD_CHARS.search(s):
        return None
    return s


def clean_domain(d: Any) -> str | None:
    s = str(d or "").strip().lower()
    s = re.sub(r"^https?://", "", s).split("/")[0]
    s = s[4:] if s.startswith("www.") else s
    return s if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", s) else None


def normalize(req: dict[str, Any] | None, cfg: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """The request the dispatcher will fetch, and what was dropped from it (said back to the analyst)."""
    req = dict(req or {})
    notes: list[str] = []
    kws: list[str] = []
    for k in req.get("keywords") or []:
        c = clean_phrase(k)
        if c is None:
            notes.append(f"phrase dropped (DataForSEO rules: ≤ 80 chars, ≤ 10 words, no symbols): {str(k)[:90]!r}")
        elif c not in kws:
            kws.append(c)
    if len(kws) > int(cfg["max_keywords"]):
        notes.append(f"{len(kws) - int(cfg['max_keywords'])} phrase(s) over max_keywords={cfg['max_keywords']} dropped")
        kws = kws[:int(cfg["max_keywords"])]
    locs = [str(x).lower() for x in (req.get("locations") or cfg["default_locations"])]
    bad = [x for x in locs if x not in cfg["locations"]]
    if bad:
        notes.append(f"unknown location(s) {bad}; known: {sorted(cfg['locations'])}")
    locs = list(dict.fromkeys(x for x in locs if x in cfg["locations"]))
    srcs = [str(x) for x in (req.get("sources") or cfg["default_sources"])]
    if [x for x in srcs if x not in SOURCES]:
        notes.append(f"unknown source(s) {[x for x in srcs if x not in SOURCES]}; known: {list(SOURCES)}")
    srcs = [x for x in SOURCES if x in srcs]
    comps: list[str] = []
    for d in req.get("competitors") or []:
        c = clean_domain(d)
        if c and c not in comps:
            comps.append(c)
        elif not c:
            notes.append(f"competitor dropped (not a domain): {str(d)[:80]!r}")
    if len(comps) > int(cfg["max_competitors"]):
        notes.append(f"competitors over max_competitors={cfg['max_competitors']} dropped: {comps[int(cfg['max_competitors']):]}")
        comps = comps[:int(cfg["max_competitors"])]
    include = [s for s in (" ".join(str(x).lower().split()) for x in (req.get("include") or [])) if s and not _BAD_CHARS.search(s)][:8]
    if "ranked_keywords" in srcs and not comps:
        notes.append("ranked_keywords asked without competitors: skipped")
        srcs = [x for x in srcs if x != "ranked_keywords"]
    if not kws:
        srcs = [x for x in srcs if x == "ranked_keywords"]
    return {"keywords": kws, "locations": locs, "sources": srcs, "competitors": comps, "include": include}, notes


def request_key(req: dict[str, Any]) -> str:
    return hashlib.sha1(json.dumps(req, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:10]


def estimate_usd(req: dict[str, Any], cfg: dict[str, Any]) -> float:
    """The most the request can cost at the configured prices (before the cache is looked at)."""
    pr = cfg["prices"]
    n = 0.0
    if "google_ads_volume" in req["sources"] and req["keywords"]:
        n += pr["search_volume"] * len(req["locations"])
    if "ranked_keywords" in req["sources"]:
        locs = [x for x in req["locations"] if cfg["locations"][x].get("dataforseo")]
        n += len(req["competitors"]) * len(locs) * (pr["ranked_request"] + pr["ranked_result"] * int(cfg["ranked_limit"]))
    return round(n, 4)


# ------------------------------------------------------------------ cache and lock (checkpoints)


class Cache:
    """One file per answered call, keyed by endpoint and body: the checkpoint of a fetch (owner rule: long work resumes)."""

    def __init__(self, root: Path, days: int):
        self.root, self.days = root, days
        root.mkdir(parents=True, exist_ok=True)

    def _f(self, endpoint: str, body: Any) -> Path:
        return self.root / (hashlib.sha1(json.dumps([endpoint, body], sort_keys=True).encode()).hexdigest()[:20] + ".json")

    def get(self, endpoint: str, body: Any, now: datetime) -> Any | None:
        f = self._f(endpoint, body)
        if not f.exists():
            return None
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if str(d.get("ts") or "") < (now - timedelta(days=self.days)).isoformat():
            return None
        return d.get("answer")

    def put(self, endpoint: str, body: Any, answer: Any, now: datetime) -> None:
        f = self._f(endpoint, body)
        tmp = f.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": now.isoformat(timespec="seconds"), "endpoint": endpoint, "answer": answer}, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(f)


@contextlib.contextmanager
def paid_lock(path: Path) -> Iterator[None]:
    """One paid fetch per product at a time, across lanes and processes: the ceiling check and the booking are one step."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


# ------------------------------------------------------------------ DataForSEO


class DataForSEO:
    def __init__(self, login: str, password: str, http: HttpFn = _http):
        self.http = http
        self.auth = {"Authorization": "Basic " + base64.b64encode(f"{login}:{password}".encode()).decode()}

    def balance(self) -> float:
        d = self.http("GET", f"{DFS_API}/appendix/user_data", None, self.auth)
        t = (d.get("tasks") or [{}])[0]
        if int(t.get("status_code") or d.get("status_code") or 0) != DFS_OK:
            raise RuntimeError(f"user_data: {t.get('status_code')} {t.get('status_message') or d.get('status_message')}")
        return float(((t.get("result") or [{}])[0].get("money") or {}).get("balance"))

    def post(self, path: str, task: dict[str, Any]) -> tuple[dict[str, Any], float]:
        """One live task; returns (task, cost). A task-level error is returned, not raised (it is reported per row)."""
        d = self.http("POST", f"{DFS_API}/{path}", [task], self.auth)
        t = (d.get("tasks") or [{}])[0]
        return t, float(t.get("cost") if t.get("cost") is not None else d.get("cost") or 0.0)


def _volume_body(keywords: list[str], loc: dict[str, Any], lang: str) -> dict[str, Any]:
    body: dict[str, Any] = {"keywords": keywords, "language_code": lang}
    if loc.get("dataforseo"):
        body["location_code"] = int(loc["dataforseo"])
    return body


def _ranked_body(target: str, loc: dict[str, Any], lang: str, limit: int, include: list[str]) -> dict[str, Any]:
    body: dict[str, Any] = {"target": target, "location_code": int(loc["dataforseo"]), "language_code": lang, "limit": limit,
                            "order_by": ["keyword_data.keyword_info.search_volume,desc"]}
    if include:
        flt: list[Any] = []
        for i, w in enumerate(include):
            if i:
                flt.append("or")
            flt.append(["keyword_data.keyword", "like", f"%{w}%"])
        body["filters"] = flt
    return body


def _parse_volume(task: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in task.get("result") or []:
        ms = [m for m in (r.get("monthly_searches") or []) if m.get("year")]
        months = sorted(f"{m['year']}-{int(m['month']):02d}" for m in ms)
        out[str(r.get("keyword"))] = {"volume": r.get("search_volume"), "cpc": r.get("cpc"), "competition": r.get("competition"),
                                      "months": f"{months[0]}…{months[-1]}" if months else None}
    return out


def _parse_ranked(task: dict[str, Any], competitor: str, loc_name: str) -> list[dict[str, Any]]:
    rows = []
    for res in task.get("result") or []:
        for it in res.get("items") or []:
            kd = it.get("keyword_data") or {}
            serp = ((it.get("ranked_serp_element") or {}).get("serp_item") or {})
            rows.append({"keyword": kd.get("keyword"), "location": loc_name, "volume": (kd.get("keyword_info") or {}).get("search_volume"),
                         "competitor": competitor, "position": serp.get("rank_group"), "url": serp.get("relative_url") or serp.get("url")})
    return rows


# ------------------------------------------------------------------ Bing


class Bing:
    def __init__(self, key: str, http: HttpFn = _http, sleep: Callable[[float], None] = time.sleep, interval_s: float = 1.5):
        self.key, self.http, self.sleep, self.interval = key, http, sleep, interval_s
        self._last = 0.0
        self.calls = 0

    def _get(self, op: str, params: dict[str, str]) -> Any:
        wait = self.interval - (time.monotonic() - self._last)
        if self._last and wait > 0:
            self.sleep(wait)
        self._last = time.monotonic()
        self.calls += 1
        url = f"{BING_API}/{op}?" + urllib.parse.urlencode({**params, "apikey": self.key})
        return (self.http("GET", url, None, {}) or {}).get("d")


def bing_window(now: datetime, cfg: dict[str, Any]) -> tuple[str, str]:
    end = (now - timedelta(days=int(cfg["bing_lag_days"]))).date()
    start = end - timedelta(days=int(cfg["bing_window_days"]))
    return start.isoformat(), end.isoformat()


# ------------------------------------------------------------------ the fetch


def fetch(req: dict[str, Any], cfg: dict[str, Any], *, out_dir: Path, store: Any, task_id: str, env: dict[str, str] | None = None,
          http: HttpFn | None = None, sleep: Callable[[float], None] | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Fetch one normalized request. Never raises for an API answer: every failure is a row in ``errors`` and the value is
    missing. ``BudgetRefused`` before any paid call that would pass the ceiling or the balance is recorded the same way and
    stops the paid part (the free Bing part still runs). Writes ``<task>-<key>.json`` and ``.md`` into ``out_dir``."""
    env = os.environ if env is None else env
    http, sleep = http or _http, sleep or time.sleep
    now = now or datetime.now(timezone.utc)
    key = request_key(req)
    cache = Cache(out_dir / "cache", int(cfg["cache_days"]))
    res: dict[str, Any] = {"task": task_id, "key": key, "request": req, "generated": now.isoformat(timespec="seconds"),
                           "volume": {}, "ranked": [], "bing": {}, "bing_related": {}, "errors": [], "spend": [],
                           "cap_usd": cfg["dataforseo_cap_usd"], "refused": None}
    lang = str(cfg["language"])
    log.info("keywords %s: request %s — %d phrase(s), locations %s, sources %s, competitors %s", task_id, key, len(req["keywords"]),
             req["locations"], req["sources"], req["competitors"])

    paid: list[tuple[str, str, dict[str, Any], Callable[[dict[str, Any]], None], float]] = []
    if "google_ads_volume" in req["sources"] and req["keywords"]:
        for ln in req["locations"]:
            loc = cfg["locations"][ln]
            def _put_vol(task: dict[str, Any], ln: str = ln) -> None:
                res["volume"][ln] = _parse_volume(task)
            paid.append((f"volume {ln}", "keywords_data/google_ads/search_volume/live", _volume_body(req["keywords"], loc, lang), _put_vol,
                         float(cfg["prices"]["search_volume"])))
    if "ranked_keywords" in req["sources"]:
        for comp in req["competitors"]:
            for ln in req["locations"]:
                loc = cfg["locations"][ln]
                if not loc.get("dataforseo"):
                    res["errors"].append({"what": f"ranked_keywords {comp} {ln}", "error": "DataForSEO Labs needs one location; not called for 'all'"})
                    continue
                def _put_rk(task: dict[str, Any], comp: str = comp, name: str = str(loc["name"])) -> None:
                    res["ranked"].extend(_parse_ranked(task, comp, name))
                paid.append((f"ranked {comp} {ln}", "dataforseo_labs/google/ranked_keywords/live",
                             _ranked_body(comp, loc, lang, int(cfg["ranked_limit"]), req["include"]), _put_rk,
                             float(cfg["prices"]["ranked_request"]) + float(cfg["prices"]["ranked_result"]) * int(cfg["ranked_limit"])))
    if paid:
        _run_paid(paid, cfg, res, cache, store, task_id, env, http, now, out_dir)
    if req["keywords"] and ("bing_keyword" in req["sources"] or "bing_related" in req["sources"]):
        _run_bing(req, cfg, res, cache, env, http, sleep, now)
    res["spent_usd"] = round(sum(x["usd"] for x in res["spend"]), 4)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{task_id}-{key}.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / f"{task_id}-{key}.md").write_text(to_md(res, cfg), encoding="utf-8")
    log.info("keywords %s: request %s done — spent $%.4f, %d error(s), refused: %s", task_id, key, res["spent_usd"], len(res["errors"]),
             res["refused"] or "no")
    return res


def _run_paid(paid: list, cfg: dict[str, Any], res: dict[str, Any], cache: Cache, store: Any, task_id: str, env: dict[str, str],
              http: HttpFn, now: datetime, out_dir: Path) -> None:
    login, pw = env.get(ENV_DFS_LOGIN) or "", env.get(ENV_DFS_PASSWORD) or ""
    todo = []
    for what, ep, body, put, est in paid:
        hit = cache.get(ep, body, now)
        if hit is not None:
            log.info("keywords %s: %s from the cache (no charge)", task_id, what)
            put(hit)
        else:
            todo.append((what, ep, body, put, est))
    if not todo:
        return
    if not (login and pw):
        res["refused"] = f"{ENV_DFS_LOGIN}/{ENV_DFS_PASSWORD} are not set in the dispatcher's environment"
        res["errors"] += [{"what": w, "error": res["refused"]} for w, *_ in todo]
        return
    cap = float(cfg["dataforseo_cap_usd"])
    dfs = DataForSEO(login, pw, http)
    with paid_lock(out_dir / ".dataforseo.lock"):
        for i, (what, ep, body, put, est) in enumerate(todo):
            spent = float(store.api_spent("dataforseo"))
            try:
                if spent + est > cap + 1e-9:
                    raise BudgetRefused(f"the DataForSEO ceiling of this product is ${cap:.2f} (keywords.dataforseo_cap_usd), "
                                        f"${spent:.4f} is spent and {what} may cost ${est:.4f}")
                bal = dfs.balance()
                if bal < est:
                    raise BudgetRefused(f"the DataForSEO account balance ${bal:.4f} is below the ${est:.4f} that {what} may cost")
            except BudgetRefused as exc:
                res["refused"] = str(exc)
                res["errors"] += [{"what": w, "error": f"not called: {exc}"} for w, *_ in todo[i:]]
                log.warning("keywords %s: paid calls stopped: %s", task_id, exc)
                return
            except Exception as exc:  # noqa: BLE001 — no balance, no paid call
                msg = scrub(f"balance check failed: {exc}", env)
                res["refused"] = msg
                res["errors"] += [{"what": w, "error": f"not called: {msg}"} for w, *_ in todo[i:]]
                log.warning("keywords %s: %s", task_id, msg)
                return
            try:
                task, cost = dfs.post(ep, body)
            except Exception as exc:  # noqa: BLE001
                msg = scrub(str(exc), env)
                res["errors"].append({"what": what, "error": msg[:300]})
                log.warning("keywords %s: %s failed: %s", task_id, what, msg[:300])
                continue
            store.book_api_spend("dataforseo", ep, task_id, cost, bal, f"{what} · request {res['key']}")
            res["spend"].append({"what": what, "usd": cost, "balance_before": bal})
            log.info("keywords %s: %s cost $%.4f (balance before $%.4f, product total $%.4f of $%.2f)", task_id, what, cost, bal, spent + cost, cap)
            code = int(task.get("status_code") or 0)
            if code != DFS_OK:
                res["errors"].append({"what": what, "error": f"{code} {task.get('status_message')}"[:300]})
                continue
            cache.put(ep, body, task, now)
            put(task)
        try:
            res["balance_after"] = dfs.balance()
        except Exception as exc:  # noqa: BLE001
            res["errors"].append({"what": "balance after", "error": scrub(str(exc), env)[:200]})


def _run_bing(req: dict[str, Any], cfg: dict[str, Any], res: dict[str, Any], cache: Cache, env: dict[str, str], http: HttpFn,
              sleep: Callable[[float], None], now: datetime) -> None:
    key = env.get(ENV_BING_KEY) or ""
    start, end = bing_window(now, cfg)
    res["bing_window"] = [start, end]
    if not key:
        res["errors"].append({"what": "bing", "error": f"{ENV_BING_KEY} is not set in the dispatcher's environment"})
        return
    bing = Bing(key, http, sleep, float(cfg["bing_interval_s"]))
    limit = int(cfg["bing_max_calls"])
    skipped = 0

    def call(op: str, q: str, loc: dict[str, Any]) -> Any:
        nonlocal skipped
        params = {"q": q, "country": str(loc["bing_country"]), "language": str(loc["bing_language"]), "startDate": start, "endDate": end}
        hit = cache.get(f"bing/{op}", params, now)
        if hit is not None:
            return hit
        if bing.calls >= limit:
            skipped += 1
            return None
        try:
            ans = bing._get(op, params)
        except urllib.error.HTTPError as exc:
            body = ""
            with contextlib.suppress(Exception):
                body = exc.read().decode("utf-8", "replace")[:200]
            res["errors"].append({"what": f"bing {op} {q!r} {loc['name']}", "error": scrub(f"HTTP {exc.code} {body}", env)})
            return None
        except Exception as exc:  # noqa: BLE001
            res["errors"].append({"what": f"bing {op} {q!r} {loc['name']}", "error": scrub(str(exc), env)[:200]})
            return None
        cache.put(f"bing/{op}", params, ans, now)
        return ans

    for ln in req["locations"]:
        loc = cfg["locations"][ln]
        if "bing_keyword" in req["sources"]:
            col: dict[str, Any] = {}
            for q in req["keywords"]:
                d = call("GetKeyword", q, loc)
                if isinstance(d, dict):
                    col[q] = {"impressions": d.get("Impressions"), "broad": d.get("BroadImpressions")}
            res["bing"][ln] = col
        if "bing_related" in req["sources"] and ln in cfg["bing_related_locations"]:
            seeds = [q for q in req["keywords"] if ((res["bing"].get(ln) or {}).get(q) or {}).get("impressions")] \
                if "bing_keyword" in req["sources"] else list(req["keywords"])
            rel: dict[str, list[dict[str, Any]]] = {}
            for q in seeds:
                d = call("GetRelatedKeywords", q, loc)
                if isinstance(d, list):
                    rel[q] = [{"query": x.get("Query"), "impressions": x.get("Impressions"), "broad": x.get("BroadImpressions")}
                              for x in d if x.get("Impressions")]
            res["bing_related"][ln] = rel
    if skipped:
        res["errors"].append({"what": "bing", "error": f"{skipped} call(s) not made: bing_max_calls={limit} per request"})


# ------------------------------------------------------------------ markdown for the prompt and the product folder


def _n(v: Any) -> str:
    return "—" if v is None else f"{v:,}".replace(",", " ") if isinstance(v, (int, float)) else str(v)


def to_md(res: dict[str, Any], cfg: dict[str, Any]) -> str:
    req = res["request"]
    locs = req["locations"]
    names = {ln: cfg["locations"][ln]["name"] for ln in locs if ln in cfg["locations"]}
    lines = [f"# Keyword data for {res['task']} (request {res['key']}, fetched {res['generated']} by the dispatcher)", ""]
    if res.get("volume"):
        months = sorted({v.get("months") for col in res["volume"].values() for v in col.values() if v.get("months")})
        lines += [f"## Google Ads monthly search volume — DataForSEO, language {cfg['language']}, months {', '.join(months) or 'not returned'}",
                  "", "| phrase | " + " | ".join(names[ln] for ln in locs) + " |", "|---|" + "---|" * len(locs)]
        for q in req["keywords"]:
            cells = []
            for ln in locs:
                col = res["volume"].get(ln)
                cells.append("not fetched" if col is None else _n((col.get(q) or {}).get("volume")) if q in col else "missing")
            lines.append(f"| {q} | " + " | ".join(cells) + " |")
        lines += ["", "«—» = the API returned null (no data); «missing» = the phrase is not in the answer.", ""]
    if res.get("bing"):
        s, e = res.get("bing_window") or ["?", "?"]
        lines += [f"## Bing Webmaster impressions, {s}…{e} (Bing, not Google; exact-match impressions, broad in brackets)", "",
                  "| phrase | " + " | ".join(f"{names[ln]} ({cfg['locations'][ln]['bing_country'] or 'any'}/{cfg['locations'][ln]['bing_language'] or 'any'})"
                                             for ln in locs if ln in res["bing"]) + " |",
                  "|---|" + "---|" * len([ln for ln in locs if ln in res["bing"]])]
        for q in req["keywords"]:
            cells = []
            for ln in locs:
                if ln not in res["bing"]:
                    continue
                v = res["bing"][ln].get(q)
                cells.append("missing" if v is None else f"{_n(v.get('impressions'))} ({_n(v.get('broad'))})")
            lines.append(f"| {q} | " + " | ".join(cells) + " |")
        lines.append("")
    for ln, rel in (res.get("bing_related") or {}).items():
        if not rel:
            continue
        lines += [f"## Bing related queries with impressions > 0 — {names.get(ln, ln)}", ""]
        for q, rows in rel.items():
            if rows:
                lines.append(f"- **{q}**: " + "; ".join(f"{r['query']} {_n(r['impressions'])}" for r in rows[:15]))
        lines.append("")
    if res.get("ranked"):
        lines += [f"## Competitor keywords — DataForSEO Labs ranked_keywords (Google organic), filter {req['include'] or 'none'}", "",
                  "| keyword | location | monthly volume | competitor | position | url |", "|---|---|---|---|---|---|"]
        for r in sorted(res["ranked"], key=lambda r: -(r.get("volume") or 0)):
            lines.append(f"| {r['keyword']} | {r['location']} | {_n(r['volume'])} | {r['competitor']} | {_n(r['position'])} | {r.get('url') or ''} |")
        lines.append("")
    lines += ["## Spend (DataForSEO; Bing is free)", ""]
    for x in res.get("spend") or []:
        lines.append(f"- {x['what']}: ${x['usd']:.4f} (balance before ${x['balance_before']:.4f})")
    lines.append(f"- this request: ${sum(x['usd'] for x in res.get('spend') or []):.4f}; balance after: "
                 + (f"${res['balance_after']:.4f}" if res.get("balance_after") is not None else "not read")
                 + f"; product ceiling ${float(res.get('cap_usd') or 0):.2f}")
    if res.get("refused"):
        lines += ["", f"**Paid calls refused:** {res['refused']}"]
    if res.get("errors"):
        lines += ["", "## Errors (the values are missing, never estimate them)", ""]
        lines += [f"- {e['what']}: {e['error']}" for e in res["errors"][:40]]
        if len(res["errors"]) > 40:
            lines.append(f"- … {len(res['errors']) - 40} more")
    return "\n".join(lines) + "\n"


def has_data(res: dict[str, Any]) -> bool:
    return any(col for col in (res.get("volume") or {}).values()) or bool(res.get("ranked")) \
        or any(col for col in (res.get("bing") or {}).values())


def spend_line(store: Any, cfg: dict[str, Any]) -> str | None:
    """One line for `team status` and the daily digest; None when keyword data is off and nothing was ever paid."""
    spent, calls = store.api_spent("dataforseo"), store.api_calls("dataforseo")
    if not cfg["enabled"] and not calls:
        return None
    return (f"keyword data: DataForSEO ${spent:.2f} of ceiling ${cfg['dataforseo_cap_usd']:.2f} ({calls} paid call(s))"
            + ("" if cfg["enabled"] else " · keywords.enabled is off"))
