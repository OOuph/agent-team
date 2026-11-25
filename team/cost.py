"""Cost per accepted task and prompt-cache share (owner 25.11: "$172 for 1,108 runs" is an invoice, not economics).

Read-only: everything comes from what the dispatcher already writes — `data/runs/<slug>/<cycle>/<run>/run.json`
(role, ok, cost) and `events.jsonl` (the CLI's stream: token usage of the first call and of the whole run) — and from the
backlog (the task's state now). Nothing is sent anywhere and no model is called.

What the numbers mean:
- cost per accepted task = all spend on runs that carry a task id (development, review, testing, failed attempts,
  redone and rejected work) / tasks that are accepted now. With zero accepted tasks there is no ratio: the report shows
  the attempts and the spend instead.
- all-in = the same with the runs that carry no task id (PO, analyst, marketer…) in the numerator.
- cache read share = cache_read / (input + cache_creation + cache_read) input tokens: how much of the input was paid at
  the cached price. "cold first call" = runs whose first model call read nothing from the cache.

Limits: a task counts as accepted by its state at the moment of the report, not at the end of the window; a task whose
runs started before the window has only its in-window spend here.
"""
from __future__ import annotations

import json
import logging
import re
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import config as C
from .store import Store

log = logging.getLogger("team.cost")

DEFAULT_DAYS = 7
ACCEPTED_STATES = ("accepted", "released", "measured", "done")   # the work was taken; `rejected` and the rest were not
TASK_ID_RE = re.compile(r"T-\d{3,}")
P_HIGH = 0.9


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[int(q * (len(s) - 1))]


def _when(run_json: Path, rec: dict[str, Any]) -> datetime:
    f = rec.get("finished")
    if isinstance(f, str):
        try:
            d = datetime.fromisoformat(f)
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return datetime.fromtimestamp(run_json.stat().st_mtime, tz=timezone.utc)


def _usage(events: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(usage of the first model call, usage of the whole run from the result event); None where the stream has none."""
    first = total = None
    try:
        with events.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("type") == "assistant" and first is None:
                    first = (e.get("message") or {}).get("usage")
                elif e.get("type") == "result":
                    total = e.get("usage")
    except OSError as exc:
        log.debug("no events for %s: %s", events, exc)
    return (first if isinstance(first, dict) else None), (total if isinstance(total, dict) else None)


def _runs(p: C.Product, since: datetime, until: datetime) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not p.runs_dir.is_dir():
        return out
    for rj in p.runs_dir.glob("*/*/run.json"):
        try:
            if datetime.fromtimestamp(rj.stat().st_mtime, tz=timezone.utc) < since - timedelta(days=1):
                continue                                    # cheap pre-filter: the file was last written well before the window
            rec = json.loads(rj.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("unreadable run record %s: %s", rj, exc)
            continue
        when = _when(rj, rec)
        if not (since <= when <= until):
            continue
        first, total = _usage(rj.parent / "events.jsonl")
        c = rec.get("cost_usd")
        m = TASK_ID_RE.search(rj.parent.name)
        out.append({"product": p.slug, "role": str(rec.get("role") or "unknown"), "ok": bool(rec.get("ok")),
                    "usd": float(c) if isinstance(c, (int, float)) else None, "task": m.group(0) if m else None,
                    "first": first, "total": total})
    log.debug("%s: %d runs in the window", p.slug, len(out))
    return out


def _tok(u: dict[str, Any] | None, k: str) -> int:
    v = (u or {}).get(k)
    return int(v) if isinstance(v, (int, float)) else 0


def _cache(runs: list[dict[str, Any]]) -> dict[str, Any]:
    with_usage = [r for r in runs if r["total"]]
    inp = sum(_tok(r["total"], "input_tokens") for r in with_usage)
    cc = sum(_tok(r["total"], "cache_creation_input_tokens") for r in with_usage)
    cr = sum(_tok(r["total"], "cache_read_input_tokens") for r in with_usage)
    firsts = [r for r in runs if r["first"]]
    cold = sum(1 for r in firsts if _tok(r["first"], "cache_read_input_tokens") == 0)
    return {"with_usage": len(with_usage), "input_tokens": inp, "cache_creation_tokens": cc, "cache_read_tokens": cr,
            "cache_read_share": cr / (inp + cc + cr) if (inp + cc + cr) else None,
            "first_call_cold_share": cold / len(firsts) if firsts else None}


def collect(st: C.Settings, products: list[C.Product], days: float = DEFAULT_DAYS, now: datetime | None = None) -> dict[str, Any]:
    until = now or datetime.now(timezone.utc)
    since = until - timedelta(days=days)
    log.info("cost: %d product(s), window %s .. %s", len(products), since.isoformat(timespec="minutes"), until.isoformat(timespec="minutes"))
    runs: list[dict[str, Any]] = []
    states: dict[tuple[str, str], str] = {}
    for p in products:
        runs += _runs(p, since, until)
        try:
            for t in Store(p.dir).tasks():
                states[(p.slug, str(t.get("id")))] = str(t.get("state"))
        except Exception as exc:                            # a broken backlog file must not hide the spend
            log.warning("%s: backlog not read: %s", p.slug, exc)

    roles: dict[str, dict[str, Any]] = {}
    for role in sorted({r["role"] for r in runs}):
        rs = [r for r in runs if r["role"] == role]
        costs = [r["usd"] for r in rs if r["usd"] is not None]
        roles[role] = {"runs": len(rs), "failed": sum(1 for r in rs if not r["ok"]), "usd": sum(costs),
                       "median": statistics.median(costs) if costs else None, "p90": _pct(costs, P_HIGH),
                       "unknown_cost": sum(1 for r in rs if r["usd"] is None), **_cache(rs)}

    per_task: dict[tuple[str, str], float] = {}
    for r in runs:
        if r["task"]:
            key = (r["product"], r["task"])
            per_task[key] = per_task.get(key, 0.0) + (r["usd"] or 0.0)
    accepted = {k: v for k, v in per_task.items() if states.get(k) in ACCEPTED_STATES}
    total = sum(r["usd"] or 0.0 for r in runs)
    task_spend = sum(per_task.values())
    by_state: dict[str, dict[str, Any]] = {}
    for k, v in per_task.items():
        s = states.get(k, "unknown")
        b = by_state.setdefault(s, {"tasks": 0, "usd": 0.0})
        b["tasks"] += 1
        b["usd"] += v
    n = len(accepted)
    own = list(accepted.values())
    return {"since": since.isoformat(timespec="minutes"), "until": until.isoformat(timespec="minutes"), "days": days,
            "products": [p.slug for p in products], "runs": len(runs), "failed": sum(1 for r in runs if not r["ok"]),
            "total": total, "unknown_cost": sum(1 for r in runs if r["usd"] is None), "roles": roles,
            "tasks": {"touched": len(per_task), "accepted": n, "task_spend": task_spend, "overhead_spend": total - task_spend,
                      "per_accepted": task_spend / n if n else None, "per_accepted_all_in": total / n if n else None,
                      "accepted_median": statistics.median(own) if own else None, "accepted_p90": _pct(own, P_HIGH),
                      "by_state": by_state},
            **{k: v for k, v in _cache(runs).items() if k in ("cache_read_share", "first_call_cold_share", "with_usage")}}


def _usd(x: float | None) -> str:
    return "—" if x is None else f"${x:.2f}" if x >= 10 else f"${x:.3f}" if x < 1 else f"${x:.2f}"


def _share(x: float | None) -> str:
    return "—" if x is None else f"{100 * x:.0f}%"


def render(r: dict[str, Any]) -> str:
    t = r["tasks"]
    lines = [f"Cost report · {', '.join(r['products'])} · {r['since']} .. {r['until']} ({r['days']:g} days)", "",
             f"runs: {r['runs']} (failed {r['failed']}) · spend: ${r['total']:.2f} at API prices · "
             f"cache read share: {_share(r['cache_read_share'])} · cold first call: {_share(r['first_call_cold_share'])}"]
    if r["unknown_cost"]:
        lines.append(f"cost unknown for {r['unknown_cost']} run(s) (killed or no result event): their spend is not in the sums")
    lines += ["", f"accepted tasks: {t['accepted']} of {t['touched']} touched in the window"]
    if t["accepted"]:
        lines += [f"  per accepted task: {_usd(t['per_accepted'])} (all spend on task runs, failed and rejected work included)",
                  f"  all-in, with PO/analyst/marketing overhead: {_usd(t['per_accepted_all_in'])}",
                  f"  an accepted task's own runs: median {_usd(t['accepted_median'])}, p90 {_usd(t['accepted_p90'])}"]
    else:
        lines.append(f"  no ratio: nothing was accepted. Spend on task runs ${t['task_spend']:.2f}, overhead ${t['overhead_spend']:.2f}")
    if t["by_state"]:
        lines.append("  spend by the task's state now: " + " · ".join(
            f"{s} {b['tasks']} / ${b['usd']:.2f}" for s, b in sorted(t["by_state"].items(), key=lambda kv: -kv[1]["usd"])))
    lines += ["", f"{'role':<16}{'runs':>6}{'fail':>6}{'spend':>10}{'median':>9}{'p90':>9}{'cache':>7}{'cold1':>7}"]
    for role, x in sorted(r["roles"].items(), key=lambda kv: -kv[1]["usd"]):
        lines.append(f"{role:<16}{x['runs']:>6}{x['failed']:>6}{'$%.2f' % x['usd']:>10}{_usd(x['median']):>9}{_usd(x['p90']):>9}"
                     f"{_share(x['cache_read_share']):>7}{_share(x['first_call_cold_share']):>7}")
    lines += ["", "cache = share of input tokens read from the prompt cache; cold1 = runs whose first call read nothing from it."]
    return "\n".join(lines)
