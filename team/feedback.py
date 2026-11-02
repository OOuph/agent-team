"""Feedback collectors: what real people say about a product, turned into observation cards.

Sources (each optional, each silent when not configured):
  github   open issues and new comments of the product's site repository (GITHUB_TOKEN; public repos work without it)
  bluesky  replies, mentions and quotes of the product's Bluesky account (BLUESKY_HANDLE + BLUESKY_APP_PASSWORD)
  telegram reactions to channel posts are visible in the channel and read by the analyst; the Bot API delivers reaction
           counts only through updates, so they are not collected here (see the feedback analysis in the framework docs)
Every item carries a stable `fingerprint` so the store appends repeats instead of duplicating cards.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .config import Product

log = logging.getLogger("team.feedback")

HttpFn = Callable[[str, dict[str, str]], tuple[int, bytes]]
PostFn = Callable[[str, dict[str, Any], dict[str, str]], tuple[int, bytes]]
LOOKBACK_H = 48
MAX_ITEMS = 20


def _http(url: str, headers: dict[str, str]) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": "agent-team/feedback", **headers})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _post(url: str, body: dict[str, Any], headers: dict[str, str]) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "agent-team/feedback", **headers})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _since() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_H)


def _card(source: str, title: str, text: str, url: str, fp: str, author: str) -> dict[str, Any]:
    return {"title": title[:140], "classification": "feedback", "source": source, "author": author[:80], "url": url,
            "expected": "", "actual": text[:800], "evidence": [url], "confidence": "high", "fingerprint": fp}


# ------------------------------------------------------------------ GitHub issues and comments

def github_issues(product: Product, http: HttpFn = _http, env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    env = env if env is not None else dict(os.environ)
    m = re.match(r"https://github\.com/([^/]+)/([^/.]+)", product.repo_url or "")
    if not m:
        return []
    owner, repo = m.group(1), m.group(2)
    headers = {"Accept": "application/vnd.github+json"}
    if env.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"token {env['GITHUB_TOKEN']}"
    since = _since().isoformat(timespec="seconds").replace("+00:00", "Z")
    out: list[dict[str, Any]] = []
    status, body = http(f"https://api.github.com/repos/{owner}/{repo}/issues?state=open&sort=updated&per_page={MAX_ITEMS}&since={since}", headers)
    if status != 200:
        log.info("github issues %s/%s: HTTP %s", owner, repo, status)
        return out
    for it in json.loads(body or b"[]"):
        if "pull_request" in it:
            continue
        n = it.get("number")
        out.append(_card("github-issue", f"Issue #{n}: {it.get('title', '')}", str(it.get("body") or ""),
                         str(it.get("html_url")), f"gh-issue-{owner}-{repo}-{n}", (it.get("user") or {}).get("login", "")))
    status, body = http(f"https://api.github.com/repos/{owner}/{repo}/issues/comments?sort=updated&direction=desc&per_page={MAX_ITEMS}&since={since}", headers)
    if status == 200:
        for c in json.loads(body or b"[]"):
            login = (c.get("user") or {}).get("login", "")
            if login.endswith("[bot]"):
                continue
            num = str(c.get("issue_url", "")).rsplit("/", 1)[-1]
            out.append(_card("github-comment", f"Comment on #{num} by {login}", str(c.get("body") or ""),
                             str(c.get("html_url")), f"gh-comment-{c.get('id')}", login))
    return out


# ------------------------------------------------------------------ Bluesky replies, mentions, quotes

def bluesky_replies(product: Product, http: HttpFn = _http, post: PostFn = _post, env: dict[str, str] | None = None) -> list[dict[str, Any]]:
    env = env if env is not None else dict(os.environ)
    handle, pw = env.get("BLUESKY_HANDLE"), env.get("BLUESKY_APP_PASSWORD")
    if not handle or not pw or not (product.raw.get("channels") or {}).get("bluesky"):
        return []
    pds = env.get("BLUESKY_PDS", "https://bsky.social").rstrip("/")
    status, body = post(f"{pds}/xrpc/com.atproto.server.createSession", {"identifier": handle, "password": pw}, {})
    if status != 200:
        log.info("bluesky session: HTTP %s", status)
        return []
    tok = json.loads(body).get("accessJwt")
    status, body = http(f"{pds}/xrpc/app.bsky.notification.listNotifications?limit=50", {"Authorization": f"Bearer {tok}"})
    if status != 200:
        log.info("bluesky notifications: HTTP %s", status)
        return []
    out: list[dict[str, Any]] = []
    since = _since()
    for n in json.loads(body).get("notifications") or []:
        reason = n.get("reason")
        if reason not in ("reply", "mention", "quote"):
            continue
        try:
            ts = datetime.fromisoformat(str(n.get("indexedAt", "")).replace("Z", "+00:00"))
        except ValueError:
            ts = datetime.now(timezone.utc)
        if ts < since:
            continue
        author = (n.get("author") or {}).get("handle", "")
        text = str(((n.get("record") or {}).get("text")) or "")
        uri = str(n.get("uri", ""))
        rkey = uri.rsplit("/", 1)[-1]
        url = f"https://bsky.app/profile/{author}/post/{rkey}" if author and rkey else uri
        out.append(_card(f"bluesky-{reason}", f"{reason} from @{author}: {text[:80]}", text, url, f"bsky-{uri}", author))
    return out[:MAX_ITEMS]


# ------------------------------------------------------------------ all sources

def collect(product: Product) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for fn in (github_issues, bluesky_replies):
        try:
            got = fn(product)
            items.extend(got)
            log.info("%s: %s -> %d items", product.slug, fn.__name__, len(got))
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: %s failed: %s", product.slug, fn.__name__, exc)
    return items
