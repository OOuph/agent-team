"""Reader experience scores from persona runs (owner 04.11: personas on Opus, focused on user experience, interest and
value — "the external critic must not be the only one working on this").

Every persona run now ends with an `experience` block: first impression, how many steps until the persona understood
what the page gives them, interest 1–5, usefulness 1–5, would they come back / share, why, and what would make it worth
returning. The dispatcher appends one line per run to ``products/<slug>/ux-scores.jsonl``; the PO prompt and the daily
digest show the rolling picture; a low interest or usefulness becomes an observation for the PO even when the persona
did not file one.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Product

SCORES_FILE = "ux-scores.jsonl"
WINDOW_DAYS = 7
LOW_SCORE = 2              # interest or usefulness at or below this → a value_gap observation


def scores_path(product: Product) -> Path:
    return product.dir / SCORES_FILE


def record(product: Product, persona: str, scenario: str, run_id: str, exp: dict[str, Any], outcome: str) -> dict[str, Any]:
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "persona": persona, "scenario": scenario,
           "run_id": run_id, "outcome": outcome,
           **{k: exp.get(k) for k in ("first_impression", "steps_to_understand", "interest", "usefulness", "would_return",
                                      "would_share", "why", "wish")}}
    with scores_path(product).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def load(product: Product, days: int = WINDOW_DAYS) -> list[dict[str, Any]]:
    p = scores_path(product)
    if not p.exists():
        return []
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        try:
            r = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if str(r.get("ts", "")) >= since:
            out.append(r)
    return out


def _mean(xs: list[Any]) -> float | None:
    vals = [float(x) for x in xs if isinstance(x, (int, float))]
    return round(sum(vals) / len(vals), 1) if vals else None


def summary(product: Product, days: int = WINDOW_DAYS) -> dict[str, Any]:
    recs = load(product, days)
    return {"runs": len(recs), "interest": _mean([r.get("interest") for r in recs]),
            "usefulness": _mean([r.get("usefulness") for r in recs]),
            "return_yes": sum(1 for r in recs if r.get("would_return") == "yes"),
            "share_yes": sum(1 for r in recs if r.get("would_share") == "yes"),
            "wishes": [f"{r.get('persona')}: {r.get('wish')}" for r in recs[-6:] if r.get("wish")],
            "low": [r for r in recs[-12:] if (r.get("interest") or 5) <= LOW_SCORE or (r.get("usefulness") or 5) <= LOW_SCORE]}


def digest_line(product: Product) -> str:
    s = summary(product)
    if not s["runs"]:
        return "Опыт читателя (персоны): оценок ещё нет"
    return (f"Опыт читателя (персоны, {WINDOW_DAYS} дн., {s['runs']} прогонов): интерес {s['interest']}/5, польза {s['usefulness']}/5, "
            f"вернулись бы {s['return_yes']}/{s['runs']}, поделились бы {s['share_yes']}/{s['runs']}")


def prompt_block(product: Product) -> str:
    s = summary(product)
    if not s["runs"]:
        return "(no persona experience scores yet)"
    lines = [digest_line(product).replace("Опыт читателя (персоны, ", "Reader experience (personas, ")]
    lines += ["Recent wishes (what would make a persona come back):"] + [f"- {w}" for w in s["wishes"]]
    if s["low"]:
        lines += ["Low scores (interest or usefulness ≤ 2):"] + [f"- {r.get('persona')} / {r.get('scenario')}: interest {r.get('interest')}, "
                                                                    f"usefulness {r.get('usefulness')} — {r.get('why')}" for r in s["low"]]
    return "\n".join(lines)
