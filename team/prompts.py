"""Prompt builders: what each role sees. Everything a role needs travels in the prompt; roles never read the store."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from .config import Product
from .store import Store


def _y(obj: Any) -> str:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=110)


def _task_brief(t: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "title", "type", "state", "priority", "size", "goal", "role", "problem", "scope", "acceptance",
            "negative_cases", "observations", "links", "review_fails", "created")
    return {k: t.get(k) for k in keys if t.get(k) not in (None, [], "")}


def _tail(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + f"\n… [cut {len(s) - n} chars]"


def po_prompt(product: Product, store: Store, *, cycle_id: str, observations: list[dict[str, Any]], questions: list[dict[str, Any]],
              metrics_md: str, spent_today: float, cap: float, max_ready: int, idle: bool) -> str:
    tasks = [t for t in store.tasks() if t.get("state") not in ("done", "rejected")]
    recent_done = [t for t in store.tasks(["done", "measured", "accepted", "released"])][-10:]
    obs_txt = "\n\n".join(_y({k: v for k, v in o.items() if k not in ("fingerprint",)}) for o in observations) or "(none)"
    q_txt = "\n\n".join(_y(q) for q in questions) or "(none)"
    answered = [q for q in store.questions(["answered"]) if q.get("to") == "owner" and str(q.get("answered") or "") >= _hours_ago(72)][-8:]
    answered_txt = "\n\n".join(_y({k: q.get(k) for k in ("id", "question", "answer", "answered_by", "answered")}) for q in answered) or "(none)"
    fw_txt = framework_context(product)
    return (f"# Cycle {cycle_id} — PO triage for product `{product.slug}` ({product.site_base})\n\n"
            f"Spend today ${spent_today:.2f} of the daily cap ${cap:.2f}. Propose at most {max_ready} Ready tasks."
            + (" The team is idle: no task is ready or in progress — if VISION promises daily work, create it." if idle else "")
            + "\n\n# VISION\n" + product.vision()
            + "\n\n# BACKLOG (open tasks)\n" + (_y([_task_brief(t) for t in tasks]) if tasks else "(empty)")
            + "\n\n# RECENTLY FINISHED\n" + (_y([{k: t.get(k) for k in ("id", "title", "state")} for t in recent_done]) if recent_done else "(none)")
            + "\n\n# OPEN QUESTIONS TO YOU\n" + q_txt
            + "\n\n# QUESTIONS THE OWNER ALREADY ANSWERED (last 72 h — do not ask these again)\n" + answered_txt
            + (("\n\n# FRAMEWORK CONTEXT (this product is the framework itself)\n" + fw_txt) if fw_txt else "")
            + "\n\n# NEW OBSERVATIONS\n" + obs_txt
            + "\n\n# LATEST METRICS\n" + (_tail(metrics_md, 6000) or "(no metrics yet)") + "\n")


def _hours_ago(h: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(hours=h)).isoformat(timespec="seconds")


def framework_context(product: Product) -> str:
    """For the product whose repository is the framework: the friction register (7 days), the latest retro report and the
    open retro tasks, so the PO reads the loop instead of asking the owner whether it runs."""
    fw = dict(product.st.raw.get("framework") or {})
    if product.slug != str(fw.get("product") or ""):
        return ""
    base = product.st.data_dir / "framework"
    parts = []
    reg = base / "register.md"
    if reg.exists():
        parts.append("## Friction register (last 7 days)\n" + _tail(reg.read_text(encoding="utf-8"), 6000))
    retros = sorted((base / "retro").glob("20*.md"))
    if retros:
        parts.append(f"## Latest retro report ({retros[-1].name})\n" + _tail(retros[-1].read_text(encoding="utf-8"), 5000))
    stamp = base / "last-retro"
    parts.append("## Loop status\n" + (f"last retro: {stamp.read_text(encoding='utf-8').strip()}" if stamp.exists() else "retro has not run yet")
                 + f" · retro every {fw.get('retro_every_h', '?')} h · auto_merge={fw.get('auto_merge')} · canary={fw.get('canary_product')}"
                 + "\nThe loop register → retro → issues → tasks → PR → canary → merge runs by itself; do not ask the owner whether it does.")
    return "\n\n".join(parts)


def developer_prompt(product: Product, task: dict[str, Any], repo_dir: Path, branch: str, previous: list[dict[str, Any]],
                     review: dict[str, Any] | None) -> str:
    readme = ""
    seen: set[Path] = set()
    for name in ("README.md", "readme.md", "AGENTS.md"):
        p = repo_dir / name
        if p.exists() and p.resolve() not in seen:          # case-insensitive file systems: README.md == readme.md
            seen.add(p.resolve())
            readme += f"\n\n--- {name} ---\n" + _tail(p.read_text(encoding="utf-8", errors="replace"), 8000)
    prev = ("\n\n# PREVIOUS ATTEMPTS ON THIS TASK\n" + _y(previous[-2:])) if previous else ""
    rev = ("\n\n# REVIEW THAT FAILED (fix this first)\n" + _y({k: review.get(k) for k in ("verdict", "first_failure", "criteria", "summary")})) if review else ""
    return (f"# TASK {task['id']} — product `{product.slug}` ({product.site_base})\n\n"
            f"You are in the repository checkout `{repo_dir}` on branch `{branch}`. Commit there; do not push.\n\n"
            "# TASK\n" + _y(_task_brief(task)) + prev + rev
            + "\n\n# VISION\n" + product.vision()
            + "\n\n# REPOSITORY NOTES" + (readme or "\n(no README — follow the conventions you find in the tree)") + "\n")


def reviewer_prompt(product: Product, task: dict[str, Any], result: dict[str, Any] | None, sha: str | None) -> str:
    return (f"# REVIEW of task {task['id']} — product `{product.slug}`\n\nPublic base URL: {product.site_base}\n"
            f"Expected release: commit {sha or 'unknown'} (the site may show a build marker in its footer or /build.json).\n\n"
            "# TASK\n" + _y(_task_brief(task))
            + "\n\n# IMPLEMENTER'S REPORT (a claim, not evidence)\n" + (_y(result) if result else "(none)")
            + "\n\nUse only `team-fetch <url>`. Check every acceptance criterion and negative case on the live site.\n")


def analyst_prompt(product: Product, store: Store, *, metrics: list[tuple[str, str]], accepted: list[dict[str, Any]],
                   posts: list[dict[str, Any]]) -> str:
    m = "\n\n".join(f"--- {day} ---\n{_tail(txt, 5000)}" for day, txt in metrics) or "(no metrics files yet)"
    return (f"# ANALYST — product `{product.slug}` ({product.site_base})\n\n# VISION (goals table)\n" + product.vision()
            + "\n\n# METRICS (latest first)\n" + m
            + "\n\n# TASKS ACCEPTED RECENTLY\n" + (_y([{**_task_brief(t), "accepted_at": t.get("accepted_at"), "released_sha": t.get("released_sha")} for t in accepted]) if accepted else "(none)")
            + "\n\n# POSTS PUBLISHED\n" + (_y(posts) if posts else "(none)") + "\n")


def marketer_prompt(product: Product, task: dict[str, Any], channels: dict[str, Any], previous: list[dict[str, Any]]) -> str:
    prev = ("\n\n# PREVIOUS ATTEMPTS\n" + _y(previous[-2:])) if previous else ""
    return (f"# MARKETING TASK {task['id']} — product `{product.slug}` ({product.site_base})\n\n"
            "# TASK\n" + _y(_task_brief(task))
            + "\n\n# CHANNELS CONFIGURED (channel → target)\n" + _y(channels)
            + "\n\n# VISION\n" + product.vision() + prev
            + "\n\nWrite each post to a file here, then `team-publish --channel <channel> --target <target> --text-file <file> --url <url>`.\n")


def persona_prompt(persona: dict[str, Any], body: str, scenario: dict[str, Any], sbody: str) -> str:
    return ("# PERSONA\n" + _y(persona) + "\n" + body + "\n\n# SCENARIO\n" + _y(scenario) + "\n## Task\n" + sbody
            + f"\n\nRules: only `team-fetch <url>`; at most {scenario.get('max_steps', 10)} fetches; start at {scenario.get('entry')}.\n")
