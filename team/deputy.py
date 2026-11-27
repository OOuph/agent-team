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
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Product, Settings
from .store import Store, parse_ts

log = logging.getLogger("team.deputy")

# ------------------------------------------------------------------ parameters
OWNER_WORDS_DEFAULT = False       # `deputy.owner_words` (team.yaml, product.yaml overrides): true = the deputy reads what the owner
                                  # has put on record for this product and leaves to him a question about his own words (27.11)
VISION_CHARS = 9000               # without the switch: the first so many characters of VISION, as before
VISION_OWNER_CHARS = 60000        # with it: the sections whose heading names the owner, whole, up to this many characters …
VISION_REST_CHARS = 9000          # … and of the other sections this many
DECISIONS_FILE = "decisions.md"
DECISIONS_CHARS = 14000           # the END of the product's decisions.md (the newest decisions are the last lines)
OWNER_OBS_MAX = 20                # the newest observations written by the owner …
OWNER_OBS_CHARS = 700             # … this many characters of each
OWNER_SOURCES = ("owner",)        # `source` of an observation that is the owner's own word
OWNER_HEADING_RE = re.compile(r"владел|owner", re.I)
KIND_OWNER_ONLY = "owner_only"    # the deputy's answer that is no answer: the question stays open for the owner
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
        if q.get("deputy_left"):
            continue                              # the deputy has said this one is the owner's alone (`owner_only`)
        if q.get("to") != "owner" or q.get("from") == "onboarding":
            continue                              # onboarding (accounts, goals) is the owner's alone: no deputy answer
        c = parse_ts(q.get("created"))
        if c and now - c >= after:
            out.append(q)
    return out[:MAX_QUESTIONS]


def owner_words(st: Settings, product: Product) -> bool:
    raw = (product.raw.get("deputy") or {}).get("owner_words", settings(st).get("owner_words", OWNER_WORDS_DEFAULT))
    return raw is True


def vision_for_deputy(text: str) -> str:
    """VISION with the owner's own sections first-class: a section whose `##` heading names the owner is kept whole (up to
    VISION_OWNER_CHARS in all), the other sections share VISION_REST_CHARS. 27.11, ai-battles: the VISION was 53 KB, the
    prompt carried its first 9000 characters, and the section «Машина по описанию создаётся всегда (владелец, 26–27.11)»
    — the very decision the question was about — began past character 30000."""
    parts = re.split(r"(?m)^(?=## )", text)
    own = [x for x in parts if OWNER_HEADING_RE.search(x.split("\n", 1)[0])]
    rest = [x for x in parts if x not in own]
    out_rest, left = [], VISION_REST_CHARS
    for x in rest:
        if left <= 0:
            out_rest.append(x.split("\n", 1)[0].rstrip() + "\n(section not shown here)\n")
            continue
        out_rest.append(x[:left] + ("\n(… cut)\n" if len(x) > left else ""))
        left -= len(x)
    out_own, left = [], VISION_OWNER_CHARS
    for x in own:
        if left <= 0:
            out_own.append(x.split("\n", 1)[0].rstrip() + "\n(section not shown here: over the size limit — a question about it is the owner's)\n")
            continue
        out_own.append(x[:left] + ("\n(… cut)\n" if len(x) > left else ""))
        left -= len(x)
    return ("### Sections that are the owner's direct words (binding; whole)\n" + ("".join(out_own) or "(none)\n")
            + "\n### The rest of VISION (shortened)\n" + "".join(out_rest))


def decisions_tail(product: Product) -> str:
    p = product.dir / DECISIONS_FILE
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8", errors="replace")
    if len(text) <= DECISIONS_CHARS:
        return text
    tail = text[-DECISIONS_CHARS:]
    return "(… older decisions are not shown)\n" + tail[tail.find("\n") + 1:]


def owner_observations(store: Store) -> list[dict[str, Any]]:
    obs = [o for o in store.observations() if str(o.get("source") or "") in OWNER_SOURCES]
    obs.sort(key=lambda o: str(o.get("observed_at") or ""), reverse=True)
    return [{"id": o.get("id"), "at": o.get("observed_at"), "title": str(o.get("title") or "")[:200],
             "text": str(o.get("actual") or o.get("expected") or "")[:OWNER_OBS_CHARS]} for o in obs[:OWNER_OBS_MAX]]


def deputy_prompt(product: Product, store: Store, questions: list[dict[str, Any]], mandate_txt: str, owner: bool = False) -> str:
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
            "\n\n## VISION of this product\n" + (vision_for_deputy(product.vision()) if owner else product.vision()[:VISION_CHARS]) +
            (("\n\n## DECISIONS THE OWNER PUT ON RECORD (the product's decisions.md, newest last; binding)\n" + (decisions_tail(product) or "(none)") +
              "\n\n## WHAT THE OWNER WROTE TO THE TEAM (his observations, newest first; binding)\n" +
              json.dumps(owner_observations(store), ensure_ascii=False, indent=1)) if owner else "") +
            "\n\n## QUESTIONS\n" + json.dumps(qs, ensure_ascii=False, indent=1) +
            "\n\n## RULES\n- One answer per question id, in the language of the question, 1–4 sentences, decisive.\n"
            "- `kind`: `decided` (you chose inside the mandate — say what and why in one clause), `unavailable` (secrets, money, "
            "accounts, channels, VISION goals, anything the mandate reserves — say it is not available until the owner is back "
            "and what to do meanwhile), `info` (the question only needed a fact you have).\n"
            "- Prefer the conservative option: the one that changes nothing public, costs nothing new and can be undone.\n"
            "- Never invent facts about accounts, repositories, tokens or data you cannot see; if unsure whether something "
            "exists, answer `unavailable` and tell the role to proceed as if it does not.\n"
            "- `not_before` only when the answer is 'wait until <moment>'.\n" +
            ("- `owner_only`: the question asks to CHANGE, RANK or INTERPRET the owner's own recorded words — a VISION section "
             "that is his, a line of decisions.md, one of his observations — or two of them against each other. You do not "
             "choose between the owner's words and you do not replace one of them «for now». Answer `owner_only` with one "
             "sentence naming the words in question; the question then stays open for the owner and its task waits. Before any "
             "`decided` answer, check it against the three blocks above: an answer that goes against a recorded decision is "
             "wrong even when it is the conservative option.\n" if owner else ""))


def apply(store: Store, res: dict[str, Any], questions: list[dict[str, Any]]) -> int:
    ids = {q["id"] for q in questions}
    n = 0
    for a in res.get("answers") or []:
        qid = str(a.get("question_id") or "")
        if qid not in ids:
            continue
        if str(a.get("kind")) == KIND_OWNER_ONLY:
            # not an answer: the question stays open and its task keeps waiting for the owner; the deputy is not asked again
            try:
                store.mark_question(qid, {"deputy_left": {"why": str(a.get("answer") or "").strip()[:600]}})
            except FileNotFoundError:
                continue
            store.event("deputy.left_to_owner", qid, "deputy", {"why": str(a.get("answer") or "")[:200]})
            log.info("deputy left %s to the owner: %s", qid, str(a.get("answer") or "")[:160])
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
    prompt = deputy_prompt(p, store, qs, mandate(st), owner=owner_words(st, p))
    rr = cycle.role_run("deputy", prompt, "deputy-answers", cwd=work, label="deputy", cap=RUN_CAP_USD, allowed_tools=[], max_turns=MAX_TURNS)
    if rr is None or not (rr.ok and rr.structured):
        log.warning("%s: deputy run failed: %s", p.slug, rr.error if rr else "no run")
        return 0
    n = apply(store, rr.structured, qs)
    store.event("deputy.answered", p.slug, "deputy", {"questions": [q["id"] for q in qs], "answered": n})
    log.info("%s: deputy answered %d of %d owner questions", p.slug, n, len(qs))
    return n
