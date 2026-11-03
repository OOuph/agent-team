"""Twelve-hour role report to the owner's Telegram (owner 03.11: "every 12 hours every role that did tasks reports in
Telegram, and the PO says what was chosen and implemented").

Two layers, so the report is both readable and auditable:

1. ``ledger(...)`` — a deterministic ledger per product and per role built only from the store (task histories, results,
   reviews, posts, editorial and support events, PO decisions, merges, builds, spend). No model, no guessing; this is
   what gets saved to ``data/reports/<stamp>.md`` and is the fallback text when the model is unavailable.
2. ``narrative(...)`` — one cheap model run (role ``reporter``) that turns the ledger into short first-person paragraphs,
   one per role that worked, plus the PO's "what we chose / what got implemented". The prompt forbids anything that is
   not in the ledger; the ledger is attached to the report so every sentence can be checked.

CLI: ``team report [--hours 12] [--send] [--no-llm] [--out PATH]``; cron: ``report-roles.sh`` at 07:30 and 19:30 UTC.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import publish
from .config import Product, Settings
from .store import Store, parse_ts

log = logging.getLogger("team.report")

ROLES_WORKING = ("developer", "marketer", "analyst", "reviewer", "editor", "support", "framework-retro", "persona", "geo-probe")
ROLE_RU = {"developer": "Разработчик", "marketer": "Маркетолог", "analyst": "Аналитик", "reviewer": "Ревьюер", "editor": "Редактор",
           "support": "Поддержка", "framework-retro": "Ретро фреймворка", "persona": "Персоны", "geo-probe": "GEO-проба",
           "po": "Менеджер продукта (PO)", "dispatcher": "Диспетчер"}
TELEGRAM_CHUNK = 3500
MAX_ITEMS = 12            # per role per product in the ledger text (the JSON keeps everything)
REPORT_RUN_CAP_USD = 1.0
REPORT_MAX_TURNS = 4


# ------------------------------------------------------------------ time helpers

def _since(hours: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def _in_window(ts: Any, since: datetime) -> bool:
    d = parse_ts(str(ts)) if ts else None
    return bool(d and d >= since)


_CYCLE_RE = re.compile(r"(\d{8}T\d{4})")


def _cycle_ts(s: Any) -> datetime | None:
    """A cycle id or run id ("20261103T0407-exec-T-0017") → its start time; None when it carries no stamp."""
    m = _CYCLE_RE.search(str(s or ""))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%dT%H%M").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _short(s: Any, n: int = 160) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


# ------------------------------------------------------------------ ledger

def product_ledger(p: Product, store: Store, since: datetime) -> dict[str, Any]:
    since_iso = since.isoformat(timespec="seconds")
    roles: dict[str, dict[str, list[dict[str, Any]]]] = {r: {"tasks": [], "items": []} for r in ROLES_WORKING}
    po: dict[str, Any] = {"created": [], "decisions": {}, "answers": 0, "closed": [], "implemented": [], "owner_questions": []}
    disp: dict[str, Any] = {"merged": [], "duplicates_skipped": 0, "phase_errors": 0, "posts": []}

    # tasks: who moved them and what came out
    for t in store.tasks():
        hist = [h for h in t.get("history") or [] if _in_window(h.get("ts"), since)]
        if not hist:
            continue
        role = str(t.get("role") or "developer")
        moved_by_role = [h for h in hist if h.get("actor") == role]
        last_result = None
        for r in reversed(t.get("results") or []):
            rt = _cycle_ts(r.get("run_id"))
            if rt is None or rt >= since:
                last_result = r
                break
        if moved_by_role or (last_result and role in roles):
            roles.setdefault(role, {"tasks": [], "items": []})["tasks"].append({
                "id": t["id"], "title": _short(t.get("title"), 110), "state": t.get("state"),
                "moves": [f"{h.get('from')}→{h.get('to')}" for h in moved_by_role][-4:],
                "result": _short((last_result or {}).get("summary"), 220) if last_result else None,
                "result_status": (last_result or {}).get("status") if last_result else None,
                "blocked_by": t.get("blocked_by"), "pr": t.get("pr_url")})
        for h in hist:
            if h.get("to") in ("accepted", "done") and h.get("from") not in ("accepted", "done", "measured"):
                po["implemented"].append({"id": t["id"], "title": _short(t.get("title"), 110), "role": role, "at": h.get("ts")})
            if h.get("to") == "rejected" and h.get("actor") in ("po", "owner", "dispatcher"):
                po["closed"].append({"id": t["id"], "title": _short(t.get("title"), 90), "by": h.get("actor"), "note": _short(h.get("note"), 120)})
        if t.get("created_by") == "po" and _in_window(t.get("created"), since):
            po["created"].append({"id": t["id"], "title": _short(t.get("title"), 110), "role": role, "priority": t.get("priority"),
                                  "not_before": t.get("not_before")})

    # PO decisions on observations (the decision carries the cycle id)
    for o in store.observations():
        d = o.get("decision") or {}
        ct = _cycle_ts(d.get("cycle"))
        if d and ct and ct >= since:
            a = str(d.get("action") or "?")
            po["decisions"][a] = po["decisions"].get(a, 0) + 1

    # questions
    for q in store.questions():
        if q.get("to") == "owner" and _in_window(q.get("created"), since):
            po["owner_questions"].append(_short(q.get("question"), 160))
        if q.get("answered_by") == "po" and _in_window(q.get("answered"), since):
            po["answers"] += 1
        if q.get("from") in roles and _in_window(q.get("created"), since):
            roles[q["from"]]["items"].append({"kind": "question", "task": q.get("task_id"), "text": _short(q.get("question"), 160),
                                               "state": q.get("state")})

    # reviews, posts, editorial, support, merges, errors — from the event log and review files
    for r in store._load_all("reviews"):
        if _in_window(r.get("created"), since):
            crit = r.get("criteria") or []
            roles["reviewer"]["items"].append({"kind": "review", "task": r.get("task"), "verdict": r.get("verdict"),
                                               "met": sum(1 for c in crit if c.get("met")), "of": len(crit)})
    for e in store.events(since=since_iso, limit=5000):
        k, d = e.get("kind"), e.get("detail") or {}
        if k == "post":
            disp["posts"].append({"channel": e.get("subject"), "url": d.get("url"), "task": d.get("task")})
            if e.get("actor") == "marketer":
                roles["marketer"]["items"].append({"kind": "post", "channel": e.get("subject"), "url": d.get("url"), "task": d.get("task")})
        elif k == "editorial":
            roles["editor"]["items"].append({"kind": "editorial", "task": e.get("subject"), "round": d.get("round"),
                                             "verdicts": d.get("verdicts"), "summary": _short(d.get("summary"), 160)})
        elif k == "support.reply":
            roles["support"]["items"].append({"kind": "reply", "channel": d.get("channel"), "url": d.get("url"), "action": d.get("action")})
        elif k == "observation.new" and e.get("actor") in ("persona", "geo-probe"):
            roles[e["actor"]]["items"].append({"kind": "observation", "title": _short(d.get("title"), 120), "class": d.get("classification")})
        elif k == "framework.merged":
            disp["merged"].append({"task": e.get("subject"), "pr": d.get("pr"), "files": (d.get("files") or [])[:6]})
        elif k == "task.duplicate_skipped":
            disp["duplicates_skipped"] += 1
        elif k == "phase.error":
            disp["phase_errors"] += 1

    spend: dict[str, float] = {}
    for role, usd in store.db.execute("SELECT role, SUM(usd) FROM spend WHERE ts >= ? GROUP BY role", (since_iso,)):
        spend[str(role)] = round(float(usd or 0), 2)

    roles = {r: v for r, v in roles.items() if v["tasks"] or v["items"]}
    return {"product": p.slug, "name": p.raw.get("name") or p.slug, "site": p.site_base, "roles": roles, "po": po,
            "dispatcher": disp, "spend_usd": spend}


def framework_items(st: Settings, since: datetime) -> dict[str, Any]:
    """Retro proposals and site builds in the window (they live outside product stores)."""
    out: dict[str, Any] = {"retro": [], "builds": []}
    rdir = st.data_dir / "framework" / "retro"
    if rdir.exists():
        for y in sorted(rdir.glob("*.yaml")):
            if datetime.fromtimestamp(y.stat().st_mtime, timezone.utc) < since:
                continue
            try:
                import yaml
                rep = yaml.safe_load(y.read_text(encoding="utf-8")) or {}
            except Exception:  # noqa: BLE001
                continue
            for pr in (rep.get("proposals") or [])[:10]:
                out["retro"].append({"title": _short(pr.get("title"), 120), "risk": pr.get("risk")})
    blog = st.data_dir / "builds.log"
    if blog.exists():
        for ln in blog.read_text(encoding="utf-8").splitlines()[-400:]:
            m = re.match(r"(\S+) \[([^\]]+)\] (pushed|FAILED|no changes)(.*)", ln)
            if m and _in_window(m.group(1), since):
                out["builds"].append({"slug": m.group(2), "status": m.group(3), "note": _short(m.group(4), 80)})
    return out


def ledger(st: Settings, products: list[Product], hours: float) -> dict[str, Any]:
    since = _since(hours)
    items = []
    for p in products:
        s = Store(p.dir)
        try:
            items.append(product_ledger(p, s, since))
        finally:
            s.close()
    return {"since": since.isoformat(timespec="seconds"), "until": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "hours": hours, "products": items, "framework": framework_items(st, since)}


# ------------------------------------------------------------------ deterministic text

def _fmt_task(t: dict[str, Any]) -> str:
    s = f"{t['id']} «{t['title']}» → {t['state']}"
    if t.get("result"):
        s += f": {t['result']}"
    if t.get("blocked_by"):
        s += f" (ждёт {t['blocked_by']})"
    return s


def ledger_md(led: dict[str, Any]) -> str:
    lines = [f"# Ведомость ролей за {led['hours']:.0f} ч — {led['since'][:16]} → {led['until'][:16]} UTC", ""]
    for p in led["products"]:
        lines.append(f"## {p['name']} ({p['site'] or p['product']})")
        po = p["po"]
        lines.append("**PO** — выбрал: " + (", ".join(f"{k} {v}" for k, v in sorted(po["decisions"].items())) or "решений по наблюдениям нет")
                     + f"; ответов ролям {po['answers']}; задач создано {len(po['created'])}; закрыто {len(po['closed'])}.")
        for c in po["created"][:MAX_ITEMS]:
            lines.append(f"- создал {c['id']} «{c['title']}» ({c['role']}, {c['priority']}" + (f", старт {c['not_before'][:16]}" if c.get("not_before") else "") + ")")
        for c in po["closed"][:6]:
            lines.append(f"- закрыл {c['id']} «{c['title']}» ({c['by']}: {c['note']})")
        lines.append("**Реализовано (принято):** " + ("; ".join(f"{i['id']} «{i['title']}»" for i in po["implemented"][:MAX_ITEMS]) or "ничего не принято"))
        if po["owner_questions"]:
            lines.append("**Вопросы владельцу:** " + " | ".join(po["owner_questions"][:4]))
        for role, v in p["roles"].items():
            lines.append(f"**{ROLE_RU.get(role, role)}:**")
            for t in v["tasks"][:MAX_ITEMS]:
                lines.append(f"- {_fmt_task(t)}")
            for it in v["items"][:MAX_ITEMS]:
                k = it.get("kind")
                if k == "review":
                    lines.append(f"- ревью {it['task']}: {it['verdict']} ({it['met']}/{it['of']} критериев)")
                elif k == "post":
                    lines.append(f"- пост {it['channel']}: {it.get('url') or '—'}" + (f" ({it['task']})" if it.get("task") else ""))
                elif k == "editorial":
                    lines.append(f"- вычитка {it['task']} раунд {it.get('round')}: {it.get('verdicts')} — {it.get('summary')}")
                elif k == "reply":
                    lines.append(f"- ответ {it.get('channel')}: {it.get('url') or '—'} ({it.get('action')})")
                elif k == "question":
                    lines.append(f"- вопрос ({it.get('state')}) по {it.get('task') or '—'}: {it['text']}")
                elif k == "observation":
                    lines.append(f"- наблюдение [{it.get('class')}]: {it['title']}")
        d = p["dispatcher"]
        extra = []
        if d["merged"]:
            extra.append("влито без человека: " + ", ".join(f"{m['task']} ({m['pr']})" for m in d["merged"]))
        if d["duplicates_skipped"]:
            extra.append(f"дублей задач отброшено {d['duplicates_skipped']}")
        if d["phase_errors"]:
            extra.append(f"ошибок фаз {d['phase_errors']}")
        if extra:
            lines.append("**Диспетчер:** " + "; ".join(extra))
        if p["spend_usd"]:
            lines.append("Расход: " + ", ".join(f"{r} ${u:.2f}" for r, u in sorted(p["spend_usd"].items())) + f" = ${sum(p['spend_usd'].values()):.2f}")
        lines.append("")
    fw = led["framework"]
    if fw["retro"] or fw["builds"]:
        lines.append("## Фреймворк и сборки")
        for r in fw["retro"]:
            lines.append(f"- ретро предложило [{r.get('risk')}] {r['title']}")
        for b in fw["builds"]:
            lines.append(f"- сборка {b['slug']}: {b['status']}{b['note']}")
        lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------ model narrative

PROMPT_LEDGER_CHARS = 150_000


def compact_for_prompt(led: dict[str, Any]) -> dict[str, Any]:
    """The ledger with long tails trimmed, so the whole window fits the prompt (the first live run cut the JSON at 60K
    chars and the reporter rightly wrote "the ledger is truncated")."""
    out = {"since": led["since"], "until": led["until"], "hours": led["hours"], "framework": led["framework"], "products": []}
    for p in led["products"]:
        roles = {}
        for r, v in p["roles"].items():
            roles[r] = {"tasks": [{k: (_short(x[k], 140) if k == "result" else x[k]) for k in ("id", "title", "state", "result", "result_status", "blocked_by", "pr") if x.get(k) is not None}
                                  for x in v["tasks"][:MAX_ITEMS]],
                        "items": v["items"][:MAX_ITEMS]}
            if len(v["tasks"]) > MAX_ITEMS or len(v["items"]) > MAX_ITEMS:
                roles[r]["omitted"] = f"{max(0, len(v['tasks']) - MAX_ITEMS)} tasks, {max(0, len(v['items']) - MAX_ITEMS)} items not shown"
        po = dict(p["po"])
        po["created"] = po["created"][:MAX_ITEMS]
        po["implemented"] = po["implemented"][:20]
        po["owner_questions"] = po["owner_questions"][:4]
        out["products"].append({"product": p["product"], "name": p["name"], "site": p["site"], "roles": roles, "po": po,
                                "dispatcher": p["dispatcher"], "spend_usd": p["spend_usd"]})
    return out


def reporter_prompt(led: dict[str, Any]) -> str:
    active = sorted({r for p in led["products"] for r in p["roles"]})
    body = json.dumps(compact_for_prompt(led), ensure_ascii=False, indent=1)
    if len(body) > PROMPT_LEDGER_CHARS:
        body = body[:PROMPT_LEDGER_CHARS] + "\n… [ведомость обрезана; скажи об этом в заголовке]"
    return ("# ОТЧЁТ РОЛЕЙ ВЛАДЕЛЬЦУ — окно " + f"{led['since'][:16]} → {led['until'][:16]} UTC\n\n"
            "Ниже ведомость (JSON) — всё, что команда ролей сделала за окно, по продуктам. Напиши отчёт владельцу в Telegram.\n\n"
            "## ПРАВИЛА\n"
            "- Русский язык, от первого лица каждой роли («Я взял…», «Опубликовал…»). Одна секция на роль из списка активных: "
            + ", ".join(ROLE_RU.get(r, r) for r in active) + ". Роль без записей в ведомости — не упоминать.\n"
            "- Внутри секции роли — по продуктам, коротко: что взял, что вышло, что застряло и почему. 2–6 предложений на роль, "
            "до 700 символов. Ссылки на посты и PR приводить как есть.\n"
            "- Секция PO: «что выбрали» (решения по наблюдениям, созданные задачи с приоритетом, закрытые) и «что реализовано» "
            "(принятые задачи) — по продуктам, до 900 символов; вопросы владельцу — отдельной строкой, если есть.\n"
            "- Заголовок — одна строка: главное событие окна и общий расход.\n"
            "- Только факты из ведомости. Ни одного числа, названия, ссылки или причины, которых в ней нет. Если роль ничего не "
            "завершила — так и написать. Без оценок «отлично/успешно», без обещаний.\n\n"
            "## ВЕДОМОСТЬ\n```json\n" + body + "\n```\n")


def narrative(st: Settings, runner: Any, led: dict[str, Any], work_dir: Path, book_store: Store | None) -> dict[str, Any] | None:
    from .cycle import schema
    work_dir.mkdir(parents=True, exist_ok=True)
    prompt = reporter_prompt(led)
    (work_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    run_id = "report-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")
    model = (st.claude.get("models") or {}).get("reporter")
    rr = runner.run("reporter", prompt, schema("role-report"), max_usd=REPORT_RUN_CAP_USD, cwd=work_dir, model=str(model) if model else None,
                    allowed_tools=[], run_dir=work_dir, run_id=run_id, max_turns=REPORT_MAX_TURNS)
    if book_store is not None:
        cost = rr.cost_usd if rr.cost_usd is not None else float(st.budget.get("unknown_cost_reserve_usd", 0.5))
        book_store.book_spend(run_id, "reporter", cost, rr.cost_usd is not None)
    if not (rr.ok and rr.structured):
        log.warning("reporter run failed: %s", rr.error)
        return None
    return rr.structured


def narrative_text(n: dict[str, Any], led: dict[str, Any]) -> str:
    # Telegram gets html-escaped plain text from team-publish, so no markup: role names in capitals with a marker
    parts = [f"🗂 {n.get('headline', '').strip()}", ""]
    for sec in n.get("roles") or []:
        parts.append(f"▪ {ROLE_RU.get(sec.get('role'), sec.get('role')).upper()}\n{sec.get('text', '').strip()}")
        parts.append("")
    parts.append(f"▪ {ROLE_RU['po'].upper()}\n{n.get('po', '').strip()}")
    spend = sum(sum(p["spend_usd"].values()) for p in led["products"])
    parts += ["", f"Окно {led['since'][11:16]}–{led['until'][11:16]} UTC, {led['hours']:.0f} ч · расход ${spend:.2f} · ведомость в data/reports/."]
    return "\n".join(parts)


# ------------------------------------------------------------------ send

def send_telegram(text: str, dry_run: bool) -> list[dict[str, Any]]:
    chat = os.environ.get("TELEGRAM_OPS_CHAT_ID")
    if not chat or not os.environ.get("TELEGRAM_BOT_TOKEN"):
        log.warning("TELEGRAM_OPS_CHAT_ID / TELEGRAM_BOT_TOKEN not set — not sending")
        return []
    out = []
    chunks, cur = [], ""
    for para in text.split("\n\n"):
        if len(cur) + len(para) + 2 > TELEGRAM_CHUNK and cur:
            chunks.append(cur)
            cur = ""
        cur = (cur + "\n\n" + para) if cur else para
    if cur:
        chunks.append(cur)
    for i, ch in enumerate(chunks):
        r = publish.post("telegram", ch if i == 0 else f"(продолжение {i + 1}/{len(chunks)})\n\n{ch}", target=chat, disclosure=" ", dry_run=dry_run)
        out.append({"ok": r.ok, "error": r.error, "dry_run": r.dry_run})
    return out


def run_report(st: Settings, products: list[Product], hours: float, *, runner: Any = None, send: bool = False, dry_run: bool = False,
               out: Path | None = None) -> dict[str, Any]:
    led = ledger(st, products, hours)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M")
    rdir = st.data_dir / "reports"
    rdir.mkdir(parents=True, exist_ok=True)
    md = ledger_md(led)
    (rdir / f"{stamp}.json").write_text(json.dumps(led, ensure_ascii=False, indent=1), encoding="utf-8")
    nar = narrative(st, runner, led, rdir / f"{stamp}-run", _book_store(products)) if runner is not None else None
    text = narrative_text(nar, led) if nar else ("🗂 Ведомость ролей (модель недоступна, сухой вариант)\n\n" + md)
    (rdir / f"{stamp}.md").write_text(text + "\n\n---\n\n" + md, encoding="utf-8")
    if out:
        out.write_text(text + "\n\n---\n\n" + md, encoding="utf-8")
    sent = send_telegram(text, dry_run) if send else []
    return {"stamp": stamp, "text": text, "ledger_md": md, "narrative": bool(nar), "sent": sent, "path": str(rdir / f"{stamp}.md")}


def _book_store(products: list[Product]) -> Store | None:
    for p in products:
        if p.slug == "agent-team":
            return Store(p.dir)
    return Store(products[0].dir) if products else None
