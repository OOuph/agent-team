"""Stuck goals: after N failed attempts on one goal the PO replans with three alternatives, probes test them, a working
probe becomes the next task, and when none works the goal goes to the owner (the deputy answers first).

Owner 11.11 (Luz T-0117 → T-0129 → T-0130): the developer asked the same OMIE URL seven times and reported «404, not
published yet» every hour; the per-task guards missed it because the PO re-created the goal under new ids and each
partial summary differed only by its timestamp. The common file with the Portuguese column was there all along.

Failures are counted over the chain of tasks that pursue one goal: a task's predecessors are named in `continues`
(set by the PO) or in its title/problem as «replaces T-0129», «retry of …», «instead of …». One replan per chain: a
chain that already went through a replan goes straight to the owner the next time it gets stuck.

Replan states on the stuck task (`task["replan"]["state"]`):
  asked     → the PO must answer in `replans` (diagnosis + 3 alternatives); the task stays blocked
  probing   → three probe tasks run (developer, no commit to the product)
  done      → a probe worked; the PO creates the implementation task with `continues` = this task
  exhausted → no probe worked; a question to the owner is open
  owner     → the owner (or deputy) answered; no further automatic replan on this chain
"""
from __future__ import annotations

import re
from typing import Any

DEFAULTS = {"threshold": 3, "probe_budget_usd": 1.5, "tried_max": 8}
PREDECESSOR_RE = re.compile(r"\b(?:replaces|supersedes|retry of|retries|instead of|re-?opens?|continues|follow-?up (?:of|to))\s+(T-\d{3,5})",
                            re.IGNORECASE)
FAILED_STATUSES = ("partial", "blocked", "error")
TERMINAL = ("done", "rejected", "accepted", "measured")


def settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    return {**DEFAULTS, **dict(raw or {})}


def predecessors(t: dict[str, Any]) -> list[str]:
    out = [str(x) for x in (t.get("continues") or []) if x] if isinstance(t.get("continues"), list) else \
        ([str(t["continues"])] if t.get("continues") else [])
    for text in (t.get("title"), t.get("problem")):
        out += PREDECESSOR_RE.findall(str(text or ""))
    seen, uniq = set(), []
    for x in out:
        if x != t.get("id") and x not in seen:
            seen.add(x)
            uniq.append(x)
    return uniq


def chain(store: Any, t: dict[str, Any], depth: int = 10) -> list[dict[str, Any]]:
    """The task and the tasks it continues, newest first, up to (not including) a task whose replan finished
    (`done`/`owner`): failures before a finished replan were already answered by it."""
    out, seen, todo = [], {t.get("id")}, [t]
    while todo and len(out) < depth:
        cur = todo.pop(0)
        out.append(cur)
        for pid in predecessors(cur):
            if pid in seen:
                continue
            seen.add(pid)
            prev = store.task(pid)
            if prev is None:
                continue
            if (prev.get("replan") or {}).get("state") in ("done", "owner"):
                continue
            todo.append(prev)
    return out


def replanned_before(store: Any, t: dict[str, Any], depth: int = 10) -> bool:
    """True when any task in the full predecessor chain (finished replans included) already went through a replan."""
    seen, todo, n = {t.get("id")}, list(predecessors(t)), 0
    while todo and n < depth:
        pid = todo.pop(0)
        n += 1
        prev = store.task(pid)
        if prev is None:
            continue
        if prev.get("replan"):
            return True
        for x in predecessors(prev):
            if x not in seen:
                seen.add(x)
                todo.append(x)
    return False


def failures(t: dict[str, Any]) -> int:
    """Failed attempts on one task: partial/blocked/error results plus «committed nothing» records."""
    n = sum(1 for r in (t.get("results") or []) if str(r.get("status")) in FAILED_STATUSES)
    n += sum(1 for h in (t.get("history") or []) if "committed nothing" in str(h.get("note") or ""))
    return n


def chain_failures(store: Any, t: dict[str, Any]) -> int:
    return sum(failures(x) for x in chain(store, t))


def tried(store: Any, t: dict[str, Any], limit: int = 8) -> list[str]:
    """What was already tried on this goal, one line per failed attempt, oldest first (the PO's «do not repeat» list)."""
    lines = []
    for x in reversed(chain(store, t)):
        for r in x.get("results") or []:
            if str(r.get("status")) in FAILED_STATUSES or str(r.get("status")) == "done":
                lines.append(f"{x['id']} [{r.get('status')}]: {' '.join(str(r.get('summary') or '').split())[:260]}")
        for h in x.get("history") or []:
            if "committed nothing" in str(h.get("note") or ""):
                lines.append(f"{x['id']} [no commit]: {' '.join(str(h.get('note')).split())[:260]}")
    return lines[-limit:]


def is_probe(t: dict[str, Any]) -> bool:
    return bool(t.get("probe_of"))


def needs_attention(store: Any, t: dict[str, Any], threshold: int) -> str | None:
    """None, or what to do with a developer task about to run: 'replan' (first time stuck) or 'owner' (stuck again
    after a replan on this chain)."""
    if (t.get("role") or "developer") != "developer" or is_probe(t):
        return None
    if (t.get("replan") or {}).get("state"):
        return None                     # already in a replan (or the owner answered): the state machine owns it
    if chain_failures(store, t) < threshold:
        return None
    return "owner" if replanned_before(store, t) else "replan"


def validate(rp: dict[str, Any]) -> list[str]:
    alts = rp.get("alternatives") or []
    problems = []
    if len(alts) != 3:
        problems.append(f"{len(alts)} alternatives instead of 3")
    if not any(a.get("changes_source_or_framing") for a in alts):
        problems.append("no alternative changes the data source or the framing of the task")
    if not str(rp.get("diagnosis") or "").strip():
        problems.append("no diagnosis")
    for i, a in enumerate(alts):
        if not str(a.get("differs_from_tried") or "").strip():
            problems.append(f"alternative {i + 1} does not say how it differs from what was tried")
    return problems


def probe_verdict(res: dict[str, Any] | None) -> bool:
    """A probe works when it reported done and every self-check criterion is met with evidence."""
    if not res or res.get("status") != "done":
        return False
    checks = [c for c in res.get("self_check") or [] if isinstance(c, dict)]
    return bool(checks) and all(c.get("met") and str(c.get("evidence") or "").strip() for c in checks)


def prompt_block(store: Any, cfg: dict[str, Any]) -> str:
    """# STUCK GOALS for the PO prompt: every task in replan state `asked`, with what was tried."""
    stuck = [t for t in store.tasks() if (t.get("replan") or {}).get("state") == "asked"]
    if not stuck:
        return ""
    parts = []
    for t in stuck:
        lines = "\n".join(f"  - {x}" for x in tried(store, t, int(cfg.get("tried_max", 8)))) or "  (no results recorded)"
        parts.append(f"## {t['id']} — {t.get('title')}\nproblem: {' '.join(str(t.get('problem') or '').split())[:600]}\n"
                     f"failed attempts on this goal: {t['replan'].get('failures')}\nalready tried (do not propose these again):\n{lines}")
    return ("# STUCK GOALS — REPLAN REQUIRED (answer each in `replans`)\n"
            "The developer failed on each goal below several times. Do not re-create the same task. For each, write a short "
            "`diagnosis` (why the approach keeps failing: a wrong assumption, a source that changed, a goal that is not "
            "reachable as written) and exactly 3 `alternatives` that differ in substance from everything tried — not a "
            "rewording. At least one must change the data source or the framing of the task (another file, endpoint, "
            "provider, a fallback, or dropping the part that cannot be done). Each gets `probe_check`: one cheap check a "
            "developer can run in one run without committing (a command, a URL to fetch, a value to compare) that answers "
            "«does this approach give what the goal needs». The dispatcher runs the 3 probes; a working one comes back to "
            "you as a question; if none works, the goal goes to the owner.\n\n" + "\n\n".join(parts))
