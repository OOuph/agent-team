"""Deputy owner: answers the questions roles address to the owner while the owner is away.

Why (owner 03.11, before a week unreachable: "в дороге я ничего не вижу, буду недоступен, надо автономию"): a question
to the owner puts its task into `waiting_owner`, and with nobody answering the task sits there for the whole pilot.
The deputy reads DEPUTY.md (the owner's written mandate: what may be decided in his absence, what is simply unavailable
until he is back, and the known answers), the product VISION and the question, and answers as the owner would — or
says "unavailable until <date>, proceed without it" so the PO closes or re-plans the task instead of waiting.

It never touches secrets, money, channels, accounts or VISION goals: those answers are always "unavailable", by mandate.
Runs inside phase_triage before the PO, for every open owner question older than ``deputy.after_h`` (team.yaml).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Product, Settings
from .store import Store, parse_ts

log = logging.getLogger("team.deputy")

MANDATE_FILE = "DEPUTY.md"
DEFAULT_AFTER_H = 1.0
RUN_CAP_USD = 1.0
MAX_TURNS = 4
MAX_QUESTIONS = 8


def settings(st: Settings) -> dict[str, Any]:
    return dict(st.raw.get("deputy") or {})


def enabled(st: Settings) -> bool:
    return bool(settings(st).get("enabled", False)) and (st.root / MANDATE_FILE).exists()


def mandate(st: Settings) -> str:
    p = st.root / MANDATE_FILE
    return p.read_text(encoding="utf-8") if p.exists() else ""


def due_questions(st: Settings, store: Store, now: datetime | None = None) -> list[dict[str, Any]]:
    """Open questions to the owner older than deputy.after_h (the owner gets that long to answer himself)."""
    now = now or datetime.now(timezone.utc)
    after = timedelta(hours=float(settings(st).get("after_h", DEFAULT_AFTER_H)))
    out = []
    for q in store.questions(["open"]):
        if q.get("to") != "owner":
            continue
        c = parse_ts(q.get("created"))
        if c and now - c >= after:
            out.append(q)
    return out[:MAX_QUESTIONS]


def deputy_prompt(product: Product, store: Store, questions: list[dict[str, Any]], mandate_txt: str) -> str:
    qs = []
    for q in questions:
        t = store.task(str(q.get("task") or "")) if q.get("task") else None
        qs.append({"id": q["id"], "from": q.get("from"), "created": q.get("created"), "question": q.get("question"),
                   "task": ({"id": t["id"], "title": t.get("title"), "state": t.get("state"), "role": t.get("role"),
                             "problem": str(t.get("problem") or "")[:400]} if t else None)})
    return (f"# DEPUTY OWNER — product `{product.slug}` ({product.site_base})\n\n"
            "The owner is away and unreachable. You answer the questions below in his place, strictly inside the mandate. "
            "An answer unblocks the task that asked; a 'not available' answer tells the PO to proceed without the thing or "
            "to close the task — never to keep waiting.\n\n"
            "## MANDATE (the owner's words; binding)\n" + mandate_txt +
            "\n\n## VISION of this product\n" + product.vision()[:9000] +
            "\n\n## QUESTIONS\n" + json.dumps(qs, ensure_ascii=False, indent=1) +
            "\n\n## RULES\n- One answer per question id, in the language of the question, 1–4 sentences, decisive.\n"
            "- `kind`: `decided` (you chose inside the mandate — say what and why in one clause), `unavailable` (secrets, money, "
            "accounts, channels, VISION goals, anything the mandate reserves — say it is not available until the owner is back "
            "and what to do meanwhile), `info` (the question only needed a fact you have).\n"
            "- Prefer the conservative option: the one that changes nothing public, costs nothing new and can be undone.\n"
            "- Never invent facts about accounts, repositories, tokens or data you cannot see; if unsure whether something "
            "exists, answer `unavailable` and tell the role to proceed as if it does not.\n"
            "- `not_before` only when the answer is 'wait until <moment>'.\n")


def apply(store: Store, res: dict[str, Any], questions: list[dict[str, Any]]) -> int:
    ids = {q["id"] for q in questions}
    n = 0
    for a in res.get("answers") or []:
        qid = str(a.get("question_id") or "")
        if qid not in ids:
            continue
        text = f"[{a.get('kind', 'decided')}] {str(a.get('answer') or '').strip()}"
        try:
            store.answer(qid, text, "deputy", not_before=a.get("not_before") or None)
            n += 1
        except FileNotFoundError:
            continue
    return n


def run_for_product(cycle: Any) -> int:
    """Called from phase_triage. Returns the number of answers written."""
    st, store, p = cycle.st, cycle.store, cycle.p
    if not enabled(st) or getattr(cycle, "dry_run", False):
        return 0                              # dry-run (canary): no model run, and no "run failed" line to trip the canary check
    qs = due_questions(st, store)
    if not qs:
        return 0
    from .cycle import schema
    work = p.work_dir / cycle.cycle_id / "deputy"
    prompt = deputy_prompt(p, store, qs, mandate(st))
    rr = cycle.role_run("deputy", prompt, "deputy-answers", cwd=work, label="deputy", cap=RUN_CAP_USD, allowed_tools=[], max_turns=MAX_TURNS)
    if rr is None or not (rr.ok and rr.structured):
        log.warning("%s: deputy run failed: %s", p.slug, rr.error if rr else "no run")
        return 0
    n = apply(store, rr.structured, qs)
    store.event("deputy.answered", p.slug, "deputy", {"questions": [q["id"] for q in qs], "answered": n})
    log.info("%s: deputy answered %d of %d owner questions", p.slug, n, len(qs))
    return n
