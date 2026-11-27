"""Drain on request: a running cycle finishes what is running and ends, instead of being waited for (owner 27.11.2026:
«надо разбираться с обновлением фреймворка, это важнее, чем разработка»).

27.11: with refill and chain a cycle of ai-battles lasts 58–80 minutes. Five times that day a code update or an edit of
product.yaml waited 20–60 minutes for it, twice with the gate closed for every product. A request file changes that:

    products/<slug>/DRAIN         this product only (an edit of product.yaml, `team hold`)
    data/locks/update.pending     every product (a code update, `update-code.sh`)

A cycle that sees a request closes refill and retake, starts no new executor task, no new review and no further phase,
does not chain, and ends as soon as the runs already going are done. No new cycle of the product starts while the
request stands. The one who placed the request takes the lock, does the work and removes the request — also on an error;
a request older than `drain.max_min` (team.yaml, 90) is ignored and removed with a WARNING, so a forgotten file cannot
stop the team for good.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings

log = logging.getLogger("team.drain")

# ------------------------------------------------------------------ parameters
DEFAULT_MAX_MIN = 90.0            # team.yaml `drain.max_min` (1..1440): a request older than this is forgotten, with a WARNING
PRODUCT_FILE = "DRAIN"            # in the product's folder
GLOBAL_FILE = "locks/update.pending"      # under the data directory
CACHE_S = 2.0                     # a cycle asks often (every pick of every lane): the files are looked at this rarely


def max_min(st: Settings) -> float:
    raw = (st.raw.get("drain") or {}).get("max_min", DEFAULT_MAX_MIN)
    try:
        v = float(raw)
        if isinstance(raw, bool) or not 1 <= v <= 1440:
            raise ValueError
        return v
    except (TypeError, ValueError):
        log.warning("team.yaml drain.max_min=%r is not a number in 1..1440 — %s in use", raw, DEFAULT_MAX_MIN)
        return DEFAULT_MAX_MIN


def product_file(st: Settings, slug: str) -> Path:
    return st.products_dir / slug / PRODUCT_FILE


def global_file(st: Settings) -> Path:
    return st.data_dir / GLOBAL_FILE


def place(path: Path, by: str, why: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} by {by}: {why}\n".replace("\n", " ").strip() + "\n", encoding="utf-8")
    log.info("drain requested: %s (by %s%s)", path, by, f": {why}" if why else "")
    return path


def clear(path: Path) -> bool:
    try:
        path.unlink()
        log.info("drain request removed: %s", path)
        return True
    except FileNotFoundError:
        return False


def _fresh(path: Path, limit_min: float) -> bool:
    """Does a request stand at `path`? A request past its limit is removed, with a WARNING, and does not count."""
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return False
    except OSError as exc:
        log.warning("drain request %s cannot be read (%s) — ignored", path, exc)
        return False
    if age > limit_min * 60:
        log.warning("drain request %s is %.0f min old, over drain.max_min (%g): forgotten by whoever placed it — removed, work goes on",
                    path, age / 60, limit_min)
        try:
            path.unlink()
        except OSError:
            pass
        return False
    return True


def requested(st: Settings, slug: str) -> str | None:
    """The request that stands for this product (its file path), or None."""
    limit = max_min(st)
    for f in (product_file(st, slug), global_file(st)):
        if _fresh(f, limit):
            return str(f)
    return None


class Watch:
    """`requested` for one cycle, looked up at most every CACHE_S seconds; remembers the moment it first said yes."""

    def __init__(self, st: Settings, slug: str):
        self.st, self.slug = st, slug
        self._at, self._val = 0.0, None
        self.since: float | None = None            # monotonic time the request was first seen by this cycle

    def __call__(self) -> str | None:
        now = time.monotonic()
        if now - self._at >= CACHE_S or self._at == 0.0:
            self._val, self._at = requested(self.st, self.slug), now
            if self._val and self.since is None:
                self.since = now
        return self._val


def hold(st: Settings, slug: str, argv: list[str], by: str, why: str, wait_s: float, runner: Any = None) -> int:
    """`team hold <slug> -- <command>`: place the product's request, take the product's lock as soon as its cycle has
    drained, run the command while holding it, remove the request. Returns the command's exit code; 75 when the lock was
    not free within `wait_s` (nothing was run)."""
    import subprocess
    from . import locks
    f = product_file(st, slug)
    t0 = time.monotonic()
    place(f, by, why or " ".join(argv)[:120])
    try:
        with locks.product_cycle(st, slug, note=f"hold by {by}: {why or ' '.join(argv)[:80]}", wait_s=wait_s) as free:
            waited = time.monotonic() - t0
            if not free:
                log.warning("hold %s: the product lock was not free after %.0fs — nothing was run", slug, waited)
                print(f"hold {slug}: the lock was not free after {waited:.0f}s; nothing was run")
                return 75
            log.info("hold %s: lock taken %.0fs after the request", slug, waited)
            print(f"hold {slug}: lock taken {waited:.0f}s after the request")
            cp = (runner or subprocess.run)(argv, cwd=str(st.root))
            rc = int(getattr(cp, "returncode", 0))
            log.info("hold %s: command ended with %d after %.0fs under the lock", slug, rc, time.monotonic() - t0 - waited)
            return rc
    finally:
        clear(f)
