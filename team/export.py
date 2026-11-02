"""Public export of a product's state for the team diary site (allowlisted fields only).

`team export-public [--out DIR] [--days N]` writes <out>/<slug>/day-YYYY-MM-DD.json, one per UTC day, built from the
product's backlog, reviews, digests, posts and metrics. Nothing that is not on the allowlist leaves: no secrets, no
question texts, no raw observation bodies, no owner notes. Costs are rounded to $0.1.

Day file:
{
  "slug", "name", "site", "date",
  "tasks": [{"id","title","type","state","priority","goal","role","created","accepted_at","released_sha",
             "history": [{"ts","actor","to","note"}]}],          # tasks that had any transition on that day
  "reviews": [{"task","verdict","summary"}],                      # reviews created that day
  "posts": [{"channel","url","ts"}],
  "metrics": {"humans","engaged","returns","agents","crawlers","requests"} | null,   # that day's slice of metrics/<date>.json
  "spend_usd": 1.2,
  "po_digest_md": "...",                                           # latest PO digest of that day (text written for the owner)
  "analyst_md": "...",                                             # latest analyst summary of that day
  "observations": {"new": N, "by_class": {...}},                  # counts only
  "owner_questions_open": N
}
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from .config import Product
from .store import Store

log = logging.getLogger("team.export")

TASK_FIELDS = ("id", "title", "type", "state", "priority", "goal", "role", "created", "accepted_at", "released_sha")
NOTE_MAX = 200


def _day(ts: str | None) -> str | None:
    return str(ts)[:10] if ts else None


def _metrics_for(product: Product, date: str) -> dict[str, Any] | None:
    files = sorted((product.dir / "metrics").glob("????-??-??.json"))
    for f in reversed(files):                       # the newest file holds the fullest day slices
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        day = (d.get("days") or {}).get(date)
        if day:
            h = day.get("humans") or {}
            ag, cr = day.get("agents") or {}, day.get("crawlers") or {}
            return {"humans": h.get("sources", 0), "engaged": h.get("engaged", 0), "returns": h.get("returns", 0),
                    "agents": ag.get("requests", 0) if isinstance(ag, dict) else ag,
                    "crawlers": cr.get("requests", 0) if isinstance(cr, dict) else cr,
                    "requests": day.get("requests_raw", 0)}
    return None


def _latest_md(dir_: Path, patterns: list[str], date: str) -> str:
    files: list[Path] = []
    for pat in patterns:
        files += [p for p in dir_.glob(pat) if date.replace("-", "") in p.name or date in p.name]
    if not files:
        return ""
    txt = sorted(files)[-1].read_text(encoding="utf-8")
    body = txt.split("\n", 2)[-1] if txt.startswith("#") else txt
    return body.split("```")[0].strip()[:4000]


def export_day(product: Product, store: Store, date: str) -> dict[str, Any]:
    tasks = []
    for t in store.tasks():
        hist = [h for h in (t.get("history") or []) if _day(h.get("ts")) == date]
        if not hist:
            continue
        tasks.append({**{k: t.get(k) for k in TASK_FIELDS},
                      "history": [{"ts": h.get("ts"), "actor": h.get("actor"), "to": h.get("to"), "note": str(h.get("note") or "")[:NOTE_MAX]} for h in hist]})
    reviews = [{"task": r.get("task"), "verdict": r.get("verdict"), "summary": str(r.get("summary") or "")[:600]}
               for r in store._load_all("reviews") if _day(r.get("created")) == date]
    posts = [{"channel": r[1], "url": r[3], "ts": r[5]} for r in store.db.execute("SELECT * FROM posts WHERE day=? ORDER BY ts", (date,))]
    spend = float(store.db.execute("SELECT COALESCE(SUM(usd),0) FROM spend WHERE day=?", (date,)).fetchone()[0] or 0.0)
    obs = [o for o in store.observations() if _day(o.get("observed_at")) == date]
    by_class: dict[str, int] = {}
    for o in obs:
        by_class[str(o.get("classification"))] = by_class.get(str(o.get("classification")), 0) + 1
    digests = product.dir / "digests"
    return {"slug": product.slug, "name": product.raw.get("name") or product.slug, "site": product.site_base, "date": date,
            "tasks": tasks, "reviews": reviews, "posts": posts, "metrics": _metrics_for(product, date),
            "spend_usd": round(spend, 1),
            "po_digest_md": _latest_md(digests, [f"{date.replace('-', '')}T*.md", f"{date.replace('-', '')}*.md"], date),
            "analyst_md": _latest_md(digests, [f"analyst-{date}*.md"], date),
            "observations": {"new": len(obs), "by_class": by_class},
            "owner_questions_open": len([q for q in store.questions(["open"]) if q.get("to") == "owner"])}


def export_product(product: Product, out: Path, days: int = 14) -> list[Path]:
    store = Store(product.dir)
    written: list[Path] = []
    today = datetime.now(timezone.utc).date()
    (out / product.slug).mkdir(parents=True, exist_ok=True)
    for i in range(days):
        date = (today - timedelta(days=i)).isoformat()
        d = export_day(product, store, date)
        if not (d["tasks"] or d["reviews"] or d["posts"] or d["po_digest_md"] or d["metrics"]):
            continue
        p = out / product.slug / f"day-{date}.json"
        p.write_text(json.dumps(d, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        written.append(p)
    store.close()
    (out / product.slug / "product.json").write_text(json.dumps(
        {"slug": product.slug, "name": product.raw.get("name") or product.slug, "site": product.site_base,
         "goals": _goals_from_vision(product.vision()), "generated": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("%s: exported %d day files to %s", product.slug, len(written), out / product.slug)
    return written


def _goals_from_vision(vision: str) -> list[dict[str, str]]:
    """The goals table of VISION.md (id · metric · definition · base · target · how) as public rows."""
    rows = []
    in_table = False
    for ln in vision.splitlines():
        if ln.startswith("## Цели"):
            in_table = True
            continue
        if in_table and ln.startswith("## "):
            break
        if in_table and ln.startswith("|") and not ln.startswith("|---") and not ln.startswith("| id"):
            cells = [c.strip() for c in ln.strip("|").split("|")]
            if len(cells) >= 5:
                rows.append({"id": cells[0], "metric": cells[1], "definition": cells[2], "target": cells[4]})
    return rows
