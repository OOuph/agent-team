"""Site metrics from the nginx access log: per-day aggregates for one host, without addresses in the output.

Stdlib only (runs from cron on a small server). Python >= 3.10.

Input: nginx ``log_format full``::

    '$remote_addr - - [$time_iso8601] "$request" $status $body_bytes_sent "$http_referer" "$http_user_agent" $host $request_time'

Files: ``access.log``, ``access.log.1``, ``access.log.N.gz`` (gz is read transparently).

Every request falls into exactly one class, checked in this order:
  own      - UA starts with one of ``own_ua_prefixes`` (default ``agent-team/``) or the address is loopback
  crawler  - UA names a known bot (``CRAWLERS``); ``social_preview`` is counted separately for link-preview bots;
             a UA with a generic bot marker (``GENERIC_CRAWLER_MARKERS``) that names no known bot counts as crawler ``other``
  scanner  - path hits ``SCANNER_PATHS`` or UA names a scanner (``SCANNER_UAS``)
  agent    - programmatic UA family (``AGENT_FAMILIES``), on any path
  human    - browser UA (``BROWSER_MARKERS``) without any of ``NON_HUMAN_MARKERS``
  unknown  - everything else, including an empty UA

A source is the pair (ip, ua); it is never written to the output. ``humans.engaged`` are human sources that
loaded an asset (``ASSET_EXTS``, status < 400) or came with an external referer. ``humans.returns`` are today's
engaged sources that were seen as human sources on any earlier day of the parsed logs (days before ``since``
count as history, so a 7-day window over a month of logs still knows who came back).

CLI::

    python3 -m team.sitemetrics --host x.formachines.ai --log /var/log/nginx/access.log [--log ...]
        [--since 2026-11-03] [--until 2026-11-10] [--target /feed.xml --target /api/] [--own-ua agent-team/]
        --out /var/www/x/_team/          # writes <out>/metrics.json and <out>/metrics.md ; --out - prints JSON
    python3 -m team.sitemetrics --all-hosts --log ... --out /var/www/_metrics/   # <out>/<host>/metrics.json per host
"""
from __future__ import annotations

import argparse
import gzip
import io
import json
import logging
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from urllib.parse import urlsplit

log = logging.getLogger("team.sitemetrics")

# ----------------------------------------------------------------------------------------------------------------
# Parameters (everything tunable lives here)
# ----------------------------------------------------------------------------------------------------------------

CLASSES: tuple[str, ...] = ("own", "crawler", "scanner", "agent", "human", "unknown")

DEFAULT_OWN_UA_PREFIXES: tuple[str, ...] = ("agent-team/",)
LOOPBACK_IPS: frozenset[str] = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})

# Known crawlers: canonical name -> substring of the UA (matched case-insensitively). Order matters for overlaps.
CRAWLERS: tuple[tuple[str, str], ...] = (
    ("Googlebot", "googlebot"),
    ("GoogleOther", "googleother"),
    ("Google-Extended", "google-extended"),
    ("bingbot", "bingbot"),
    ("BingPreview", "bingpreview"),
    ("GPTBot", "gptbot"),
    ("OAI-SearchBot", "oai-searchbot"),
    ("ClaudeBot", "claudebot"),
    ("Claude-Web", "claude-web"),
    ("Claude-SearchBot", "claude-searchbot"),
    ("PerplexityBot", "perplexitybot"),
    ("Perplexity-User", "perplexity-user"),
    ("Applebot", "applebot"),
    ("YandexBot", "yandexbot"),
    ("DuckDuckBot", "duckduckbot"),
    ("Bytespider", "bytespider"),
    ("CCBot", "ccbot"),
    ("Amazonbot", "amazonbot"),
    ("meta-externalagent", "meta-externalagent"),
    ("facebookexternalhit", "facebookexternalhit"),
    ("TelegramBot", "telegrambot"),  # real UA: "TelegramBot (like TwitterBot)" — must precede Twitterbot
    ("Twitterbot", "twitterbot"),
    ("LinkedInBot", "linkedinbot"),
    ("Slackbot", "slackbot"),
    ("Discordbot", "discordbot"),
    ("Mastodon", "mastodon"),
    ("Bluesky", "bluesky"),
    ("AhrefsBot", "ahrefsbot"),
    ("SemrushBot", "semrushbot"),
    ("MJ12bot", "mj12bot"),
    ("DotBot", "dotbot"),
    ("PetalBot", "petalbot"),
    ("Baiduspider", "baiduspider"),
    ("SeznamBot", "seznambot"),
    ("ia_archiver", "ia_archiver"),
    ("archive.org_bot", "archive.org_bot"),
)
SOCIAL_PREVIEW: frozenset[str] = frozenset(
    {"facebookexternalhit", "Twitterbot", "TelegramBot", "Slackbot", "Discordbot", "Bluesky", "Mastodon", "LinkedInBot"}
)
# A UA with one of these and no known name is still a crawler (name "other"); set to () to disable.
GENERIC_CRAWLER_MARKERS: tuple[str, ...] = ("bot/", "bot;", "bot)", "crawler", "spider")

SCANNER_PATHS: tuple[str, ...] = (
    "/.env", "/.git", "/.aws", "/.ssh", "/.DS_Store", "/wp-admin", "/wp-login", "/wp-content", "/wp-includes",
    "/wordpress", "/xmlrpc.php", "/phpmyadmin", "/pma/", "/vendor/", "/cgi-bin", "/admin/config", "/config.json",
    "/phpinfo", "/.well-known/security.txt.bak", "/shell", "/boaform", "/actuator", "/solr/", "/manager/html",
    "/owa/", "/ecp/", "/telescope", "/_ignition", "/console", "/etc/passwd", "/wp-json",
)
SCANNER_UAS: tuple[str, ...] = ("zgrab", "masscan", "nmap", "nuclei", "sqlmap", "nikto", "gobuster", "dirbuster",
                                "wpscan", "l9explore", "censys")

# Programmatic clients: family name -> substring (case-insensitive). Order matters (okhttp before java, ...).
AGENT_FAMILIES: tuple[tuple[str, str], ...] = (
    ("claude-code", "claude-code"),
    ("claude-code", "claudecode"),
    ("cursor", "cursor"),
    ("codex", "codex"),
    ("chatgpt", "chatgpt"),
    ("openai", "openai"),
    ("anthropic", "anthropic"),
    ("langchain", "langchain"),
    ("llama", "llama"),
    ("python-requests", "python-requests"),
    ("python-urllib", "python-urllib"),
    ("httpx", "httpx"),
    ("aiohttp", "aiohttp"),
    ("python", "python"),
    ("curl", "curl"),
    ("wget", "wget"),
    ("go-http-client", "go-http-client"),
    ("node-fetch", "node-fetch"),
    ("undici", "undici"),
    ("axios", "axios"),
    ("okhttp", "okhttp"),
    ("apache-httpclient", "apache-httpclient"),
    ("java", "java"),
    ("postman", "postmanruntime"),
    ("insomnia", "insomnia"),
    ("libwww-perl", "libwww-perl"),
    ("ruby", "ruby"),
    ("php", "guzzlehttp"),
    ("dotnet", "dotnet"),
    ("powershell", "powershell"),
)

BROWSER_MARKERS: tuple[str, ...] = ("mozilla/", "opera/", "opera ")
NON_HUMAN_MARKERS: tuple[str, ...] = ("bot", "crawl", "spider", "headless", "http", "python", "curl", "wget", "java", "go-")

ASSET_EXTS: frozenset[str] = frozenset(
    {"css", "js", "mjs", "woff", "woff2", "ttf", "otf", "png", "jpg", "jpeg", "gif", "webp", "svg", "ico"}
)

TOP_PAGES_N = 20
TOP_REFERERS_N = 10
MAX_PATH_LEN = 200

JSON_NAME = "metrics.json"
MD_NAME = "metrics.md"

# ----------------------------------------------------------------------------------------------------------------
# Parsing
# ----------------------------------------------------------------------------------------------------------------

LINE_RE = re.compile(
    r'^(?P<ip>\S+) - (?P<user>\S+) \[(?P<time>[^\]]+)\] "(?P<request>[^"]*)" (?P<status>\d{3}) (?P<bytes>\d+|-) '
    r'"(?P<referer>[^"]*)" "(?P<ua>[^"]*)" (?P<host>\S+)(?: (?P<rt>\S+))?\s*$'
)
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
HOST_SAFE_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]{0,250}[a-z0-9])?$")


@dataclass(frozen=True)
class Record:
    """One parsed log line. ``ip`` and ``ua`` identify the source and never leave the process."""
    ip: str
    day: str            # YYYY-MM-DD from $time_iso8601 (log's own timezone)
    method: str
    path: str           # without query string, truncated to MAX_PATH_LEN
    status: int
    referer: str        # raw $http_referer ("-" when absent)
    ua: str             # raw $http_user_agent ("-" when absent)
    host: str           # lower-cased $host


def _scrub_ip(s: str) -> str:
    """Replace IPv4 literals inside free text (paths, referer hosts) so they never reach the output."""
    return IPV4_RE.sub("x.x.x.x", s)


def _split_request(request: str) -> tuple[str, str]:
    """'GET /a/b?x=1 HTTP/1.1' -> ('GET', '/a/b'). Garbage (TLS bytes, '-') -> ('', <raw>)."""
    parts = request.split(" ")
    if len(parts) >= 2 and parts[0].isalpha() and parts[0].isupper():
        method, target = parts[0], parts[1]
    else:
        method, target = "", request
    if target.startswith(("http://", "https://")):
        target = urlsplit(target).path or "/"
    target = target.split("?", 1)[0].split("#", 1)[0]
    return method, _scrub_ip(target[:MAX_PATH_LEN])


def parse_line(line: str) -> Record | None:
    """Parse one ``log_format full`` line; None when it does not match or the timestamp is unusable."""
    m = LINE_RE.match(line)
    if not m:
        return None
    day = m.group("time")[:10]
    if not DAY_RE.match(day):
        return None
    method, path = _split_request(m.group("request"))
    ua = m.group("ua")
    return Record(
        ip=m.group("ip"),
        day=day,
        method=method,
        path=path,
        status=int(m.group("status")),
        referer=m.group("referer"),
        ua="" if ua == "-" else ua,
        host=m.group("host").lower(),
    )


def _open_log(path: Path) -> io.TextIOBase:
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")  # type: ignore[return-value]
    return open(path, "r", encoding="utf-8", errors="replace")


@dataclass
class ParseStats:
    files: int = 0
    missing: int = 0
    lines: int = 0
    parsed: int = 0
    skipped: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"files": self.files, "missing": self.missing, "lines": self.lines, "parsed": self.parsed,
                "skipped": self.skipped}


def iter_records(log_paths: Iterable[str | os.PathLike[str]], stats: ParseStats | None = None) -> Iterator[Record]:
    """Yield Records from every log file (plain or .gz). Missing files are logged and skipped."""
    stats = stats if stats is not None else ParseStats()
    for raw in log_paths:
        path = Path(raw)
        if not path.exists():
            log.warning("log file missing, skipped: %s", path)
            stats.missing += 1
            continue
        stats.files += 1
        n_lines = n_ok = 0
        with _open_log(path) as fh:
            for line in fh:
                n_lines += 1
                rec = parse_line(line)
                if rec is None:
                    if n_lines - n_ok <= 3:
                        log.debug("unparsed line in %s: %r", path, line[:160])
                    continue
                n_ok += 1
                yield rec
        log.info("read %s: lines=%d parsed=%d skipped=%d", path, n_lines, n_ok, n_lines - n_ok)
        stats.lines += n_lines
        stats.parsed += n_ok
        stats.skipped += n_lines - n_ok


# ----------------------------------------------------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------------------------------------------------

def classify(ua: str, ip: str, path: str, own_ua_prefixes: Iterable[str] = DEFAULT_OWN_UA_PREFIXES) -> tuple[str, str]:
    """Return (class, name). ``name`` is the bot for crawlers, the family for agents, '' otherwise."""
    ua = ua or ""
    low = ua.lower()
    if ip in LOOPBACK_IPS or any(ua.startswith(p) for p in own_ua_prefixes):
        return "own", ""
    for name, needle in CRAWLERS:
        if needle in low:
            return "crawler", name
    if any(mk in low for mk in GENERIC_CRAWLER_MARKERS):
        return "crawler", "other"
    plow = path.lower()
    if any(plow.startswith(p.lower()) for p in SCANNER_PATHS) or any(s in low for s in SCANNER_UAS):
        return "scanner", ""
    for family, needle in AGENT_FAMILIES:
        if needle in low:
            return "agent", family
    if any(mk in low for mk in BROWSER_MARKERS) and not any(mk in low for mk in NON_HUMAN_MARKERS):
        return "human", ""
    return "unknown", ""


def is_asset(path: str) -> bool:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else ""
    return ext in ASSET_EXTS


def _norm_host(h: str) -> str:
    h = h.lower().strip().rstrip(".")
    return h[4:] if h.startswith("www.") else h


def external_referer_host(referer: str, site_host: str) -> str | None:
    """Host of the referer when it is set and is not the site itself (www. ignored); IP literals are hidden."""
    if not referer or referer == "-":
        return None
    ref = referer if "://" in referer else "http://" + referer
    try:
        host = urlsplit(ref).hostname
    except ValueError:
        return None
    if not host:
        return None
    if _norm_host(host) == _norm_host(site_host):
        return None
    return _scrub_ip(host.lower())


def target_matches(path: str, target: str) -> bool:
    """'/feed.xml' exact · '/api/' prefix (also matches '/api') · 'index.json' substring anywhere in the path."""
    if target.startswith("/"):
        if target.endswith("/") and len(target) > 1:
            return path.startswith(target) or path == target[:-1]
        return path == target
    return target in path


# ----------------------------------------------------------------------------------------------------------------
# Aggregation
# ----------------------------------------------------------------------------------------------------------------

Source = tuple[str, str]  # (ip, ua) — in-memory only


@dataclass
class _Day:
    requests: int = 0
    status: Counter = field(default_factory=Counter)
    class_requests: Counter = field(default_factory=Counter)
    class_sources: dict[str, set[Source]] = field(default_factory=lambda: {c: set() for c in CLASSES})
    engaged: set[Source] = field(default_factory=set)
    returns: int = 0
    crawler_requests: Counter = field(default_factory=Counter)
    social_preview: int = 0
    agent_requests: Counter = field(default_factory=Counter)
    target_requests: dict[str, Counter] = field(default_factory=dict)            # target -> Counter(class)
    target_sources: dict[str, dict[str, set[Source]]] = field(default_factory=dict)  # target -> class -> sources
    human_pages: Counter = field(default_factory=Counter)
    referers: Counter = field(default_factory=Counter)


class Aggregator:
    """Collects one host's requests by day. Feed Records with ``add``, then ``result``."""

    def __init__(self, host: str, targets: Iterable[str] = (), own_ua_prefixes: Iterable[str] | None = None,
                 since: str | None = None, until: str | None = None) -> None:
        self.host = host.lower()
        self.targets = [t for t in dict.fromkeys(targets) if t]
        self.own_ua_prefixes = tuple(own_ua_prefixes) if own_ua_prefixes else DEFAULT_OWN_UA_PREFIXES
        self.since = _to_day(since)
        self.until = _to_day(until)
        self.days: dict[str, _Day] = {}
        self.history: dict[str, set[Source]] = {}   # human sources of every day (also before ``since``)
        self.added = 0
        self.all_days: set[str] = set()

    def add(self, rec: Record) -> None:
        if self.until and rec.day > self.until:
            return
        cls, name = classify(rec.ua, rec.ip, rec.path, self.own_ua_prefixes)
        src: Source = (rec.ip, rec.ua)
        self.all_days.add(rec.day)
        if cls == "human":
            self.history.setdefault(rec.day, set()).add(src)
        if self.since and rec.day < self.since:
            return
        self.added += 1
        d = self.days.get(rec.day)
        if d is None:
            d = self.days[rec.day] = _Day()
            for t in self.targets:
                d.target_requests[t] = Counter()
                d.target_sources[t] = {c: set() for c in CLASSES}
        d.requests += 1
        d.status[f"{rec.status // 100}xx"] += 1
        d.class_requests[cls] += 1
        d.class_sources[cls].add(src)
        ext_ref = external_referer_host(rec.referer, self.host)
        if cls == "crawler":
            d.crawler_requests[name] += 1
            if name in SOCIAL_PREVIEW:
                d.social_preview += 1
        elif cls == "agent":
            d.agent_requests[name] += 1
        elif cls == "human":
            d.human_pages[rec.path] += 1
            if ext_ref or (rec.status < 400 and is_asset(rec.path)):
                d.engaged.add(src)
        if ext_ref and cls != "own":
            d.referers[ext_ref] += 1
        for t in self.targets:
            if target_matches(rec.path, t):
                d.target_requests[t][cls] += 1
                d.target_sources[t][cls].add(src)

    # -- output --------------------------------------------------------------------------------------------------

    def _finalize_returns(self) -> None:
        seen: set[Source] = set()
        for day in sorted(set(self.history) | set(self.days)):
            d = self.days.get(day)
            if d is not None:
                d.returns = len(d.engaged & seen)
            seen |= self.history.get(day, set())

    def _day_dict(self, d: _Day) -> dict[str, Any]:
        return {
            "requests_raw": d.requests,
            "status": {k: d.status.get(k, 0) for k in ("2xx", "3xx", "4xx", "5xx")} | _extra_status(d.status),
            "classes": {c: {"requests": d.class_requests.get(c, 0), "sources": len(d.class_sources[c])} for c in CLASSES},
            "humans": {"requests": d.class_requests.get("human", 0), "sources": len(d.class_sources["human"]),
                       "engaged": len(d.engaged), "returns": d.returns},
            "agents": {"requests": d.class_requests.get("agent", 0), "sources": len(d.class_sources["agent"]),
                       "by_family": _sorted_counter(d.agent_requests)},
            "crawlers": {"requests": d.class_requests.get("crawler", 0), "sources": len(d.class_sources["crawler"]),
                         "social_preview": d.social_preview, "by_bot": _sorted_counter(d.crawler_requests)},
            "scanners": {"requests": d.class_requests.get("scanner", 0), "sources": len(d.class_sources["scanner"])},
            "targets": {t: _target_dict(d.target_requests[t], d.target_sources[t]) for t in self.targets},
            "top_pages": [{"path": p, "requests": n} for p, n in d.human_pages.most_common(TOP_PAGES_N)],
            "referers": [{"host": h, "requests": n} for h, n in d.referers.most_common(TOP_REFERERS_N)],
        }

    def _totals(self, days: list[str]) -> dict[str, Any]:
        t = _Day()
        for day in days:
            d = self.days[day]
            t.requests += d.requests
            t.status.update(d.status)
            t.class_requests.update(d.class_requests)
            for c in CLASSES:
                t.class_sources[c] |= d.class_sources[c]
            t.engaged |= d.engaged
            t.returns += d.returns
            t.crawler_requests.update(d.crawler_requests)
            t.social_preview += d.social_preview
            t.agent_requests.update(d.agent_requests)
            for tg in self.targets:
                t.target_requests.setdefault(tg, Counter()).update(d.target_requests[tg])
                ts = t.target_sources.setdefault(tg, {c: set() for c in CLASSES})
                for c in CLASSES:
                    ts[c] |= d.target_sources[tg][c]
            t.human_pages.update(d.human_pages)
            t.referers.update(d.referers)
        for tg in self.targets:  # a window without days still lists every target
            t.target_requests.setdefault(tg, Counter())
            t.target_sources.setdefault(tg, {c: set() for c in CLASSES})
        out = self._day_dict(t)
        out["days"] = len(days)
        return out

    def result(self, stats: ParseStats | None = None) -> dict[str, Any]:
        self._finalize_returns()
        days = sorted(self.days)
        since = self.since or (days[0] if days else None)
        until = self.until or (days[-1] if days else None)
        return {
            "host": self.host,
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "window": {"since": since, "until": until},
            "targets": list(self.targets),
            "parse": (stats.as_dict() if stats else None),
            "days": {day: self._day_dict(self.days[day]) for day in days},
            "totals": self._totals(days),
        }


def _to_day(v: str | date | None) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    if not DAY_RE.match(s):
        raise ValueError(f"expected YYYY-MM-DD, got {v!r}")
    return s


def _extra_status(c: Counter) -> dict[str, int]:
    return {k: v for k, v in sorted(c.items()) if k not in ("2xx", "3xx", "4xx", "5xx")}


def _sorted_counter(c: Counter) -> dict[str, int]:
    return {k: v for k, v in c.most_common()}


def _target_dict(req: Counter, src: dict[str, set[Source]]) -> dict[str, Any]:
    out: dict[str, Any] = {"requests": sum(req.values())}
    for c in CLASSES:
        out[c] = {"requests": req.get(c, 0), "sources": len(src[c])}
    return out


# ----------------------------------------------------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------------------------------------------------

def run(log_paths: Iterable[str | os.PathLike[str]], host: str, since: str | date | None = None,
        until: str | date | None = None, targets: Iterable[str] = (),
        own_ua_prefixes: Iterable[str] | None = None) -> dict[str, Any]:
    """Aggregate the logs for ``host`` and return the metrics dict (see module docstring for the shape)."""
    host_l = host.lower()
    log.info("sitemetrics run: host=%s since=%s until=%s targets=%s", host_l, since, until, list(targets))
    agg = Aggregator(host_l, targets, own_ua_prefixes, since, until)
    stats = ParseStats()
    for rec in iter_records(log_paths, stats):
        if rec.host == host_l:
            agg.add(rec)
    log.info("host=%s: parsed=%d skipped=%d matched_in_window=%d days=%d", host_l, stats.parsed, stats.skipped,
             agg.added, len(agg.days))
    return agg.result(stats)


def run_all_hosts(log_paths: Iterable[str | os.PathLike[str]], since: str | date | None = None,
                  until: str | date | None = None, targets: Iterable[str] = (),
                  own_ua_prefixes: Iterable[str] | None = None, min_requests: int = 1) -> dict[str, dict[str, Any]]:
    """One pass over the logs; a metrics dict per host seen. Hosts that are not safe directory names are dropped."""
    aggs: dict[str, Aggregator] = {}
    stats = ParseStats()
    dropped: Counter = Counter()
    for rec in iter_records(log_paths, stats):
        if not HOST_SAFE_RE.match(rec.host) or IPV4_RE.fullmatch(rec.host) or rec.host in ("localhost", "_"):
            dropped[rec.host[:60]] += 1
            continue
        agg = aggs.get(rec.host)
        if agg is None:
            agg = aggs[rec.host] = Aggregator(rec.host, targets, own_ua_prefixes, since, until)
        agg.add(rec)
    if dropped:
        log.info("all-hosts: %d request(s) under %d unsafe/unnamed host(s) dropped", sum(dropped.values()), len(dropped))
    out: dict[str, dict[str, Any]] = {}
    for host, agg in sorted(aggs.items()):
        if agg.added < min_requests:
            continue
        out[host] = agg.result(stats)
    log.info("all-hosts: parsed=%d skipped=%d hosts=%d", stats.parsed, stats.skipped, len(out))
    return out


def to_markdown(result: dict[str, Any]) -> str:
    """Short per-day table: humans sources/engaged/returns, agents, crawlers, targets; totals and top lists."""
    targets: list[str] = result.get("targets", [])
    w = result["window"]
    lines = [
        f"# {result['host']} — site metrics",
        "",
        f"Generated {result['generated']} · window {w['since']} → {w['until']} · days {result['totals']['days']} · "
        "sources = unique (address, user-agent) pairs, never listed.",
        "",
    ]
    head = ["day", "req", "humans src/eng/ret", "agents src (req)", "crawlers req (social)", "scan", "unk"]
    head += [f"`{t}` h/a src" for t in targets]
    lines.append("| " + " | ".join(head) + " |")
    lines.append("|" + "|".join(["---"] * len(head)) + "|")

    def row(label: str, d: dict[str, Any]) -> str:
        h, a, c = d["humans"], d["agents"], d["crawlers"]
        cells = [label, str(d["requests_raw"]), f"{h['sources']}/{h['engaged']}/{h['returns']}",
                 f"{a['sources']} ({a['requests']})", f"{c['requests']} ({c['social_preview']})",
                 str(d["scanners"]["requests"]), str(d["classes"]["unknown"]["requests"])]
        for t in targets:
            tg = d["targets"].get(t, {})
            cells.append(f"{tg.get('human', {}).get('sources', 0)}/{tg.get('agent', {}).get('sources', 0)}")
        return "| " + " | ".join(cells) + " |"

    for day, d in result["days"].items():
        lines.append(row(day, d))
    lines.append(row("**total**", result["totals"]))
    tot = result["totals"]
    lines += ["", "## Totals", ""]
    st = tot["status"]
    lines.append(f"- status: 2xx {st.get('2xx', 0)} · 3xx {st.get('3xx', 0)} · 4xx {st.get('4xx', 0)} · 5xx {st.get('5xx', 0)}")
    lines.append("- classes (requests / sources): " + " · ".join(
        f"{c} {v['requests']}/{v['sources']}" for c, v in tot["classes"].items()))
    if tot["crawlers"]["by_bot"]:
        lines.append("- crawlers: " + " · ".join(f"{k} {v}" for k, v in list(tot["crawlers"]["by_bot"].items())[:12]))
    if tot["agents"]["by_family"]:
        lines.append("- agents: " + " · ".join(f"{k} {v}" for k, v in list(tot["agents"]["by_family"].items())[:12]))
    for t in targets:
        tg = tot["targets"][t]
        lines.append(f"- target `{t}`: {tg['requests']} req · human {tg['human']['requests']}/{tg['human']['sources']} · "
                     f"agent {tg['agent']['requests']}/{tg['agent']['sources']} · crawler {tg['crawler']['requests']}")
    if tot["top_pages"]:
        lines += ["", "## Top pages (human requests)", ""]
        lines += [f"- `{p['path']}` — {p['requests']}" for p in tot["top_pages"]]
    if tot["referers"]:
        lines += ["", "## External referers", ""]
        lines += [f"- {r['host']} — {r['requests']}" for r in tot["referers"]]
    return "\n".join(lines) + "\n"


def write_outputs(result: dict[str, Any], out_dir: str | os.PathLike[str]) -> tuple[Path, Path]:
    """Write <out_dir>/metrics.json and <out_dir>/metrics.md atomically (temp file + rename)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path, md_path = out / JSON_NAME, out / MD_NAME
    _atomic_write(json_path, json.dumps(result, ensure_ascii=False, indent=1) + "\n")
    _atomic_write(md_path, to_markdown(result))
    log.info("written %s and %s", json_path, md_path)
    return json_path, md_path


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ----------------------------------------------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python3 -m team.sitemetrics",
                                description="Per-day site metrics from the nginx access log, without addresses.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--host", help="site host to report on, e.g. x.formachines.ai")
    g.add_argument("--all-hosts", action="store_true", help="one report per host seen in the log: <out>/<host>/")
    p.add_argument("--log", action="append", required=True, metavar="PATH",
                   help="access log (plain or .gz); repeat for rotated files")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="first day to report (earlier days feed 'returns' only)")
    p.add_argument("--until", metavar="YYYY-MM-DD", help="last day to report (inclusive)")
    p.add_argument("--target", action="append", default=[], metavar="PATH",
                   help="target path: '/feed.xml' exact, '/api/' prefix, 'index.json' substring; repeatable")
    p.add_argument("--own-ua", action="append", default=[], metavar="PREFIX",
                   help=f"UA prefix counted as 'own' (default: {', '.join(DEFAULT_OWN_UA_PREFIXES)}); repeatable")
    p.add_argument("--out", default="-", metavar="DIR",
                   help="output directory for metrics.json + metrics.md; '-' prints JSON to stdout (default)")
    p.add_argument("--min-requests", type=int, default=1,
                   help="--all-hosts: skip hosts with fewer matching requests (default 1)")
    p.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        if args.all_hosts:
            results = run_all_hosts(args.log, args.since, args.until, args.target, args.own_ua, args.min_requests)
            if args.out == "-":
                json.dump(results, sys.stdout, ensure_ascii=False, indent=1)
                sys.stdout.write("\n")
                return 0
            for host, res in results.items():
                write_outputs(res, Path(args.out) / host)
            log.info("all-hosts: %d host report(s) under %s", len(results), args.out)
            return 0
        result = run(args.log, args.host, args.since, args.until, args.target, args.own_ua)
        if args.out == "-":
            json.dump(result, sys.stdout, ensure_ascii=False, indent=1)
            sys.stdout.write("\n")
        else:
            write_outputs(result, args.out)
        return 0
    except ValueError as e:
        log.error("%s", e)
        return 2


if __name__ == "__main__":
    sys.exit(main())
