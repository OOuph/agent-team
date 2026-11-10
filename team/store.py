"""The product store: tasks and questions as YAML files in git, claims and events in SQLite.

Files are the source of truth (anyone can read them, git keeps history); SQLite only holds leases and the event log.
State machine of a task (ТЗ v2 §4.2):
  proposed → ready → claimed → in_progress → review → accepted → released → measured → done
  side states: blocked (question id), waiting_owner, rejected, deferred
Every transition is appended to task["history"] with actor and note: that is the decision history of the pilot.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

log = logging.getLogger("team.store")

TASK_STATES = ("proposed", "ready", "claimed", "in_progress", "review", "accepted", "released", "measured", "done",
               "blocked", "waiting_owner", "rejected", "deferred")
TERMINAL = ("done", "rejected")
TASK_TYPES = ("feature", "defect", "quality", "research", "content", "marketing", "ops")
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
        self.db = sqlite3.connect(str(self.db_path), timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS claims(task_id TEXT PRIMARY KEY, role TEXT, run_id TEXT, lease_until REAL, claimed_at REAL);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, kind TEXT, subject TEXT, actor TEXT,
                                              detail TEXT);
            CREATE TABLE IF NOT EXISTS spend(day TEXT, run_id TEXT, role TEXT, usd REAL, reported INTEGER, ts TEXT);
            CREATE TABLE IF NOT EXISTS cycles(cycle_id TEXT PRIMARY KEY, started TEXT, finished TEXT, phases TEXT, spent_usd REAL);
            CREATE TABLE IF NOT EXISTS posts(day TEXT, channel TEXT, post_id TEXT, url TEXT, task_id TEXT, ts TEXT);
        """)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(posts)")}
        if "text" not in cols:          # 03.11: the marketer's memory ("published in the last 7 days") needs the text that went out
            self.db.execute("ALTER TABLE posts ADD COLUMN text TEXT")
        self.db.commit()

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

    def book_spend(self, run_id: str, role: str, usd: float, reported: bool) -> None:
        self.db.execute("INSERT INTO spend(day, run_id, role, usd, reported, ts) VALUES(?,?,?,?,?,?)",
                        (datetime.now(timezone.utc).strftime("%Y-%m-%d"), run_id, role, float(usd), int(reported), utcnow()))
        self.db.commit()

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

    def save_task(self, t: dict[str, Any]) -> Path:
        t["updated"] = utcnow()
        p = self._path("T", t["id"])
        _write(p, t)
        return p

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

    def add_result(self, task_id: str, result: dict[str, Any]) -> None:
        t = self.task(task_id)
        if t is None:
            return
        t.setdefault("results", []).append(result)
        self.save_task(t)

    # ------------------------------------------------------------ claims (leases)

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

    def heartbeat(self, task_id: str, run_id: str, lease_s: int = 7200) -> None:
        self.db.execute("UPDATE claims SET lease_until=? WHERE task_id=? AND run_id=?", (time.time() + lease_s, task_id, run_id))
        self.db.commit()

    def release(self, task_id: str) -> None:
        self.db.execute("DELETE FROM claims WHERE task_id=?", (task_id,))
        self.db.commit()

    def stale_claims(self) -> list[dict[str, Any]]:
        now = time.time()
        return [{"task_id": r[0], "role": r[1], "run_id": r[2]} for r in
                self.db.execute("SELECT task_id, role, run_id FROM claims WHERE lease_until < ?", (now,))]

    # ------------------------------------------------------------ questions

    def questions(self, states: Iterable[str] | None = None) -> list[dict[str, Any]]:
        qs = self._load_all("questions")
        if states:
            s = set(states)
            qs = [q for q in qs if q.get("state") in s]
        return qs

    def ask(self, *, from_: str, to: str, question: str, task_id: str | None = None, options: list[str] | None = None,
            blocks: list[str] | None = None) -> dict[str, Any]:
        norm = " ".join(question.lower().split())[:300]
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

    def observations(self, status: str | None = None) -> list[dict[str, Any]]:
        obs = self._load_all("observations")
        return [o for o in obs if o.get("status") == status] if status else obs

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

    def update_observation(self, oid: str, **fields: Any) -> None:
        p = self._path("OBS", oid)
        if not p.exists():
            return
        o = yaml.safe_load(p.read_text(encoding="utf-8"))
        o.update(fields)
        _write(p, o)

    def add_review(self, task_id: str, verdict: dict[str, Any], run_id: str) -> dict[str, Any]:
        rid = self._next_id("R", "reviews")
        r = {"id": rid, "task": task_id, "run_id": run_id, "created": utcnow(), **verdict}
        _write(self._path("R", rid), r)
        self.event("review", task_id, "reviewer", {"id": rid, "verdict": verdict.get("verdict")})
        return r

    # ------------------------------------------------------------ posts

    def posts_today(self, channel: str) -> int:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return int(self.db.execute("SELECT COUNT(*) FROM posts WHERE day=? AND channel=?", (day, channel)).fetchone()[0])

    def posts_last_days(self, channel: str, days: int = 7) -> int:
        """Posts on `channel` in the last `days` calendar days, today included (the weekly cap's window)."""
        since = (datetime.now(timezone.utc) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
        return int(self.db.execute("SELECT COUNT(*) FROM posts WHERE day>=? AND channel=?", (since, channel)).fetchone()[0])

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

    def cycle_start(self, cycle_id: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO cycles(cycle_id, started, phases, spent_usd) VALUES(?,?,?,0)", (cycle_id, utcnow(), "[]"))
        self.db.commit()

    def cycle_finish(self, cycle_id: str, phases: list[str], spent: float) -> None:
        self.db.execute("UPDATE cycles SET finished=?, phases=?, spent_usd=? WHERE cycle_id=?",
                        (utcnow(), json.dumps(phases), spent, cycle_id))
        self.db.commit()

    def cycles(self, limit: int = 50) -> list[dict[str, Any]]:
        return [{"cycle_id": r[0], "started": r[1], "finished": r[2], "phases": json.loads(r[3] or "[]"), "spent_usd": r[4]}
                for r in self.db.execute("SELECT cycle_id, started, finished, phases, spent_usd FROM cycles ORDER BY started DESC LIMIT ?", (limit,))]

    def close(self) -> None:
        self.db.close()


def summary(store: Store) -> dict[str, Any]:
    tasks = store.tasks()
    by_state: dict[str, int] = {}
    for t in tasks:
        by_state[t.get("state", "?")] = by_state.get(t.get("state", "?"), 0) + 1
    return {"tasks": len(tasks), "by_state": by_state, "open_questions": len(store.questions(["open"])),
            "new_observations": len(store.observations("new")), "spent_today": round(store.spent_today(), 3),
            "spent_by_day": store.spent_by_day()}
