"""External critic → observation cards (owner 03.11: "считай это критикой ещё одного ревьюера, можно порождать задачи").

A separate system (Codex-based, outside this repo) writes a Markdown report every two hours into
``external_critic.dir`` on the server. Each report has product sections (``## Release Radar`` …) and findings of the form
``**TXT-RR-01 · P2 · title**`` followed by observation, suggestion and links. This module turns every new finding into an
observation card for the product it concerns, classification ``feedback`` with source ``external-critic``, so the PO must
answer it with a task or a reasoned reject (the PO card says how to weigh it: below real people, like a reviewer). A
finding id repeated in a later report attaches to the live card instead of duplicating it (fingerprint ``critic:<id>``).

Reports already ingested are listed in ``data/external-critic/seen.json`` (file → products that took their findings).
A report without recognisable findings is not dropped: it goes as one card to the default product (the framework).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Product, Settings
from .store import Store, utcnow

log = logging.getLogger("team.critic")

# `**TXT-RR-01 · P2 · title**` and `**RR-01 · P1 · title.** body…` (optional family prefix; title may close with a period)
FINDING_RE = re.compile(r"^\*\*((?:[A-Z]{2,6}-)?([A-Z]{2,4})-\d{2,3})\s*·\s*(P\d)\s*·\s*(.+?)\*\*\s*(.*)$", re.M)
# section = a markdown heading of any depth, or a whole line in bold without "·" (the technical report uses `**Release Radar**`)
HEADING_RE = re.compile(r"^(?:#{2,4}\s+(.+?)|\*\*([^*·\n]{2,60})\*\*)\s*$", re.M)
# channel rows of the social table: `| [Telegram Radar](url) | observation | feedback |`
CHANNEL_ROW_RE = re.compile(r"^\|\s*\[([^\]]+)\]\((https?://[^)\s]+)\)\s*\|(.+)\|\s*$", re.M)
CHANNEL_KEYS = {"radar": "release-radar", "tracker": "agentsmd-tracker", "luz": "luz-amanha", "команд": "agent-team-diary", "team": "agent-team-diary"}
LINK_RE = re.compile(r"\((https?://[^)\s]+)\)")
MAX_DETAIL = 2500
MAX_FALLBACK = 1800

# finding-id middle token and heading keywords → product slug; hosts are matched from product.yaml at runtime
ID_TO_SLUG = {"RR": "release-radar", "AT": "agentsmd-tracker", "LU": "luz-amanha", "DI": "agent-team-diary", "FW": "agent-team",
              "DIARY": "agent-team-diary", "LUZ": "luz-amanha", "AGT": "agent-team"}
HEADING_KEYS = {"release radar": "release-radar", "agents.md": "agentsmd-tracker", "luz": "luz-amanha", "дневник": "agent-team-diary",
                "diary": "agent-team-diary", "фреймворк": "agent-team", "framework": "agent-team", "agent-team": "agent-team"}


@dataclass
class Finding:
    id: str
    priority: str
    title: str
    body: str
    heading: str
    slug: str | None = None
    url: str | None = None
    links: list[str] = field(default_factory=list)


def settings(st: Settings) -> dict[str, Any]:
    return dict(st.raw.get("external_critic") or {})


def reports_dir(st: Settings) -> Path | None:
    d = settings(st).get("dir")
    return Path(str(d)) if d else None


def seen_path(st: Settings) -> Path:
    return st.data_dir / "external-critic" / "seen.json"


def load_seen(st: Settings) -> dict[str, list[str]]:
    p = seen_path(st)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_seen(st: Settings, seen: dict[str, list[str]]) -> None:
    p = seen_path(st)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(seen, ensure_ascii=False, indent=1), encoding="utf-8")


def _slug_for_heading(heading: str, hosts: dict[str, str]) -> str | None:
    h = heading.lower()
    for host, slug in hosts.items():
        if host and host in h:
            return slug
    for key, slug in HEADING_KEYS.items():
        if key in h:
            return slug
    return None


def parse_report(text: str, hosts: dict[str, str] | None = None) -> list[Finding]:
    """Findings with their section heading and the product they concern (by id token, heading, or a host in the body)."""
    hosts = hosts or {}
    heads = [(m.start(), (m.group(1) or m.group(2) or "").strip()) for m in HEADING_RE.finditer(text)]
    finds = list(FINDING_RE.finditer(text))
    out: list[Finding] = []
    for i, m in enumerate(finds):
        start = m.start()
        end = finds[i + 1].start() if i + 1 < len(finds) else len(text)
        # a heading inside the body ends the finding
        for pos, _ in heads:
            if start < pos < end:
                end = pos
                break
        heading = ""
        for pos, h in heads:
            if pos < start:
                heading = h
        body = (m.group(5) + "\n" + text[m.end():end]).strip()
        links = LINK_RE.findall(body)
        slug = ID_TO_SLUG.get(m.group(2)) or _slug_for_heading(heading, hosts)
        if slug is None:
            low = body.lower()
            for host, s in hosts.items():
                if host and host in low:
                    slug = s
                    break
        out.append(Finding(id=m.group(1), priority=m.group(3), title=" ".join(m.group(4).split())[:120], body=body[:MAX_DETAIL],
                           heading=heading, slug=slug, url=links[0] if links else None, links=links[:5]))
    return out


def parse_channel_rows(text: str) -> list[dict[str, Any]]:
    """Rows of the social-channel table: channel name, link, the rest of the row (observation | feedback)."""
    out = []
    for m in CHANNEL_ROW_RE.finditer(text):
        name, url, rest = m.group(1).strip(), m.group(2), m.group(3)
        if name.lower() in ("канал", "channel"):
            continue
        cells = [c.strip() for c in rest.split("|") if c.strip()]
        low = name.lower()
        slug = next((s for k, s in CHANNEL_KEYS.items() if k in low), None)
        out.append({"channel": name, "url": url, "text": " — ".join(cells)[:MAX_DETAIL], "slug": slug})
    return out


def channel_card(row: dict[str, Any], report_name: str, author: str) -> dict[str, Any]:
    key = re.sub(r"[^a-z0-9]+", "-", row["channel"].lower()).strip("-")
    return {"title": f"[критик, канал {row['channel']}] {row['text']}"[:140], "classification": "feedback", "source": "external-critic",
            "author": author[:80], "url": row["url"], "fingerprint": f"critic:channel:{key}", "detail": row["text"], "report": report_name,
            "section": "Социальные каналы",
            "note": "Строка таблицы каналов внешнего критика: наблюдение и фидбэк по каналу; для маркетолога и PO."}


def card(f: Finding, report_name: str, author: str) -> dict[str, Any]:
    return {"title": f"[критик {f.id} {f.priority}] {f.title}"[:140], "classification": "feedback", "source": "external-critic",
            "author": author[:80], "url": f.url or "", "fingerprint": f"critic:{f.id}",
            "detail": f.body, "report": report_name, "section": f.heading[:80], "links": f.links,
            "note": "Внешний критик (другая модель, вне команды). Вес — как у ревьюера: ниже слов реальных людей; каждая находка → задача или обоснованный reject."}


def fallback_card(text: str, report_name: str, author: str) -> dict[str, Any]:
    first = text.strip().splitlines()[0].lstrip("# ").strip() if text.strip() else report_name
    return {"title": f"[критик] {first}"[:140], "classification": "feedback", "source": "external-critic", "author": author[:80],
            "url": "", "fingerprint": f"critic:report:{report_name}", "detail": text.strip()[:MAX_FALLBACK], "report": report_name,
            "note": "Отчёт внешнего критика без распознанных находок — прочитать целиком и разнести по продуктам."}


VALID_SLUGS = ("release-radar", "agentsmd-tracker", "luz-amanha", "agent-team-diary", "agent-team")
ACTIVE_STATUSES = ("new", "confirmed", "")          # fixed / unverified findings make no card


def parse_json_report(data: dict[str, Any]) -> tuple[list[Finding], list[dict[str, Any]]]:
    """The JSON twin (REPORT_FORMAT.md §2): findings and channel rows, already attributed to products."""
    finds: list[Finding] = []
    for x in data.get("findings") or []:
        fid = str(x.get("id") or "").strip()
        if not fid:
            continue
        status = str(x.get("status") or "new").lower()
        slug = str(x.get("product") or "").strip() or None
        if slug not in VALID_SLUGS:
            slug = ID_TO_SLUG.get(fid.split("-")[0]) if "-" in fid else None
        body = "\n".join(f"{k}: {x.get(v)}" for k, v in (("Статус", "status"), ("Факт", "fact"), ("Предложение", "proposal"),
                                                            ("Критерий", "criterion")) if x.get(v))
        urls = [str(u) for u in (x.get("urls") or []) if str(u).startswith("http")]
        f = Finding(id=fid, priority=str(x.get("priority") or "P2"), title=" ".join(str(x.get("title") or "").split())[:120],
                    body=body[:MAX_DETAIL], heading="", slug=slug, url=urls[0] if urls else None, links=urls[:5])
        f.status = status  # type: ignore[attr-defined]
        finds.append(f)
    rows = []
    for c in data.get("channels") or []:
        slug = str(c.get("product") or "").strip()
        rows.append({"channel": str(c.get("channel") or "?"), "url": str(c.get("url") or ""),
                     "text": " — ".join(str(c.get(k)) for k in ("observation", "feedback") if c.get(k))[:MAX_DETAIL],
                     "slug": slug if slug in VALID_SLUGS else None})
    return finds, rows


def finding_status(f: Finding, body: str) -> str:
    s = getattr(f, "status", None)
    if s:
        return str(s)
    m = re.search(r"^Статус:\s*(\w+)", body, re.M | re.I)
    return m.group(1).lower() if m else ""


def write_responses(st: Settings, products: list[Product]) -> Path | None:
    """REPORT_FORMAT.md §4: what the team did with each critic finding, for the critic to read before its next run."""
    cfg = settings(st)
    out_dir = cfg.get("responses_dir")
    if not cfg.get("enabled") or not out_dir:
        return None
    out: dict[str, Any] = {"generated_at": utcnow(), "findings": {}}
    for p in products:
        s = Store(p.dir)
        try:
            tasks = {t["id"]: t for t in s.tasks()}
            for o in s.observations():
                fp = str(o.get("fingerprint") or "")
                if not fp.startswith("critic:") or fp.startswith("critic:report:"):
                    continue
                d = o.get("decision") or {}
                tid = d.get("linked_to") if d.get("action") == "link" else None
                task = None
                for t in tasks.values():
                    if o["id"] in (t.get("observations") or []):
                        task = t
                        break
                if task is None and tid in tasks:
                    task = tasks[tid]
                out["findings"][fp.split(":", 1)[1]] = {
                    "product": p.slug, "observation": o["id"], "status": o.get("status"), "repeats": o.get("repeat_count", 0),
                    "po_action": d.get("action"), "po_reason": _short(d.get("reason"), 300) if d.get("reason") else None,
                    "task": ({"id": task["id"], "state": task.get("state"), "title": _short(task.get("title"), 120), "pr": task.get("pr_url"),
                              "accepted_at": task.get("accepted_at")} if task else None)}
        finally:
            s.close()
    path = Path(str(out_dir)) / "agent-team-responses.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError as exc:
        log.warning("critic responses not written: %s", exc)
        return None
    return path


def _short(s: Any, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def ingest(st: Settings, product: Product, store: Store, all_products: list[Product] | None = None) -> int:
    """Add observation cards for this product from every report not yet ingested for it. Returns the number of cards."""
    cfg = settings(st)
    d = reports_dir(st)
    if not cfg.get("enabled") or d is None or not d.exists():
        return 0
    author = str(cfg.get("author") or "external critic")
    default_slug = str(cfg.get("default_product") or "agent-team")
    hosts = {p.host.lower(): p.slug for p in (all_products or [product]) if p.host}
    seen = load_seen(st)
    n = 0
    names = sorted({x.with_suffix("").name for x in d.iterdir() if x.suffix in (".md", ".json")})
    for name in names:
        f = d / (name + ".md")
        if product.slug in seen.get(f.name, []):
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""
        except OSError as exc:
            log.warning("critic report %s unreadable: %s", f.name, exc)
            continue
        twin = f.with_suffix(".json")
        if twin.exists():                       # REPORT_FORMAT.md: the JSON twin is the machine source
            try:
                findings, rows = parse_json_report(json.loads(twin.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, AttributeError) as exc:
                log.warning("critic twin %s unreadable (%s); falling back to markdown", twin.name, exc)
                findings, rows = parse_report(text, hosts), parse_channel_rows(text)
        else:
            findings, rows = parse_report(text, hosts), parse_channel_rows(text)
        mine = [x for x in findings if (x.slug or default_slug) == product.slug]
        my_rows = [r for r in rows if (r["slug"] or default_slug) == product.slug]
        if not findings and not rows and product.slug == default_slug:
            store.add_observation(fallback_card(text, f.name, author))
            n += 1
        for x in mine:
            if finding_status(x, x.body) not in ACTIVE_STATUSES:
                continue                        # fixed / unverified: nothing to act on
            store.add_observation(card(x, f.name, author))
            n += 1
        for r in my_rows:
            store.add_observation(channel_card(r, f.name, author))
            n += 1
        seen.setdefault(f.name, []).append(product.slug)
        log.info("%s: critic report %s → %d card(s)", product.slug, f.name, len(mine))
    save_seen(st, seen)
    if n:
        store.event("critic.collected", product.slug, "dispatcher", {"cards": n})
    return n
