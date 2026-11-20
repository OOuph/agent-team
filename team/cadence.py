"""Cycle cadence per product (owner 20.11: «в режиме разработки если простой — запускай цикл ранее»; friction F12).

`product.yaml` may carry `cycle: {every_min: N}` (N = 5..60, see config.CYCLE_EVERY_MIN_RANGE). Such a product is still
part of the hourly `team cycle --product all`; in between, `team cycle --due` (cron every 5 min through
run-cycle-fast.sh) starts its cycle as soon as the last finished one is at least N minutes old. A product without the
setting is never picked here: it stays on the hourly cron. The two entries run side by side: each product has its own
lock (team/locks.py), so a due product does not wait for the hourly pass over the others.

The decision is read-only: the `cycles` table of the product's team.db, the STOP files, `paused` and the onboarding
state. Nothing is written until a cycle really starts, so a product that is not due leaves no trace in its store.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Product, Settings
from .store import Store, parse_ts

log = logging.getLogger("team.cadence")

CYCLES_LOOKBACK = 20      # rows of `cycles` read to find the last finished one (unfinished rows of a crashed cycle are skipped)


@dataclass
class Due:
    product: Product
    due: bool
    reason: str                          # why it runs / why it does not, for the log and `team status`
    every_min: int | None = None
    last_finished: datetime | None = None
    wait_min: float = 0.0                # minutes until it becomes due (0 when due or not on a cadence)


def stop_reason(st: Settings, p: Product) -> str | None:
    """Why the product's cycle must not run now, or None. The single rule for `Cycle.stopped()` and for `--due`."""
    for f in (p.stop_file, st.root / "STOP"):
        if f.exists():
            return str(f)
    if p.raw.get("paused"):
        return "product.yaml: paused"
    from . import onboard
    return onboard.not_finished(p)       # owner 20.11: a product in onboarding has no VISION yet; the cycle waits


def last_finished(p: Product) -> datetime | None:
    """Finish time (UTC) of the product's most recent finished cycle, whichever cron entry ran it; None if there is none."""
    if not (p.dir / "team.db").exists():
        return None
    s = Store(p.dir)
    try:
        stamps = [parse_ts(c.get("finished")) for c in s.cycles(CYCLES_LOOKBACK)]
    finally:
        s.close()
    stamps = [t if t.tzinfo else t.replace(tzinfo=timezone.utc) for t in stamps if t]
    return max(stamps) if stamps else None


def has_cycle(p: Product, cycle_id: str) -> bool:
    """True when the product's store already has a cycle with this id (ids are UTC minutes: two entries started in the
    same minute would otherwise write one run directory and one `cycles` row twice)."""
    if not (p.dir / "team.db").exists():
        return False
    s = Store(p.dir)
    try:
        return any(c.get("cycle_id") == cycle_id for c in s.cycles(CYCLES_LOOKBACK))
    finally:
        s.close()


def check(st: Settings, p: Product, now: datetime | None = None) -> Due:
    now = now or datetime.now(timezone.utc)
    every = p.cycle_every_min
    if not every:
        return Due(p, False, "hourly: no cycle.every_min in product.yaml")
    stop = stop_reason(st, p)
    if stop:
        return Due(p, False, f"stopped ({stop})", every)
    last = last_finished(p)
    if last is None:
        return Due(p, True, "no finished cycle yet", every)
    age = (now - last).total_seconds() / 60
    if age >= every:
        return Due(p, True, f"last cycle finished {age:.0f} min ago ≥ {every} min", every, last)
    return Due(p, False, f"last cycle finished {age:.1f} min ago < {every} min", every, last, round(every - age, 1))


def due_products(st: Settings, products: list[Product], now: datetime | None = None) -> list[Product]:
    """Products whose cycle `team cycle --due` starts now. Every decision is logged: DEBUG for hourly products, INFO for
    the ones on a cadence (so data/cron.log shows why a fast cycle did or did not start)."""
    now = now or datetime.now(timezone.utc)
    out: list[Product] = []
    for p in products:
        d = check(st, p, now)
        log.log(logging.INFO if d.every_min else logging.DEBUG, "cadence %s: %s — %s", p.slug, "due" if d.due else "skip", d.reason)
        if d.due:
            out.append(p)
    return out
