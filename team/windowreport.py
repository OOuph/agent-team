"""`team window <product> --from … --to … [--vs-from … --vs-to …]` — the same measures for any window, side by side.

28.11.2026, after a night of ten updates on a live team: «how do we compare yesterday and today?» The answer was a new
ad-hoc script every time, each counting something else. This is the one yardstick: tasks accepted and merged, share of
executor runs that ended «partial», minutes the seats of a role stood idle inside the cycles, the load of the server,
the length of a cycle — read from what the dispatcher already writes (events, cycles, spend, data/cron.log,
data/logs/stall-watch.log). Read-only.
"""
from __future__ import annotations

import json
import re
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ------------------------------------------------------------------ parameters
EXEC_ROLES = ("developer", "designer", "game-designer", "analyst", "marketer", "tester")
IDLE_ROLE = "developer"           # the seats whose idle minutes are counted
SIGNAL_RE = re.compile(r"exit (143|137|130|-15|-9)\b")
LANE_END_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),\d+ \w+ \[\d+ (exec-[\w-]+)\] team\.lanes: (\S+): lane \S+ ended after (\d+)s")
LOAD_RE = re.compile(r"^(\S+?)Z? \w+ what=heartbeat.*load=\"([\d.]+) ")
CRON_LOG, LOAD_LOG = "cron.log", "logs/stall-watch.log"
ROWS = (  # key, title, unit, lower is better?
    ("accepted", "Принято задач", "", False), ("merged", "Слито задач", "", False), ("taken", "Взято в работу", "", False),
    ("exec_runs", "Запусков исполнителей", "", None), ("failed_runs", "Из них сбойных", "", True),
    ("partial", "Закончились «partial»", "", True), ("partial_share", "Доля «partial»", "%", True),
    ("signalled", "Запусков, оборванных сигналом", "", True), ("dead_deps", "Событий «мёртвая зависимость»", "", True),
    ("cycles", "Завершённых циклов", "", None), ("cycle_med_min", "Длительность цикла, медиана", "мин", None),
    ("cycle_max_min", "Длительность цикла, максимум", "мин", True),
    ("idle_min", f"Простой мест ({IDLE_ROLE}) внутри циклов", "мин", True), ("idle_share", "Доля простоя мест", "%", True),
    ("merge_to_accept_min", "Слито → принято, медиана", "мин", True),
    ("load_med", "Нагрузка load1 в циклах, медиана", "", True), ("load_p90", "Нагрузка load1, 90-й процентиль", "", True),
    ("load_max", "Нагрузка load1, максимум", "", True), ("spent_usd", "Расход", "$", None),
)


def parse_when(s: str) -> datetime:
    """ISO moment (a bare date-time is UTC)."""
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat(timespec="seconds")


def _log_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def measure(product_dir: Path, data_dir: Path, slug: str, start: datetime, end: datetime, seats: int) -> dict[str, Any]:
    """The measures of one window [start, end) for one product. A measure that cannot be read is None, never a guess."""
    out: dict[str, Any] = {k: None for k, *_ in ROWS}
    db = sqlite3.connect(f"file:{product_dir / 'team.db'}?mode=ro", uri=True)
    try:
        a, b = _iso(start), _iso(end)
        ev = [(t, k, s, act, json.loads(d) if d else None) for t, k, s, act, d in
              db.execute("SELECT ts, kind, subject, actor, detail FROM events WHERE ts >= ? AND ts < ? ORDER BY id", (a, b))]
        tr = [(t, s, act, d) for t, k, s, act, d in ev if k == "task.transition" and isinstance(d, dict)]
        out["accepted"] = sum(1 for t, s, act, d in tr if d.get("to") == "accepted")
        out["merged"] = sum(1 for t, s, act, d in tr if d.get("to") == "released")
        out["taken"] = len({s for t, s, act, d in tr if d.get("to") == "claimed"})
        runs = [(s, d) for t, k, s, act, d in ev if k == "run" and act in EXEC_ROLES and isinstance(d, dict)]
        out["exec_runs"] = len(runs)
        out["failed_runs"] = sum(1 for s, d in runs if d.get("ok") is False)
        out["signalled"] = sum(1 for s, d in runs if SIGNAL_RE.search(str(d.get("error") or "")))
        out["partial"] = sum(1 for t, s, act, d in tr if str(d.get("note") or "").startswith("partial"))
        out["partial_share"] = round(100 * out["partial"] / len(runs), 1) if runs else None
        out["dead_deps"] = sum(1 for t, k, s, act, d in ev if k == "task.dependency_dead")
        first_rel: dict[str, datetime] = {}
        waits = []
        for t, s, act, d in tr:
            if d.get("to") == "released":
                first_rel.setdefault(s, parse_when(t))
            elif d.get("to") == "accepted" and s in first_rel:
                waits.append((parse_when(t) - first_rel.pop(s)).total_seconds() / 60)
        out["merge_to_accept_min"] = round(statistics.median(waits), 1) if waits else None
        cyc = [(c, parse_when(x), parse_when(y)) for c, x, y in
               db.execute("SELECT cycle_id, started, finished FROM cycles WHERE started >= ? AND started < ? AND finished IS NOT NULL AND finished != ''", (a, b))]
        out["cycles"] = len(cyc)
        if cyc:
            mins = [(y - x).total_seconds() / 60 for c, x, y in cyc]
            out["cycle_med_min"], out["cycle_max_min"] = round(statistics.median(mins), 1), round(max(mins), 1)
        out["spent_usd"] = round(float(db.execute("SELECT COALESCE(SUM(usd), 0) FROM spend WHERE ts >= ? AND ts < ?", (a, b)).fetchone()[0] or 0), 1)
    finally:
        db.close()
    # the seats' idle minutes: from the end of a seat's lane to the end of its cycle (a seat that had no lane: the whole cycle)
    log = data_dir / CRON_LOG
    if cyc and log.exists() and seats > 0:
        ends: list[tuple[datetime, str]] = []
        day0, day1 = (start - timedelta(days=1)).strftime("%Y-%m-%d"), (end + timedelta(days=1)).strftime("%Y-%m-%d")
        with log.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not day0 <= line[:10] <= day1:
                    continue
                m = LANE_END_RE.match(line)
                if m and m.group(3) == slug and m.group(2).startswith(f"exec-{IDLE_ROLE}"):
                    ends.append((_log_ts(m.group(1)), m.group(2)))
        idle = total = 0.0
        for c, x, y in cyc:
            last: dict[str, datetime] = {}
            for t, lane in ends:
                if x <= t <= y + timedelta(seconds=5):
                    last[lane] = max(last.get(lane, t), t)
            idle += sum(max(0.0, (y - t).total_seconds()) for t in last.values()) + max(0, seats - len(last)) * (y - x).total_seconds()
            total += seats * (y - x).total_seconds()
        out["idle_min"], out["idle_share"] = round(idle / 60), (round(100 * idle / total, 1) if total else None)
    # the load of the server while a cycle of this product ran
    lf = data_dir / LOAD_LOG
    if cyc and lf.exists():
        vals = []
        with lf.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = LOAD_RE.match(line)
                if not m:
                    continue
                try:
                    t = parse_when(m.group(1))
                except ValueError:
                    continue
                if any(x <= t <= y for c, x, y in cyc):
                    vals.append(float(m.group(2)))
        if vals:
            vals.sort()
            out["load_med"], out["load_max"] = round(statistics.median(vals), 1), round(vals[-1], 1)
            out["load_p90"] = round(vals[min(len(vals) - 1, int(len(vals) * 0.9))], 1)
    return out


def table(now: dict[str, Any], before: dict[str, Any] | None, now_name: str, before_name: str = "") -> str:
    """A Markdown table of one window, or of two side by side with the difference."""
    def f(v: Any, unit: str) -> str:
        return "—" if v is None else f"{v:g}{(' ' + unit) if unit and unit not in ('%', '$') else unit}".replace("$", " $")
    head = f"| Мера | {now_name} |" + (f" {before_name} | Разница |" if before is not None else "")
    lines = [head, "|---|---|" + ("---|---|" if before is not None else "")]
    for key, title, unit, _ in ROWS:
        row = f"| {title} | {f(now.get(key), unit)} |"
        if before is not None:
            x, y = now.get(key), before.get(key)
            diff = "—" if x is None or y is None else f"{x - y:+g}"
            row += f" {f(y, unit)} | {diff} |"
        lines.append(row)
    return "\n".join(lines)
