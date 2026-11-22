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
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Product

log = logging.getLogger("team.ux")

SCORES_FILE = "ux-scores.jsonl"
WINDOW_DAYS = 7
LOW_SCORE = 2              # interest or usefulness at or below this → a value_gap observation

# --- schedule of the persona runs: by the clock, like seo_every_h / voice_every_h -------------------------------------
PERSONA_EVERY_KEY = "persona_every_h"             # team.yaml `cycle.persona_every_h`: hours between runs per product; 0 = off
PERSONA_EVERY_LEGACY_KEY = "persona_every_n_cycles"   # deprecated synonym (cycles were hourly, so N cycles is read as N hours)
PERSONA_EVERY_H_DEFAULT = 12                      # twice a day (owner 04.11)
PERSONA_RETRY_KEY = "persona_retry_h"             # a run that brought no result at all is tried again this soon
PERSONA_RETRY_H_DEFAULT = 3
PERSONA_MARK_FILE = "personas-last.json"          # products/<slug>/: when the last run started, what it took, what came back
_legacy_warned = False


def persona_every_h(cycle_cfg: dict[str, Any]) -> int:
    """Hours between persona runs of a product: `cycle.persona_every_h`, else the deprecated `persona_every_n_cycles`
    read as hours (with one WARNING per process), else 12. 0 or less switches the personas off; a value that is not a
    number falls back to the default with a WARNING."""
    global _legacy_warned
    key = PERSONA_EVERY_KEY if PERSONA_EVERY_KEY in cycle_cfg else (PERSONA_EVERY_LEGACY_KEY if PERSONA_EVERY_LEGACY_KEY in cycle_cfg else None)
    if key is None:
        return PERSONA_EVERY_H_DEFAULT
    if key == PERSONA_EVERY_LEGACY_KEY and not _legacy_warned:
        _legacy_warned = True
        log.warning("cycle.%s is deprecated: its value %r is read as hours; rename the key to cycle.%s",
                    PERSONA_EVERY_LEGACY_KEY, cycle_cfg.get(key), PERSONA_EVERY_KEY)
    try:
        return int(cycle_cfg.get(key) or 0)
    except (TypeError, ValueError):
        log.warning("cycle.%s=%r is not a number — %d h in use", key, cycle_cfg.get(key), PERSONA_EVERY_H_DEFAULT)
        return PERSONA_EVERY_H_DEFAULT


def persona_retry_h(cycle_cfg: dict[str, Any]) -> int:
    try:
        return max(1, int(cycle_cfg.get(PERSONA_RETRY_KEY, PERSONA_RETRY_H_DEFAULT)))
    except (TypeError, ValueError):
        return PERSONA_RETRY_H_DEFAULT


def _ts(v: Any) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def persona_mark(product_dir: Path) -> dict[str, Any]:
    """The record of the last persona run of a product ({} when there was none or the file is unreadable):
    `started`, `cycle`, `scenarios`, `next` (rotation index of the next run), `finished`, `ok` (runs that returned a
    result), `last_ok` (start of the last run with at least one result)."""
    f = Path(product_dir) / PERSONA_MARK_FILE
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def write_persona_mark(product_dir: Path, mark: dict[str, Any]) -> None:
    f = Path(product_dir) / PERSONA_MARK_FILE
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(mark, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(f)                                     # atomic: a cycle killed here leaves the old mark or the new one


def personas_due(product_dir: Path, every_h: int, now: datetime | None = None, retry_h: int = PERSONA_RETRY_H_DEFAULT) -> bool:
    """True when the product's personas should run now: never ran, or the last run started at least `every_h` hours
    ago. A run that brought no result (every role run failed, the cycle was killed mid-run, the budget was out) is
    tried again after `retry_h` hours instead of waiting the whole period. The number of cycles plays no part (until
    22.11 the schedule counted cycles through `Store.cycles()`, which returns at most 50 rows: after a product's 50th
    cycle the personas never ran again)."""
    if every_h <= 0:
        return False
    now = now or datetime.now(timezone.utc)
    mark = persona_mark(product_dir)
    started = _ts(mark.get("started")) if mark else None
    if started is None:
        return True
    wait_h = every_h if int(mark.get("ok") or 0) > 0 else min(every_h, retry_h)
    return now - started >= timedelta(hours=wait_h)


def last_persona_ok(product: Product) -> str | None:
    """ISO time of the last persona run that returned a result: the mark's `last_ok`, else the newest line of
    ux-scores.jsonl (runs made before the mark existed), else None."""
    ok = persona_mark(product.dir).get("last_ok")
    if ok and _ts(ok):
        return str(ok)
    p = scores_path(product)
    if not p.exists():
        return None
    for ln in reversed(p.read_text(encoding="utf-8").splitlines()):
        try:
            ts = json.loads(ln).get("ts")
        except (ValueError, AttributeError):
            continue
        if ts and _ts(ts):
            return str(ts)
    return None


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
