"""`team onboard` — a new product from a brief, in two steps (owner 20.11: «на вход описание, потом от главного агента
уточняющие вопросы и запросы на аккаунты, а затем производство автономными циклами»).

Step 1 (`--brief`): the brief → the `onboarding` role → ONE batch of clarifying questions and the accounts the team will
need, each with a reason. Written as products/<slug>/onboarding/questions.md for the owner (an `Ответ:` line under
every question, a `Решение:` line under every account) and as one open owner question, so it reaches the digest.
Step 2 (`--answers`): the filled file → the role again → VISION.md, GAME-DESIGN.md v1, product.yaml fields, the first
backlog (tasks that need an account the owner did not grant go to `deferred` with a return condition). Then
`onboarding.state` becomes `done` and the hourly cycle may run the product; before that `Cycle.stopped()` refuses.

`--dry-run` (and the tests) use the fixture team/templates/onboarding.sample.yaml instead of a model run: the whole path
is exercised without a token, an account or a network call. Everything configurable is at the top of this module.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from . import config as C
from .store import Store, utcnow

log = logging.getLogger("team.onboard")

ROLE = "onboarding"
DIR = "onboarding"                           # products/<slug>/onboarding/
BRIEF_FILE = "brief.md"
QUESTIONS_YAML = "questions.yaml"
STRAY_NOTES_MAX = 40                       # notes outside the answer fields passed to the plan: at most this many pieces
STRAY_NOTE_CHARS = 2000                    # … of this many characters each
QUESTIONS_MD = "questions.md"
ANSWERS_YAML = "answers.yaml"
PLAN_YAML = "plan.yaml"
LOG_FILE = "log.jsonl"
GAME_DESIGN_FILE = "GAME-DESIGN.md"
STATE_QUESTIONS, STATE_DONE = "questions", "done"
QUESTIONS_CAP_USD = 3.0
PLAN_CAP_USD = 3.0
MAX_TURNS = 8
ANSWER_LABEL = "Ответ:"
DECISION_LABEL = "Решение:"
DEFAULT_MARK = "(по умолчанию)"
TEMPLATES = Path(__file__).parent / "templates"
GAME_TEMPLATE = TEMPLATES / "VISION.game.template.md"
SITE_TEMPLATE = TEMPLATES / "VISION.template.md"          # a static site (owner 26.11: Hit the Line is a site, not a game)
SAMPLE_FIXTURE = TEMPLATES / "onboarding.sample.yaml"
SITE_FIXTURE = TEMPLATES / "onboarding.sample-site.yaml"
SAMPLE_BRIEF = TEMPLATES / "onboarding.sample-brief.md"
DEFAULT_PROFILE = "game"
PROFILES = ("game", "site")
VISION_STANDING_HEADING = "Постоянные направления"   # the VISION section a game product must have (team/standing.py, owner 21.11)


class OnboardingError(RuntimeError):
    pass


# ------------------------------------------------------------------ files and state

def product_state(p: C.Product) -> str | None:
    """`onboarding.state` of a product, None when the product was never onboarded this way (legacy `team init`)."""
    ob = p.raw.get("onboarding")
    return str(ob.get("state") or "") if isinstance(ob, dict) else None


def not_finished(p: C.Product) -> str | None:
    """The reason the cycle must not run this product yet, or None."""
    s = product_state(p)
    if s is not None and s != STATE_DONE:
        return f"onboarding not finished (state={s}): answer {p.dir / DIR / QUESTIONS_MD} and run `team onboard {p.slug} --answers`"
    return None


def _dir(pdir: Path) -> Path:
    d = pdir / DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def _log(pdir: Path, kind: str, **detail: Any) -> None:
    with (_dir(pdir) / LOG_FILE).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": utcnow(), "kind": kind, **detail}, ensure_ascii=False, default=str) + "\n")


def _read_yaml(p: Path) -> dict[str, Any]:
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _write_yaml(p: Path, obj: dict[str, Any]) -> None:
    p.write_text(yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=110), encoding="utf-8")


def fixture(profile: str = DEFAULT_PROFILE) -> dict[str, Any]:
    return _read_yaml(SITE_FIXTURE if profile == "site" else SAMPLE_FIXTURE)


def _profile(raw: dict[str, Any]) -> str:
    return str(raw.get("profile") or DEFAULT_PROFILE)


def vision_template(profile: str) -> str:
    return (SITE_TEMPLATE if profile == "site" else GAME_TEMPLATE).read_text(encoding="utf-8")


# ------------------------------------------------------------------ prompts

def questions_prompt(slug: str, brief: str, raw: dict[str, Any]) -> str:
    return (f"# ONBOARDING — step 1: QUESTIONS — product `{slug}` · profile `{_profile(raw)}`\n\n"
            "Read the owner's brief and return the onboarding-questions schema: one batch of clarifying questions (each with "
            "why, options, default) and the accounts/access the team needs with reasons. Nothing the brief already answers.\n\n"
            "## BRIEF (the owner's words)\n" + brief.strip() +
            "\n\n## WHAT THE DISPATCHER ALREADY KNOWS\n" + yaml.safe_dump({k: raw.get(k) for k in ("slug", "name", "host", "repo", "budget_usd_per_day")},
                                                                         sort_keys=False, allow_unicode=True) +
            "\n## VISION TEMPLATE (what step 2 will have to fill — ask what it needs)\n" + vision_template(_profile(raw)))


def standing_note() -> str:
    """Owner 21.11: what the dispatcher will create by itself on a game product (team/standing.py), so that the VISION
    names the standing directions and the first backlog does not duplicate them."""
    from . import standing
    return ("## STANDING DIRECTIONS (the dispatcher creates these tasks itself — keep them OUT of the backlog)\n"
            f"A game product gets recurring tasks without the PO: the list below, each from stage {standing.GAME_FROM_STAGE}. Fill the "
            f"VISION section «{VISION_STANDING_HEADING}» with both directions and their cadences (adapt the wording to this game: what "
            "its units, options and levels are; the balance corridor that fits its number of players per round). The first backlog "
            "must contain NO task that does the same work (no «invent mechanics», no «check the balance», no recurring task).\n"
            + standing.vision_section() + "\n\n")


def plan_prompt(slug: str, brief: str, qa: dict[str, Any], raw: dict[str, Any], notes: list[dict[str, str]] | None = None) -> str:
    game = _profile(raw) == "game"
    what = ("the full VISION.md by the template, GAME-DESIGN.md v1 or a first game-designer task, product fields, the first "
            "backlog (M0 only; " if game else
            "the full VISION.md by the template (a static site: no GAME-DESIGN.md, `game_design_md` empty), product fields "
            "(profile `site`), the first backlog (the first weeks of the brief's plan; ")
    return (f"# ONBOARDING — step 2: PLAN — product `{slug}` · profile `{_profile(raw)}`\n\n"
            "Read the brief and the owner's answers (empty answer = the default you proposed; say so in `decisions`) and return "
            "the onboarding-plan schema: " + what + "a task that needs an account the owner declined or postponed is "
            "`deferred` with `return_condition`), decisions and a Russian summary for the owner.\n\n"
            + (standing_note() if game else "") +
            "## BRIEF\n" + brief.strip() +
            "\n\n## QUESTIONS AND ANSWERS\n" + yaml.safe_dump(qa, sort_keys=False, allow_unicode=True, width=110) +
            ("\n## OWNER'S NOTES OUTSIDE THE ANSWER FIELDS\nThe owner wrote these in the questions file outside `Ответ:` / `Решение:`. They are "
             "the owner's words with the weight of an answer: a note that corrects the summary, an assumption or a default wins over it. "
             "Name each note you applied in `decisions`.\n" + yaml.safe_dump(notes, sort_keys=False, allow_unicode=True, width=110) if notes else "") +
            "\n## PRODUCT (product.yaml as it is now)\n" + yaml.safe_dump({k: raw.get(k) for k in ("slug", "name", "host", "repo", "budget_usd_per_day")},
                                                                          sort_keys=False, allow_unicode=True) +
            "\n## VISION TEMPLATE (fill every section; keep the headings)\n" + vision_template(_profile(raw)))


# ------------------------------------------------------------------ rendering and parsing the owner's file

def render_questions_md(slug: str, res: dict[str, Any]) -> str:
    out = [f"# Онбординг `{slug}` — вопросы и доступы", "",
           "Ответьте под каждым вопросом после `Ответ:` (пусто = ответ по умолчанию) и под каждым доступом после "
           f"`Решение:` (дал / позже / нет — и как передать). Затем: `team onboard {slug} --answers {DIR}/{QUESTIONS_MD}`.", "",
           "## Как мы поняли продукт", str(res.get("summary") or "").strip()]
    if res.get("proposed_name"):
        out += ["", f"Предлагаемое имя: {res['proposed_name']}"]
    out += ["", "## Вопросы"]
    for q in res.get("questions") or []:
        out += ["", f"### {q['id']} · {q.get('topic', '')} · {q['question']}", f"Зачем: {q.get('why', '')}"]
        if q.get("options"):
            out += ["Варианты: " + " · ".join(str(o) for o in q["options"])]
        out += [f"По умолчанию: {q.get('default', '')}", f"{ANSWER_LABEL} "]
    out += ["", "## Доступы и аккаунты"]
    for a in res.get("accounts") or []:
        out += ["", f"### {a['id']} · {a['what']}", f"Зачем: {a.get('why', '')}", f"Когда: {a.get('needed_by', '')}",
                f"Без него: {a.get('without_it', '')}", f"Кто создаёт: {a.get('who_creates', '')}", f"Что передать: {a.get('hand_over', '')}",
                f"{DECISION_LABEL} "]
    if res.get("assumptions"):
        out += ["", "## Что приняли без вопроса (поправьте, если не так)"] + [f"- {s}" for s in res["assumptions"]]
    if res.get("risks"):
        out += ["", "## Риски"] + [f"- {s}" for s in res["risks"]]
    return "\n".join(out) + "\n"


_HEAD = re.compile(r"^###\s+([QA]\d{1,2})\b")


def parse_answers(md: str) -> dict[str, dict[str, str]]:
    """`### Q1 …` / `Ответ: text` and `### A1 …` / `Решение: text` → {"answers": {Q1: text}, "decisions": {A1: text}}.
    Text may continue on the following lines up to the next heading; an empty field is an empty string."""
    answers: dict[str, str] = {}
    decisions: dict[str, str] = {}
    cur: str | None = None
    collecting: str | None = None
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf, collecting
        if cur and collecting:
            text = "\n".join(buf).strip()
            (answers if cur.startswith("Q") else decisions)[cur] = text
        buf, collecting = [], None

    for ln in md.splitlines():
        m = _HEAD.match(ln)
        if m:
            flush()
            cur = m.group(1)
            continue
        if ln.startswith("## "):
            flush()
            cur = None
            continue
        if cur and ln.startswith(ANSWER_LABEL) and cur.startswith("Q"):
            flush(); collecting = "a"; buf = [ln[len(ANSWER_LABEL):].strip()]
        elif cur and ln.startswith(DECISION_LABEL) and cur.startswith("A"):
            flush(); collecting = "d"; buf = [ln[len(DECISION_LABEL):].strip()]
        elif collecting:
            buf.append(ln.rstrip())
    flush()
    return {"answers": answers, "decisions": decisions}


def stray_notes(md: str, rendered: str) -> list[dict[str, str]]:
    """What the owner wrote OUTSIDE the `Ответ:` / `Решение:` fields: lines of the filled file that are neither part of
    an answer or a decision nor a line of the file as it was rendered. Grouped by the heading they stand under.
    28.11, pokerbr К6: a correction written under «Как мы поняли продукт» was read by nobody and the plan ignored it."""
    known = {ln.strip() for ln in rendered.splitlines() if ln.strip()}
    out: list[dict[str, str]] = []
    head, cur, collecting = "(начало файла)", None, False
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf
        if buf:
            out.append({"under": head, "text": "\n".join(buf)[:STRAY_NOTE_CHARS]})
        buf = []

    for ln in md.splitlines():
        text = ln.strip()
        m = _HEAD.match(ln)
        if m or ln.startswith("## ") or ln.startswith("# "):
            flush()
            head, cur, collecting = text.lstrip("# ").strip()[:120], (m.group(1) if m else None), False
            if text not in known:
                buf.append(text)                   # a heading the owner added or rewrote is itself a note
            continue
        if cur and ((ln.startswith(ANSWER_LABEL) and cur.startswith("Q")) or (ln.startswith(DECISION_LABEL) and cur.startswith("A"))):
            flush()
            collecting = True
            continue
        if collecting or not text or text in known:
            continue
        buf.append(text)
    flush()
    return out[:STRAY_NOTES_MAX]


def merge_answers(questions: dict[str, Any], parsed: dict[str, dict[str, str]]) -> dict[str, Any]:
    """The Q&A block for the plan prompt: every question with the owner's answer or its default (marked)."""
    qa = []
    for q in questions.get("questions") or []:
        a = parsed["answers"].get(q["id"], "").strip()
        qa.append({"id": q["id"], "question": q["question"], "answer": a or f"{q.get('default', '')} {DEFAULT_MARK}".strip(),
                   "by_default": not a})
    acc = []
    for a in questions.get("accounts") or []:
        d = parsed["decisions"].get(a["id"], "").strip()
        acc.append({"id": a["id"], "what": a["what"], "needed_by": a.get("needed_by"), "decision": d or "позже (ответа нет)", "answered": bool(d)})
    return {"questions": qa, "accounts": acc}


# ------------------------------------------------------------------ the two steps

def _run(st: C.Settings, p: C.Product, runner: Any, prompt: str, schema_name: str, label: str, cap: float, dry_run: bool) -> dict[str, Any] | None:
    """One onboarding role run through Cycle.role_run (budget, billing, transcript as every other role); None when nothing ran."""
    if dry_run or runner is None:
        return None
    from .cycle import Cycle
    cyc = Cycle(st, p, runner, "onboard-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S"))
    try:
        rr = cyc.role_run(ROLE, prompt, schema_name, cwd=p.work_dir / "onboarding" / label, label=label, cap=cap, allowed_tools=[], max_turns=MAX_TURNS)
    finally:
        cyc.store.close()
    if rr is None or not (rr.ok and rr.structured):
        raise OnboardingError(f"{label}: the onboarding role did not return a result" + (f": {rr.error}" if rr and rr.error else ""))
    return rr.structured


def start(st: C.Settings, slug: str, brief_path: Path, *, name: str | None = None, host: str = "", repo: str = "",
          budget: float | None = None, runner: Any = None, dry_run: bool = False, profile: str | None = None) -> dict[str, Any]:
    """Step 1: create the product folder (if needed), run the questions step, write questions.md, open one owner question.
    `profile`: `game` (default) or `site` — picks the VISION template, the role's instructions and the dry-run fixture."""
    if profile is not None and profile not in PROFILES:
        raise OnboardingError(f"profile {profile!r}: expected one of {', '.join(PROFILES)}")
    d = st.products_dir / slug
    brief = brief_path.read_text(encoding="utf-8")
    if not brief.strip():
        raise OnboardingError(f"{brief_path}: the brief is empty")
    if (d / "product.yaml").exists():
        raw = _read_yaml(d / "product.yaml")
        if product_state(C.load_product(st, d)) == STATE_DONE:
            raise OnboardingError(f"{d}: onboarding already finished; edit VISION.md or create a new product")
    else:
        d.mkdir(parents=True)
        raw = {"slug": slug, "name": name or slug, "host": host, "site_base": f"https://{host}" if host else "",
               "profile": profile or DEFAULT_PROFILE, "repo": {"url": repo or "", "branch": "main", "public_dir": "public"}, "channels": {},
               "targets": [], "budget_usd_per_day": budget, "paused": False}
        (d / "decisions.md").write_text(f"# {raw['name']} — решения\n\n", encoding="utf-8")
    if profile:
        raw["profile"] = profile
    raw["onboarding"] = {"state": STATE_QUESTIONS, "brief": f"{DIR}/{BRIEF_FILE}", "asked": utcnow()}
    _write_yaml(d / "product.yaml", raw)
    od = _dir(d)
    (od / BRIEF_FILE).write_text(brief, encoding="utf-8")
    p = C.load_product(st, d)
    prompt = questions_prompt(slug, brief, raw)
    (od / "questions-prompt.md").write_text(prompt, encoding="utf-8")
    res = _run(st, p, runner, prompt, "onboarding-questions", "onboarding-questions", QUESTIONS_CAP_USD, dry_run)
    source = "model"
    if res is None:
        res, source = fixture(_profile(raw))["questions"], "fixture (dry-run)"
    _write_yaml(od / QUESTIONS_YAML, res)
    md_path = od / QUESTIONS_MD
    md_path.write_text(render_questions_md(slug, res), encoding="utf-8")
    store = Store(d)
    try:
        q = store.ask(from_=ROLE, to="owner",
                      question=f"Онбординг {slug}: {len(res.get('questions') or [])} вопросов и {len(res.get('accounts') or [])} доступов ждут ответа в "
                               f"{md_path}; затем `team onboard {slug} --answers {md_path}`.")
    finally:
        store.close()
    raw["onboarding"]["question"] = q["id"]
    _write_yaml(d / "product.yaml", raw)
    _log(d, "questions", source=source, questions=len(res.get("questions") or []), accounts=len(res.get("accounts") or []), question_id=q["id"])
    log.info("%s: onboarding step 1 done (%s): %s", slug, source, md_path)
    return {"product_dir": str(d), "questions_md": str(md_path), "question_id": q["id"], "source": source,
            "n_questions": len(res.get("questions") or []), "n_accounts": len(res.get("accounts") or [])}


def apply_answers(st: C.Settings, slug: str, answers_path: Path | None = None, *, runner: Any = None, dry_run: bool = False) -> dict[str, Any]:
    """Step 2: the filled questions.md → plan → VISION.md, GAME-DESIGN.md, product.yaml, backlog; state → done."""
    d = st.products_dir / slug
    if not (d / "product.yaml").exists():
        raise OnboardingError(f"{d}: no product; run `team onboard {slug} --brief …` first")
    raw = _read_yaml(d / "product.yaml")
    state = str((raw.get("onboarding") or {}).get("state") or "")
    if state != STATE_QUESTIONS:
        raise OnboardingError(f"{slug}: onboarding state is {state or 'none'!r}, expected {STATE_QUESTIONS!r}")
    od = _dir(d)
    questions = _read_yaml(od / QUESTIONS_YAML)
    md_path = answers_path or (od / QUESTIONS_MD)
    md_text = md_path.read_text(encoding="utf-8")
    parsed = parse_answers(md_text)
    qa = merge_answers(questions, parsed)
    notes = stray_notes(md_text, render_questions_md(slug, questions))
    if notes:
        log.warning("%s: %d piece(s) of text outside the `%s` / `%s` fields of %s — passed to the plan as the owner's notes: %s", slug, len(notes),
                    ANSWER_LABEL, DECISION_LABEL, md_path, "; ".join(f"«{n['under'][:40]}»: {n['text'][:80]}" for n in notes)[:600])
    _write_yaml(od / ANSWERS_YAML, {"source_file": str(md_path), "applied": utcnow(), **qa, **({"notes": notes} if notes else {})})
    brief = (od / BRIEF_FILE).read_text(encoding="utf-8")
    p = C.load_product(st, d)
    prompt = plan_prompt(slug, brief, qa, raw, notes)
    (od / "plan-prompt.md").write_text(prompt, encoding="utf-8")
    plan = _run(st, p, runner, prompt, "onboarding-plan", "onboarding-plan", PLAN_CAP_USD, dry_run)
    source = "model"
    if plan is None:
        plan, source = fixture(_profile(raw))["plan"], "fixture (dry-run)"
    _write_yaml(od / PLAN_YAML, plan)
    created = apply_plan(d, raw, plan, qa)
    _log(d, "plan", source=source, **{k: v for k, v in created.items() if k != "tasks"}, tasks=len(created["tasks"]))
    log.info("%s: onboarding step 2 done (%s): %d tasks, VISION %d chars", slug, source, len(created["tasks"]), len(plan.get("vision_md") or ""))
    return {"source": source, **created, "notes_outside_fields": len(notes)}


def apply_plan(d: Path, raw: dict[str, Any], plan: dict[str, Any], qa: dict[str, Any]) -> dict[str, Any]:
    """Write the plan into the product folder. Pure file/store work, no model: tested directly."""
    vision = str(plan.get("vision_md") or "")
    profile = str((plan.get("product") or {}).get("profile") or _profile(raw))
    if profile == "game" and VISION_STANDING_HEADING.lower() not in vision.lower():
        # owner 21.11: every game VISION names the standing directions; a plan that left the section out gets the default one
        from . import standing
        vision = vision.rstrip("\n") + f"\n\n## {VISION_STANDING_HEADING}\n{standing.vision_section()}\n"
        log.warning("%s: the onboarding plan's VISION had no «%s» section — the default one was appended", d.name, VISION_STANDING_HEADING)
    (d / "VISION.md").write_text(vision, encoding="utf-8")
    gdd = str(plan.get("game_design_md") or "").strip()
    if gdd:
        (d / GAME_DESIGN_FILE).write_text(gdd + "\n", encoding="utf-8")
    prod = plan.get("product") or {}
    raw["profile"] = profile
    raw["stage"] = str(prod.get("stage") or "M0")
    if prod.get("stack"):
        raw["stack"] = prod["stack"]
    repo = dict(raw.get("repo") or {})
    for k, v in (prod.get("repo") or {}).items():
        if v not in (None, "") and not (k == "url" and repo.get("url")):     # a URL the owner passed to `team onboard` wins
            repo[k] = v
    raw["repo"] = repo
    if prod.get("deploy"):
        raw["deploy"] = {k: v for k, v in prod["deploy"].items() if v not in (None, "")} or {"mode": "pending"}
    if prod.get("phase_gates"):
        raw["phase_gates"] = dict(prod["phase_gates"])
    raw.setdefault("onboarding", {})
    raw["onboarding"].update({"state": STATE_DONE, "answered": utcnow()})
    _write_yaml(d / "product.yaml", raw)
    lines = [f"\n## {utcnow()[:10]} — онбординг\n"] + [f"- {s}" for s in plan.get("decisions") or []]
    for a in qa.get("accounts") or []:
        lines.append(f"- доступ {a['id']} ({a['what']}): {a['decision']}")
    with (d / "decisions.md").open("a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    store = Store(d)
    ids: dict[str, str] = {}
    tasks: list[str] = []
    try:
        for bt in plan.get("backlog") or []:
            state = str(bt.get("state") or "ready")
            t = store.create_task(title=bt["title"], type_=bt["type"], problem=bt["problem"], scope=bt.get("scope") or "",
                                  acceptance=list(bt.get("acceptance") or []), negative_cases=list(bt.get("negative_cases") or []),
                                  goal=str(bt.get("goal") or raw.get("stage") or "M0"), created_by=ROLE, priority=bt.get("priority", "P2"),
                                  size=bt.get("size", "S"), role=bt.get("role", "developer"), state=state,
                                  depends_on=[ids[x] for x in (bt.get("depends_on") or []) if x in ids], note="created by onboarding")
            changed = False
            if bt.get("repo_only"):
                t["repo_only"] = True; changed = True
            if state == "deferred":
                t["return_condition"] = str(bt.get("return_condition") or "")[:300]; changed = True
            if changed:
                store.save_task(t)
            ids[bt["title"]] = t["id"]
            tasks.append(t["id"])
        qid = raw["onboarding"].get("question")
        if qid:
            try:
                store.answer(str(qid), "answers applied by `team onboard --answers`", "owner")
            except FileNotFoundError:
                pass
        (d / "digests").mkdir(exist_ok=True)
        (d / "digests" / "onboarding.md").write_text(f"# Онбординг {raw.get('slug', d.name)}\n\n{plan.get('owner_summary_md', '')}\n", encoding="utf-8")
    finally:
        store.close()
    return {"vision": str(d / "VISION.md"), "game_design": str(d / GAME_DESIGN_FILE) if gdd else "", "tasks": tasks,
            "deferred": [tid for tid, bt in zip(tasks, plan.get("backlog") or []) if bt.get("state") == "deferred"],
            "summary_md": str(plan.get("owner_summary_md") or "")}


def status(st: C.Settings, slug: str) -> str:
    p = C.load_product(st, slug)
    s = product_state(p)
    if s is None:
        return f"{slug}: no onboarding record (created with `team init`)"
    ob = p.raw.get("onboarding") or {}
    out = [f"{slug}: onboarding state={s} asked={ob.get('asked', '—')} answered={ob.get('answered', '—')} question={ob.get('question', '—')}"]
    if s == STATE_QUESTIONS:
        out.append(f"  answer {p.dir / DIR / QUESTIONS_MD} then `team onboard {slug} --answers {p.dir / DIR / QUESTIONS_MD}`")
    return "\n".join(out)
