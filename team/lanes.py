"""Parallel lanes inside one product's cycle (owner 21.11: «the absence of parallel work is a huge blocker»; frictions
F21 and F29 of the ai-battles launch; SCHEME §3 «Параллельные дорожки»).

Until 21.11 a cycle ran every role one after another: on the first game the reviews took 13 minutes, the game designer
8, and only then the developer got its P0 task — 22 minutes of waiting on a server that sat idle. `team/locks.py` already
keeps the PRODUCTS apart (one process and one lock per product); this module is about the threads INSIDE one cycle.

A lane is one thread that does a whole piece of work — prepare, run the role (a `claude -p` subprocess, so a thread is
enough), apply the result:

  review lane    one per released task: reviewer, or the tester's play-test
  execute lane   one per seat of `cycle.execute_slots`: the tasks of that seat one after another, exactly as the
                 sequential loop picked them
  main           the cycle's own thread: collect, the PO's triage, measure, geo, voice, digest

`Cycle.phase_review` starts the review lanes and returns; triage runs beside them; `Cycle.phase_execute` starts the
execute lanes and joins everything. With `cycle.parallel.enabled: false` `submit` calls the function at once in the
caller's thread and no thread exists: the cycle is strictly sequential, as before.

INVARIANTS (what the rest of the code relies on)

 1. Runs at once. At most `max_runs` lanes work at the same time, the main thread's own role run included (`RunGate`).
    Of them at most `review_max_runs` are reviews, so a slot is always left for execution; a waiting execute lane goes
    before a waiting review lane, the main thread before both. A lane holds its slot from start to end: the clone,
    `npm ci`, build and unit tests it does around the run load the host as much as the run does.
 2. One lane per task. A task is in one lane at a time (`working` / `busy`): review never takes a task whose merge or
    deploy is in flight, execute never takes a task under review. So the read-modify-write of a task's own file inside
    its lane needs no further lock. Since 25.11 the mark is also kept in the store (`on_working` → table `working`), so
    a writer of ANOTHER process — `team task move` typed while the cycle runs — sees it and leaves that task alone.
 3. One lane per role. The tasks of one role run one after another (the marketer's per-day post counts, one load test
    at a time); only the roles named in `overlap_roles` may use several seats at once. `cycle.flow.refill`
    (team/flow.py) lets a lane take more tasks in one cycle than the seat has places; it never adds a lane, so the
    work at once stays what this invariant and invariant 1 allow.
 4. The store. Every writing method of `Store` holds `Store.lock` (re-entrant), each thread has its own SQLite
    connection; ids (T-, Q-, OBS-, R-) are taken and written under that lock. Since 25.11 the lock is also a flock on
    products/<slug>/store.lock (`store.StoreLock`): it excludes the writers of other processes too, so `team obs`,
    `team task new|move`, `team ask|answer` need no product lock and wait for a single write, not for the cycle. A result is applied to the store AS ITS
    RUN FINISHES, under that lock — not in a fixed order after all runs: a lane mixes runs with writes (marketer →
    editor → marketer; developer → merge → deploy), and holding results back would hold back merges, deploys and
    leases. Each task's own history stays in order; the numbers of observations, questions and reviews born in
    different lanes follow the order the runs finished in. Code that changes a task another thread may touch (the PO's
    decisions beside running reviews) does its read-modify-write under `Store.lock`.
 5. The repository. The product's clone (data/repos/<slug>) is only touched under `repo_lock`: fetch, creating and
    removing a task's worktree, merge to the base branch, push, and the deploy that follows — one critical section, so
    the last deployed commit is always the last merged one. With `deploy.batch: true` (owner 25.11) the deploy does not
    follow every merge: it is made once per cycle, still under `repo_lock`, on the head of the base branch — so AT THE
    MOMENT OF A DEPLOY the commit that goes live is the last merged one and holds every merge since the previous
    deploy; between two deploys the host is on an older merge, and the tasks merged since carry `deploy_pending`
    (not reviewed, not called deployed) until the batch that takes them. A review copies the clone under the same lock (the copy
    only; tests, install and the run are outside). A role works in its own worktree and never needs the lock.
 6. The live host. `host` is a reader–writer lock: a deploy switches the host exclusively, a play-test or load test
    against the deployed host holds it shared for the tester's run. A waiting deploy holds back new play-tests.
    Local play-test servers on free ports are not affected.
 7. Money. `Cycle.role_run` reserves the run's cap under the budget lock before the run starts and releases it when the
    spend is booked: spent + reserved never exceeds the product's daily cap, whatever starts in parallel.
 8. Lock order (never the other way round): gate slot → repo_lock → host → Store.lock / budget lock. Nothing waits for
    a slot or for `repo_lock` while it holds `Store.lock`.
 9. Errors. An exception in a lane ends that lane only; `join` returns it with the phase it belongs to and the cycle
    records `phase.error`, as it does for a phase that raised.
"""
from __future__ import annotations

import itertools
import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

log = logging.getLogger("team.lanes")

# ------------------------------------------------------------------ parameters (team.yaml `cycle.parallel`, product.yaml `cycle.parallel` overrides)

DEFAULTS: dict[str, Any] = {
    "enabled": True,              # false = strictly sequential cycle (no thread is started)
    "max_runs": 3,                # lanes (role runs with their preparation) at once inside one product's cycle
    "review_max_runs": None,      # of them reviews / play-tests at most; None = max_runs - 1 (at least 1)
    "main_outside": False,        # true = a run of the cycle's own thread (the PO, the deputy, the analyst) takes no run slot and
                                  # waits for none. 27.11: with the seats started early all six slots were taken before the PO
                                  # needed one, and it waited 499–601 s in three cycles; it is one light run
    "review_reserve": 0,          # run slots the execute lanes never take, so a waiting review always finds one (0..max_runs-1).
                                  # 27.11: with refill a seat lane lives as long as the cycle and holds its slot all that time —
                                  # six seats held all six slots and reviews waited up to 56 minutes
    "overlap_roles": [],          # roles whose several seats (execute_slots.<role> > 1) work at once; [] = one task of a role at a time
    "max_merge_conflicts": 2,     # merge conflicts in a row on one task before it is blocked with a question to the PO
    "worktree_keep_h": 72,        # a blocked / deferred / waiting_owner task keeps its checkout this many hours (the branch stays for ever)
    "workdir_keep_h": 24,         # a finished cycle keeps the checkouts of its reviews and play-tests this many hours (the logs stay for ever)
}
MAX_RUNS_CEILING = 8              # whatever the config says: the subscription's rate limits and the host's CPU are shared
LANE_THREADS_MAX = 32             # threads of the pool; lanes beyond `max_runs` wait at the gate, not in the pool's queue
SLOT_WAIT_LOG_S = 5.0             # a lane that waited longer than this for its slot says so at INFO
LANE_MAIN, LANE_EXECUTE, LANE_REVIEW = "main", "execute", "review"
PRIORITY = {LANE_MAIN: 0, LANE_EXECUTE: 1, LANE_REVIEW: 2}      # who gets a freed slot first
THREAD_PREFIX = "lane"

_ctx = threading.local()          # .label — the lane this thread works for ("" on the main thread); .slot — it holds a gate slot


@dataclass(frozen=True)
class Config:
    enabled: bool = True
    max_runs: int = 3
    review_max_runs: int = 2
    review_reserve: int = 0
    main_outside: bool = False
    overlap_roles: tuple[str, ...] = ()
    max_merge_conflicts: int = 2
    worktree_keep_h: float = 72.0
    workdir_keep_h: float = 24.0

    def describe(self) -> str:
        if not self.enabled:
            return "sequential (cycle.parallel.enabled: false)"
        extra = f", several seats at once for {', '.join(self.overlap_roles)}" if self.overlap_roles else ""
        res = f", {self.review_reserve} kept for reviews" if self.review_reserve else ""
        return f"parallel, up to {self.max_runs} runs at once (reviews ≤ {self.review_max_runs}{res}){extra}"


def _int(v: Any, default: int, lo: int, hi: int, where: str) -> int:
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float)) or int(v) != v:
        log.warning("%s=%r is not a whole number — %d in use", where, v, default)
        return default
    if not lo <= int(v) <= hi:
        log.warning("%s=%r is outside %d..%d — clamped", where, v, lo, hi)
    return max(lo, min(hi, int(v)))


def settings(team_cycle: dict[str, Any] | None, product_raw: dict[str, Any] | None = None, slug: str = "-") -> Config:
    """`cycle.parallel` of team.yaml with the product's own `cycle.parallel` on top, validated: a wrong value is logged
    and replaced by the default, never an exception — a typo must not stop the product's cycle."""
    raw = dict(DEFAULTS)
    for where, block in (("team.yaml", (team_cycle or {}).get("parallel")),
                         ("product.yaml", ((product_raw or {}).get("cycle") or {}).get("parallel") if isinstance((product_raw or {}).get("cycle"), dict) else None)):
        if block is None:
            continue
        if isinstance(block, bool):                      # `parallel: false` is the short form of `parallel: {enabled: false}`
            block = {"enabled": block}
        if not isinstance(block, dict):
            log.warning("%s: %s cycle.parallel must be a mapping like {enabled: true, max_runs: 3}, got %r — ignored", slug, where, block)
            continue
        unknown = sorted(set(block) - set(DEFAULTS))
        if unknown:
            log.warning("%s: %s cycle.parallel has unknown key(s) %s — ignored", slug, where, ", ".join(unknown))
        raw.update({k: v for k, v in block.items() if k in DEFAULTS})
    pre = f"{slug}: cycle.parallel."
    enabled = raw["enabled"]
    if not isinstance(enabled, bool):
        log.warning("%senabled=%r is not true/false — %s in use", pre, enabled, DEFAULTS["enabled"])
        enabled = bool(DEFAULTS["enabled"])
    max_runs = _int(raw["max_runs"], int(DEFAULTS["max_runs"]), 1, MAX_RUNS_CEILING, pre + "max_runs")
    review = _int(raw["review_max_runs"], max(1, max_runs - 1), 1, max_runs, pre + "review_max_runs")
    roles = raw["overlap_roles"]
    if not isinstance(roles, (list, tuple)) or not all(isinstance(r, str) for r in roles):
        log.warning("%soverlap_roles=%r is not a list of role names — ignored", pre, roles)
        roles = []
    hours: dict[str, float] = {}
    for key in ("worktree_keep_h", "workdir_keep_h"):
        keep = raw[key]
        if isinstance(keep, bool) or not isinstance(keep, (int, float)) or keep < 0:
            log.warning("%s%s=%r is not a number of hours — %s in use", pre, key, keep, DEFAULTS[key])
            keep = DEFAULTS[key]
        hours[key] = float(keep)
    reserve = _int(raw.get("review_reserve", 0), 0, 0, max(0, max_runs - 1), pre + "review_reserve")
    main_outside = raw.get("main_outside", False)
    if not isinstance(main_outside, bool):
        log.warning("%smain_outside=%r is not true/false — false in use", pre, main_outside)
        main_outside = False
    cfg = Config(enabled=enabled, max_runs=max_runs, review_max_runs=review, review_reserve=reserve, main_outside=main_outside, overlap_roles=tuple(str(r) for r in roles),
                 max_merge_conflicts=_int(raw["max_merge_conflicts"], int(DEFAULTS["max_merge_conflicts"]), 1, 20, pre + "max_merge_conflicts"),
                 worktree_keep_h=hours["worktree_keep_h"], workdir_keep_h=hours["workdir_keep_h"])
    log.debug("%s: lanes: %s", slug, cfg.describe())
    return cfg


# ------------------------------------------------------------------ log lines of parallel lanes

def current_lane() -> str:
    """The lane label of the calling thread, "" on the cycle's main thread."""
    return str(getattr(_ctx, "label", "") or "")


def install_log_lane() -> None:
    """Every log record gets `lane` ("" on the main thread, " <label>" inside a lane) and `lane_tag` ("" / " [<label>]").
    `team.cli.LOG_FORMAT` prints the first after the pid — `INFO [12345 review-T-0042] team.cycle: …` — and the cycle's
    own log the second, so the interleaved lines of parallel lanes in data/cron.log and cycle.log can be told apart; a
    line of the main thread looks as it always did."""
    old = logging.getLogRecordFactory()
    if getattr(old, "_team_lane", False):
        return

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        rec = old(*args, **kwargs)
        lane = current_lane()
        rec.lane = f" {lane}" if lane else ""
        rec.lane_tag = f" [{lane}]" if lane else ""
        return rec

    factory._team_lane = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)


# ------------------------------------------------------------------ the gate: how many lanes work at once

class RunGate:
    """Counting gate with priorities (invariant 1). `slot(kind)` blocks until the lane may work."""

    def __init__(self, max_runs: int, limits: dict[str, int] | None = None):
        self.max_runs = max(1, int(max_runs))
        self.limits = {k: max(1, int(v)) for k, v in (limits or {}).items()}
        self._cv = threading.Condition()
        self._running: dict[str, int] = {}
        self._waiting: list[tuple[int, int, str]] = []
        self._seq = itertools.count()
        self.peak = 0                              # the most lanes that worked at once (for the cycle's summary line)
        self.waited_s = 0.0                        # seconds lanes spent waiting for a slot, in total
        self.max_wait: dict[str, float] = {}       # kind → the longest wait of one lane for its slot (team/firstcycle.py)

    def _may_go(self, ticket: tuple[int, int, str]) -> bool:
        if sum(self._running.values()) >= self.max_runs:
            return False
        for tk in sorted(self._waiting):           # the first waiting ticket whose own kind is not at its limit goes
            if self._running.get(tk[2], 0) < self.limits.get(tk[2], self.max_runs):
                return tk == ticket
        return False

    @contextmanager
    def slot(self, kind: str, label: str = "") -> Iterator[None]:
        ticket = (PRIORITY.get(kind, PRIORITY[LANE_EXECUTE]), next(self._seq), kind)
        t0 = time.monotonic()
        with self._cv:
            self._waiting.append(ticket)
            while not self._may_go(ticket):
                self._cv.wait()
            self._waiting.remove(ticket)
            self._running[kind] = self._running.get(kind, 0) + 1
            now = sum(self._running.values())
            self.peak = max(self.peak, now)
            waited = time.monotonic() - t0
            self.waited_s += waited
            self.max_wait[kind] = max(self.max_wait.get(kind, 0.0), waited)
            self._cv.notify_all()                  # the next ticket in line may be free to go as well
        log.log(logging.INFO if waited >= SLOT_WAIT_LOG_S else logging.DEBUG, "lane %s (%s): slot taken after %.1fs, %d of %d at work",
                label or "-", kind, waited, now, self.max_runs)
        try:
            yield
        finally:
            with self._cv:
                self._running[kind] -= 1
                self._cv.notify_all()

    def yield_slot(self, kind: str, label: str = "") -> bool:
        """The calling lane holds a slot of `kind` and stands between two pieces of work: when another lane of the same
        kind waits for a slot, give the slot up and queue behind it (returns True once the slot is held again). Nobody
        waits = nothing happens (False). `cycle.flow.fair_seats`: a seat lane lives as long as the cycle, and without
        this the seat that was started last would wait for a slot until another lane ENDS."""
        with self._cv:
            if not any(tk[2] == kind for tk in self._waiting):
                return False
            t0 = time.monotonic()
            self._running[kind] -= 1
            ticket = (PRIORITY.get(kind, PRIORITY[LANE_EXECUTE]), next(self._seq), kind)
            self._waiting.append(ticket)
            self._cv.notify_all()
            while not self._may_go(ticket):
                self._cv.wait()
            self._waiting.remove(ticket)
            self._running[kind] = self._running.get(kind, 0) + 1
            waited = time.monotonic() - t0
            self.waited_s += waited
            self._cv.notify_all()
        log.log(logging.INFO if waited >= SLOT_WAIT_LOG_S else logging.DEBUG, "lane %s (%s): gave its slot to a waiting lane, back after %.1fs",
                label or "-", kind, waited)
        return True

    def at_work(self) -> int:
        with self._cv:
            return sum(self._running.values())

    def in_flight(self, kind: str) -> int:
        """Lanes of `kind` that hold a slot or wait for one right now."""
        with self._cv:
            return self._running.get(kind, 0) + sum(1 for tk in self._waiting if tk[2] == kind)


class HostLock:
    """Reader–writer lock of the deployed host (invariant 6): `exclusive` for a deploy, `shared` for a play-test or a
    load test against the live host. Writers first: while a deploy waits, no new play-test starts."""

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._readers = 0
        self._writer = False
        self._writers_waiting = 0

    @contextmanager
    def shared(self, on: bool = True) -> Iterator[None]:
        if not on:                                 # a local play-test server: nothing to share
            yield
            return
        t0 = time.monotonic()
        with self._cv:
            while self._writer or self._writers_waiting:
                self._cv.wait()
            self._readers += 1
        if time.monotonic() - t0 >= SLOT_WAIT_LOG_S:
            log.info("live host: the play-test waited %.0fs for a deploy to finish", time.monotonic() - t0)
        try:
            yield
        finally:
            with self._cv:
                self._readers -= 1
                self._cv.notify_all()

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        t0 = time.monotonic()
        with self._cv:
            self._writers_waiting += 1
            try:
                while self._writer or self._readers:
                    self._cv.wait()
            finally:
                self._writers_waiting -= 1
            self._writer = True
        if time.monotonic() - t0 >= SLOT_WAIT_LOG_S:
            log.info("live host: the deploy waited %.0fs for the running play-test(s) to finish", time.monotonic() - t0)
        try:
            yield
        finally:
            with self._cv:
                self._writer = False
                self._cv.notify_all()


# ------------------------------------------------------------------ the executor

@dataclass
class LaneError:
    phase: str
    label: str
    error: BaseException


@dataclass
class _Lane:
    phase: str
    label: str
    future: Future = field(repr=False)


class Lanes:
    """The lanes of one cycle: `submit` a piece of work, `join` them. See the module text for the invariants."""

    def __init__(self, cfg: Config, slug: str = "-", on_lane_exit: Callable[[], Any] | None = None,
                 on_working: Callable[[str, str | None], Any] | None = None):
        self.cfg, self.slug = cfg, slug
        self.parallel = bool(cfg.enabled)
        self.main_outside = bool(cfg.main_outside)
        limits = {LANE_REVIEW: cfg.review_max_runs}
        if cfg.review_reserve > 0:                 # the execute lanes leave this many slots to the reviews
            limits[LANE_EXECUTE] = max(1, cfg.max_runs - cfg.review_reserve)
        self.gate = RunGate(cfg.max_runs, limits)
        self.repo_lock = threading.RLock()         # invariant 5
        self.host = HostLock()                     # invariant 6
        self._on_lane_exit = on_lane_exit          # e.g. close the thread's SQLite connection
        self._on_working = on_working              # (task id, lane label | None): invariant 2 told to the store, for other processes
        self._pool: ThreadPoolExecutor | None = None
        self._lanes: list[_Lane] = []
        self._working: dict[str, str] = {}         # task id -> lane label (invariant 2)
        self._mu = threading.Lock()
        self.started = 0                           # lanes started in this cycle (threads; 0 in a sequential cycle)

    # -- one lane per task ---------------------------------------------------------------------------------------

    def _tell(self, task_id: str, label: str | None) -> None:
        """Pass a task's mark (label) or its removal (None) on to `on_working`. Never under `_mu`: the callback takes the
        store lock, and code that holds the store lock asks `busy`. A failure is logged, the lane goes on — the mark
        protects a write from OUTSIDE the cycle, the cycle itself relies on `_working`."""
        if self._on_working is None:
            return
        try:
            self._on_working(task_id, label)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: task %s: lane mark %s not passed on: %s", self.slug, task_id, label or "removal", exc)

    def busy(self, task_id: str) -> bool:
        with self._mu:
            return task_id in self._working

    @contextmanager
    def working(self, task_id: str | None) -> Iterator[None]:
        """Mark the task as being worked on by the calling lane for the duration of the block."""
        if not task_id:
            yield
            return
        with self._mu:
            other = self._working.get(task_id)
            if other is not None and other != current_lane():
                raise RuntimeError(f"task {task_id} is already in lane {other or 'main'}")
            nested = other is not None
            self._working[task_id] = current_lane()
        if not nested:
            self._tell(task_id, current_lane() or LANE_MAIN)
        try:
            yield
        finally:
            if not nested:
                with self._mu:
                    self._working.pop(task_id, None)
                self._tell(task_id, None)

    # -- submit and join -------------------------------------------------------------------------------------------

    def submit(self, phase: str, kind: str, label: str, fn: Callable[..., Any], *args: Any, task_id: str | None = None,
               outside: bool = False) -> None:
        """Run `fn(*args)` as a lane of `phase`. Sequential cycle: called here and now, exceptions propagate to the
        caller as they always did. Parallel cycle: a thread; the exception, if any, comes back from `join`."""
        if not self.parallel:
            with self.working(task_id):
                fn(*args)
            return
        if task_id:
            with self._mu:
                if task_id in self._working:
                    raise RuntimeError(f"task {task_id} is already in lane {self._working[task_id]}")
                self._working[task_id] = label     # from now on, not from the moment the thread starts
            self._tell(task_id, label)             # before the thread exists: its removal can only come after this
        with self._mu:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=LANE_THREADS_MAX, thread_name_prefix=f"{THREAD_PREFIX}-{self.slug}")
            self.started += 1
            fut = self._pool.submit(self._run_lane, kind, label, task_id, fn, args, outside)
            self._lanes.append(_Lane(phase, label, fut))
        log.debug("%s: lane %s (%s, phase %s) submitted", self.slug, label, kind, phase)

    def _run_lane(self, kind: str, label: str, task_id: str | None, fn: Callable[..., Any], args: tuple[Any, ...],
                  outside: bool = False) -> None:
        """`outside`: the lane takes no run slot and waits for none (a light piece of work: the review of a document)."""
        _ctx.label, _ctx.slot = label, False
        t0 = time.monotonic()
        try:
            with (nullcontext() if outside else self.gate.slot(kind, label)):
                _ctx.slot = True
                log.info("%s: lane %s started%s", self.slug, label, " (outside the run slots)" if outside else "")
                fn(*args)
        finally:
            _ctx.slot = False
            if task_id:
                with self._mu:
                    self._working.pop(task_id, None)
                self._tell(task_id, None)
            log.info("%s: lane %s ended after %.0fs", self.slug, label, time.monotonic() - t0)
            if self._on_lane_exit is not None:
                try:
                    self._on_lane_exit()
                except Exception as exc:  # noqa: BLE001 — cleanup must not hide the lane's own outcome
                    log.warning("%s: lane %s cleanup failed: %s", self.slug, label, exc)
            _ctx.label = ""

    def yield_slot(self, kind: str) -> bool:
        """`RunGate.yield_slot` for the calling lane (nothing in a sequential cycle or outside a lane's slot)."""
        if not self.parallel or not getattr(_ctx, "slot", False):
            return False
        return self.gate.yield_slot(kind, current_lane() or "")

    def pending(self) -> int:
        """Lanes submitted and not joined yet (finished or not)."""
        with self._mu:
            return len(self._lanes)

    def join(self) -> list[LaneError]:
        """Wait for every submitted lane; returns the lanes that raised, in the order they were submitted. Afterwards
        no lane thread is alive."""
        errors: list[LaneError] = []
        while True:
            with self._mu:
                lanes, self._lanes = self._lanes, []
            if not lanes:
                break
            for ln in lanes:
                try:
                    ln.future.result()
                except BaseException as exc:  # noqa: BLE001 — a lane's failure is reported, not raised here
                    errors.append(LaneError(ln.phase, ln.label, exc))
        with self._mu:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=True)
        return errors

    # -- the main thread's own role run ----------------------------------------------------------------------------

    @contextmanager
    def run_slot(self, label: str = "") -> Iterator[None]:
        """A gate slot for a role run made outside a lane (the PO beside running reviews). Inside a lane the lane's own
        slot covers the run; a sequential cycle has no gate."""
        if not self.parallel or getattr(_ctx, "slot", False):
            yield
            return
        if self.main_outside:                      # `cycle.parallel.main_outside`: beside the lanes, not in their count
            log.debug("%s: run %s goes beside the lanes, outside the run slots (%d of %d at work)", self.slug, label or "main",
                      self.gate.at_work(), self.gate.max_runs)
            yield
            return
        with self.gate.slot(LANE_MAIN, label or "main"):
            _ctx.slot = True
            try:
                yield
            finally:
                _ctx.slot = False
