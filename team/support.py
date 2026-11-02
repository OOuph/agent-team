"""Support: the team answers people who wrote to it — GitHub issues and comments on the site repository, replies and
mentions on Bluesky — within the product's knowledge (VISION.md, the live site). One answer per message, a daily cap,
the disclosure added by the publish tool, questions outside VISION escalated to the PO instead of answered.

The feedback collectors (feedback.py) turn messages into observation cards of class `feedback`; this module picks the
unanswered ones (no `support.reply` / `support.skip` event for the card's fingerprint), runs the `support` role, and
posts its answer with publish.post (channel github: comment on the issue; channel bluesky: reply to the post).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from . import publish
from .config import Product
from .store import Store

log = logging.getLogger("team.support")

ANSWERABLE_SOURCES = ("github-issue", "github-comment", "bluesky-reply", "bluesky-mention", "bluesky-quote")
DEFAULT_MAX_PER_DAY = 10
DEFAULT_MAX_PER_CYCLE = 3


def _cfg(product: Product) -> dict[str, Any]:
    return dict(product.st.raw.get("support") or {})


def own_accounts(product: Product) -> set[str]:
    acc = {str(a).lstrip("@").lower() for a in (_cfg(product).get("ignore_authors") or [])}
    for k in ("BLUESKY_HANDLE", "GITHUB_LOGIN"):
        if os.environ.get(k):
            acc.add(os.environ[k].lstrip("@").lower())
    return acc


def answered_fingerprints(store: Store) -> set[str]:
    out = set()
    for e in store.events(limit=5000):
        if e.get("kind") in ("support.reply", "support.skip"):
            out.add(str(e.get("subject")))
    return out


def replies_today(store: Store) -> int:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return sum(1 for e in store.events(limit=5000) if e.get("kind") == "support.reply" and str(e.get("ts", "")).startswith(day))


def pending(product: Product, store: Store) -> list[dict[str, Any]]:
    done = answered_fingerprints(store)
    own = own_accounts(product)
    out = []
    for o in store.observations():
        if o.get("classification") != "feedback" or o.get("source") not in ANSWERABLE_SOURCES:
            continue
        if o.get("fingerprint") in done or str(o.get("author", "")).lstrip("@").lower() in own:
            continue
        out.append(o)
    return sorted(out, key=lambda o: str(o.get("observed_at")))


def channel_for(o: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """('github', {target: issue url}) or ('bluesky', {reply_to: at-uri}); the Bluesky at-uri is kept in the fingerprint."""
    src = str(o.get("source", ""))
    if src.startswith("github"):
        url = str(o.get("url", ""))
        issue = url.split("#")[0]
        if "/pull/" in issue:
            raise ValueError("pull requests are not answered")
        return "github", {"target": issue}
    fp = str(o.get("fingerprint", ""))
    uri = fp[len("bsky-"):] if fp.startswith("bsky-") else ""
    if not uri.startswith("at://"):
        raise ValueError("no at-uri for the Bluesky post")
    return "bluesky", {"reply_to": uri}


def support_prompt(product: Product, o: dict[str, Any], vision: str) -> str:
    return "\n\n".join([
        "# Support: answer one message from a person",
        f"Product: {product.raw.get('name') or product.slug} — {product.site_base}",
        "## VISION.md (your knowledge: what the product is, promises, what it does not do)\n" + vision[:9000],
        f"## The message\n- source: {o.get('source')}\n- author: {o.get('author')}\n- url: {o.get('url')}\n- title: {o.get('title')}\n- text:\n{str(o.get('actual') or '')[:2000]}",
        "## Rules\n- Answer only what VISION and the live site support; you may open pages with `team-fetch <url>` to check facts.\n"
        "- Short, specific, friendly, no marketing. GitHub: up to ~120 words, markdown. Bluesky: ≤ 250 characters.\n"
        "- A bug report: thank, say what you checked, say it goes to the backlog (it does: every feedback card reaches the product owner).\n"
        "- A request outside VISION (new data, new feature, money, legal, accounts): do not promise; `escalate` with the question for the PO, "
        "and `text` with a one-line acknowledgement that it was passed on.\n- Spam, abuse, or nothing to answer: `skip`.\n"
        "- Never ask for personal data, never invent numbers, never claim to be human (the tool appends the AI disclosure).",
    ])
