"""The product store: tasks and questions as YAML files in git, claims and events in SQLite.

Files are the source of truth (anyone can read them, git keeps history); SQLite only holds leases and the event log.
State machine of a task (ТЗ v2 §4.2):
  proposed → ready → claimed → in_progress → review → accepted → released → measured → done
  side states: blocked (question id), waiting_owner, rejected, deferred
Every transition is appended to task["history"] with actor and note: that is the decision history of the pilot.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from functools import wraps
from typing import Any, Callable, Iterable

import yaml

log = logging.getLogger("team.store")

TASK_STATES = ("proposed", "ready", "claimed", "in_progress", "review", "accepted", "released", "measured", "done",
               "blocked", "waiting_owner", "rejected", "deferred")
TERMINAL = ("done", "rejected")
TASK_TYPES = ("feature", "defect", "quality", "research", "content", "marketing", "ops",
              "milestone", "design", "load")   # owner 20.11: game profile — milestone (outside the hypothesis rule until M2), design (designer / game-designer), load (tester: N clients vs a threshold)
PRIORITIES = ("P0", "P1", "P2", "P3")
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "proposed": ("ready", "rejected", "deferred"),
    "ready": ("claimed", "deferred", "rejected", "blocked", "waiting_owner"),
    "claimed": ("in_progress", "ready"),
    "in_progress": ("review", "released", "blocked", "ready", "waiting_owner"),
    "review": ("accepted", "in_progress", "blocked"),
    "released": ("review", "accepted", "in_progress", "blocked"),
    "accepted": ("released", "measured", "done"),
    "measured": ("done",),
    "blocked": ("ready", "in_progress", "waiting_owner", "rejected", "deferred"),
    "waiting_owner": ("ready", "in_progress", "rejected", "deferred"),
    "deferred": ("ready", "rejected"),
    "done": (), "rejected": (),
}
QUESTION_STATES = ("open", "answered", "expired")
_ID = re.compile(r"^(T|Q|OBS|R)-(\d{4})")


QUESTION_DEDUPE_H = 72


def _iso_hours_ago(h: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat(timespec="seconds")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _dump(obj: dict[str, Any]) -> str:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=110)


SQLITE_TIMEOUT_S = 30          # a writer of another process (CLI, another connection) is waited for this long
STORE_LOCK_FILE = "store.lock"   # products/<slug>/store.lock: the flock half of `Store.lock`
STORE_LOCK_WAIT_LOG_S = 2.0    # a writer that waited longer than this for another process says so at INFO


class StoreLock:
    """`Store.lock`: one writer at a time — among the threads of a process (a re-entrant thread lock, as before) AND
    among processes (flock on products/<slug>/store.lock, owner 25.11). The cycle holds it for single writes and for
    short read-modify-write sections only (team/lanes.py invariants 4 and 8: never across a model run, a slot or the
    repository lock), so a writer of another process — `team obs`, `team task new|move`, `team ask|answer` typed by the
    operator while the product's cycle runs — waits milliseconds for it, not the whole cycle for the product lock.

    One object per store directory per process (`for_dir`): two `Store` instances of one product in one process share
    it, so a write through the second inside a section of the first re-enters instead of deadlocking on the flock."""

    _by_dir: dict[str, "StoreLock"] = {}
    _registry = threading.Lock()

    def __init__(self, path: Path):
        self.path = Path(path)
        self._threads = threading.RLock()
        self._depth = 0                       # how many times the owning thread holds it; guarded by `_threads`
        self._fh: Any = None

    @classmethod
    def for_dir(cls, product_dir: Path) -> "StoreLock":
        key = os.path.realpath(str(product_dir))
        with cls._registry:
            lk = cls._by_dir.get(key)
            if lk is None:
                lk = cls._by_dir[key] = cls(Path(key) / STORE_LOCK_FILE)
            return lk

    def acquire(self) -> bool:
        self._threads.acquire()
        if self._depth == 0:
            try:
                t0 = time.monotonic()
                if self._fh is None:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    self._fh = self.path.open("a+", encoding="utf-8")
                fcntl.flock(self._fh, fcntl.LOCK_EX)          # blocking: every holder is a short section without model runs
                waited = time.monotonic() - t0
                if waited >= STORE_LOCK_WAIT_LOG_S:
                    log.info("store lock %s: waited %.1fs for a writer of another process", self.path, waited)
            except BaseException:
                self._threads.release()
                raise
        self._depth += 1
        return True

    def release(self) -> None:
        self._depth -= 1
        if self._depth == 0 and self._fh is not None:
            try:
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            except OSError as exc:                            # the description is closed below: the kernel drops the lock with it
                log.warning("store lock %s: unlock failed (%s); closing the file", self.path, exc)
                try:
                    self._fh.close()
                finally:
                    self._fh = None
        self._threads.release()

    def __enter__(self) -> "StoreLock":
        self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


def _locked(fn: Callable[..., Any]) -> Callable[..., Any]:
    """The method runs under `Store.lock`: one writer at a time, among the lanes of a cycle (team/lanes.py) and among
    processes (the operator's CLI beside a running cycle)."""
    @wraps(fn)
    def inner(self: "Store", *args: Any, **kwargs: Any) -> Any:
        with self.lock:
            return fn(self, *args, **kwargs)
    return inner


def _write(p: Path, obj: dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(_dump(obj), encoding="utf-8")
    tmp.replace(p)


class Store:
    """One product's store rooted at products/<slug>/."""

    def __init__(self, product_dir: Path, db_path: Path | None = None):
        self.dir = Path(product_dir)
        for sub in ("backlog", "questions", "observations", "reviews", "digests", "metrics"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        self.db_path = db_path or (self.dir / "team.db")
        # Thread safety (parallel lanes of one cycle, team/lanes.py invariant 4): every writing method holds `lock`
        # (re-entrant: ask → transition → event), so taking an id and writing its file is one step and two lanes never
        # interleave inside a method; every thread has its own SQLite connection (`db`), so a lane may also read with
        # `store.db.execute(...)` directly. Code that reads a task, changes it and saves it while another thread may
        # touch the same task wraps the three steps in `with store.lock:`.
        # Since 25.11 the same lock also excludes the writers of OTHER processes (`StoreLock`: flock on store.lock).
        self.lock = StoreLock.for_dir(self.dir)
        self._local = threading.local()
        self._conns: list[sqlite3.Connection] = []
        self._conn_lock = threading.Lock()
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS claims(task_id TEXT PRIMARY KEY, role TEXT, run_id TEXT, lease_until REAL, claimed_at REAL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, subject TEXT, actor TEXT,
                                              detail TEXT);
            CREATE TABLE IF NOT EXISTS spend(day TEXT, run_id TEXT, role TEXT, usd REAL, reported INTEGER, ts TEXT);
            CREATE TABLE IF NOT EXISTS cycles(cycle_id TEXT PRIMARY KEY, started TEXT, finished TEXT, phases TEXT, spent_usd REAL);
            CREATE TABLE IF NOT EXISTS posts(day TEXT, channel TEXT, post_id TEXT, url TEXT, task_id TEXT, ts TEXT);
            CREATE TABLE IF NOT EXISTS working(task_id TEXT PRIMARY KEY, lane TEXT, pid INTEGER, since TEXT);
        """)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(posts)")}
        if "text" not in cols:          # 03.11: the marketer's memory ("published in the last 7 days") needs the text that went out
            self.db.execute("ALTER TABLE posts ADD COLUMN text TEXT")
        self.db.commit()

    @property
    def db(self) -> sqlite3.Connection:
        """The calling thread's own connection to team.db (opened on first use, closed by `close` / `close_thread`)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            # check_same_thread=False only so that `close()` may close it from the main thread; it is never shared
            conn = sqlite3.connect(str(self.db_path), timeout=SQLITE_TIMEOUT_S, check_same_thread=False)
            with self._conn_lock:
                self._conns.append(conn)
            self._local.conn = conn
            log.debug("store %s: SQLite connection #%d for thread %s", self.dir.name, len(self._conns), threading.current_thread().name)
        return conn

    # ------------------------------------------------------------ ids and files

    def _next_id(self, prefix: str, sub: str) -> str:
        n = 0
        for p in (self.dir / sub).glob(f"{prefix}-*.yaml"):
            m = _ID.match(p.name)
            if m:
                n = max(n, int(m.group(2)))
        return f"{prefix}-{n + 1:04d}"

    def _path(self, kind: str, id_: str) -> Path:
        sub = {"T": "backlog", "Q": "questions", "OBS": "observations", "R": "reviews"}[kind]
        return self.dir / sub / f"{id_}.yaml"

    def _load_all(self, sub: str) -> list[dict[str, Any]]:
        out = []
        for p in sorted((self.dir / sub).glob("*.yaml")):
            try:
                d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                if isinstance(d, dict) and d.get("id"):
                    out.append(d)
            except yaml.YAMLError:
                log.warning("unreadable %s", p)
        return out

    # ------------------------------------------------------------ events

    @_locked
    def event(self, kind: str, subject: str, actor: str, detail: dict[str, Any] | str | None = None) -> None:
        self.db.execute("INSERT INTO events(ts, kind, subject, actor, detail) VALUES(?,?,?,?,?)",
                        (utcnow(), kind, subject, actor, json.dumps(detail, ensure_ascii=False, default=str) if detail is not None else None))
        self.db.commit()
        log.info("event %s %s by %s %s", kind, subject, actor, (json.dumps(detail, ensure_ascii=False, default=str)[:200] if detail else ""))

    def events(self, since: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        q = "SELECT ts, kind, subject, actor, detail FROM events" + (" WHERE ts >= ?" if since else "") + " ORDER BY id DESC LIMIT ?"
        rows = self.db.execute(q, ((since, limit) if since else (limit,))).fetchall()
        return [{"ts": r[0], "kind": r[1], "subject": r[2], "actor": r[3], "detail": json.loads(r[4]) if r[4] else None} for r in rows]

    # ------------------------------------------------------------ spend

    @_locked
    def book_spend(self, run_id: str, role: str, usd: float, reported: bool) -> None:
        self.db.execute("INSERT INTO spend(day, run_id, role, usd, reported, ts) VALUES(?,?,?,?,?,?)",
                        (datetime.now(timezone.utc).strftime("%Y-%m-%d"), run_id, role, float(usd), int(reported), utcnow()))
        self.db.commit()

    def spent_by_cycle(self, cycle_id: str) -> float:
        """What the runs of one cycle spent: the rows of `spend` whose run id is `<cycle_id>-<label>`, whatever day they
        were booked on. A cycle that crosses midnight UTC has runs on both days (27.11: the difference of two
        `spent_today()` readings gave «$-345.13» for ai-battles 20261126T2350)."""
        esc = str(cycle_id).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        row = self.db.execute("SELECT COALESCE(SUM(usd), 0) FROM spend WHERE run_id LIKE ? ESCAPE '\\'", (esc + "-%",)).fetchone()
        return round(float(row[0] or 0.0), 6)

    def spent_today(self) -> float:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        r = self.db.execute("SELECT COALESCE(SUM(usd),0) FROM spend WHERE day=?", (day,)).fetchone()
        return float(r[0] or 0.0)

    def spent_by_day(self) -> dict[str, float]:
        return {r[0]: float(r[1]) for r in self.db.execute("SELECT day, SUM(usd) FROM spend GROUP BY day ORDER BY day")}

    # ------------------------------------------------------------ tasks

    def tasks(self, states: Iterable[str] | None = None) -> list[dict[str, Any]]:
        ts = self._load_all("backlog")
        if states:
            s = set(states)
            ts = [t for t in ts if t.get("state") in s]
        return ts

    def task(self, task_id: str) -> dict[str, Any] | None:
        p = self._path("T", task_id)
        return (yaml.safe_load(p.read_text(encoding="utf-8")) or None) if p.exists() else None

    @_locked
    def save_task(self, t: dict[str, Any]) -> Path:
        t["updated"] = utcnow()
        p = self._path("T", t["id"])
        _write(p, t)
        return p

    @_locked
    def create_task(self, *, title: str, type_: str, problem: str, scope: str, acceptance: list[str], goal: str,
                    created_by: str, priority: str = "P2", size: str = "S", negative_cases: list[str] | None = None,
                    observations: list[str] | None = None, role: str = "developer", state: str = "proposed",
                    budget_usd: float | None = None, links: list[str] | None = None, note: str = "", not_before: str | None = None,
                    plan: bool = False, depends_on: list[str] | None = None) -> dict[str, Any]:
        tid = self._next_id("T", "backlog")
        t = {"id": tid, "title": title[:140], "type": type_ if type_ in TASK_TYPES else "feature", "state": state,
             "priority": priority if priority in PRIORITIES else "P2", "size": size, "goal": goal, "role": role,
             "plan": bool(plan),            # marketer: write products/<slug>/marketing-plan.md instead of posts (accepted without a post)
             "problem": problem, "scope": scope, "acceptance": list(acceptance or []), "negative_cases": list(negative_cases or []),
             "observations": list(observations or []), "links": list(links or []), "budget_usd": budget_usd,
             "assignee_run": None, "review_fails": 0, "results": [], "created": utcnow(), "created_by": created_by, "not_before": not_before,
             "depends_on": list(depends_on or []),
             "history": [{"ts": utcnow(), "actor": created_by, "from": None, "to": state, "note": note[:500]}]}
        self.save_task(t)
        self.event("task.create", tid, created_by, {"title": t["title"], "state": state, "priority": t["priority"]})
        return t

    @_locked
    def transition(self, task_id: str, to: str, actor: str, note: str = "", force: bool = False, **fields: Any) -> dict[str, Any]:
        t = self.task(task_id)
        if t is None:
            raise KeyError(task_id)
        frm = t.get("state")
        if not force and to not in TRANSITIONS.get(frm, ()):
            raise ValueError(f"{task_id}: {frm} → {to} is not allowed")
        t["state"] = to
        for k, v in fields.items():
            t[k] = v
        t.setdefault("history", []).append({"ts": utcnow(), "actor": actor, "from": frm, "to": to, "note": note[:500]})
        self.save_task(t)
        self.event("task.transition", task_id, actor, {"from": frm, "to": to, "note": note[:200]})
        if to in TERMINAL or to in ("ready", "blocked", "waiting_owner", "deferred"):
            self.release(task_id)
        return t

    @_locked
    def add_result(self, task_id: str, result: dict[str, Any]) -> None:
        t = self.task(task_id)
        if t is None:
            return
        t.setdefault("results", []).append(result)
        self.save_task(t)

    # ------------------------------------------------------------ claims (leases)

    @_locked
    def claim(self, task_id: str, role: str, run_id: str, lease_s: int = 7200) -> bool:
        """Atomic: only one live lease per task. Returns False when another run holds it (never retry the same cycle)."""
        now = time.time()
        cur = self.db.execute("SELECT run_id, lease_until FROM claims WHERE task_id=?", (task_id,)).fetchone()
        if cur and cur[1] > now and cur[0] != run_id:
            return False
        self.db.execute("INSERT OR REPLACE INTO claims(task_id, role, run_id, lease_until, claimed_at) VALUES(?,?,?,?,?)",
                        (task_id, role, run_id, now + lease_s, now))
        self.db.commit()
        return True

    @_locked
    def heartbeat(self, task_id: str, run_id: str, lease_s: int = 7200) -> None:
        self.db.execute("UPDATE claims SET lease_until=? WHERE task_id=? AND run_id=?", (time.time() + lease_s, task_id, run_id))
        self.db.commit()

    @_locked
    def release(self, task_id: str) -> None:
        self.db.execute("DELETE FROM claims WHERE task_id=?", (task_id,))
        self.db.commit()

    def stale_claims(self) -> list[dict[str, Any]]:
        now = time.time()
        return [{"task_id": r[0], "role": r[1], "run_id": r[2]} for r in
                self.db.execute("SELECT task_id, role, run_id FROM claims WHERE lease_until < ?", (now,))]

    # ------------------------------------------------------------ tasks in a lane of the running cycle

    # team/lanes.py invariant 2 («one lane per task») made visible to other processes: a lane keeps its own copy of the
    # task for the length of its run and writes it back, so a `team task move` of that task from outside would be lost
    # or would break the lane. The cycle marks the task here while a lane works on it; the CLI looks before it moves.

    @_locked
    def mark_working(self, task_id: str, lane: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO working(task_id, lane, pid, since) VALUES(?,?,?,?)", (task_id, lane or "main", os.getpid(), utcnow()))
        self.db.commit()

    @_locked
    def unmark_working(self, task_id: str) -> None:
        self.db.execute("DELETE FROM working WHERE task_id=?", (task_id,))
        self.db.commit()

    @_locked
    def clear_working(self) -> int:
        """Forget every mark: called by a starting cycle, which holds the product lock — whatever is marked then was
        left by a cycle that died. Returns how many marks there were."""
        n = int(self.db.execute("SELECT COUNT(*) FROM working").fetchone()[0])
        if n:
            self.db.execute("DELETE FROM working")
            self.db.commit()
            log.warning("store %s: %d task mark(s) of a cycle that did not finish were cleared", self.dir.name, n)
        return n

    def working_lane(self, task_id: str) -> str | None:
        """The lane of the running cycle that works on the task right now, or None."""
        r = self.db.execute("SELECT lane FROM working WHERE task_id=?", (task_id,)).fetchone()
        return str(r[0]) if r else None

    # ------------------------------------------------------------ questions

    def questions(self, states: Iterable[str] | None = None) -> list[dict[str, Any]]:
        qs = self._load_all("questions")
        if states:
            s = set(states)
            qs = [q for q in qs if q.get("state") in s]
        return qs

    @_locked
    def ask(self, *, from_: str, to: str, question: str, task_id: str | None = None, options: list[str] | None = None,
            blocks: list[str] | None = None) -> dict[str, Any]:
        norm = " ".join(question.lower().split())[:300]
        if task_id:
            # T-0091: a repeated block of the same task links to its still-open question instead of opening a second one
            for prev in self.questions(["open"]):
                if prev.get("to") == to and task_id in (prev.get("blocks") or []):
                    self.event("question.repeat", prev["id"], from_, {"to": to, "question": question[:200], "task": task_id})
                    try:
                        self.transition(task_id, "waiting_owner" if to == "owner" else "blocked", from_, f"question {prev['id']} (repeat)", blocked_by=prev["id"])
                    except ValueError:
                        pass
                    return prev
        for prev in self.questions(["open", "answered"]):
            if prev.get("to") == to and " ".join(str(prev.get("question", "")).lower().split())[:300] == norm \
                    and str(prev.get("created") or "") >= _iso_hours_ago(QUESTION_DEDUPE_H):
                self.event("question.repeat", prev["id"], from_, {"to": to, "question": question[:200]})
                if task_id and prev.get("state") == "open":
                    try:
                        self.transition(task_id, "waiting_owner" if to == "owner" else "blocked", from_, f"question {prev['id']} (repeat)", blocked_by=prev["id"])
                    except ValueError:
                        pass
                return prev
        qid = self._next_id("Q", "questions")
        q = {"id": qid, "from": from_, "to": to, "task": task_id, "question": question[:1500], "options": list(options or []),
             "blocks": list(blocks or ([task_id] if task_id else [])), "state": "open", "answer": None, "answered_by": None,
             "created": utcnow(), "answered": None}
        _write(self._path("Q", qid), q)
        self.event("question.open", qid, from_, {"to": to, "task": task_id, "question": question[:200]})
        if task_id:
            try:
                self.transition(task_id, "waiting_owner" if to == "owner" else "blocked", from_, f"question {qid}", blocked_by=qid)
            except ValueError:
                pass
        return q

    @_locked
    def mark_question(self, qid: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Set fields of a question (e.g. `told`: when its line went to the owner's channel)."""
        p = self._path("Q", qid)
        q = yaml.safe_load(p.read_text(encoding="utf-8"))
        q.update(fields)
        _write(p, q)
        return q

    @_locked
    def hold_task(self, task_id: str, by: str, note: str = "") -> dict[str, Any]:
        """The operator takes a task (`team task <product> take`): `held_by`, a line in its history, an event. The state
        does not change; with `cycle.escalation.operator_hold` the cycle leaves a held task alone."""
        t = self.task(task_id)
        if t is None:
            raise KeyError(task_id)
        t.update({"held_by": by, "held_at": utcnow(), "held_note": note[:300]})
        t.setdefault("history", []).append({"ts": utcnow(), "actor": by, "from": t.get("state"), "to": t.get("state"),
                                            "note": f"taken by {by}: the cycle leaves it alone" + (f" — {note[:200]}" if note else "")})
        self.save_task(t)
        self.event("task.held", task_id, by, {"note": note[:200], "state": t.get("state")})
        return t

    @_locked
    def give_task(self, task_id: str, by: str, note: str = "") -> dict[str, Any]:
        """The operator gives a taken task back (`team task <product> give`)."""
        t = self.task(task_id)
        if t is None:
            raise KeyError(task_id)
        was = t.pop("held_by", None)
        t.pop("held_at", None); t.pop("held_note", None)
        t.setdefault("history", []).append({"ts": utcnow(), "actor": by, "from": t.get("state"), "to": t.get("state"),
                                            "note": f"given back by {by} (was held by {was or 'nobody'})" + (f" — {note[:200]}" if note else "")})
        self.save_task(t)
        self.event("task.given", task_id, by, {"note": note[:200], "state": t.get("state"), "was_held_by": was})
        return t

    @_locked
    def answer(self, qid: str, answer: str, by: str, not_before: str | None = None) -> dict[str, Any]:
        """Answer a question; blocked tasks return to ready. `not_before` (ISO UTC) keeps them from restarting before
        that moment — the fix for 'dated post → blocked → answered → restarted an hour later → blocked again'."""
        p = self._path("Q", qid)
        q = yaml.safe_load(p.read_text(encoding="utf-8"))
        q.update({"state": "answered", "answer": answer[:3000], "answered_by": by, "answered": utcnow()})
        _write(p, q)
        self.event("question.answer", qid, by, {"answer": answer[:200], "not_before": not_before})
        norm = " ".join(str(q.get("question", "")).lower().split())[:300]
        for tid in q.get("blocks") or []:
            t = self.task(tid)
            if t and t.get("state") in ("blocked", "waiting_owner"):
                same = [o for o in self.questions() if tid in (o.get("blocks") or [])
                        and " ".join(str(o.get("question", "")).lower().split())[:300] == norm]
                if by != "owner" and not not_before and len(same) >= 2:
                    # blocked twice on the same question: an automatic answer does not restart it a third time
                    self.event("task.restart_suppressed", tid, by, {"question": qid, "times": len(same)})
                    continue
                fields = {"blocked_by": None}
                if not_before:
                    fields["not_before"] = not_before
                self.transition(tid, "ready", by, f"answered {qid}" + (f"; not before {not_before}" if not_before else ""), **fields)
        return q

    # ------------------------------------------------------------ observations and reviews

    @_locked
    def mark_question(self, qid: str, fields: dict[str, Any]) -> dict[str, Any]:
        """Write extra fields on a question without answering it (the deputy's «this one is the owner's»)."""
        p = self._path("Q", qid)
        q = yaml.safe_load(p.read_text(encoding="utf-8"))
        q.update(fields)
        _write(p, q)
        return q

    def observations(self, status: str | None = None) -> list[dict[str, Any]]:
        obs = self._load_all("observations")
        return [o for o in obs if o.get("status") == status] if status else obs

    @_locked
    def add_observation(self, card: dict[str, Any]) -> dict[str, Any]:
        """Dedup by fingerprint: a repeat of a live card is appended to it, not duplicated."""
        fp = card.get("fingerprint")
        if fp:
            for o in self.observations():
                if o.get("fingerprint") == fp and o.get("status") != "archived":
                    o.setdefault("repeats", []).append({"observed_at": utcnow(), "run_id": card.get("run_id")})
                    o["repeat_count"] = len(o["repeats"])
                    o["last_seen"] = utcnow()
                    _write(self._path("OBS", o["id"]), o)
                    return o
        oid = self._next_id("OBS", "observations")
        card = {"id": oid, "status": "new", "observed_at": utcnow(), **card}
        _write(self._path("OBS", oid), card)
        self.event("observation.new", oid, str(card.get("source", "?")), {"title": card.get("title", "")[:140],
                                                                          "classification": card.get("classification")})
        return card

    @_locked
    def update_observation(self, oid: str, **fields: Any) -> None:
        p = self._path("OBS", oid)
        if not p.exists():
            return
        o = yaml.safe_load(p.read_text(encoding="utf-8"))
        o.update(fields)
        _write(p, o)

    @_locked
    def add_review(self, task_id: str, verdict: dict[str, Any], run_id: str) -> dict[str, Any]:
        rid = self._next_id("R", "reviews")
        r = {"id": rid, "task": task_id, "run_id": run_id, "created": utcnow(), **verdict}
        _write(self._path("R", rid), r)
        self.event("review", task_id, "reviewer", {"id": rid, "verdict": verdict.get("verdict"),
                                                   **({"reason": verdict["inconclusive_reason"]} if verdict.get("inconclusive_reason") else
                                                      {"reason": verdict["fail_reason"]} if verdict.get("fail_reason") else {})})
        return r

    # ------------------------------------------------------------ posts

    def posts_today(self, channel: str) -> int:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return int(self.db.execute("SELECT COUNT(*) FROM posts WHERE day=? AND channel=?", (day, channel)).fetchone()[0])

    def posts_last_days(self, channel: str, days: int = 7) -> int:
        """Posts on `channel` in the last `days` calendar days, today included (the weekly cap's window)."""
        since = (datetime.now(timezone.utc) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        return int(self.db.execute("SELECT COUNT(*) FROM posts WHERE day>=? AND channel=?", (since, channel)).fetchone()[0])

    @_locked
    def record_post(self, channel: str, post_id: str, url: str, task_id: str | None, text: str | None = None) -> None:
        self.db.execute("INSERT INTO posts(day, channel, post_id, url, task_id, ts, text) VALUES(?,?,?,?,?,?,?)",
                        (datetime.now(timezone.utc).strftime("%Y-%m-%d"), channel, post_id, url, task_id, utcnow(), (text or "")[:4000]))
        self.db.commit()
        self.event("post", channel, "marketer", {"post_id": post_id, "url": url, "task": task_id})

    def recent_posts(self, days: int = 7, text_chars: int = 120) -> list[dict[str, Any]]:
        """What went out in the last `days` days, newest first: day, channel, url, task, the first `text_chars` of the text.
        The marketer's memory: it reads this before writing so it does not repeat itself; the PO reads it to cut the plan."""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = self.db.execute("SELECT day, channel, url, task_id, ts, text FROM posts WHERE day>=? ORDER BY ts DESC, rowid DESC", (since,))
        return [{"day": r[0], "channel": r[1], "url": r[2], "task": r[3], "ts": r[4], "text": " ".join(str(r[5] or "").split())[:text_chars]}
                for r in rows]

    def first_post_day(self, channel: str) -> str | None:
        r = self.db.execute("SELECT MIN(day) FROM posts WHERE channel=?", (channel,)).fetchone()
        return r[0] if r and r[0] else None

    # ------------------------------------------------------------ cycles

    @_locked
    def cycle_start(self, cycle_id: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO cycles(cycle_id, started, phases, spent_usd) VALUES(?,?,?,0)", (cycle_id, utcnow(), "[]"))
        self.db.commit()

    @_locked
    def cycle_finish(self, cycle_id: str, phases: list[str], spent: float) -> None:
        self.db.execute("UPDATE cycles SET finished=?, phases=?, spent_usd=? WHERE cycle_id=?",
                        (utcnow(), json.dumps(phases), spent, cycle_id))
        self.db.commit()

    def cycles(self, limit: int = 50) -> list[dict[str, Any]]:
        return [{"cycle_id": r[0], "started": r[1], "finished": r[2], "phases": json.loads(r[3] or "[]"), "spent_usd": r[4]}
                for r in self.db.execute("SELECT cycle_id, started, finished, phases, spent_usd FROM cycles ORDER BY started DESC LIMIT ?", (limit,))]

    def close_thread(self) -> None:
        """Close the calling thread's connection (a lane's thread ends; the store itself stays open)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            return
        self._local.conn = None
        with self._conn_lock:
            if conn in self._conns:
                self._conns.remove(conn)
        conn.close()

    def close(self) -> None:
        """Close the connections of every thread."""
        with self._conn_lock:
            conns, self._conns = self._conns, []
        for conn in conns:
            conn.close()
        self._local = threading.local()


def summary(store: Store) -> dict[str, Any]:
    tasks = store.tasks()
    by_state: dict[str, int] = {}
    for t in tasks:
        by_state[t.get("state", "?")] = by_state.get(t.get("state", "?"), 0) + 1
    return {"tasks": len(tasks), "by_state": by_state, "open_questions": len(store.questions(["open"])),
            "new_observations": len(store.observations("new")), "spent_today": round(store.spent_today(), 3),
            "spent_by_day": store.spent_by_day()}
