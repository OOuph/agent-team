"""Publish posts to social channels on behalf of the marketer role (CLI: ``team-publish``).

The marketer role has no network libraries of its own: every outgoing post goes through this module,
which appends the mandatory "written and posted by an AI agent" disclosure (EU AI Act, art. 50).
The role cannot switch the disclosure off: an empty disclosure falls back to ``DEFAULT_DISCLOSURE``.

Channels: telegram (Bot API), bluesky (AT Protocol), devto (Forem API), mastodon, threads (Threads API, two steps:
container then publish), x (API v2 ``POST /2/tweets``, OAuth 1.0a user context signed with HMAC-SHA1 on stdlib).
stdlib only. Network calls go through an injectable ``http(method, url, headers, body, timeout)``
callable returning ``(status, body_bytes)``; tests pass a fake, production uses urllib.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets as _secrets
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("team.publish")

# ---- settings ---------------------------------------------------------------------------------
DEFAULT_DISCLOSURE = "Written and posted by an AI agent without human editing."
TIMEOUT_S = 20
ERROR_BODY_CHARS = 200
USER_AGENT = "agent-team-publish/0.1"
DRY_RUN_ENV = "TEAM_PUBLISH_DRY_RUN"
DRAFT_DIR_ENV = "TEAM_PUBLISH_DRAFT_DIR"      # set by the dispatcher: posts become drafts for the editor, nothing is sent

TELEGRAM_API = "https://api.telegram.org"
TELEGRAM_MAX_CHARS = 4096
BLUESKY_PDS_DEFAULT = "https://bsky.social"
BLUESKY_MAX_GRAPHEMES = 300
BLUESKY_POST_COLLECTION = "app.bsky.feed.post"
DEVTO_API = "https://dev.to/api/articles"
DEVTO_MAX_TAGS = 4
MASTODON_MAX_CHARS = 500
THREADS_API = "https://graph.threads.net/v1.0"
THREADS_MAX_CHARS = 500
THREADS_PUBLISH_RETRIES = 4           # a fresh container may answer "not ready" for a few seconds
THREADS_RETRY_SLEEP_S = 3
THREADS_REFRESH_URL = "https://graph.threads.net/refresh_access_token"
X_API = "https://api.x.com/2/tweets"
X_MAX_WEIGHT = 280                    # X counts in weighted units: most CJK/emoji count 2, every link counts 23
X_URL_WEIGHT = 23
X_LIGHT_RANGES = ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247))   # twitter-text v3: weight 1, the rest 2
ELLIPSIS = "…"

CHANNELS = ("telegram", "bluesky", "devto", "mastodon", "threads", "x", "github")
SECRET_KEYS = {"telegram": ("TELEGRAM_BOT_TOKEN",), "bluesky": ("BLUESKY_HANDLE", "BLUESKY_APP_PASSWORD"),
               "devto": ("DEVTO_API_KEY",), "mastodon": ("MASTODON_INSTANCE", "MASTODON_TOKEN"),
               "threads": ("THREADS_USER_ID", "THREADS_ACCESS_TOKEN"),
               "x": ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_SECRET"), "github": ("GITHUB_TOKEN",)}
BLUESKY_PUBLIC_API = "https://public.api.bsky.app"
GITHUB_API = "https://api.github.com"
GITHUB_COMMENT_MAX = 4000

HttpFn = Callable[[str, str, dict[str, str], bytes | None, int], tuple[int, bytes]]


@dataclass
class PostResult:
    ok: bool
    channel: str
    post_id: str | None
    url: str | None
    error: str | None
    dry_run: bool

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


class PublishError(Exception):
    """Raised internally for a failed step; converted to PostResult(ok=False) in post()."""


# ---- helpers ----------------------------------------------------------------------------------
_URL_RE = re.compile(r"https?://[^\s<>()\[\]\"']+")
_URL_TRAIL = ".,;:!?'\""
_VARIATION_SELECTORS = range(0xFE00, 0xFE10)
_SKIN_TONES = range(0x1F3FB, 0x1F400)
_REGIONAL = range(0x1F1E6, 0x1F200)
_ZWJ = "‍"
_KEYCAP = 0x20E3


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def graphemes(text: str) -> list[str]:
    """Approximate extended grapheme clusters: combining marks, ZWJ sequences, variation selectors,
    skin tones, keycaps and regional-indicator pairs stay attached to their base character."""
    out: list[str] = []
    prev_zwj = False
    ri_run = 0
    for ch in text:
        cp = ord(ch)
        is_ri = cp in _REGIONAL
        ri_run = ri_run + 1 if is_ri else 0
        join = bool(out) and (
            prev_zwj or ch == _ZWJ or unicodedata.combining(ch) != 0 or unicodedata.category(ch) in ("Mn", "Mc", "Me")
            or cp in _VARIATION_SELECTORS or cp in _SKIN_TONES or cp == _KEYCAP or (is_ri and ri_run % 2 == 0)
            or (ch == "\n" and out[-1] == "\r")
        )
        if join:
            out[-1] += ch
        else:
            out.append(ch)
        prev_zwj = ch == _ZWJ
    return out


def truncate_graphemes(text: str, limit: int) -> str:
    """Cut text to ``limit`` graphemes, ending with an ellipsis when something was removed."""
    g = graphemes(text)
    if len(g) <= limit:
        return text
    if limit <= 1:
        return ELLIPSIS if limit == 1 else ""
    return "".join(g[: limit - 1]).rstrip() + ELLIPSIS


def truncate_chars(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 1:
        return ELLIPSIS if limit == 1 else ""
    return text[: limit - 1].rstrip() + ELLIPSIS


def find_urls(text: str) -> list[tuple[int, int, str]]:
    """(start, end, url) character spans of http(s) links; trailing punctuation is not part of the link."""
    out = []
    for m in _URL_RE.finditer(text):
        s, e, u = m.start(), m.end(), m.group(0)
        stripped = u.rstrip(_URL_TRAIL)
        e -= len(u) - len(stripped)
        if stripped:
            out.append((s, e, stripped))
    return out


def bluesky_facets(text: str) -> list[dict[str, Any]]:
    """AT Protocol link facets with UTF-8 byte offsets for every URL in ``text``."""
    facets = []
    for s, e, u in find_urls(text):
        b_start = len(text[:s].encode("utf-8"))
        b_end = b_start + len(u.encode("utf-8"))
        facets.append({"index": {"byteStart": b_start, "byteEnd": b_end},
                       "features": [{"$type": "app.bsky.richtext.facet#link", "uri": u}]})
    return facets


def compose(text: str, disclosure: str, url: str | None, *, limit: int | None, counter: str = "graphemes") -> str:
    """body + optional url line + disclosure, truncating only the body (with an ellipsis) to fit ``limit``."""
    body = text.strip()
    fixed = ""
    if url and url not in body:
        fixed += "\n" + url
    fixed += "\n\n" + disclosure
    if limit is not None:
        budget = limit - (len(graphemes(fixed)) if counter == "graphemes" else len(fixed))
        if budget < 1:
            raise PublishError(f"disclosure and url do not fit the {limit} limit")
        body = truncate_graphemes(body, budget) if counter == "graphemes" else truncate_chars(body, budget)
    return body + fixed


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _redact(s: str, secrets: list[str]) -> str:
    for sec in secrets:
        if sec:
            s = s.replace(sec, "<redacted>")
    return s


def _require(env: dict[str, str], keys: tuple[str, ...]) -> dict[str, str]:
    missing = [k for k in keys if not env.get(k)]
    if missing:
        raise PublishError("missing secret(s): " + ", ".join(missing))
    return {k: env[k] for k in keys}


def urllib_http(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: int) -> tuple[int, bytes]:
    """Default transport. HTTP error statuses are returned, not raised; network failures raise."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return int(r.status), r.read()
    except urllib.error.HTTPError as e:
        return int(e.code), e.read()


def _call_json(http: HttpFn, method: str, url: str, payload: dict[str, Any] | None, headers: dict[str, str],
               secrets: list[str], label: str, *, form: dict[str, str] | None = None) -> dict[str, Any]:
    """POST/GET JSON (or a form body when ``form`` is given), raising PublishError with status and the first 200 chars
    of the body on any failure."""
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json", **headers}
    body = None
    if form is not None:
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        body = urllib.parse.urlencode(form).encode("utf-8")
    elif payload is not None:
        hdrs["Content-Type"] = "application/json"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    log.debug("%s %s %s", label, method, _redact(url, secrets))
    try:
        status, raw = http(method, url, hdrs, body, TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 - any transport failure becomes a result, never an exception
        raise PublishError(f"{label}: network error: {_redact(f'{type(e).__name__}: {e}', secrets)}") from e
    text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
    if not 200 <= status < 300:
        raise PublishError(f"{label}: HTTP {status}: {_redact(text[:ERROR_BODY_CHARS], secrets)}")
    try:
        data = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError as e:
        raise PublishError(f"{label}: HTTP {status}: non-JSON body: {_redact(text[:ERROR_BODY_CHARS], secrets)}") from e
    return data if isinstance(data, dict) else {"data": data}


def _print_dry_run(channel: str, payload: dict[str, Any]) -> None:
    print(json.dumps({"dry_run": True, "channel": channel, "payload": payload}, ensure_ascii=False, indent=2),
          file=sys.stderr)


# ---- channels ---------------------------------------------------------------------------------
def _telegram(text: str, target: str | None, url: str | None, disclosure: str, env: dict[str, str],
              dry_run: bool, http: HttpFn) -> PostResult:
    if not target:
        raise PublishError("telegram: target (chat id or @channel) is required")
    body = compose(html.escape(text), f"<i>{html.escape(disclosure)}</i>", html.escape(url) if url else None,
                   limit=TELEGRAM_MAX_CHARS, counter="chars")
    payload = {"chat_id": target, "text": body, "parse_mode": "HTML", "disable_web_page_preview": False}
    if dry_run:
        _print_dry_run("telegram", payload)
        return PostResult(True, "telegram", None, None, None, True)
    token = _require(env, SECRET_KEYS["telegram"])["TELEGRAM_BOT_TOKEN"]
    data = _call_json(http, "POST", f"{TELEGRAM_API}/bot{token}/sendMessage", payload, {}, [token], "telegram")
    if not data.get("ok"):
        raise PublishError(f"telegram: API error: {_redact(json.dumps(data)[:ERROR_BODY_CHARS], [token])}")
    msg = data.get("result") or {}
    mid = msg.get("message_id")
    username = (msg.get("chat") or {}).get("username") or (target[1:] if target.startswith("@") else None)
    link = f"https://t.me/{username}/{mid}" if username and mid is not None else None
    return PostResult(True, "telegram", str(mid) if mid is not None else None, link, None, False)


def bluesky_reply_ref(http: HttpFn, parent_uri: str) -> dict[str, Any]:
    """{"root": {uri,cid}, "parent": {uri,cid}} for a reply to ``parent_uri`` (public API, no auth)."""
    data = _call_json(http, "GET", f"{BLUESKY_PUBLIC_API}/xrpc/app.bsky.feed.getPosts?uris={urllib.parse.quote(parent_uri, safe='')}",
                      None, {}, [], "bluesky.getPosts")
    posts = data.get("posts") or []
    if not posts:
        raise PublishError(f"bluesky.getPosts: parent not found: {parent_uri}")
    p = posts[0]
    parent = {"uri": p.get("uri"), "cid": p.get("cid")}
    root = ((p.get("record") or {}).get("reply") or {}).get("root") or parent
    return {"root": {"uri": root.get("uri"), "cid": root.get("cid")}, "parent": parent}


def _bluesky(text: str, url: str | None, disclosure: str, env: dict[str, str], dry_run: bool, http: HttpFn,
             reply_to: str | None = None) -> PostResult:
    body = compose(text, disclosure, url, limit=BLUESKY_MAX_GRAPHEMES, counter="graphemes")
    record: dict[str, Any] = {"$type": BLUESKY_POST_COLLECTION, "text": body, "createdAt": utcnow_iso()}
    facets = bluesky_facets(body)
    if facets:
        record["facets"] = facets
    if reply_to:
        record["reply"] = bluesky_reply_ref(http, reply_to) if not dry_run else {"root": {"uri": reply_to, "cid": "dry"}, "parent": {"uri": reply_to, "cid": "dry"}}
    if dry_run:
        _print_dry_run("bluesky", {"collection": BLUESKY_POST_COLLECTION, "record": record})
        return PostResult(True, "bluesky", None, None, None, True)
    sec = _require(env, SECRET_KEYS["bluesky"])
    pds = (env.get("BLUESKY_PDS") or BLUESKY_PDS_DEFAULT).rstrip("/")
    handle, password = sec["BLUESKY_HANDLE"].lstrip("@"), sec["BLUESKY_APP_PASSWORD"]
    session = _call_json(http, "POST", f"{pds}/xrpc/com.atproto.server.createSession",
                         {"identifier": handle, "password": password}, {}, [password], "bluesky.createSession")
    jwt, did = session.get("accessJwt"), session.get("did")
    if not jwt or not did:
        raise PublishError("bluesky.createSession: response without accessJwt/did")
    out = _call_json(http, "POST", f"{pds}/xrpc/com.atproto.repo.createRecord",
                     {"repo": did, "collection": BLUESKY_POST_COLLECTION, "record": record},
                     {"Authorization": f"Bearer {jwt}"}, [password, jwt], "bluesky.createRecord")
    uri = str(out.get("uri") or "")
    rkey = uri.rsplit("/", 1)[-1] if uri else None
    link = f"https://bsky.app/profile/{session.get('handle') or handle}/post/{rkey}" if rkey else None
    return PostResult(True, "bluesky", uri or None, link, None, False)


def _github(text: str, target: str | None, disclosure: str, env: dict[str, str], dry_run: bool, http: HttpFn) -> PostResult:
    """A comment on a GitHub issue; ``target`` is the issue's html url. The disclosure goes on its own last line."""
    m = re.match(r"https://github\.com/([^/]+)/([^/]+)/issues/(\d+)", target or "")
    if not m:
        raise PublishError("github: target must be an issue url https://github.com/<owner>/<repo>/issues/<n>")
    owner, repo, num = m.group(1), m.group(2), m.group(3)
    body = text.strip()[:GITHUB_COMMENT_MAX] + f"\n\n_{disclosure.strip()}_"
    if dry_run:
        _print_dry_run("github", {"issue": f"{owner}/{repo}#{num}", "body": body})
        return PostResult(True, "github", None, None, None, True)
    tok = _require(env, SECRET_KEYS["github"])["GITHUB_TOKEN"]
    data = _call_json(http, "POST", f"{GITHUB_API}/repos/{owner}/{repo}/issues/{num}/comments", {"body": body},
                      {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json"}, [tok], "github.comment")
    cid = data.get("id")
    return PostResult(True, "github", str(cid) if cid is not None else None, data.get("html_url"), None, False)


def _devto(text: str, title: str | None, url: str | None, tags: list[str] | None, disclosure: str,
           env: dict[str, str], dry_run: bool, http: HttpFn) -> PostResult:
    if not title:
        raise PublishError("devto: title is required")
    article: dict[str, Any] = {"title": title, "body_markdown": f"*{disclosure}*\n\n{text.strip()}\n", "published": True,
                               "tags": [t for t in (tags or []) if t][:DEVTO_MAX_TAGS]}
    if url:
        article["canonical_url"] = url
    payload = {"article": article}
    if dry_run:
        _print_dry_run("devto", payload)
        return PostResult(True, "devto", None, None, None, True)
    key = _require(env, SECRET_KEYS["devto"])["DEVTO_API_KEY"]
    data = _call_json(http, "POST", DEVTO_API, payload, {"api-key": key}, [key], "devto")
    pid = data.get("id")
    return PostResult(True, "devto", str(pid) if pid is not None else None, data.get("url"), None, False)


def _mastodon(text: str, url: str | None, disclosure: str, env: dict[str, str], dry_run: bool, http: HttpFn) -> PostResult:
    body = compose(text, disclosure, url, limit=MASTODON_MAX_CHARS, counter="chars")
    payload = {"status": body, "visibility": "public"}
    if dry_run:
        _print_dry_run("mastodon", payload)
        return PostResult(True, "mastodon", None, None, None, True)
    sec = _require(env, SECRET_KEYS["mastodon"])
    instance = sec["MASTODON_INSTANCE"].rstrip("/")
    if not instance.startswith("http"):
        instance = "https://" + instance
    token = sec["MASTODON_TOKEN"]
    data = _call_json(http, "POST", f"{instance}/api/v1/statuses", payload,
                      {"Authorization": f"Bearer {token}", "Idempotency-Key": sha256_text(body)[:32]}, [token], "mastodon")
    pid = data.get("id")
    return PostResult(True, "mastodon", str(pid) if pid is not None else None, data.get("url"), None, False)


def _threads(text: str, url: str | None, disclosure: str, env: dict[str, str], dry_run: bool, http: HttpFn,
             reply_to: str | None = None) -> PostResult:
    """Threads API: POST /{user-id}/threads (media_type=TEXT) creates a container, POST /{user-id}/threads_publish
    publishes it; the permalink is read back from the media object. The token goes in the form body, never in a URL
    that is logged (the permalink GET is redacted)."""
    body = compose(text, disclosure, url, limit=THREADS_MAX_CHARS, counter="chars")
    form = {"media_type": "TEXT", "text": body}
    if reply_to:
        form["reply_to_id"] = reply_to
    if dry_run:
        _print_dry_run("threads", form)
        return PostResult(True, "threads", None, None, None, True)
    sec = _require(env, SECRET_KEYS["threads"])
    uid, tok = sec["THREADS_USER_ID"].strip(), sec["THREADS_ACCESS_TOKEN"].strip()
    created = _call_json(http, "POST", f"{THREADS_API}/{uid}/threads", None, {}, [tok], "threads.create",
                         form={**form, "access_token": tok})
    cid = created.get("id")
    if not cid:
        raise PublishError("threads.create: response without id")
    last: PublishError | None = None
    for attempt in range(THREADS_PUBLISH_RETRIES):
        try:
            pub = _call_json(http, "POST", f"{THREADS_API}/{uid}/threads_publish", None, {}, [tok], "threads.publish",
                             form={"creation_id": str(cid), "access_token": tok})
            break
        except PublishError as e:  # container not ready yet: wait and retry
            last = e
            log.info("threads.publish attempt %d failed: %s", attempt + 1, e)
            time.sleep(THREADS_RETRY_SLEEP_S)
    else:
        raise last or PublishError("threads.publish: failed")
    mid = pub.get("id")
    link = None
    if mid:
        try:
            meta = _call_json(http, "GET", f"{THREADS_API}/{mid}?fields=permalink&access_token={urllib.parse.quote(tok, safe='')}",
                              None, {}, [tok, urllib.parse.quote(tok, safe="")], "threads.permalink")
            link = meta.get("permalink")
        except PublishError as e:  # the post is out; a missing link is not a failure
            log.warning("threads: published %s but permalink failed: %s", mid, e)
    return PostResult(True, "threads", str(mid) if mid else None, link, None, False)


def threads_refresh_token(token: str, http: HttpFn | None = None) -> tuple[str, int]:
    """Exchange a long-lived Threads token (older than 24 h, not expired) for a fresh 60-day one: (token, expires_in_s)."""
    http = http or urllib_http
    q = urllib.parse.urlencode({"grant_type": "th_refresh_token", "access_token": token})
    data = _call_json(http, "GET", f"{THREADS_REFRESH_URL}?{q}", None, {}, [token, urllib.parse.quote(token, safe="")],
                      "threads.refresh")
    new = str(data.get("access_token") or "")
    if not new:
        raise PublishError("threads.refresh: response without access_token")
    return new, int(data.get("expires_in") or 0)


# ---- x ---------------------------------------------------------------------------------------
_X_DOMAIN_RE = re.compile(r"(?<![@\w./])(?:[a-z0-9-]+\.)+[a-z]{2,}(?:/[^\s]*)?", re.IGNORECASE)


def _x_char_weight(ch: str) -> int:
    cp = ord(ch)
    return 1 if any(lo <= cp <= hi for lo, hi in X_LIGHT_RANGES) else 2


def x_weight(text: str) -> int:
    """Conservative X length: an http(s) link counts 23 (t.co), a bare domain max(len, 23) (X may or may not link it),
    a grapheme whose first code point is outside the light ranges counts 2 (emoji, CJK), other characters 1 each."""
    n, pos = 0, 0
    spans = [(s, e, X_URL_WEIGHT) for s, e, _ in find_urls(text)]
    covered = [(s, e) for s, e, _ in spans]
    for m in _X_DOMAIN_RE.finditer(text):
        if not any(s <= m.start() < e for s, e in covered):
            spans.append((m.start(), m.end(), max(len(m.group(0)), X_URL_WEIGHT)))
    for s, e, w in sorted(spans):
        if s < pos:
            continue
        n += _x_plain_weight(text[pos:s]) + w
        pos = e
    return n + _x_plain_weight(text[pos:])


def _x_plain_weight(text: str) -> int:
    n = 0
    for g in graphemes(text):
        n += 2 if _x_char_weight(g[0]) == 2 else sum(_x_char_weight(c) for c in g)
    return n


def x_compose(text: str, disclosure: str, url: str | None) -> str:
    """body + optional url + disclosure within X_MAX_WEIGHT, cutting only the body (grapheme by grapheme)."""
    body = text.strip()
    fixed = ("\n" + url if url and url not in body else "") + "\n\n" + disclosure
    if x_weight(fixed) >= X_MAX_WEIGHT:
        raise PublishError(f"x: disclosure and url do not fit the {X_MAX_WEIGHT} limit")
    if x_weight(body + fixed) <= X_MAX_WEIGHT:
        return body + fixed
    g = graphemes(body)
    lo, hi = 0, len(g)                     # largest prefix that fits with the ellipsis
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if x_weight("".join(g[:mid]).rstrip() + ELLIPSIS + fixed) <= X_MAX_WEIGHT:
            lo = mid
        else:
            hi = mid - 1
    return "".join(g[:lo]).rstrip() + ELLIPSIS + fixed


def _pct(s: str) -> str:
    return urllib.parse.quote(str(s), safe="~-._")


def oauth1_header(method: str, url: str, consumer_key: str, consumer_secret: str, token: str, token_secret: str,
                  *, nonce: str | None = None, timestamp: str | None = None, params: dict[str, str] | None = None) -> str:
    """RFC 5849 Authorization header, HMAC-SHA1. A JSON body is not part of the signature (X API v2); query
    ``params`` are. ``url`` must be without a query string."""
    oauth = {"oauth_consumer_key": consumer_key, "oauth_nonce": nonce or _secrets.token_hex(16),
             "oauth_signature_method": "HMAC-SHA1", "oauth_timestamp": timestamp or str(int(time.time())),
             "oauth_token": token, "oauth_version": "1.0"}
    allp = {**(params or {}), **oauth}
    norm = "&".join(f"{k}={v}" for k, v in sorted((_pct(k), _pct(v)) for k, v in allp.items()))
    base = "&".join((method.upper(), _pct(url), _pct(norm)))
    key = f"{_pct(consumer_secret)}&{_pct(token_secret)}".encode()
    oauth["oauth_signature"] = base64.b64encode(hmac.new(key, base.encode(), hashlib.sha1).digest()).decode()
    return "OAuth " + ", ".join(f'{_pct(k)}="{_pct(v)}"' for k, v in sorted(oauth.items()))


def _x(text: str, url: str | None, disclosure: str, env: dict[str, str], dry_run: bool, http: HttpFn,
       reply_to: str | None = None) -> PostResult:
    body = x_compose(text, disclosure, url)
    payload: dict[str, Any] = {"text": body}
    if reply_to:
        payload["reply"] = {"in_reply_to_tweet_id": str(reply_to).rstrip("/").rsplit("/", 1)[-1]}
    if dry_run:
        _print_dry_run("x", {**payload, "weight": x_weight(body)})
        return PostResult(True, "x", None, None, None, True)
    sec = _require(env, SECRET_KEYS["x"])
    auth = oauth1_header("POST", X_API, sec["X_API_KEY"], sec["X_API_SECRET"], sec["X_ACCESS_TOKEN"], sec["X_ACCESS_SECRET"])
    data = _call_json(http, "POST", X_API, payload, {"Authorization": auth}, list(sec.values()), "x")
    d = data.get("data") or {}
    tid = d.get("id")
    if not tid:
        raise PublishError(f"x: response without data.id: {_redact(json.dumps(data)[:ERROR_BODY_CHARS], list(sec.values()))}")
    user = (env.get("X_HANDLE") or "i/web").lstrip("@")
    return PostResult(True, "x", str(tid), f"https://x.com/{user}/status/{tid}", None, False)


# ---- public API -------------------------------------------------------------------------------
def post(channel: str, text: str, *, url: str | None = None, title: str | None = None, tags: list[str] | None = None,
         disclosure: str, dry_run: bool = False, env: dict[str, str] | None = None, http: HttpFn | None = None,
         target: str | None = None, reply_to: str | None = None) -> PostResult:
    """Publish ``text`` to ``channel`` with the disclosure appended. Never raises: failures are PostResult(ok=False).

    ``target`` is channel-specific (telegram: chat id or @channel). ``dry_run`` builds and prints the payload
    (stderr, no secrets) without any network call; ``TEAM_PUBLISH_DRY_RUN=1`` in the environment forces it.
    """
    env = dict(os.environ if env is None else env)
    http = http or urllib_http
    disclosure = (disclosure or "").strip() or DEFAULT_DISCLOSURE
    dry_run = dry_run or env.get(DRY_RUN_ENV, "") == "1"
    log.info("post channel=%s target=%s dry_run=%s chars=%d", channel, target, dry_run, len(text or ""))
    try:
        if channel not in CHANNELS:
            raise PublishError(f"unknown channel {channel!r}; known: {', '.join(CHANNELS)}")
        if not (text or "").strip():
            raise PublishError("text is empty")
        if channel == "telegram":
            res = _telegram(text, target, url, disclosure, env, dry_run, http)
        elif channel == "bluesky":
            res = _bluesky(text, url, disclosure, env, dry_run, http, reply_to=reply_to)
        elif channel == "github":
            res = _github(text, target, disclosure, env, dry_run, http)
        elif channel == "devto":
            res = _devto(text, title, url, tags, disclosure, env, dry_run, http)
        elif channel == "mastodon":
            res = _mastodon(text, url, disclosure, env, dry_run, http)
        elif channel == "threads":
            res = _threads(text, url, disclosure, env, dry_run, http, reply_to=reply_to)
        elif channel == "x":
            res = _x(text, url, disclosure, env, dry_run, http, reply_to=reply_to)
        else:
            raise PublishError(f"{channel}: not configured")
    except PublishError as e:
        log.warning("post failed channel=%s: %s", channel, e)
        return PostResult(False, channel, None, None, str(e), dry_run)
    except Exception as e:  # noqa: BLE001 - keep the contract: no exception escapes post()
        log.exception("post crashed channel=%s", channel)
        return PostResult(False, channel, None, None, f"{channel}: {type(e).__name__}: {e}", dry_run)
    log.info("post done channel=%s ok=%s id=%s url=%s", channel, res.ok, res.post_id, res.url)
    return res


def save_draft(dir_: Path, channel: str, text: str, *, target: str | None, url: str | None, title: str | None,
               tags: list[str] | None, reply_to: str | None, disclosure: str) -> PostResult:
    """Draft mode: the composed post is written as JSON for the editor; the dispatcher publishes it later with
    publish_draft(). Returned as dry_run so ledgers and daily caps do not count it."""
    dir_.mkdir(parents=True, exist_ok=True)
    n = len(list(dir_.glob("*.json"))) + 1
    path = dir_ / f"{n:02d}-{channel}.json"
    path.write_text(json.dumps({"channel": channel, "target": target, "text": text, "url": url, "title": title, "tags": tags or [],
                                "reply_to": reply_to, "disclosure": disclosure}, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("draft saved: %s (%d chars)", path, len(text))
    return PostResult(True, channel, f"draft:{path}", None, None, True)


def load_draft(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def publish_draft(path: Path, *, text: str | None = None, dry_run: bool = False) -> PostResult:
    """Send a draft (optionally with the editor's final text) for real."""
    d = load_draft(path)
    return post(str(d["channel"]), text if text is not None else str(d["text"]), url=d.get("url"), title=d.get("title"),
                tags=list(d.get("tags") or []), disclosure=str(d.get("disclosure") or ""), dry_run=dry_run,
                target=d.get("target"), reply_to=d.get("reply_to"))


def append_ledger(path: Path, res: PostResult, target: str | None, text: str) -> None:
    """Append one JSON line (the dispatcher's journal) to ``path``, creating parent dirs."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {"ts": utcnow_iso(), "channel": res.channel, "target": target, "ok": res.ok, "post_id": res.post_id,
           "url": res.url, "error": res.error, "dry_run": res.dry_run, "sha256": sha256_text(text)}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    log.debug("ledger appended %s", path)



def _disclosure_for(channel: str) -> str:
    """Per-channel disclosure from the dispatcher (TEAM_PUBLISH_DISCLOSURES: JSON channel -> text, with `default`),
    else TEAM_PUBLISH_DISCLOSURE, else the module default. Never empty: the tool always discloses."""
    raw = os.environ.get("TEAM_PUBLISH_DISCLOSURES")
    if raw:
        try:
            d = json.loads(raw) or {}
            txt = str(d.get(channel) or d.get("default") or "").strip()
            if txt:
                return txt
        except (ValueError, TypeError):
            pass
    return os.environ.get("TEAM_PUBLISH_DISCLOSURE") or DEFAULT_DISCLOSURE


def _with_src(url: str | None, channel: str) -> str | None:
    """Attribution: ?src=<channel> on the linked page so that sitemetrics can tell which channel brought a visit.
    Only for http(s) links without a src= already; TEAM_PUBLISH_NO_SRC=1 switches it off."""
    if not url or not url.startswith("http") or os.environ.get("TEAM_PUBLISH_NO_SRC") == "1" or "src=" in url:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}src={channel}"


def _daily_limit_error(channel: str, ledger: Path | None) -> str | None:
    """The dispatcher's daily cap per channel: TEAM_PUBLISH_MAX_PER_DAY, plus posts already booked today
    (TEAM_PUBLISH_COUNTS, a JSON object channel → count) and the ok lines of this run's ledger. Final for today."""
    raw = os.environ.get("TEAM_PUBLISH_MAX_PER_DAY")
    if not raw:
        return None
    try:
        cap = int(raw)
    except ValueError:
        return None
    n = 0
    try:
        n += int((json.loads(os.environ.get("TEAM_PUBLISH_COUNTS") or "{}") or {}).get(channel, 0))
    except (ValueError, TypeError):
        pass
    if ledger and ledger.exists():
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        for ln in ledger.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(ln)
            except json.JSONDecodeError:
                continue
            if e.get("channel") == channel and e.get("ok") and not e.get("dry_run") and str(e.get("ts", "")).startswith(today):
                n += 1
    if n >= cap:
        return f"daily limit reached for {channel}: {n} of {cap} posts today; no more posts until tomorrow"
    return None

# ---- CLI --------------------------------------------------------------------------------------
def _parse_tags(s: str | None) -> list[str]:
    return [t.strip() for t in (s or "").split(",") if t.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="team-publish", description=__doc__.splitlines()[0])
    p.add_argument("--channel", required=True, choices=CHANNELS)
    p.add_argument("--target", help="telegram: chat id or @channel; github: issue url")
    p.add_argument("--reply-to", help="bluesky: at:// uri; threads: media id; x: post id or url — of the post to reply to")
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--text-file", type=Path)
    src.add_argument("--text")
    p.add_argument("--title", help="devto: article title")
    p.add_argument("--url", help="link to attach / canonical_url for devto")
    p.add_argument("--tags", help="comma-separated")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--disclosure", default=DEFAULT_DISCLOSURE)
    p.add_argument("--ledger", type=Path, default=(Path(os.environ["TEAM_PUBLISH_LEDGER"]) if os.environ.get("TEAM_PUBLISH_LEDGER") else None),
                   help="JSONL journal to append the result to (default: $TEAM_PUBLISH_LEDGER, set by the dispatcher)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        text = args.text_file.read_text(encoding="utf-8") if args.text_file else args.text
    except OSError as e:
        res = PostResult(False, args.channel, None, None, f"cannot read text file: {e}", args.dry_run)
        print(res.to_json())
        return 1
    disclosure = args.disclosure if args.disclosure != DEFAULT_DISCLOSURE else _disclosure_for(args.channel)
    url = _with_src(args.url, args.channel)
    limit_err = _daily_limit_error(args.channel, args.ledger)
    if limit_err:
        res = PostResult(False, args.channel, None, None, limit_err, args.dry_run)
    elif os.environ.get(DRAFT_DIR_ENV):
        res = save_draft(Path(os.environ[DRAFT_DIR_ENV]), args.channel, text, target=args.target, url=url, title=args.title,
                         tags=_parse_tags(args.tags), reply_to=args.reply_to, disclosure=disclosure)
    else:
        res = post(args.channel, text, url=url, title=args.title, tags=_parse_tags(args.tags),
                   disclosure=disclosure, dry_run=args.dry_run, target=args.target, reply_to=args.reply_to)
    if args.ledger:
        try:
            append_ledger(args.ledger, res, args.target, text)
        except OSError as e:
            log.error("ledger write failed: %s", e)
    print(res.to_json())
    return 0 if res.ok else 1


if __name__ == "__main__":
    sys.exit(main())
