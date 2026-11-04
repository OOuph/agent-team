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

Channel attribution: a link in a post carries ``?src=<channel>`` (``SRC_PARAM``; value must match ``SRC_RE``,
anything else is ignored). The query string is still stripped from ``path`` before classification, but ``src`` is
kept on the record and aggregated into ``by_src`` per day and in ``totals``: ``requests`` and ``sources`` over all
classes, ``human_sources`` (class human) and ``engaged_sources`` (human sources with that src that are engaged by
the rule above — the asset/referer hit itself need not carry the src). Requests without a src are not counted.

CLI::

    python3 -m team.sitemetrics --host x.formachines.ai --log /var/log/nginx/access.log [--log ...]
        [--since 2026-11-03] [--until 2026-11-10] [--target /feed.xml --target /api/] [--own-ua agent-team/]
        --out /var/www/x/_team/          # writes <out>/metrics.json and <out>/metrics.md ; --out - prints JSON
    python3 -m team.sitemetrics --all-hosts --log ... --out /var/www/_metrics/   # <out>/<host>/metrics.json per host
        [--hosts a.example,b.example]    # only these hosts (others ignored, no directories created)
        [--min-requests 1]
"""
from __future__ import annotations

import argparse
import bisect
import ipaddress
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
from urllib.parse import parse_qsl, urlsplit

log = logging.getLogger("team.sitemetrics")

# ----------------------------------------------------------------------------------------------------------------
# Parameters (everything tunable lives here)
# ----------------------------------------------------------------------------------------------------------------

CLASSES: tuple[str, ...] = ("own", "crawler", "scanner", "agent", "human", "unknown")

DEFAULT_OWN_UA_PREFIXES: tuple[str, ...] = ("agent-team/",)
LOOPBACK_IPS: frozenset[str] = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})
OWN_IPS: set[str] = set()      # addresses counted as 'own' whatever the UA (`--own-ip`): the team's servers doing manual checks

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

# probes that appear at any depth, not only at the root (`/app/.env`, `/api/.env`, `/old/wp-login.php`, `/x/.git/config`)
SCANNER_ANYWHERE: tuple[str, ...] = (".env", "/.git/", "/.git", "wp-login.php", "/wp-admin", "/phpmyadmin", "/.aws/", "/.ssh/",
                                     "xmlrpc.php", "/phpinfo", "/.svn/", "/.hg/", "/etc/passwd", "/.DS_Store")


def _scanner_anywhere(plow: str) -> bool:
    return any(plow.endswith(m) or (m.endswith("/") and m in plow) or (m + "/" in plow) for m in SCANNER_ANYWHERE)


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

# --- "not a person" signals for browser-looking traffic (owner 04.11: cloud networks and bots with a browser UA
#     must not count as people). A browser UA is classified human only if none of these fire.
# Published address lists (refreshed by `--update-net-ranges`, cached as JSON; `--net-ranges PATH` loads it).
NET_RANGE_SOURCES: tuple[tuple[str, str, str, str], ...] = (
    # (owner, kind, url, format)
    ("googlebot", "crawler", "https://developers.google.com/static/search/apis/ipranges/googlebot.json", "google"),
    ("google-special", "crawler", "https://developers.google.com/static/search/apis/ipranges/special-crawlers.json", "google"),
    ("google-fetchers", "crawler", "https://developers.google.com/static/search/apis/ipranges/user-triggered-fetchers.json", "google"),
    ("google-fetchers", "crawler", "https://developers.google.com/static/search/apis/ipranges/user-triggered-fetchers-google.json", "google"),
    ("bingbot", "crawler", "https://www.bing.com/toolbox/bingbot.json", "google"),
    ("aws", "cloud", "https://ip-ranges.amazonaws.com/ip-ranges.json", "aws"),
    ("gcp", "cloud", "https://www.gstatic.com/ipranges/cloud.json", "google"),
    ("oracle", "cloud", "https://docs.oracle.com/en-us/iaas/tools/public_ip_ranges.json", "oracle"),
    ("digitalocean", "cloud", "https://digitalocean.com/geo/google.csv", "csv"),
    ("linode", "cloud", "https://geoip.linode.com/", "csv"),
    ("vultr", "cloud", "https://geofeed.constant.com/?text", "csv"),
)
# Always-on fallback for big hosting networks without a public list (and in case a download fails). Coarse on purpose.
BUILTIN_NET_RANGES: tuple[tuple[str, str, str], ...] = (
    # Tencent Cloud
    ("43.128.0.0/10", "cloud", "tencent"), ("49.51.0.0/16", "cloud", "tencent"), ("101.32.0.0/16", "cloud", "tencent"),
    ("119.28.0.0/15", "cloud", "tencent"), ("129.226.0.0/16", "cloud", "tencent"), ("150.109.0.0/16", "cloud", "tencent"),
    ("162.62.0.0/16", "cloud", "tencent"), ("170.106.0.0/16", "cloud", "tencent"),
    # Alibaba Cloud
    ("8.208.0.0/12", "cloud", "alibaba"), ("47.74.0.0/15", "cloud", "alibaba"), ("47.76.0.0/14", "cloud", "alibaba"),
    ("47.80.0.0/13", "cloud", "alibaba"), ("47.88.0.0/14", "cloud", "alibaba"), ("47.235.0.0/16", "cloud", "alibaba"),
    ("47.236.0.0/14", "cloud", "alibaba"), ("47.240.0.0/14", "cloud", "alibaba"), ("47.244.0.0/15", "cloud", "alibaba"),
    ("47.246.0.0/16", "cloud", "alibaba"), ("47.250.0.0/15", "cloud", "alibaba"), ("47.252.0.0/15", "cloud", "alibaba"),
    ("47.254.0.0/16", "cloud", "alibaba"),
    # Microsoft Azure (main public blocks; the service-tag file has no stable URL)
    ("13.64.0.0/11", "cloud", "azure"), ("13.104.0.0/14", "cloud", "azure"), ("20.33.0.0/16", "cloud", "azure"),
    ("20.34.0.0/15", "cloud", "azure"), ("20.36.0.0/14", "cloud", "azure"), ("20.40.0.0/13", "cloud", "azure"),
    ("20.48.0.0/12", "cloud", "azure"), ("20.64.0.0/10", "cloud", "azure"), ("20.128.0.0/16", "cloud", "azure"),
    ("20.135.0.0/16", "cloud", "azure"), ("20.136.0.0/13", "cloud", "azure"), ("20.150.0.0/15", "cloud", "azure"),
    ("20.160.0.0/12", "cloud", "azure"), ("20.176.0.0/12", "cloud", "azure"), ("20.192.0.0/10", "cloud", "azure"),
    ("40.64.0.0/10", "cloud", "azure"), ("51.4.0.0/15", "cloud", "azure"), ("51.8.0.0/16", "cloud", "azure"),
    ("51.10.0.0/15", "cloud", "azure"), ("51.12.0.0/15", "cloud", "azure"), ("51.18.0.0/16", "cloud", "azure"),
    ("51.51.0.0/16", "cloud", "azure"), ("51.53.0.0/16", "cloud", "azure"), ("51.103.0.0/16", "cloud", "azure"),
    ("51.104.0.0/15", "cloud", "azure"), ("51.107.0.0/16", "cloud", "azure"), ("51.116.0.0/16", "cloud", "azure"),
    ("51.120.0.0/16", "cloud", "azure"), ("51.124.0.0/16", "cloud", "azure"), ("51.132.0.0/16", "cloud", "azure"),
    ("51.136.0.0/15", "cloud", "azure"), ("51.138.0.0/16", "cloud", "azure"), ("51.140.0.0/14", "cloud", "azure"),
    ("52.96.0.0/12", "cloud", "azure"), ("52.136.0.0/13", "cloud", "azure"), ("52.224.0.0/11", "cloud", "azure"),
    ("104.40.0.0/13", "cloud", "azure"), ("137.116.0.0/15", "cloud", "azure"), ("168.61.0.0/16", "cloud", "azure"),
    ("168.62.0.0/15", "cloud", "azure"), ("191.232.0.0/13", "cloud", "azure"),
    # Hetzner
    ("5.9.0.0/16", "cloud", "hetzner"), ("49.12.0.0/15", "cloud", "hetzner"), ("65.21.0.0/16", "cloud", "hetzner"),
    ("65.108.0.0/15", "cloud", "hetzner"), ("78.46.0.0/15", "cloud", "hetzner"), ("88.198.0.0/16", "cloud", "hetzner"),
    ("88.99.0.0/16", "cloud", "hetzner"), ("94.130.0.0/16", "cloud", "hetzner"), ("95.216.0.0/15", "cloud", "hetzner"),
    ("116.202.0.0/15", "cloud", "hetzner"), ("135.181.0.0/16", "cloud", "hetzner"), ("136.243.0.0/16", "cloud", "hetzner"),
    ("138.201.0.0/16", "cloud", "hetzner"), ("142.132.128.0/17", "cloud", "hetzner"), ("144.76.0.0/16", "cloud", "hetzner"),
    ("148.251.0.0/16", "cloud", "hetzner"), ("157.90.0.0/16", "cloud", "hetzner"), ("159.69.0.0/16", "cloud", "hetzner"),
    ("162.55.0.0/16", "cloud", "hetzner"), ("167.233.0.0/16", "cloud", "hetzner"), ("168.119.0.0/16", "cloud", "hetzner"),
    ("176.9.0.0/16", "cloud", "hetzner"), ("178.63.0.0/16", "cloud", "hetzner"), ("188.40.0.0/16", "cloud", "hetzner"),
    ("195.201.0.0/16", "cloud", "hetzner"),
    # OVH
    ("51.38.0.0/16", "cloud", "ovh"), ("51.68.0.0/16", "cloud", "ovh"), ("51.75.0.0/16", "cloud", "ovh"),
    ("51.77.0.0/16", "cloud", "ovh"), ("51.79.0.0/16", "cloud", "ovh"), ("51.81.0.0/16", "cloud", "ovh"),
    ("51.83.0.0/16", "cloud", "ovh"), ("51.89.0.0/16", "cloud", "ovh"), ("51.91.0.0/16", "cloud", "ovh"),
    ("51.161.0.0/16", "cloud", "ovh"), ("51.178.0.0/16", "cloud", "ovh"), ("51.195.0.0/16", "cloud", "ovh"),
    ("51.210.0.0/16", "cloud", "ovh"), ("51.222.0.0/16", "cloud", "ovh"), ("54.36.0.0/14", "cloud", "ovh"),
    ("135.125.0.0/16", "cloud", "ovh"), ("137.74.0.0/16", "cloud", "ovh"), ("141.94.0.0/15", "cloud", "ovh"),
    ("145.239.0.0/16", "cloud", "ovh"), ("146.59.0.0/16", "cloud", "ovh"), ("147.135.0.0/16", "cloud", "ovh"),
    ("149.202.0.0/16", "cloud", "ovh"), ("151.80.0.0/16", "cloud", "ovh"), ("158.69.0.0/16", "cloud", "ovh"),
    ("164.132.0.0/16", "cloud", "ovh"), ("176.31.0.0/16", "cloud", "ovh"), ("178.32.0.0/15", "cloud", "ovh"),
    ("188.165.0.0/16", "cloud", "ovh"), ("192.99.0.0/16", "cloud", "ovh"), ("193.70.0.0/17", "cloud", "ovh"),
    ("198.27.64.0/18", "cloud", "ovh"), ("198.50.128.0/17", "cloud", "ovh"), ("213.32.0.0/17", "cloud", "ovh"),
    ("217.182.0.0/16", "cloud", "ovh"),
    # smaller hosting networks seen with browser UAs on our sites (whois-checked 04.11)
    ("133.242.0.0/16", "cloud", "sakura"), ("107.172.0.0/14", "cloud", "colocrossing"), ("192.3.0.0/16", "cloud", "colocrossing"),
    ("23.94.0.0/15", "cloud", "colocrossing"), ("198.23.128.0/17", "cloud", "colocrossing"), ("109.233.108.0/22", "cloud", "pskz"),
    # Meta crawler networks (link previews, meta-externalagent)
    ("57.141.0.0/16", "crawler", "meta"), ("31.13.24.0/21", "crawler", "meta"), ("66.220.144.0/20", "crawler", "meta"), ("69.63.176.0/20", "crawler", "meta"),
    ("69.171.224.0/19", "crawler", "meta"), ("173.252.64.0/18", "crawler", "meta"),
)
# A browser UA this old is almost never a person in 2026 (bots keep stale strings): Chrome/Firefox major below the
# threshold, Presto Opera, Internet Explorer, Android 6 and older, iOS 13 and older.
STALE_CHROME_MAJOR = 100
STALE_FIREFOX_MAJOR = 100
STALE_UA_RE = re.compile(r"presto/|msie |trident/|android [1-6]\.|iphone os (?:[1-9]|1[0-3])_|cpu os (?:[1-9]|1[0-3])_", re.I)
CHROME_RE = re.compile(r"(?:chrome|crios)/(\d+)", re.I)
FIREFOX_RE = re.compile(r"(?:firefox|fxios)/(\d+)", re.I)
# One address that presents this many different operating systems on one day is rotating its UA — a tool, not a person.
ROTATING_OS_MIN = 3
OS_FAMILIES: tuple[tuple[str, str], ...] = (("iphone", "ios"), ("ipad", "ios"), ("android", "android"), ("windows", "windows"),
                                           ("macintosh", "mac"), ("cros", "chromeos"), ("linux", "linux"))
NET_RANGES_MAX_AGE_DAYS = 7
# A proxy pool: the same exact browser UA from this many different addresses of one /16 on one day, none of which loaded
# a single asset (CSS, image) — a fetch fleet, not visitors (04.11: 87.81/16 and 104.29/16 fetching only /day/<date>/).
POOL_MIN_IPS = 5

ASSET_EXTS: frozenset[str] = frozenset(
    {"css", "js", "mjs", "woff", "woff2", "ttf", "otf", "png", "jpg", "jpeg", "gif", "webp", "svg", "ico"}
)

TOP_PAGES_N = 20
TOP_REFERERS_N = 10
MAX_PATH_LEN = 200

# Channel attribution: query parameter name and the accepted value shape (anything else is dropped).
SRC_PARAM = "src"
SRC_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

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
    src: str = ""       # ?src=<channel> from the query string when it matches SRC_RE, else ""


def _scrub_ip(s: str) -> str:
    """Replace IPv4 literals inside free text (paths, referer hosts) so they never reach the output."""
    return IPV4_RE.sub("x.x.x.x", s)


def _extract_src(query: str) -> str:
    """Value of the ``src`` parameter when it matches SRC_RE; '' otherwise (first occurrence wins)."""
    if not query:
        return ""
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key == SRC_PARAM:
            return value if SRC_RE.match(value) else ""
    return ""


def _split_request(request: str) -> tuple[str, str, str]:
    """'GET /a/b?src=x&y=1 HTTP/1.1' -> ('GET', '/a/b', 'x'). Garbage (TLS bytes, '-') -> ('', <raw>, '')."""
    parts = request.split(" ")
    if len(parts) >= 2 and parts[0].isalpha() and parts[0].isupper():
        method, target = parts[0], parts[1]
    else:
        method, target = "", request
    if target.startswith(("http://", "https://")):
        parsed = urlsplit(target)
        target = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
    target = target.split("#", 1)[0]
    path, _, query = target.partition("?")
    return method, _scrub_ip(path[:MAX_PATH_LEN]), _extract_src(query)


def parse_line(line: str) -> Record | None:
    """Parse one ``log_format full`` line; None when it does not match or the timestamp is unusable."""
    m = LINE_RE.match(line)
    if not m:
        return None
    day = m.group("time")[:10]
    if not DAY_RE.match(day):
        return None
    method, path, src = _split_request(m.group("request"))
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
        src=src,
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

class NetIndex:
    """Sorted integer intervals per IP version; lookup by bisect. Entries: (start, end, kind, owner)."""

    def __init__(self) -> None:
        self._v: dict[int, list[tuple[int, int, str, str]]] = {4: [], 6: []}
        self._starts: dict[int, list[int]] = {4: [], 6: []}
        self.count = 0

    def add(self, cidr: str, kind: str, owner: str) -> None:
        try:
            net = ipaddress.ip_network(cidr.strip(), strict=False)
        except ValueError:
            return
        self._v[net.version].append((int(net.network_address), int(net.broadcast_address), kind, owner))
        self.count += 1

    def freeze(self) -> "NetIndex":
        for v in (4, 6):
            # crawler ranges win over cloud ranges when they overlap (Googlebot lives inside Google's space)
            self._v[v].sort(key=lambda r: (r[0], 0 if r[2] == "crawler" else 1))
            self._starts[v] = [r[0] for r in self._v[v]]
        return self

    def lookup(self, ip: str) -> tuple[str, str] | None:
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            return None
        if a.version == 6 and a.ipv4_mapped:
            a = a.ipv4_mapped
        n, rows, starts = int(a), self._v[a.version], self._starts[a.version]
        i = bisect.bisect_right(starts, n)
        best = None
        # walk back over intervals that start at or before n; nested ranges are short lists, stop after a few misses
        j, misses = i - 1, 0
        while j >= 0 and misses < 64:
            lo, hi, kind, owner = rows[j]
            if lo <= n <= hi:
                if kind == "crawler":
                    return kind, owner
                best = best or (kind, owner)
            else:
                misses += 1
            j -= 1
        return best


NETS: NetIndex = NetIndex()
ROTATING: set[tuple[str, str]] = set()      # (ip, day) that rotate their browser UA across operating systems


def load_net_ranges(path: str | os.PathLike[str] | None) -> NetIndex:
    """Built-in ranges plus the cached published lists at ``path`` (if readable). Replaces the global index."""
    global NETS
    idx = NetIndex()
    for cidr, kind, owner in BUILTIN_NET_RANGES:
        idx.add(cidr, kind, owner)
    if path:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            for r in data.get("ranges") or []:
                idx.add(str(r[0]), str(r[1]), str(r[2]))
        except (OSError, json.JSONDecodeError, IndexError, TypeError) as e:
            log.warning("net ranges %s not loaded (%s); built-in list only", path, e)
    NETS = idx.freeze()
    log.info("net ranges: %d entries", NETS.count)
    return NETS


def _parse_ranges(fmt: str, body: bytes) -> list[str]:
    text = body.decode("utf-8", "replace")
    if fmt == "csv":
        return [ln.split(",")[0].strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#") and "/" in ln.split(",")[0]]
    data = json.loads(text)
    if fmt == "aws":
        return [x["ip_prefix"] for x in data.get("prefixes", [])] + [x["ipv6_prefix"] for x in data.get("ipv6_prefixes", [])]
    if fmt == "oracle":
        return [c["cidr"] for r in data.get("regions", []) for c in r.get("cidrs", [])]
    return [x.get("ipv4Prefix") or x.get("ipv6Prefix") for x in data.get("prefixes", []) if x.get("ipv4Prefix") or x.get("ipv6Prefix")]


def update_net_ranges(path: str | os.PathLike[str], fetch: Any = None) -> dict[str, int]:
    """Download the published lists into ``path`` (JSON). A failing source keeps its previous entries."""
    import urllib.request
    fetch = fetch or (lambda url: urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "agent-team/sitemetrics"}), timeout=60).read())
    old: dict[str, list[list[str]]] = {}
    p = Path(path)
    if p.exists():
        try:
            for r in json.loads(p.read_text(encoding="utf-8")).get("ranges") or []:
                old.setdefault(r[2], []).append(r)
        except (OSError, json.JSONDecodeError):
            pass
    by_owner: dict[str, list[list[str]]] = {}
    counts: dict[str, int] = {}
    for owner, kind, url, fmt in NET_RANGE_SOURCES:
        try:
            cidrs = [c for c in _parse_ranges(fmt, fetch(url)) if c]
            by_owner.setdefault(owner, []).extend([c, kind, owner] for c in cidrs)
            counts[owner] = counts.get(owner, 0) + len(cidrs)
        except Exception as e:  # noqa: BLE001 — one source failing keeps the cached copy
            log.warning("net ranges: %s failed (%s); keeping %d cached", owner, e, len(old.get(owner, [])))
            if owner not in by_owner and old.get(owner):
                by_owner[owner] = old[owner]
                counts[owner] = len(old[owner])
    ranges = [r for rs in by_owner.values() for r in rs]
    p.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(p, json.dumps({"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "counts": counts,
                                 "ranges": ranges}))
    log.info("net ranges updated: %s", counts)
    return counts


def _os_family(low_ua: str) -> str | None:
    for needle, fam in OS_FAMILIES:
        if needle in low_ua:
            return fam
    return None


def _net16(ip: str) -> str:
    if ":" in ip:
        return ":".join(ip.split(":")[:3])          # /48-ish for IPv6
    return ".".join(ip.split(".")[:2])


def find_rotating(records: Iterable["Record"]) -> set[tuple[str, str]]:
    """(ip, day) pairs that are tools, not people: browser UAs spanning >= ROTATING_OS_MIN operating systems from one
    address, or a proxy pool (same UA from >= POOL_MIN_IPS addresses of one /16 that day, none of them loading an asset)."""
    seen: dict[tuple[str, str], set[str]] = {}
    pool: dict[tuple[str, str, str], set[str]] = {}
    with_assets: set[tuple[str, str]] = set()
    for r in records:
        low = (r.ua or "").lower()
        if not any(mk in low for mk in BROWSER_MARKERS):
            continue
        fam = _os_family(low)
        if fam:
            seen.setdefault((r.ip, r.day), set()).add(fam)
        pool.setdefault((r.day, r.ua, _net16(r.ip)), set()).add(r.ip)
        if is_asset(r.path):
            with_assets.add((r.ip, r.day))
    out = {k for k, v in seen.items() if len(v) >= ROTATING_OS_MIN}
    for (day, _ua, _net), ips in pool.items():
        if len(ips) >= POOL_MIN_IPS and not any((ip, day) in with_assets for ip in ips):
            out.update((ip, day) for ip in ips)
    return out


def stale_browser(low_ua: str) -> bool:
    if STALE_UA_RE.search(low_ua):
        return True
    m = CHROME_RE.search(low_ua)
    if m and int(m.group(1)) < STALE_CHROME_MAJOR:
        return True
    m = FIREFOX_RE.search(low_ua)
    return bool(m and int(m.group(1)) < STALE_FIREFOX_MAJOR)


def not_a_person(ip: str, low_ua: str, day: str = "") -> tuple[str, str] | None:
    """Why a browser-looking request is not a person: (class, reason) or None when it may be a person."""
    hit = NETS.lookup(ip)
    if hit:
        kind, owner = hit
        return ("crawler", owner) if kind == "crawler" else ("unknown", f"datacenter:{owner}")
    if (ip, day) in ROTATING:
        return "unknown", "rotating-ua"
    if stale_browser(low_ua):
        return "unknown", "stale-ua"
    return None


def classify(ua: str, ip: str, path: str, own_ua_prefixes: Iterable[str] = DEFAULT_OWN_UA_PREFIXES, day: str = "") -> tuple[str, str]:
    """Return (class, name). ``name`` is the bot for crawlers, the family for agents, '' otherwise."""
    ua = ua or ""
    low = ua.lower()
    if ip in LOOPBACK_IPS or ip in OWN_IPS or any(ua.startswith(p) for p in own_ua_prefixes):
        return "own", ""
    for name, needle in CRAWLERS:
        if needle in low:
            return "crawler", name
    if any(mk in low for mk in GENERIC_CRAWLER_MARKERS):
        return "crawler", "other"
    plow = path.lower()
    if any(plow.startswith(p.lower()) for p in SCANNER_PATHS) or any(s in low for s in SCANNER_UAS) or _scanner_anywhere(plow):
        return "scanner", ""
    for family, needle in AGENT_FAMILIES:
        if needle in low:
            return "agent", family
    if any(mk in low for mk in BROWSER_MARKERS) and not any(mk in low for mk in NON_HUMAN_MARKERS):
        why = not_a_person(ip, low, day)
        return why if why else ("human", "")
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
    """'/feed.xml' exact · '/api/' prefix (also matches '/api') · 'index.json' substring anywhere in the path ·
    '/go/vote/*/up/' glob (fnmatch; '*' does not cross a '/', '**' does)."""
    if "*" in target or "?" in target or "[" in target:
        rx = "^" + "".join(".*" if tok == "**" else ("[^/]*" if tok == "*" else re.escape(tok))
                           for tok in re.split(r"(\*\*|\*)", target) if tok) + "$"
        return re.match(rx, path) is not None
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
    src_requests: Counter = field(default_factory=Counter)                       # src -> requests (all classes)
    src_sources: dict[str, set[Source]] = field(default_factory=dict)            # src -> sources (all classes)
    src_human: dict[str, set[Source]] = field(default_factory=dict)              # src -> human sources
    src_engaged: dict[str, set[Source]] = field(default_factory=dict)            # src -> engaged human sources


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
        cls, name = classify(rec.ua, rec.ip, rec.path, self.own_ua_prefixes, rec.day)
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
        if rec.src:
            d.src_requests[rec.src] += 1
            d.src_sources.setdefault(rec.src, set()).add(src)
            if cls == "human":
                d.src_human.setdefault(rec.src, set()).add(src)
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

    def _finalize_src(self) -> None:
        """Engaged-by-src = human sources that arrived with the src and are engaged that day (by any request)."""
        for d in self.days.values():
            d.src_engaged = {s: srcs & d.engaged for s, srcs in d.src_human.items()}

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
            "by_src": {s: {"requests": n, "sources": len(d.src_sources.get(s, ())),
                           "human_sources": len(d.src_human.get(s, ())),
                           "engaged_sources": len(d.src_engaged.get(s, ()))}
                       for s, n in d.src_requests.most_common()},
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
            t.src_requests.update(d.src_requests)
            for s, srcs in d.src_sources.items():
                t.src_sources.setdefault(s, set()).update(srcs)
            for s, srcs in d.src_human.items():
                t.src_human.setdefault(s, set()).update(srcs)
            for s, srcs in d.src_engaged.items():
                t.src_engaged.setdefault(s, set()).update(srcs)
        for tg in self.targets:  # a window without days still lists every target
            t.target_requests.setdefault(tg, Counter())
            t.target_sources.setdefault(tg, {c: set() for c in CLASSES})
        out = self._day_dict(t)
        out["days"] = len(days)
        return out

    def result(self, stats: ParseStats | None = None) -> dict[str, Any]:
        self._finalize_returns()
        self._finalize_src()
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
    log_paths = list(log_paths)
    ROTATING.clear()
    ROTATING.update(find_rotating(iter_records(log_paths)))
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
                  own_ua_prefixes: Iterable[str] | None = None, min_requests: int = 1,
                  only_hosts: set[str] | None = None) -> dict[str, dict[str, Any]]:
    """One pass over the logs; a metrics dict per host seen. Hosts that are not safe directory names are dropped.

    ``only_hosts`` (lower-cased host names) restricts the output to those hosts; every other host is ignored
    (no aggregator, no directory). None means every safe host seen in the logs.
    """
    wanted = {h.lower() for h in only_hosts} if only_hosts is not None else None
    log_paths = list(log_paths)
    ROTATING.clear()
    ROTATING.update(find_rotating(iter_records(log_paths)))      # first pass: addresses rotating their UA across systems
    log.info("rotating-UA sources (ip, day): %d", len(ROTATING))
    aggs: dict[str, Aggregator] = {}
    stats = ParseStats()
    dropped: Counter = Counter()
    ignored = 0
    for rec in iter_records(log_paths, stats):
        if wanted is not None and rec.host not in wanted:
            ignored += 1
            continue
        if not HOST_SAFE_RE.match(rec.host) or IPV4_RE.fullmatch(rec.host) or rec.host in ("localhost", "_"):
            dropped[rec.host[:60]] += 1
            continue
        agg = aggs.get(rec.host)
        if agg is None:
            agg = aggs[rec.host] = Aggregator(rec.host, targets, own_ua_prefixes, since, until)
        agg.add(rec)
    if dropped:
        log.info("all-hosts: %d request(s) under %d unsafe/unnamed host(s) dropped", sum(dropped.values()), len(dropped))
    if wanted is not None:
        log.info("all-hosts: only %s; %d request(s) under other hosts ignored", sorted(wanted), ignored)
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
    if tot.get("by_src"):
        lines += ["", "## Sources by src", "", "| src | human sources | engaged | requests |", "|---|---|---|---|"]
        lines += [f"| `{s}` | {v['human_sources']} | {v['engaged_sources']} | {v['requests']} |"
                  for s, v in tot["by_src"].items()]
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
    g = p.add_mutually_exclusive_group()
    g.add_argument("--host", help="site host to report on, e.g. x.formachines.ai")
    g.add_argument("--all-hosts", action="store_true", help="one report per host seen in the log: <out>/<host>/")
    p.add_argument("--log", action="append", default=[], metavar="PATH",
                   help="access log (plain or .gz); repeat for rotated files")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="first day to report (earlier days feed 'returns' only)")
    p.add_argument("--until", metavar="YYYY-MM-DD", help="last day to report (inclusive)")
    p.add_argument("--target", action="append", default=[], metavar="PATH",
                   help="target path: '/feed.xml' exact, '/api/' prefix, 'index.json' substring; repeatable")
    p.add_argument("--own-ua", action="append", default=[], metavar="PREFIX",
                   help=f"UA prefix counted as 'own' (default: {', '.join(DEFAULT_OWN_UA_PREFIXES)}); repeatable")
    p.add_argument("--net-ranges", metavar="PATH", help="JSON of published cloud/crawler ranges (see --update-net-ranges); "
                   "browser traffic from them is not counted as people. The built-in list is always on.")
    p.add_argument("--update-net-ranges", metavar="PATH", help="download the published ranges into PATH and exit")
    p.add_argument("--own-ip", action="append", default=[], metavar="IP",
                   help="client address counted as 'own' whatever the UA (the team's own servers); repeatable")
    p.add_argument("--out", default="-", metavar="DIR",
                   help="output directory for metrics.json + metrics.md; '-' prints JSON to stdout (default)")
    p.add_argument("--min-requests", type=int, default=1,
                   help="--all-hosts: skip hosts with fewer matching requests (default 1)")
    p.add_argument("--hosts", default=None, metavar="A,B,C",
                   help="--all-hosts: report only these hosts (comma-separated); others are ignored entirely")
    p.add_argument("--log-level", default="INFO", choices=("DEBUG", "INFO", "WARNING", "ERROR"))
    return p


def _parse_hosts(value: str | None) -> set[str] | None:
    """'a.example, B.example' -> {'a.example', 'b.example'}; None/empty -> None (no filter)."""
    if value is None:
        return None
    hosts = {h.strip().lower() for h in value.split(",") if h.strip()}
    if not hosts:
        raise ValueError("--hosts given but no host names in it")
    return hosts


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=getattr(logging, args.log_level),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    OWN_IPS.update(ip.strip() for ip in args.own_ip if ip.strip())
    if args.update_net_ranges:
        print(json.dumps(update_net_ranges(args.update_net_ranges)))
        return 0
    load_net_ranges(args.net_ranges)
    if not (args.host or args.all_hosts) or not args.log:
        log.error("--host or --all-hosts and at least one --log are required (or --update-net-ranges PATH)")
        return 2
    try:
        if args.all_hosts:
            only_hosts = _parse_hosts(args.hosts)
            results = run_all_hosts(args.log, args.since, args.until, args.target, args.own_ua, args.min_requests,
                                    only_hosts)
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
