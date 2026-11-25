"""Locks of the cycle entries: one product's cycle must not hold up another's (friction F17, 20.11; SCHEME §3).

Until 20.11 one `flock -n data/cycle.lock` in the cron scripts wrapped the whole `team cycle` process, so the hourly
pass (15 min on the first product alone) kept a product on a 5-minute cadence waiting. Now `team cycle` takes the locks
itself, with flock(2) — the same kernel lock the `flock` tool uses, so the shell scripts and this module see each other:

  data/cycle.lock                   the dispatcher's CODE, reader–writer. Every `team cycle` process holds it SHARED for
                                    its whole life ("cycles are running on this code"); whatever replaces the code holds
                                    it EXCLUSIVE: update-code.sh (deploy), watchdog.sh (rollback) and the self-merge of
                                    the framework product (`code_update` below).
  data/locks/code-update.gate       the writer's turn. flock has no writer preference: with cycles starting every five
                                    minutes an update could wait for ever. An update takes the gate first; a cycle that
                                    cannot pass the gate does not start (exit 75), so the running cycles drain.
  data/locks/hourly-pass.lock       one `team cycle --product all` at a time (the pass also does the framework's hourly
                                    work: rollback check, friction register, retro, critic responses).
  data/locks/product-<slug>.lock    one cycle per product, whichever entry started it. A busy product is skipped.

Order (never the other way round): gate → code lock → hourly pass → product. Nothing here waits without a deadline; the
waits are in team.yaml `locks:`.
"""
from __future__ import annotations

import errno
import fcntl
import logging
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any, Iterator

from .config import Settings

log = logging.getLogger("team.locks")

CODE_LOCK = "cycle.lock"                      # under data/: the name the cron scripts and the owner's deploy have always used
LOCKS_DIR = "locks"                           # under data/
GATE = "code-update.gate"
HOURLY_PASS = "hourly-pass.lock"
PRODUCT = "product-{slug}.lock"
EX_TEMPFAIL = 75                              # exit code of a `team cycle` that started nothing because of a lock (sysexits.h)

DEFAULTS: dict[str, float] = {                # team.yaml `locks:` overrides
    "start_wait_s": 20.0,                     # a starting cycle waits this long for a short exclusive hold (watchdog, plain flock deploy)
    "code_update_wait_s": 1800.0,             # the self-merge waits this long for the other products' running cycles to end
    "restore_wait_s": 300.0,                  # after its own code update a cycle waits this long to hold the code lock shared again
    "canary_state_wait_s": 120.0,             # the canary waits this long for the canary product's own cycle before copying its state
    "poll_s": 1.0,                            # step of every wait above
}

_shared: dict[str, IO[str]] = {}              # this process's shared hold of the code lock, by lock path


def cfg(st: Settings) -> dict[str, float]:
    return {**DEFAULTS, **{k: float(v) for k, v in (st.raw.get("locks") or {}).items() if k in DEFAULTS}}


def locks_dir(st: Settings) -> Path:
    return st.data_dir / LOCKS_DIR


def code_lock_path(st: Settings) -> Path:
    return st.data_dir / CODE_LOCK


def gate_path(st: Settings) -> Path:
    return locks_dir(st) / GATE


def hourly_pass_path(st: Settings) -> Path:
    return locks_dir(st) / HOURLY_PASS


def product_lock_path(st: Settings, slug: str) -> Path:
    return locks_dir(st) / PRODUCT.format(slug=slug)


# ------------------------------------------------------------------ flock primitives

def _open(path: Path) -> IO[str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path.open("a+", encoding="utf-8")


def _try(fh: IO[str], mode: int) -> bool:
    try:
        fcntl.flock(fh, mode | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES):
            return False
        raise


def _wait(fh: IO[str], mode: int, wait_s: float, poll_s: float) -> bool:
    """Try until the deadline (wait_s <= 0: one try). Polling, not a blocking flock: every wait here has a deadline."""
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        if _try(fh, mode):
            return True
        left = deadline - time.monotonic()
        if left <= 0:
            return False
        time.sleep(min(max(poll_s, 0.01), left))


def _mark(fh: IO[str], note: str) -> None:
    """Who holds an exclusive lock, for a human reading the file on the server. The lock itself is the truth."""
    try:
        fh.seek(0)
        fh.truncate()
        fh.write(f"pid={os.getpid()} since={datetime.now(timezone.utc).isoformat(timespec='seconds')} {note}".rstrip() + "\n")
        fh.flush()
    except OSError as exc:
        log.debug("lock note not written: %s", exc)


def _clear(fh: IO[str]) -> None:
    try:
        fh.seek(0)
        fh.truncate()
        fh.flush()
    except OSError:
        pass


def busy(path: Path) -> bool:
    """True while someone holds `path` (shared or exclusive). A look for `team status`, not a way to take the lock."""
    if not path.exists():
        return False
    with path.open("a+", encoding="utf-8") as fh:
        return not _try(fh, fcntl.LOCK_EX)


@contextmanager
def _exclusive(path: Path, note: str, wait_s: float, poll_s: float) -> Iterator[bool]:
    fh = _open(path)
    try:
        if not _wait(fh, fcntl.LOCK_EX, wait_s, poll_s):
            yield False
            return
        _mark(fh, note)
        try:
            yield True
        finally:
            _clear(fh)
    finally:
        fh.close()                             # closing the description releases the lock


# ------------------------------------------------------------------ the locks of `team cycle`

@contextmanager
def cycles(st: Settings) -> Iterator[bool]:
    """The shared hold of data/cycle.lock for one `team cycle` process. Yields False — the caller starts nothing — when a
    code update has announced itself at the gate, or holds the lock for longer than `start_wait_s`."""
    path = code_lock_path(st)
    key = str(path)
    if key in _shared:                         # already held by this process (a nested call): nothing to add
        yield True
        return
    c = cfg(st)
    gate, fh = _open(gate_path(st)), _open(path)
    try:
        ok = _try(gate, fcntl.LOCK_SH) and _wait(fh, fcntl.LOCK_SH, c["start_wait_s"], c["poll_s"])
    finally:
        gate.close()                           # the gate is only passed, never kept by a cycle
    if not ok:
        fh.close()
        log.debug("code lock %s not available: a code update is waiting or running", path)
        yield False
        return
    _shared[key] = fh
    try:
        yield True
    finally:
        _shared.pop(key, None)
        fh.close()


@contextmanager
def code_update(st: Settings, wait_s: float | None = None) -> Iterator[bool]:
    """Exclusive hold of the code lock for a change of the dispatcher's own checkout made from inside a cycle (the
    self-merge). Closes the gate, so no new cycle starts, and waits up to `code_update_wait_s` for the cycles of the
    other processes to end. Yields True when this process is alone; False when the wait ran out — the caller decides.

    The process's own shared hold is given up for the wait (a failed upgrade drops it on Linux anyway, and keeping it
    would deadlock against update-code.sh, which holds the gate and waits for the code lock) and taken back afterwards."""
    c = cfg(st)
    wait_s = c["code_update_wait_s"] if wait_s is None else float(wait_s)
    path = code_lock_path(st)
    own = _shared.get(str(path))
    gate = _open(gate_path(st))
    fh = own if own is not None else _open(path)
    t0 = time.monotonic()
    got = False
    try:
        if own is not None:
            fcntl.flock(own, fcntl.LOCK_UN)
        if _wait(gate, fcntl.LOCK_EX, wait_s, c["poll_s"]):
            _mark(gate, "code update from a cycle")
            got = _wait(fh, fcntl.LOCK_EX, wait_s - (time.monotonic() - t0), c["poll_s"])
        waited = time.monotonic() - t0
        if got:
            log.info("code lock %s held exclusively after %.1fs: no other cycle is running", path, waited)
        else:
            log.warning("code lock %s not exclusive after %.0fs: another cycle or code update is still running", path, waited)
        yield got
    finally:
        if own is not None:
            # back to shared while the gate is still closed, so nothing that respects the gate can slip in between
            if not _wait(own, fcntl.LOCK_SH, c["restore_wait_s"], c["poll_s"]):
                log.warning("code lock %s could not be taken back shared within %.0fs; the cycle goes on without it", path, c["restore_wait_s"])
        else:
            fh.close()
        _clear(gate)
        gate.close()


@contextmanager
def hourly_pass(st: Settings) -> Iterator[bool]:
    """One `team cycle --product all` at a time. Yields False while another pass is running."""
    with _exclusive(hourly_pass_path(st), "hourly pass", 0.0, cfg(st)["poll_s"]) as got:
        yield got


@contextmanager
def product_cycle(st: Settings, slug: str, note: str = "", wait_s: float = 0.0) -> Iterator[bool]:
    """The product's own lock. Yields False when its cycle is already running in another process (after `wait_s`)."""
    with _exclusive(product_lock_path(st, slug), note, wait_s, cfg(st)["poll_s"]) as got:
        yield got


def update_waiting(st: Settings) -> bool:
    """True while a code update has announced itself at the gate (or holds it). A shared, non-blocking look — it never
    makes a starting cycle of another process fail at the gate, as an exclusive probe could."""
    path = gate_path(st)
    if not path.exists():
        return False
    with path.open("a+", encoding="utf-8") as fh:
        return not _try(fh, fcntl.LOCK_SH)


def product_busy(st: Settings, slug: str) -> bool:
    return busy(product_lock_path(st, slug))


@contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Blocking exclusive lock for a short read-modify-write of one shared state file (`<file>.lock` beside it).
    For sections without model runs or network calls only: it waits without a deadline."""
    lp = path.with_name(path.name + ".lock")
    with _open(lp) as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield


def describe(st: Settings) -> dict[str, Any]:
    """What is held right now (for logs and tests): code lock, gate, hourly pass, busy products."""
    d = locks_dir(st)
    prefix, suffix = PRODUCT.split("{slug}")
    slugs = sorted(p.name[len(prefix):-len(suffix)] for p in d.glob(PRODUCT.format(slug="*")) if busy(p)) if d.exists() else []
    return {"code": busy(code_lock_path(st)), "gate": busy(gate_path(st)), "hourly_pass": busy(hourly_pass_path(st)), "products": slugs}
