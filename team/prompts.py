"""Prompt builders: what each role sees. Everything a role needs travels in the prompt; roles never read the store."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from . import style
from .config import Product
from .store import Store

MARKETING_PLAN = "marketing-plan.md"       # products/<slug>/marketing-plan.md — the marketer's week, cut by the PO into post tasks
PLAN_MAX_AGE_DAYS = 7                      # older than this → the PO creates a new plan task
MEMORY_DAYS = 7                            # "published in the last N days" and feedback window
CHANNEL_DAYS = 14                          # visits by ?src= window and the "no visits for two weeks" rule


def _y(obj: Any) -> str:
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=110)


def _task_brief(t: dict[str, Any]) -> dict[str, Any]:
    keys = ("id", "title", "type", "state", "priority", "size", "goal", "role", "plan", "repo_only", "not_before", "blocked_by", "problem", "scope",
            "acceptance", "negative_cases", "observations", "links", "continues", "probe_of", "review_fails", "created",
            "standing", "docs", "visible", "redesign", "new_function", "pre_merge")                     # owner 21.11: a standing direction's task says so (team/standing.py)
    out = {k: t.get(k) for k in keys if t.get(k) not in (None, [], "")}
    if t.get("hypothesis"):
        from . import hypothesis
        out["hypothesis"] = hypothesis.brief(t["hypothesis"])
    return out


def _tail(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + f"\n… [cut {len(s) - n} chars]"


FINISHED_STATES = ("accepted", "released", "measured")   # work is done: with `cycle.po_prompt.compact_finished` the PO gets a card, not the text
FINISHED_KEYS = ("id", "title", "state", "type", "role", "goal", "links", "accepted_at")
FINISHED_OUTCOME_CHARS = 200               # of the last result's summary on that card, one line


def finished_brief(t: dict[str, Any]) -> dict[str, Any]:
    """The card of a task whose work is done (owner 26.11, rework slice 1): enough for the PO to avoid a duplicate, to
    continue the work with a new task (`continues`) and to see what came out — `outcome` is one line of the last result's
    summary, else the note of the last transition. Nothing is invented for a task that has neither."""
    out = {k: t.get(k) for k in FINISHED_KEYS if t.get(k) not in (None, [], "")}
    summary = next((str(r.get("summary")) for r in reversed(t.get("results") or []) if isinstance(r, dict) and r.get("summary")), "")
    if not summary:
        summary = next((str(h.get("note")) for h in reversed(t.get("history") or []) if isinstance(h, dict) and h.get("note")), "")
    summary = " ".join(summary.split())
    if summary:
        out["outcome"] = summary if len(summary) <= FINISHED_OUTCOME_CHARS else summary[:FINISHED_OUTCOME_CHARS - 1] + "…"
    return out


def _po_backlog(tasks: list[dict[str, Any]], compact_finished: bool) -> str:
    """`# BACKLOG (open tasks)`: every task that is not done or rejected. With `compact_finished` the finished ones come
    first as cards (they change least from cycle to cycle), the unfinished ones follow with their full text."""
    if not tasks:
        return "(empty)"
    if not compact_finished:
        return _y([_task_brief(t) for t in tasks])
    fin = [t for t in tasks if t.get("state") in FINISHED_STATES]
    rest = [t for t in tasks if t.get("state") not in FINISHED_STATES]
    out = []
    if fin:
        out.append(f"## Work already done ({len(fin)}: accepted, released, measured) — a card per task; `outcome` is one line of its last "
                   "result. Do not create a task that repeats one of these; to build on one, name it in `continues`.\n"
                   + _y([finished_brief(t) for t in fin]))
    out.append(f"## Unfinished tasks ({len(rest)}) — full text\n" + (_y([_task_brief(t) for t in rest]) if rest else "(none)"))
    return "\n".join(out)


CLOSED_STATES = ("done", "rejected")        # `# CLOSED TASKS` of the PO's prompt (`cycle.duplicates.closed_list`, rework slice 3)
CLOSED_LIST_DAYS = 7                       # closed within this many days
CLOSED_LIST_MAX = 80                       # newest of them
CLOSED_WHY_CHARS = 160                     # of the note the task was closed with


def recently_closed(store: Store, days: int, states: tuple[str, ...] = CLOSED_STATES) -> list[dict[str, Any]]:
    """Tasks in `states` whose last change is at most `days` old, oldest first."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    ts = [t for t in store.tasks(list(states)) if str(t.get("updated") or t.get("created") or "") >= since]
    return sorted(ts, key=lambda t: str(t.get("updated") or t.get("created") or ""))


def closed_block(store: Store) -> str:
    """`# CLOSED TASKS`: one line per task closed in the last `CLOSED_LIST_DAYS` days — id, state, title and why it was
    closed. Before 26.11 the PO saw no closed task at all and re-created work under new ids. "" when there is none."""
    ts = recently_closed(store, CLOSED_LIST_DAYS)
    if not ts:
        return ""
    shown = ts[-CLOSED_LIST_MAX:]
    lines = []
    for t in shown:
        why = next((str(h.get("note")) for h in reversed(t.get("history") or []) if isinstance(h, dict) and h.get("note")), "")
        why = " ".join(why.split())
        lines.append(f"- {t['id']} [{t.get('state')}] {' '.join(str(t.get('title') or '').split())[:140]}"
                     + (f" — {why if len(why) <= CLOSED_WHY_CHARS else why[:CLOSED_WHY_CHARS - 1] + '…'}" if why else ""))
    head = (f"\n\n# CLOSED TASKS (done or rejected in the last {CLOSED_LIST_DAYS} days"
            + (f", the {CLOSED_LIST_MAX} newest of {len(ts)}" if len(ts) > len(shown) else "")
            + ") — do not create a task that repeats one of these. A rejected task was closed for the reason on its line: a new task "
              "for the same goal must differ in what made the old one fail and name it in `continues`.\n")
    return head + "\n".join(lines)


def po_prompt(product: Product, store: Store, *, cycle_id: str, observations: list[dict[str, Any]], questions: list[dict[str, Any]],
              metrics_md: str, spent_today: float, cap: float, max_ready: int, idle: bool, dev_idle: bool = False,
              channel_caps: dict[str, Any] | None = None, stuck_md: str = "", queue_md: str = "") -> str:
    cfg = product.po_prompt_cfg
    tasks = [t for t in store.tasks() if t.get("state") not in ("done", "rejected")]
    recent_done = [t for t in store.tasks(["done", "measured", "accepted", "released"])][-10:]
    obs_txt = "\n\n".join(_y({k: v for k, v in o.items() if k not in ("fingerprint",)}) for o in observations) or "(none)"
    q_txt = "\n\n".join(_y(q) for q in questions) or "(none)"
    answered = [q for q in store.questions(["answered"]) if q.get("to") == "owner" and str(q.get("answered") or "") >= _hours_ago(72)][-8:]
    answered_txt = "\n\n".join(_y({k: q.get(k) for k in ("id", "question", "answer", "answered_by", "answered")}) for q in answered) or "(none)"
    fw_txt = framework_context(product)
    mkt_txt = po_marketing_block(product, store, caps=channel_caps)
    game_txt = "\n\n".join(x for x in (game_block(product), standing_block(product, store)) if x)
    this_cycle = (f"Spend today ${spent_today:.2f} of the daily cap ${cap:.2f}. Propose at most {max_ready} Ready tasks."
                  + (" The team is idle: no task is ready or in progress — if VISION promises daily work, create it." if idle else "")
                  + (" The developer has no ready work: queue 1–2 tasks of type `defect` or `quality` (see the quality rule) from failing "
                     "SEO/GEO checks, review fails, persona `ux_friction`, critic findings or facts on the live site, each naming its source "
                     "in `problem`; if nothing is broken or measurably improvable, say so in the digest and invent no feature."
                     if dev_idle and not idle else "")
                  + (("\n\n" + queue_md) if queue_md else ""))
    body = ("# VISION\n" + product.vision()
            + (("\n\n" + stuck_md) if stuck_md else "")
            + run_time_block(product, store)
            + "\n\n# BACKLOG (open tasks)\n" + _po_backlog(tasks, cfg["compact_finished"])
            + "\n\n# RECENTLY FINISHED\n" + (_y([{k: t.get(k) for k in ("id", "title", "state")} for t in recent_done]) if recent_done else "(none)")
            + (closed_block(store) if product.duplicates_cfg["closed_list"] else "")
            + "\n\n# OPEN QUESTIONS TO YOU\n" + q_txt
            + "\n\n# QUESTIONS THE OWNER ALREADY ANSWERED (last 72 h — do not ask these again)\n" + answered_txt
            + (("\n\n# FRAMEWORK CONTEXT (this product is the framework itself)\n" + fw_txt) if fw_txt else "")
            + (("\n\n" + game_txt) if game_txt else "")
            + (("\n\n" + mkt_txt) if mkt_txt else "")
            + "\n\n# VOICE OF THE USER (weekly research: real people's words about the problem)\n" + _voice_block(product)
            + "\n\n# READER EXPERIENCE (personas, scores 1–5)\n" + _ux_block(product)
            + "\n\n# NEW OBSERVATIONS\n" + obs_txt
            + "\n\n# LATEST METRICS\n" + (_tail(metrics_md, 6000) or "(no metrics yet)") + "\n")
    if cfg["stable_first"]:
        # what does not change between two cycles goes first, the cycle number and the spend last: two runs then share the
        # whole head of the prompt (before 26.11 they shared 18 characters, so nothing of it was read from the prompt cache)
        return (f"# PO triage for product `{product.slug}` ({product.site_base})\n\n" + body
                + f"\n# THIS CYCLE\nCycle {cycle_id}. " + this_cycle + "\n")
    return f"# Cycle {cycle_id} — PO triage for product `{product.slug}` ({product.site_base})\n\n" + this_cycle + "\n\n" + body


def _voice_block(product: Product) -> str:
    from . import voice
    try:
        return voice.prompt_block(product.dir)
    except Exception:  # noqa: BLE001
        return "(unavailable)"


def _ux_block(product: Product) -> str:
    from . import ux
    try:
        return ux.prompt_block(product)
    except Exception:  # noqa: BLE001
        return "(unavailable)"


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


GDD_FILE = "GAME-DESIGN.md"                # game profile: the game designer's document in the repository (or the product folder before a repo exists)
DESIGN_FILE = "DESIGN.md"                  # game profile: the designer's visual system
PROTOCOL_FILE = "PROTOCOL.md"              # service repository: the client ↔ server message contract
BALANCE_DIR = "design/balance"             # game profile: balance tables as data
# The game designer has no shell and returns COMPLETE documents, so the whole current text must travel in the prompt
# (F18, ai-battles 20.11: GAME-DESIGN.md outgrew the old 12 000-char tail and balance.json the 3 000-char one — the
# role saw a document without its head and rightly refused to overwrite it). `design: {gdd_chars, table_chars}` in
# product.yaml overrides the limits.
GDD_PROMPT_CHARS = 120000
BALANCE_TABLE_CHARS = 40000
BALANCE_TABLES_MAX = 8
TASK_DOC_CHARS = 40000                     # a task's own document (`docs`, standing directions): the design-spec limit of one returned file
TASK_DOCS_MAX = 4
GD_METRICS_CHARS = 8000                    # metrics/latest.md in the game designer's prompt (day table + the balance table)
GD_OBS_CLASSES = ("defect", "ux_friction", "value_gap", "feedback", "balance")   # observation classes the game designer reads
TRUNCATED_NOTE = ("(TRUNCATED: only the last {shown} of {total} characters are shown. Do NOT return this file in `files` — a rewrite "
                  "from a truncated copy deletes content. Put the change into a new file and ask the PO to split the document.)\n")
SERVICE_TEMPLATES = Path(__file__).parent / "templates" / "service-repo"   # starter playwright.config.ts, tests/e2e, tests/load, PROTOCOL.md
SHOTS_SCRIPT_REL = "tests/tools/battle-shots.mjs"   # the repository's battle-frames script (`team-play shots`, owner 22.11)
LOAD_SCRIPT_REL = "tests/load/clients.ts"   # the repository's load script (`team-play load`, slice 6)
GAME_METRICS_DOC = Path(__file__).parent.parent / "docs" / "game-metrics.md"   # the /_team/metrics.json contract a game server serves


def _read_tail(p: Path, n: int) -> str:
    try:
        return _tail(p.read_text(encoding="utf-8", errors="replace"), n) if p.exists() else ""
    except OSError:
        return ""


def _design_limits(product: Product) -> tuple[int, int]:
    cfg = product.raw.get("design") or {}
    try:
        return int(cfg.get("gdd_chars") or GDD_PROMPT_CHARS), int(cfg.get("table_chars") or BALANCE_TABLE_CHARS)
    except (TypeError, ValueError):
        return GDD_PROMPT_CHARS, BALANCE_TABLE_CHARS


def _doc_for_rewrite(p: Path, limit: int) -> tuple[str, bool]:
    """A document the role may return whole: the full text when it fits `limit`, else its tail with an explicit note."""
    try:
        text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
    except OSError:
        return "", False
    if len(text) <= limit:
        return text, False
    return TRUNCATED_NOTE.format(shown=limit, total=len(text)) + text[-limit:], True


def _balance_files(gdd_dir: Path) -> list[Path]:
    bdir = gdd_dir / BALANCE_DIR
    return sorted(f for f in bdir.glob("*") if f.is_file())[:BALANCE_TABLES_MAX] if bdir.exists() else []


def task_docs(task: dict[str, Any] | None) -> list[str]:
    """Repository-relative documents a task maintains (`docs` of a standing direction, team/standing.py): the game designer
    gets each of them whole and returns it complete. Unsafe paths are dropped."""
    docs = (task or {}).get("docs") or []
    return [str(d) for d in (docs if isinstance(docs, list) else [docs]) if str(d).strip() and safe_rel_path(str(d))][:TASK_DOCS_MAX]


def task_docs_for(product: Product, task: dict[str, Any] | None) -> list[str]:
    """`task_docs`, and a task of a standing direction that lost its `docs` (made by hand, or by an older dispatcher) gets the
    direction's own documents: DOCUMENTS OF THIS TASK is never missing for `standing: mechanics` (F-0605)."""
    docs = task_docs(task)
    sid = (task or {}).get("standing")
    if docs or not sid:
        return docs
    from . import standing
    d = next((d for d in standing.directions(product) if d.id == str(sid)), None)
    return task_docs({"docs": list(d.docs)}) if d else docs


REPORT_RE = re.compile(r"(?<![\w/.-])(reports/[\w./-]+\.(?:md|json|txt|csv))")
REPORTS_MAX = 5
REPORT_CHARS = 6000


def task_reports(task: dict[str, Any] | None) -> list[str]:
    """Safe `reports/...` paths the task text names (title, problem, scope, acceptance), in order, without repeats."""
    text = _y(_task_brief(task or {}))
    out: list[str] = []
    for m in REPORT_RE.findall(text):
        if m not in out and safe_rel_path(m):
            out.append(m)
    return out[:REPORTS_MAX]


FILE_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)+[\w.-]+\.(?:md|json|txt|csv|ts|tsx|js|mjs|css|html|yaml|yml))")
TASK_FILES_MAX = 12


def task_files(task: dict[str, Any] | None) -> list[str]:
    """Safe repository-relative file paths the task text names (title, problem, scope, acceptance), in order, no repeats."""
    out: list[str] = []
    for m in FILE_RE.findall(_y(_task_brief(task or {}))):
        if m not in out and safe_rel_path(m):
            out.append(m)
    return out[:TASK_FILES_MAX]


def truncated_design_docs(product: Product, gdd_dir: Path, task: dict[str, Any] | None = None) -> set[str]:
    """Repository-relative paths the game designer saw only in part: the dispatcher must not let it overwrite them."""
    gdd_limit, table_limit = _design_limits(product)
    out = set()
    if _doc_for_rewrite(gdd_dir / GDD_FILE, gdd_limit)[1]:
        out.add(GDD_FILE)
    for d in task_docs_for(product, task):
        if _doc_for_rewrite(gdd_dir / d, TASK_DOC_CHARS)[1]:
            out.add(d)
    for f in _balance_files(gdd_dir):
        if _doc_for_rewrite(f, table_limit)[1]:
            out.add(f"{BALANCE_DIR}/{f.name}")
    return out


def safe_rel_path(path: str) -> bool:
    """A repository-relative path a role may write: no absolute paths, no `..`, no .git."""
    p = Path(path)
    return bool(path) and not p.is_absolute() and ".." not in p.parts and ".git" not in p.parts and not path.startswith("/")


def service_block(product: Product, repo_dir: Path, role: str = "developer") -> str:
    """Game profile / service repository (owner 20.11): what replaces «the site is static» in the developer's prompt — the
    commands the dispatcher and the host will run, the protocol contract and the design documents in the checkout."""
    if product.repo_kind != "service" and product.profile != "game":
        return ""
    cmds = {k: v for k, v in (("run", product.run_cmd), ("build", product.build_cmd), ("test", product.test_cmd),
                              ("health", product.health_path), ("port", product.port)) if v}
    out = ["\n\n# SERVICE REPOSITORY (not a static site)",
           f"Profile `{product.profile}`, stage `{product.stage}`. This repository is a long-running process, not a `public/` folder: "
           "the server owns the state (authoritative game loop), the client only renders and sends intents; an invalid move is "
           "rejected by the server. Commands the dispatcher and the host run (product.yaml `repo`): " + (_y(cmds).strip() if cmds else "(none set yet — add them to README)") + ".",
           f"`{PROTOCOL_FILE}` is the client ↔ server contract: change it only when the task names it in scope, and update both sides in the same commit. "
           "Run the tests and start the server locally before reporting done; a task is not done while `test` fails or the health path does not answer.",
           "Acceptance is played, not read: the tester role starts the server from your merged commit and runs `tests/e2e/*.spec.ts` with "
           "Playwright (two browser contexts: player A acts, player B's page changes). Every acceptance criterion of your task needs a "
           "scenario there, with `data-testid` hooks on the elements it uses (`room-code`, `status`, `error`, `cell-<r>-<c>`, …). A starter "
           f"config, a two-player spec and a PROTOCOL skeleton are in the framework at `{SERVICE_TEMPLATES}` (copy, then adapt). "
           "The server must honour `PORT` from the environment (or `{port}` in run_cmd) and answer the health path with 200. "
           f"On the host it also gets `DATA_DIR` (files and SQLite live there, they survive releases) and `RELEASE_SHA`. "
           f"It serves its own metrics at `/_team/metrics.json` — the contract is `{GAME_METRICS_DOC}` in the framework (kind, days, rounds, "
           "players, latency, rejected moves, and the optional `balance` block: picks and wins per option, stamped with the balance "
           "tables version); the PO, analyst and game designer read nothing else about play. A load task (role tester) runs "
           f"`tests/load/clients.ts` (starter in `{SERVICE_TEMPLATES}/tests/load/`) with N WebSocket clients: keep it runnable."]
    if role == "designer":
        out.append(f"You are the designer: your files are `{DESIGN_FILE}`, styles and assets (SVG, CSS). Do not change server code or game rules; "
                   "a rule that looks wrong goes into `proposals`.")
    for name, n in ((GDD_FILE, 4000), (PROTOCOL_FILE, 3000), (DESIGN_FILE, 2000 if role == "designer" else 800)):
        txt = _read_tail(repo_dir / name, n)
        if txt:
            out.append(f"\n--- {name} ---\n{txt}")
    return "\n".join(out)


BUDGET_WRAP_UP_SHARE = 2 / 3               # share of a run's time budget after which a repository role stops starting new work


def run_time_block(product: Product, store: Store) -> str:
    """`# RUN TIME` for the PO (owner 21.11, team/runprofile.py): one line per run since its last triage that took long
    or was stopped by a limit — «developer T-0042: 1500 s (tools 1252 s), e2e 99 % in 20 calls, killed by the time
    limit» — so it re-scopes a task that does not fit into a run. Empty when there was no such run."""
    from . import runprofile
    txt = runprofile.po_block(store, product)
    return ("\n\n" + txt) if txt else ""


def budget_block(budget_s: int | None, continuation: dict[str, Any] | None = None, *, now: datetime | None = None) -> str:
    """Owner 21.11 (frictions F23/F30): a role that works in a repository knows the time budget of its run and, on a
    continuation, what the stopped run left behind and where its time went. `continuation` comes from
    `Cycle.continuation_context` (wip commit, its --stat, the run profile, continuations used / allowed)."""
    out = ""
    if budget_s:
        total, wrap = max(1, round(budget_s / 60)), max(1, round(budget_s * BUDGET_WRAP_UP_SHARE / 60))
        t0 = now or datetime.now(timezone.utc)
        started, stops, wrap_at = (t.strftime("%H:%M") for t in (t0, t0 + timedelta(seconds=budget_s), t0 + timedelta(minutes=wrap)))
        # ai-battles 21.11 (F35): the sentence used to read «stops this run 45 minutes after its start (≈ 18:00 UTC …)» with the START
        # time in the brackets; the developer took it for the stop time, saw the clock at 18:00 and returned `partial` in ten
        # seconds — twice per task, and three partials blocked T-0044 and T-0038 as stuck. Both ends are now named.
        out += ("\n\n# TIME BUDGET OF THIS RUN\n"
                f"This run STARTED at ≈ {started} UTC and has a fresh budget of {total} minutes: the dispatcher stops it at ≈ {stops} UTC, "
                f"not earlier (`date -u +%H:%M` shows the clock). Time used by earlier runs of this task does not count, and a clock "
                f"that shows about {started} means the run has just begun — do the work. The limit is hard, {budget_s} s, and includes "
                f"every command you wait for. When about two thirds of it are used (≈ {wrap} minutes, about {wrap_at} UTC): "
                "stop starting new work, make sure the tree builds and the tests you already have pass, commit, and return "
                "`status: partial` with a `summary` that says what is done and lists what is left. A partial result is continued next "
                "cycle on this branch with your summary in front of the next run; a run stopped by the limit loses its reasoning. "
                "Long commands eat the same budget: while you work on one e2e spec run that spec, and the full suite once before "
                "you report; do not repeat a command that already gave its answer.")
    c = continuation
    if c:
        why = {"timeout": "was stopped by the time limit", "inactivity": "was stopped by the inactivity watchdog (no output for too long)",
               "loop": "was stopped by the loop guard: it repeated one command with the same output again and again",
               "partial": "returned a partial result"}.get(str(c.get("reason")), "did not finish")
        after = f" after {c['duration_s']} s" if c.get("duration_s") and c.get("reason") != "partial" else ""
        out += f"\n\n# CONTINUATION — DO NOT START OVER\nThe previous run of this task {why}{after}."
        if c.get("checkout"):
            out += (" You are in the same checkout it worked in (the task's own git worktree): what it installed or built there "
                    "and git ignores (node_modules, build output) is still in place unless the checkout was created anew.")
        if c.get("wip_commit"):
            out += (f" The dispatcher saved its uncommitted work on this branch as the service commit `{str(c['wip_commit'])[:10]}` "
                    f"(«wip: …», {c.get('wip_files') or '?'} file(s)):\n```\n{c.get('stat') or '(no stat)'}\n```\n"
                    f"Start with `git show --stat {str(c['wip_commit'])[:10]}` and `git log --oneline {c.get('base') or 'main'}..HEAD`, read the "
                    "changed files, and continue from there. Do not rewrite what is already written, do not reset the branch or drop that "
                    "commit: build on it with new commits. It was taken as is — it may not build and may hold scratch files (test output, "
                    "logs); fix or remove them before you report done.")
        elif c.get("wip_lost"):
            out += (" Its service commit is not on this branch any more; `git log --oneline "
                    f"{c.get('base') or 'main'}..HEAD` shows what the branch holds — continue from that.")
        else:
            out += (f" Nothing uncommitted was left; the branch is {c.get('ahead') or 0} commit(s) ahead of {c.get('base') or 'main'} "
                    f"(`git log --oneline {c.get('base') or 'main'}..HEAD`) — read them and continue from there, do not redo them.")
        if c.get("profile_md"):
            out += ("\nWhere the stopped run spent its time (% = share of the time inside tool calls):\n" + str(c["profile_md"])
                    + "\nDo not spend this run the same way: fix the cause instead of re-running the same command.")
        if c.get("reason") != "partial":
            left = int(c.get("max") or 0) - int(c.get("used") or 0)
            out += (f"\nThis is continuation {c.get('used')} of {c.get('max')}"
                    + (f"; after {left} more stopped run(s) the task is blocked and the PO splits it." if left > 0
                       else ": if this run is stopped too, the task is blocked and the PO splits it.")
                    + " If what is left does not fit into this run, return `partial` early with a precise list of what is left.")
    return out


ANSWERS_MAX = 5                            # answered questions of one task shown to its role (newest last)


REF_RE = re.compile(r"\b(OBS-\d{3,6}|Q-\d{3,6})\b")
REFS_MAX = 6                               # observations and questions of one task shown to its role
REF_CHARS = 1800                           # of one of them
REFS_TOTAL_CHARS = 7000                    # of the whole block


def task_refs(task: dict[str, Any] | None) -> list[str]:
    """Ids of observations and questions a task names — in its title, problem, scope, acceptance, negative cases, its
    `observations` list and its `links` — in order, without repeats."""
    t = task or {}
    text = " ".join(str(x) for x in (t.get("title"), t.get("problem"), t.get("scope"), t.get("acceptance"), t.get("negative_cases"),
                                     t.get("observations"), t.get("links")) if x)
    return list(dict.fromkeys(REF_RE.findall(text)))[:REFS_MAX]


def references_block(store: Store, task: dict[str, Any] | None) -> str:
    """`# WHAT THIS TASK REFERS TO` (`cycle.escalation.references`, 27.11): the text of the observations and questions the
    task names. A role cannot open the store; before this it asked for the text and the task waited a cycle for the
    answer (ai-battles T-0624 and observation OBS-2063). "" when the task names none."""
    ids = task_refs(task)
    if not ids:
        return ""
    obs = {o.get("id"): o for o in store.observations()} if any(i.startswith("OBS-") for i in ids) else {}
    qs = {q.get("id"): q for q in store.questions()} if any(i.startswith("Q-") for i in ids) else {}
    parts, left = [], REFS_TOTAL_CHARS
    for i in ids:
        if i.startswith("OBS-"):
            o = obs.get(i)
            body = _y({k: o.get(k) for k in ("title", "source", "observed_at", "classification", "actual", "expected", "url") if o.get(k)}) if o else ""
        else:
            q = qs.get(i)
            body = _y({k: q.get(k) for k in ("from", "to", "question", "answer", "answered_by") if q.get(k)}) if q else ""
        if not body:
            body = "(not found in the product's records: the task names it, nobody can show it — say so in `missing` if you need it)\n"
        body = _tail(body, min(REF_CHARS, max(200, left)))
        parts.append(f"--- {i} ---\n{body}")
        left -= len(body)
        if left <= 0:
            break
    return ("\n\n# WHAT THIS TASK REFERS TO (the observations and questions it names, as recorded — read them, do not ask for them)\n"
            + "\n".join(parts))


def answers_block(answers: list[dict[str, Any]] | None) -> str:
    """`# ANSWERS TO THIS TASK'S QUESTIONS` (rework slice 2a): what the PO or the owner answered to the questions this task
    was stopped on — before 26.11 the answer restarted the task and the role never saw it. Empty without answers."""
    if not answers:
        return ""
    return ("\n\n# ANSWERS TO THIS TASK'S QUESTIONS (decided: act on them, do not raise the same point again)\n"
            + _y([{k: a.get(k) for k in ("question", "answer", "answered_by") if a.get(k)} for a in answers[-ANSWERS_MAX:]]))


CONFLICT_BLOCK_CHARS = 5000                # of the conflict text (git's CONFLICT lines and the other side's diff) in the prompt


def conflict_block(conflict: dict[str, Any] | None, base: str = "main") -> str:
    """Owner 21.11 (parallel lanes): the previous run's branch did not merge into the base branch — another task, working
    at the same time in its own checkout, changed the same lines and was merged first. `conflict` is the task's
    `merge_conflict` record (`Cycle.merge_conflict_back`): files, git's text, how many times in a row."""
    c = conflict
    if not c:
        return ""
    base = str(c.get("base") or base)
    files = ", ".join(f"`{f}`" for f in (c.get("files") or [])) or "(see below)"
    text = str(c.get("text") or "")
    if len(text) > CONFLICT_BLOCK_CHARS:
        text = text[:CONFLICT_BLOCK_CHARS] + "\n… [cut]"
    return ("\n\n# MERGE CONFLICT — RECONCILE FIRST, DO NOT REDO THE TASK\n"
            f"Your previous run finished, but its branch did not merge into `{base}`: another task of this product was working at the "
            f"same time in its own checkout, changed the same lines and was merged first. Nothing of your branch was merged. "
            f"Conflicting files: {files}.\n"
            f"The dispatcher did NOT merge `{base}` into your branch and resolved nothing for you (it never drops one side of a "
            f"conflict). What the other side did:\n```\n{text or '(no text recorded)'}\n```\n"
            f"Do this first: run `git fetch origin 2>/dev/null; git merge origin/{base}` (in a repository without a remote: `git merge {base}`), "
            f"open every conflicting file and resolve it so that BOTH changes are kept and work together — never take one side "
            f"wholesale, never delete the other task's lines to make the conflict go away (`git diff {base}...HEAD -- <file>` shows "
            f"your side, `git diff HEAD...origin/{base} -- <file>` the other one). Run the tests, commit the merge. Then report `done` "
            "as usual — the rest of your task is already on this branch."
            + (f" This is conflict {c.get('count')} in a row on this task." if int(c.get("count") or 1) > 1 else ""))


def developer_prompt(product: Product, task: dict[str, Any], repo_dir: Path, branch: str, previous: list[dict[str, Any]],
                     review: dict[str, Any] | None, role: str = "developer", *, budget_s: int | None = None,
                     continuation: dict[str, Any] | None = None, conflict: dict[str, Any] | None = None, base: str = "main",
                     port: int | None = None, answers: list[dict[str, Any]] | None = None, refs: str = "") -> str:
    previous = [{k: v for k, v in r.items() if k != "profile_md"} for r in previous]     # the profile is shown once, in the continuation block
    readme = ""
    seen: set[Path] = set()
    for name in ("README.md", "readme.md", "AGENTS.md"):
        p = repo_dir / name
        if p.exists() and p.resolve() not in seen:          # case-insensitive file systems: README.md == readme.md
            seen.add(p.resolve())
            readme += f"\n\n--- {name} ---\n" + _tail(p.read_text(encoding="utf-8", errors="replace"), 8000)
    prev = ("\n\n# PREVIOUS ATTEMPTS ON THIS TASK\n" + _y(previous[-2:])) if previous else ""
    rev = ("\n\n# REVIEW THAT FAILED (fix this first)\n" + _y({k: review.get(k) for k in ("verdict", "first_failure", "criteria", "summary")})) if review else ""
    from . import nodecache
    ready = nodecache.seeded(repo_dir)             # 27.11: the dispatcher put a ready node_modules for this lock file into the checkout
    test_cmd = nodecache.strip_install(product.test_cmd) if ready and product.test_cmd else product.test_cmd
    tests = (f"Tests: run `{test_cmd}` from the checkout root before reporting done (it creates its own venv; "
             "the checkout has no .venv). A task is not done while that command fails.\n\n") if test_cmd else ""
    if ready:
        tests += ("Dependencies are already installed: `node_modules` here matches this checkout's `package-lock.json`. Do NOT run `npm ci` "
                  "or `npm install` (it takes minutes and removes what is there) unless your task changes `package.json` or the lock file — "
                  "then install once and say so in `changes`.\n\n")
    probe = ("# PROBE (stuck-goal replan): answer one question, do not implement\n"
             f"Task {task.get('probe_of')} failed several times. This run tests ONE alternative approach: run the check in the "
             "acceptance (commands, fetches, comparisons in a scratch directory), do not change or commit product code; nothing "
             "you do here is merged. Report `done` with one self_check per criterion: `met: true` only when the evidence shows the "
             "approach gives what the goal needs, `met: false` with the output that rules it out. A clear «does not work» is a "
             "good result.\n\n") if task.get("probe_of") else ""
    return (probe + f"# TASK {task['id']} — product `{product.slug}` ({product.site_base or 'no live site yet'})\n\n"
            f"You are the `{role}` role in the repository checkout `{repo_dir}` on branch `{branch}`. Commit there; do not push.\n"
            "This checkout is the task's own git worktree: other roles may be working on other tasks of this repository at the same "
            f"time, each in its own checkout. Stay on `{branch}` in this directory — do not switch branches, do not touch other "
            f"worktrees or `{base}`, do not use `git stash` (it is shared between the checkouts); the dispatcher merges your branch "
            f"into `{base}` when you report done. Files git ignores are not shared between checkouts: if dependencies are missing "
            "here (no `node_modules`, no virtualenv), install them first.\n"
            + (f"If you start the server or browser tests in this run, use port `{port}` (it is in the environment as `TEAM_PORT`): "
               f"`PORT={port}` for the server and `BASE_URL=http://127.0.0.1:{port}` for the tests. Another role's run may be using the "
               "repository's default port at this very moment. Stop what you started before you report.\n" if port else "")
            + "\n" + tests +
            "# TASK\n" + _y(_task_brief(task)) + refs + answers_block(answers) + prev + rev
            + conflict_block(conflict, base)
            + budget_block(budget_s, continuation)
            + "\n\n# VISION\n" + product.vision()
            + service_block(product, repo_dir, role)
            + "\n\n# REPOSITORY NOTES" + (readme or "\n(no README — follow the conventions you find in the tree)") + "\n")


def tester_prompt(product: Product, task: dict[str, Any], result: dict[str, Any] | None, sha: str | None, *, base_url: str,
                  local: bool, checkout: Path, out_dir: Path, tests: str, server_notes: list[str] | None = None,
                  test_flags: list[str] | None = None, unit_log: bool = False) -> str:
    """The tester's prompt (game profile, slice 4): the task, the implementer's claim, where the server runs and what
    `team-play` can do there, the dispatcher's unit-test run, the checkout to read."""
    specs = sorted(str(p.relative_to(checkout)) for p in (checkout / "tests" / "e2e").glob("*.spec.*")) if (checkout / "tests" / "e2e").exists() else []
    return (f"# PLAY-TEST of task {task['id']} — product `{product.slug}` — stage {product.stage}\n\n"
            "# TASK\n" + _y(_task_brief(task))
            + "\n\n# IMPLEMENTER'S REPORT (a claim, not evidence)\n" + (_y(result) if result else "(none)")
            + "\n\n# PLAY-TEST SERVER\n"
            + (f"Base URL: {base_url} — " + ("started by the dispatcher from commit " + (sha or "?") + " in a copy of the checkout; it is stopped after your run."
               if local else "the deployed host (the merged commit should be live there; compare /healthz version with the expected commit).")
               + "\n" + ("\n".join(f"- {n}" for n in (server_notes or [])))
               + f"\nCheckout to read (Read/Glob/Grep): `{checkout}`. Review folder for screenshots and artifacts: `{out_dir}`.\n"
               + "`team-play test` runs the Playwright scenarios of the checkout against the base URL; `team-play screenshot <url> --out name.png` "
               "takes a picture of a page; `team-play logs` shows the server log.\n"
               + (f"Battle frames: `{SHOTS_SCRIPT_REL}` is present — `team-play shots --weapon KEY --ability KEY --prefix {task['id']}` plays one battle "
                  "and writes frames (start, own shot, own ability, driving) into the review folder; Read them and compare with the design.\n"
                  if (checkout / SHOTS_SCRIPT_REL).exists() else
                  f"No `{SHOTS_SCRIPT_REL}` in the checkout: nobody can look at a battle; if the task changes what the player sees in a battle, say so in `limits`.\n")
               + ("E2E specs present: " + ", ".join(specs) if specs else "No tests/e2e specs in the checkout: judge from screenshots, /healthz and the server log, and say in `limits` which spec file is missing."))
            + ("\nTest server: what the play-test server above cannot show (a feature that is switched off there, text from the dictionary "
               f"provider) is shown by a server of this checkout with the test settings — `team-play shots --local --prefix {task['id']}` and "
               "`team-play screenshot /<path> --local --out <name>.png` start it, use it and stop it. "
               + (f"Flags you may switch with `--env NAME=VALUE`: {', '.join(test_flags)}." if test_flags else "No flag can be changed.")
               if test_flags is not None else "")
            + "\n\n# TEST RUN (unit tests, by the dispatcher)\n" + (tests or "(none)")
            + ("\nOnly the end of the output is shown. `team-play unit --grep <test file or name>` prints the lines of the WHOLE output "
               "that name it (and the three lines after each): use it when the acceptance names a unit test you do not see above."
               if unit_log else "")
            + "\n\nPlay every acceptance criterion and negative case in the browsers; quote test lines and screenshot names as evidence.\n")


def load_prompt(product: Product, task: dict[str, Any], sha: str | None, *, base_url: str, local: bool, checkout: Path, out_dir: Path,
                server_notes: list[str] | None = None) -> str:
    """The tester's prompt for a `load` task (slice 6): the thresholds are in the task's acceptance; `team-play load` runs the
    repository's load script; metrics and the server log before/after are the evidence."""
    script = checkout / LOAD_SCRIPT_REL
    latest = _read_tail(product.dir / "metrics" / "latest.md", 2500)
    return (f"# LOAD TASK {task['id']} — product `{product.slug}` — stage {product.stage}\n\n"
            "# TASK (the acceptance criteria hold the thresholds: clients, seconds, p95 latency, errors)\n" + _y(_task_brief(task))
            + "\n\n# SERVER UNDER LOAD\n"
            + (f"Base URL: {base_url} — " + ("started by the dispatcher from commit " + (sha or "?") + " in a copy of the checkout; stopped after your run."
               if local else "the deployed host: real players may be online, keep the run inside the task's numbers.")
               + "\n" + "\n".join(f"- {n}" for n in (server_notes or [])))
            + f"\nCheckout to read: `{checkout}`. Review folder: `{out_dir}`.\n"
            + (f"Load script present: `{LOAD_SCRIPT_REL}`." if script.exists()
               else f"No `{LOAD_SCRIPT_REL}` in the checkout: `team-play load` cannot run; verdict `inconclusive`, and say in `limits` that the repository needs the script "
                    f"(starter in the framework's `{SERVICE_TEMPLATES}/tests/load/`).")
            + "\n\n# HOW\n"
            "1. `team-fetch <base>/healthz` and `team-fetch <base>/_team/metrics.json` before the run (version, errors, online).\n"
            "2. `team-play load --clients N --seconds S [--clients-per-room K] [--rate R] [--moves]` with the task's numbers (start smaller if the task allows a ramp); "
            "a task that names rooms («5 rooms of 4») needs `--clients-per-room 4`; keep S no longer than one battle. Quote the summary line. "
            "A line `SERVER REDEPLOYED DURING THE RUN` means a deploy dropped the clients: that run is `inconclusive`, repeat it.\n"
            "3. `team-play logs` and the metrics again after the run: server errors, disconnects, rejected moves.\n"
            "4. `test-report`: `latency_ms` {p50, p95, samples, how: 'team-play load N clients S s'}; one `scenarios` entry per run with `clients`; "
            "`pass` only when every threshold in the acceptance is met by the numbers you quote, `fail` names the first one missed, "
            "`inconclusive` when the script or the server did not run. A crash or a stack trace under load is a `reproducible_defect` observation.\n"
            + "\n# LATEST GAME METRICS (before this run)\n" + (latest or "(none yet)") + "\n")


def game_block(product: Product, gdd_dir: Path | None = None) -> str:
    """What the PO sees on a game product (owner 20.11): stage, gates, the game design document's outline, the rules."""
    if product.profile != "game":
        return ""
    gates = product.phase_gates
    closed = [f"{k} (from {v})" for k, v in gates.items() if not product.gate_open(k)]
    d = gdd_dir or product.repo_dir
    gdd = _doc_for_rewrite(d / GDD_FILE, GDD_PROMPT_CHARS)[0] or _doc_for_rewrite(product.dir / GDD_FILE, GDD_PROMPT_CHARS)[0]
    heads = [ln for ln in gdd.splitlines() if ln.startswith("#")][:60]      # the outline of the whole document (was: of its last 6000 chars)
    return ("# GAME PROFILE\n"
            f"Stage `{product.stage}` (milestones in VISION). Switched off until a later stage: {', '.join(closed) or 'nothing'}. "
            "Rules: a task of the playable core is type `milestone` (outside the hypothesis rule before M2), a document or visual task is "
            "type `design` with role `game-designer` (GAME-DESIGN.md, balance tables) or `designer` (DESIGN.md, styles, SVG); the "
            "developer owns server and client code (`repo.kind: service`, no static `public/`). Acceptance criteria are scenarios a "
            "tester runs in a browser («two tabs join room X; a move in tab 1 appears in tab 2 within 1 s») or `repo_only` checks "
            "(tests pass, file exists). Until a host is granted (`deploy.mode: pending`) every task is reviewed in the checkout. "
            "Do not edit GAME-DESIGN.md through a developer task: a rule you doubt goes to the game designer as a `design` task. "
            "The stage changes only by the owner (owner_questions when the exit criterion of the current stage is met).\n"
            + ("\n## GAME-DESIGN.md outline\n" + "\n".join(heads) if heads else "\n(no GAME-DESIGN.md yet: the first design task writes it)"))


def standing_block(product: Product, store: Store) -> str:
    """What the PO sees about standing directions (owner 21.11, team/standing.py): which exist, their cadence, which task
    is open or when the next one comes. Empty for a product without directions (every non-game product by default)."""
    from . import standing
    lines = standing.status_lines(product, store)
    if not lines:
        return ""
    return ("# STANDING DIRECTIONS (the dispatcher creates these tasks itself)\n"
            "Recurring work that one task cannot close. A task with `standing: <id>` in the backlog belongs to its direction: do not "
            "create a second one for the same purpose, do not close it as a duplicate of an earlier, finished one, and do not wait "
            "for it with other work. When YOU create a task for a direction (a `balance` observation → a game-designer task at "
            "once), set `standing: <id>` on it: the dispatcher then counts it as the direction's task.\n"
            + "\n".join(f"- {ln}" for ln in lines))


def game_designer_prompt(product: Product, task: dict[str, Any], gdd_dir: Path, store: Store,
                         answers: list[dict[str, Any]] | None = None, refs: str = "") -> str:
    """The game designer's prompt: VISION, the task, the current GAME-DESIGN.md and balance tables, game metrics, what
    testers and the PO observed. No shell: everything travels here (owner 20.11)."""
    gdd_limit, table_limit = _design_limits(product)
    gdd, _ = _doc_for_rewrite(gdd_dir / GDD_FILE, gdd_limit)
    tables = ""
    for f in _balance_files(gdd_dir):
        tables += f"\n--- {BALANCE_DIR}/{f.name} ---\n" + _doc_for_rewrite(f, table_limit)[0]
    latest = product.dir / "metrics" / "latest.md"
    metrics = _read_tail(latest, GD_METRICS_CHARS)
    obs = [o for o in store.observations() if o.get("status") in ("new", "triaged") and o.get("classification") in GD_OBS_CLASSES][-15:]
    docs = ""
    for d in task_docs_for(product, task):   # a standing direction's own document (mechanics catalogue, balance reading): whole, to be returned whole
        docs += f"\n--- {d} ---\n" + (_doc_for_rewrite(gdd_dir / d, TASK_DOC_CHARS)[0] or "(does not exist yet — this task creates it)")
    reps = ""
    for r in task_reports(task):             # repo reports the task text names: read-only, the tail
        body = _read_tail(gdd_dir / r, REPORT_CHARS)
        reps += f"\n--- {r} ---\n" + (body or "(does not exist in the repository)")
    from . import balance
    sim_path = balance.settings(product.raw).sim_report
    sim = _read_tail(gdd_dir / sim_path, balance.SIM_REPORT_CHARS) if safe_rel_path(sim_path) else ""
    sim_txt = ""
    if sim:
        sim_txt = f"\n\n# SIMULATION REPORT (`{sim_path}`, written by the repository's own simulation — read it, do not return it)\n" + sim
    elif task.get("standing"):
        sim_txt = (f"\n\n# SIMULATION REPORT\n(none: the repository has no `{sim_path}` — product.yaml `balance.sim_report` names the path. "
                   "Judge from the match data, say in `friction` that the simulation report is missing, and propose the developer task that writes it.)")
    return (f"# DESIGN TASK {task['id']} — product `{product.slug}` — stage {product.stage}\n\n"
            "You return the design-spec schema: `files` are complete documents to write into the repository (paths relative to its root, "
            f"e.g. `{GDD_FILE}`, `{BALANCE_DIR}/ships.json`); `balance_changes` say what changed and how the effect will be read in the "
            "metrics; `proposed_tasks` are suggestions for the PO (not tasks yet); `open_questions` only for what changes the genre, "
            "the audience or money (the owner decides those).\n\n"
            "# TASK\n" + _y(_task_brief(task)) + refs + answers_block(answers)
            + "\n\n# VISION\n" + product.vision()
            + "\n\n# CURRENT GAME-DESIGN.md\n" + (gdd or "(none yet — write v1)")
            + ("\n\n# BALANCE TABLES" + tables if tables else "")
            + ("\n\n# DOCUMENTS OF THIS TASK (return each one COMPLETE in `files`)" + docs if docs else "")
            + ("\n\n# REPORTS NAMED BY THE TASK (read-only: read them, do not return them in `files`)" + reps if reps else "")
            + "\n\n# GAME METRICS\n" + (metrics or "(no metrics yet: no rounds have been played)")
            + sim_txt
            + "\n\n# WHAT TESTERS, PLAYERS AND THE PO OBSERVED\n" + (_y([{k: o.get(k) for k in ("id", "title", "classification", "actual", "source")} for o in obs]) if obs else "(nothing yet)") + "\n")


def reviewer_prompt(product: Product, task: dict[str, Any], result: dict[str, Any] | None, sha: str | None) -> str:
    repo = (product.repo_url or "").removesuffix(".git")
    raw = ""
    if repo.startswith("https://github.com/"):
        raw = (f"Repository: {repo} (branch {product.repo_branch}). Repo-only deliverables (code, tests, README, scripts) are "
               f"public: read a file with team-fetch at https://raw.githubusercontent.com/{repo.removeprefix('https://github.com/')}/"
               f"{sha or product.repo_branch}/<path> and the commit at {repo}/commit/{sha or product.repo_branch}.\n")
    return (f"# REVIEW of task {task['id']} — product `{product.slug}`\n\nPublic base URL: {product.site_base}\n"
            f"Expected release: commit {sha or 'unknown'} (the site may show a build marker in its footer or /build.json; "
            f"a daily cron rebuilds the site, so a NEWER marker than this commit is normal — only an older one means "
            f"'not deployed yet').\n" + raw + "\n"
            "# TASK\n" + _y(_task_brief(task))
            + "\n\n# IMPLEMENTER'S REPORT (a claim, not evidence)\n" + (_y(result) if result else "(none)")
            + "\n\nUse only `team-fetch <url>`. Check every acceptance criterion and negative case on the live site.\n")


def _top(d: dict[str, Any] | None, n: int) -> str:
    items = sorted(((k, v) for k, v in (d or {}).items() if isinstance(v, (int, float))), key=lambda kv: -kv[1])[:n]
    return ", ".join(f"{k} {v}" for k, v in items) or "—"


GAME_METRIC_KEYS = ("rounds_started", "rounds_finished", "players", "online_peak", "move_latency_ms")   # docs/game-metrics.md


def is_game_metrics(data: dict[str, Any]) -> bool:
    """A metrics file the game server produced (docs/game-metrics.md): `kind: game`, or a day with round fields."""
    if str(data.get("kind") or "") == "game":
        return True
    days = data.get("days") or {}
    return any(isinstance(d, dict) and any(k in d for k in GAME_METRIC_KEYS) for d in days.values())


def _num(d: dict[str, Any] | None, key: str, sub: str | None = None) -> str:
    v = (d or {}).get(key)
    if sub is not None:
        v = v.get(sub) if isinstance(v, dict) else None
    return "—" if v is None else str(v)


def game_metrics_digest(days: dict[str, Any], generated: str = "", max_days: int = 7, balance_cfg: Any = None) -> str:
    """Newest day first; the fields the PO, analyst and game designer read (docs/game-metrics.md). Missing fields print —.
    The balance table (team/balance.py) follows the days when the server sends the optional `balance` block."""
    out = [f"Game metrics served by the game server (docs/game-metrics.md); snapshot generated {generated or '?'}. "
           "finished/started = rounds completed; abandoned = left before the end; first_mover_wins/finished = balance signal; "
           "rejected_moves = intents the server refused (unclear UI when high)."]
    for day in sorted(days, reverse=True)[:max_days]:
        d = days[day] or {}
        if not isinstance(d, dict):
            continue
        res = d.get("by_result") or {}
        out.append("\n".join([
            f"## {day}",
            f"- rounds: started {_num(d, 'rounds_started')}, finished {_num(d, 'rounds_finished')}, abandoned {_num(d, 'rounds_abandoned')}"
            + (f"; by result: {_top(res, 5)}" if res else ""),
            f"- players: {_num(d, 'players')} (returning {_num(d, 'returning_players')}), online peak {_num(d, 'online_peak')}",
            f"- round length s: median {_num(d, 'round_length_s', 'median')}, p90 {_num(d, 'round_length_s', 'p90')}",
            f"- move latency ms: p50 {_num(d, 'move_latency_ms', 'p50')}, p95 {_num(d, 'move_latency_ms', 'p95')} ({_num(d, 'move_latency_ms', 'samples')} samples)",
            f"- moves {_num(d, 'moves')}, rejected {_num(d, 'rejected_moves')}, disconnects {_num(d, 'disconnects')}, reconnects {_num(d, 'reconnects')}, "
            f"server errors {_num(d, 'errors')}, first-mover wins {_num(d, 'first_mover_wins')}"]))
    from . import balance
    bal = balance.render_md(days, balance_cfg)
    if bal:
        out.append(bal.rstrip("\n"))
    return "\n\n".join(out)


def metrics_digest(files: list[Path], max_days: int = 7, balance_cfg: Any = None) -> str:
    """Compact per-day view of the site metrics for the analyst (ops 04.11, release-radar Q-0009): every metrics file is a
    multi-day snapshot of ~35 KB, and cutting each file to its first 5000 chars left the analyst with only the oldest day.
    Days are merged across files (the newest file wins), newest day first; raw top_pages lists are reduced to the top 5."""
    days: dict[str, dict[str, Any]] = {}
    targets: list[str] = []
    generated = ""
    for f in sorted(files):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        days.update(data.get("days") or {})
        targets = list(data.get("targets") or targets)
        generated = str(data.get("generated") or generated)
    if not days:
        return "(no metrics files yet)"
    if is_game_metrics({"days": days}):
        return game_metrics_digest(days, generated, max_days=max_days, balance_cfg=balance_cfg)
    out = [f"Snapshot generated {generated or '?'}; humans = after the strict bot filter (04.11); "
           "'returns' = human sources seen on an earlier day too (G2); by_src = visits that came with ?src=<channel>."]
    for day in sorted(days, reverse=True)[:max_days]:
        d = days[day] or {}
        h, a, c, sc = d.get("humans") or {}, d.get("agents") or {}, d.get("crawlers") or {}, d.get("scanners") or {}
        st = d.get("status") or {}
        lines = [f"## {day}",
                 f"- requests {d.get('requests_raw', 0)} (2xx {st.get('2xx', 0)}, 3xx {st.get('3xx', 0)}, 4xx {st.get('4xx', 0)}, 5xx {st.get('5xx', 0)})",
                 f"- HUMANS: requests {h.get('requests', 0)}, sources {h.get('sources', 0)}, engaged {h.get('engaged', 0)}, returns {h.get('returns', 0)}",
                 f"- agents: requests {a.get('requests', 0)}, sources {a.get('sources', 0)}; families: {_top(a.get('by_family'), 4)}",
                 f"- crawlers: requests {c.get('requests', 0)}, sources {c.get('sources', 0)}, social previews {c.get('social_preview', 0)}; bots: {_top(c.get('by_bot'), 6)}",
                 f"- scanners: requests {sc.get('requests', 0)}, sources {sc.get('sources', 0)}"]
        tg = []
        for t in targets or list((d.get("targets") or {}).keys()):
            v = (d.get("targets") or {}).get(t) or {}
            if not v.get("requests"):
                continue
            tg.append(f"{t} req {v.get('requests', 0)} · human src {(v.get('human') or {}).get('sources', 0)} · agent src "
                      f"{(v.get('agent') or {}).get('sources', 0)} · crawler req {(v.get('crawler') or {}).get('requests', 0)}")
        lines.append("- targets: " + ("; ".join(tg) if tg else "no hits"))
        src = d.get("by_src") or {}
        if src:
            lines.append("- by_src: " + "; ".join(f"{k} req {v.get('requests', 0)} human {v.get('human_sources', 0)} engaged {v.get('engaged_sources', 0)}"
                                                  for k, v in sorted(src.items(), key=lambda kv: -int((kv[1] or {}).get('requests') or 0))[:8]))
        ref = d.get("referers") or []
        if ref:
            lines.append("- referers: " + ", ".join(f"{r.get('host')} {r.get('requests')}" for r in ref[:5]))
        tp = d.get("top_pages") or []
        if tp:
            lines.append("- top pages (all classes): " + ", ".join(f"{p.get('path')} {p.get('requests')}" for p in tp[:5]))
        out.append("\n".join(lines))
    return "\n\n".join(out)


ANALYST_KEYS = ("id", "title", "type", "goal", "accepted_at")   # what the effect of an accepted task is judged by (+ `outcome`)


def analyst_brief(t: dict[str, Any]) -> dict[str, Any]:
    """The card of a recently accepted task for the analyst (`cycle.analyst_prompt.compact_tasks`, rework slice 10): which
    goal it was for, when it went live and one line of what came out — enough to look for its effect in the metrics."""
    card = finished_brief(t)
    return {**{k: t.get(k) for k in ANALYST_KEYS if t.get(k) not in (None, [], "")}, **({"outcome": card["outcome"]} if card.get("outcome") else {})}


DAY_TO_DAY = (
    "\n\n# DAY TO DAY (this run is the product's one measurement of the day)\n"
    "Give the day beside the day before, nothing more: the metric lines you already write (for a game: battles started and finished, "
    "players, returns, latency, errors, search; live players as their own line once the metrics carry it), the state of every goal of "
    "VISION in one line each, one line on balance, and the defects the numbers show. Do not judge the effect of tasks or releases and do "
    "not return `task_effects`: features go live in batches and the cause of a rise or a fall cannot be told from these numbers. Name a "
    "cause only when the numbers show it directly (an error wave at the hour of a deploy, a broken counter).\n")


def analyst_prompt(product: Product, store: Store, *, metrics: list[tuple[str, str]], accepted: list[dict[str, Any]],
                   posts: list[dict[str, Any]]) -> str:
    m = "\n\n".join(txt if day == "digest" else f"--- {day} ---\n{_tail(txt, 5000)}" for day, txt in metrics) or "(no metrics files yet)"
    return (f"# ANALYST — product `{product.slug}` ({product.site_base})\n\n# VISION (goals table)\n" + product.vision()
            + "\n\n# METRICS (latest first)\n" + m
            + (DAY_TO_DAY if product.measure_cfg["by_day"] else
               "\n\n# TASKS ACCEPTED RECENTLY\n"
               + ((_y([analyst_brief(t) for t in accepted]) if product.analyst_prompt_cfg["compact_tasks"] else
                   _y([{**_task_brief(t), "accepted_at": t.get("accepted_at"), "released_sha": t.get("released_sha")} for t in accepted]))
                  if accepted else "(none)"))
            + "\n\n# POSTS PUBLISHED\n" + (_y(posts) if posts else "(none)") + "\n")


def marketer_prompt(product: Product, task: dict[str, Any], channels: dict[str, Any], previous: list[dict[str, Any]],
                    lessons: list[str] | None = None, editor_notes: list[dict[str, Any]] | None = None,
                    memory: dict[str, Any] | None = None) -> str:
    prev = ("\n\n# PREVIOUS ATTEMPTS\n" + _y(previous[-2:])) if previous else ""
    les = ("\n\n# EDITORIAL LESSONS (binding for every post)\n" + "\n".join(f"- {x}" for x in lessons)) if lessons else ""
    notes = ""
    if editor_notes:
        notes = "\n\n# THE EDITOR SENT YOUR DRAFTS BACK — this is your one revision. Fix every issue, then publish again:\n" + _y(editor_notes)
    mem = ("\n\n" + memory_block(memory, for_plan=False)) if memory else ""
    tl = style.checklist()
    tells = ("\n\n# AI TELLS TO AVOID (write without them; the editor sends a draft back for any of §1–§5)\n" + tl) if tl else ""
    return (f"# MARKETING TASK {task['id']} — product `{product.slug}` ({product.site_base})\n\n"
            "# TASK\n" + _y(_task_brief(task))
            + "\n\n# CHANNELS CONFIGURED (channel → target)\n" + _y(channels)
            + "\n\n# VISION\n" + product.vision() + les + tells + mem + prev + notes
            + "\n\nWrite each post to a file here, then `team-publish --channel <channel> --target <target> --text-file <file> --url <url>`. "
            "Every post goes to the editor before it is sent; the editor may approve, send it back once, or reject. "
            "team-publish answers `\"dry_run\": true` with a `draft:` id: that only means the draft is saved for the editor, "
            "the dispatcher sends approved drafts live; never report it as dry-run mode. For a reply, `--reply-to` takes the "
            "post's bsky.app or threads.com link (or its at:// uri / media id).\n")


def marketing_plan_prompt(product: Product, task: dict[str, Any], channels: dict[str, Any], lessons: list[str] | None = None,
                          memory: dict[str, Any] | None = None) -> str:
    """A `plan: true` marketing task: no posts — the marketer writes `marketing-plan.md` (the next 7 days per channel) in its
    work directory; the dispatcher copies it to products/<slug>/ and accepts the task by the file."""
    now = datetime.now(timezone.utc)
    days = ", ".join((now + timedelta(days=i)).strftime("%a %d.%m") for i in range(7))
    les = ("\n\n# EDITORIAL LESSONS (every slot must be writable under these)\n" + "\n".join(f"- {x}" for x in lessons)) if lessons else ""
    mem = ("\n\n" + memory_block(memory, for_plan=True)) if memory else ""
    return (f"# MARKETING PLAN TASK {task['id']} — product `{product.slug}` ({product.site_base})\n\n"
            f"Today is {now.strftime('%Y-%m-%d %H:%M')} UTC. Plan the next 7 days: {days}.\n\n"
            "# TASK\n" + _y(_task_brief(task))
            + "\n\n# CHANNELS CONFIGURED (channel → target)\n" + _y(channels)
            + "\n\n# VISION (channels allowed, goals G1–G5, tone)\n" + product.vision() + les + mem
            + f"\n\n# WHAT TO WRITE\nWrite ONE file `{MARKETING_PLAN}` in the current directory with the Write tool (no posts, no team-publish; "
            "`team-fetch` only to check what is online). Markdown, English, sections in this order:\n"
            "1. `## Channels` — one line per configured channel: audience, format, posts per week, the VISION goal (G1–G5) it serves.\n"
            "2. `## Rubrics` — 2–5 recurring post types with a one-line definition each (e.g. daily card, weekly digest, one finding, ask).\n"
            "3. `## Slots` — a table `| date (UTC) | channel | rubric | angle / page to link | goal |`, one row per planned post for the next 7 days, "
            "within the daily limit per channel; dates as `YYYY-MM-DD HH:MM`. Only channels VISION allows.\n"
            "4. `## Drop` — what to stop or post less often, with the number from the channel table that says so.\n"
            "5. `## Why` — 3–6 lines: what the visits by `?src=`, the feedback and the last 7 days of posts told you.\n"
            "Rule for frequency: a channel with no human visits by `?src=` for two weeks of posting → fewer slots; a channel that brought "
            "visits or replies → more slots (up to the daily limit); a channel without posts yet → try it with 1–2 slots.\n"
            "Do not repeat angles already published in the last 7 days. The result's `changes` names the file; `summary` says what "
            "changed against the previous plan.\n")


# ---------------------------------------------------------------- marketing memory: posts, visits by channel, feedback, plan

def plan_path(product: Product) -> Path:
    return product.dir / MARKETING_PLAN


def plan_age_days(product: Product) -> float | None:
    p = plan_path(product)
    if not p.exists():
        return None
    return (datetime.now(timezone.utc) - datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)).total_seconds() / 86400


def latest_metrics(product: Product) -> dict[str, Any] | None:
    """The newest metrics/<day>.json the dispatcher fetched from the site (geo-*/seo-* files are other reports)."""
    files = sorted(f for f in (product.dir / "metrics").glob("20*.json") if f.name[:4].isdigit())
    for f in reversed(files):
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
    return None


def visits_by_src(product: Product, days: int = CHANNEL_DAYS) -> dict[str, dict[str, int]]:
    """Human sources / engaged / requests that came with `?src=<channel>` over the last `days` days (per-day `by_src`
    from sitemetrics when present, else the window totals)."""
    data = latest_metrics(product)
    if not data:
        return {}
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    out: dict[str, dict[str, int]] = {}
    day_map = data.get("days") or {}
    per_day = [d for d in day_map if d >= since and isinstance((day_map[d] or {}).get("by_src"), dict)]
    if per_day:
        for d in per_day:
            for src, v in (day_map[d].get("by_src") or {}).items():
                acc = out.setdefault(str(src), {"human_sources": 0, "engaged_sources": 0, "requests": 0})
                for k in acc:
                    acc[k] += int((v or {}).get(k) or 0)
        return out
    for src, v in ((data.get("totals") or {}).get("by_src") or {}).items():
        out[str(src)] = {k: int((v or {}).get(k) or 0) for k in ("human_sources", "engaged_sources", "requests")}
    return out


def channel_report(product: Product, store: Store, days: int = CHANNEL_DAYS) -> list[dict[str, Any]]:
    """One row per channel: posts in the last `days`, visits by `?src=`, and the verdict of the frequency rule:
    no human visits after two weeks of posting → less often; visits came → keep or raise; no posts yet → try."""
    chans = {str(c) for c in (product.raw.get("channels") or {})}
    src = visits_by_src(product, days)
    posts = store.recent_posts(days, text_chars=0)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    since14 = (datetime.now(timezone.utc) - timedelta(days=14)).strftime("%Y-%m-%d")
    rows = []
    for c in sorted(chans | set(src) | {p["channel"] for p in posts}):
        n = sum(1 for p in posts if p["channel"] == c)
        v = src.get(c, {})
        hs, eng, req = v.get("human_sources", 0), v.get("engaged_sources", 0), v.get("requests", 0)
        first = store.first_post_day(c)
        if hs > 0:
            verdict = "visits came — keep or raise the frequency (within the daily limit)"
        elif not first:
            verdict = "no posts yet — try 1–2 slots"
        elif first <= since14:
            verdict = "no human visits in two weeks of posting — post less often"
        else:
            verdict = f"no visits yet; posting since {first} — too early to judge (two-week rule)"
        rows.append({"channel": c, "posts": n, "human_sources": hs, "engaged": eng, "requests": req,
                     "first_post": first or "", "verdict": verdict, "today": today})
    return rows


def feedback_cards(store: Store, days: int = MEMORY_DAYS) -> list[dict[str, Any]]:
    """Words of real people (observations classified `feedback`) from the last `days` days."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    out = []
    for o in store.observations():
        if o.get("classification") != "feedback" or str(o.get("observed_at") or "") < since:
            continue
        out.append({"id": o.get("id"), "title": str(o.get("title") or "")[:140], "url": o.get("url"), "source": o.get("source"),
                    "said": " ".join(str(o.get("actual") or "").split())[:240], "po": (o.get("decision") or {}).get("action")})
    return out[-12:]


def marketing_memory(product: Product, store: Store, caps: dict[str, Any] | None = None) -> dict[str, Any]:
    """Everything the marketer (and the PO) should remember between runs; roles have no memory of their own.
    `caps` — the owner's per-channel caps (publish.channel_caps), shown so plans do not book slots the publisher refuses."""
    pp = plan_path(product)
    age = plan_age_days(product)
    return {"recent_posts": store.recent_posts(MEMORY_DAYS), "channels": channel_report(product, store),
            "feedback": feedback_cards(store), "plan_md": pp.read_text(encoding="utf-8") if pp.exists() else "",
            "plan_age_days": round(age, 1) if age is not None else None, "caps": caps or {},
            "usage": week_usage_map(store, caps or {}, channel_report_names(product))}


def channel_report_names(product: Product) -> list[str]:
    return sorted(str(c) for c in (product.raw.get("channels") or {}))


def week_usage_map(store: Store, caps: dict[str, Any], channels: list[str]) -> dict[str, dict[str, Any]]:
    """used/cap over 7 days per channel, from the publisher's own count (publish.week_usage)."""
    from . import publish
    return {c: publish.week_usage(store, c, (caps.get(c) or {}).get("per_week"))
            for c in sorted(set(channels) | {k for k, v in caps.items() if (v or {}).get("per_week") is not None})}


def usage_lines(usage: dict[str, Any]) -> str:
    out = []
    for ch, u in sorted((usage or {}).items()):
        if u.get("cap") is None:
            continue
        st = "OVER the cap already — book nothing" if u["over"] else ("FULL — book no slot" if u["full"] else f"{u['cap'] - u['used']} slot(s) left")
        out.append(f"- {ch}: {u['used']}/{u['cap']} in the last 7 days (publisher's count) — {st}")
    return ("# 7-DAY CAP USAGE (same count as the publisher)\n" + "\n".join(out)) if out else ""


def caps_block(caps: dict[str, Any]) -> str:
    """The owner's per-channel caps (publish.channel_caps) as a prompt section; empty when there are none."""
    lines = []
    for ch, c in sorted((caps or {}).items()):
        c = c or {}
        bits = []
        if c.get("per_day") is not None:
            bits.append(f"{c['per_day']}/day")
        if c.get("per_week") is not None:
            bits.append(f"{c['per_week']}/7 days")
        if bits:
            lines.append(f"- {ch}: {', '.join(bits)}")
    if not lines:
        return ""
    return ("# OWNER'S CHANNEL CAPS (binding; the publisher refuses posts over them)\n" + "\n".join(lines)
            + "\nWhy (owner 10.11, all sites 02–10.11): X brought ~23 people from 24 posts; Bluesky 8 from 46, Telegram 3 "
              "from 46, Mastodon 3 from 11, Threads 1 from 16, dev.to 0 from 4. Put the daily slots into X; on the "
              "capped channels post only the strongest item of the week. Do not book slots over a cap.")


def memory_block(memory: dict[str, Any], *, for_plan: bool) -> str:
    posts = memory.get("recent_posts") or []
    rows = memory.get("channels") or []
    fb = memory.get("feedback") or []
    plan = str(memory.get("plan_md") or "").strip()
    age = memory.get("plan_age_days")
    parts = [f"# PUBLISHED IN THE LAST {MEMORY_DAYS} DAYS (do not repeat these angles)\n"
             + (_y([{k: p.get(k) for k in ("day", "channel", "url", "task", "text")} for p in posts]) if posts else "(nothing yet)")]
    tbl = ["| channel | posts | human sources by ?src= | engaged | requests | first post | verdict |", "|---|---|---|---|---|---|---|"]
    tbl += [f"| {r['channel']} | {r['posts']} | {r['human_sources']} | {r['engaged']} | {r['requests']} | {r['first_post'] or '—'} | {r['verdict']} |" for r in rows]
    parts.append(f"# CHANNELS: VISITS BY ?src= (last {CHANNEL_DAYS} days; by_src from the site's metrics) AND THE FREQUENCY RULE\n"
                 + ("\n".join(tbl) if rows else "(no channels, no posts, no metrics yet)")
                 + "\nRule: no human visits after two weeks of posting → post less often there; visits or replies → more often "
                   "(within the daily limit); no posts yet → try. A verdict is a number, not a mood: cite it when you follow it.")
    cb = "\n\n".join(x for x in (caps_block(memory.get("caps") or {}), usage_lines(memory.get("usage") or {})) if x)
    if cb:
        parts.append(cb)
    parts.append(f"# WHAT PEOPLE SAID (feedback, last {MEMORY_DAYS} days)\n" + (_y(fb) if fb else "(no replies, issues or owner words)"))
    if for_plan:
        parts.append("# PREVIOUS PLAN" + (f" (written {age} days ago)" if age is not None else "") + "\n"
                     + (_tail(plan, 6000) if plan else "(no plan yet — this is the first)"))
    else:
        parts.append("# CURRENT MARKETING PLAN" + (f" (written {age} days ago)" if age is not None else "") + "\n"
                     + (_tail(plan, 6000) + "\nThe task you got is one slot of this plan: keep its rubric and angle; the plan outranks your own choice of topic."
                        if plan else "(no plan yet — the PO will ask for one; write this post from the task)"))
    return "\n\n".join(parts)


def po_marketing_block(product: Product, store: Store, caps: dict[str, Any] | None = None) -> str:
    """What the PO sees to run the channels: the plan (or its absence), what went out, visits by channel. Empty for a
    product without channels."""
    if not (product.raw.get("channels") or {}):
        return ""
    if not product.gate_open("marketing"):           # game profile: no plan and no post tasks before the stage VISION names
        return (f"# MARKETING — OFF until stage {product.phase_gates.get('marketing')} (now {product.stage}). Create no marketing or "
                "content task; the marketer does not run on this product yet.")
    m = marketing_memory(product, store, caps=caps)
    age = m.get("plan_age_days")
    plan = str(m.get("plan_md") or "").strip()
    if not plan:
        head = (f"# MARKETING PLAN — NONE. Create a plan task now: type `marketing`, role `marketer`, `plan: true` (the marketer writes "
                f"`{MARKETING_PLAN}`; accepted without a post). Until it exists, post tasks come from VISION's daily promise.")
    elif age is not None and age > PLAN_MAX_AGE_DAYS:
        head = (f"# MARKETING PLAN — {age} days old (older than {PLAN_MAX_AGE_DAYS}): create a new plan task (`plan: true`) this cycle; "
                "meanwhile cut the remaining slots below into post tasks with `not_before`.\n" + _tail(plan, 5000))
    else:
        head = (f"# MARKETING PLAN ({age} days old) — cut its slots into post tasks: one task per slot, `not_before` = the slot's date and hour, "
                "the slot's rubric and angle in `scope`, its goal in `goal`; skip slots already open in the backlog.\n" + _tail(plan, 5000))
    rows = m.get("channels") or []
    us = m.get("usage") or {}

    def _uc(c: str) -> str:
        u = us.get(c) or {}
        return f"{u['used']}/{u['cap']}" + (" FULL" if u.get("full") else "") if u.get("cap") is not None else (f"{u['used']}/–" if u else "–")
    tbl = ["| channel | posts 14 d | used/cap 7 d | human sources by ?src= | engaged | verdict |", "|---|---|---|---|---|---|"]
    tbl += [f"| {r['channel']} | {r['posts']} | {_uc(r['channel'])} | {r['human_sources']} | {r['engaged']} | {r['verdict']} |" for r in rows]
    posts = m.get("recent_posts") or []
    cb = "\n\n".join(x for x in (caps_block(m.get("caps") or {}), usage_lines(us)) if x)
    return (head + ("\n\n" + cb if cb else "") + "\n\n## Channels (visits by ?src=, last 14 days)\n" + ("\n".join(tbl) if rows else "(none)")
            + f"\n\n## Published in the last {MEMORY_DAYS} days\n"
            + (_y([{k: p.get(k) for k in ("day", "channel", "url", "task", "text")} for p in posts]) if posts else "(nothing)"))


def persona_prompt(persona: dict[str, Any], body: str, scenario: dict[str, Any], sbody: str) -> str:
    return ("# PERSONA\n" + _y(persona) + "\n" + body + "\n\n# SCENARIO\n" + _y(scenario) + "\n## Task\n" + sbody
            + f"\n\nRules: only `team-fetch <url>`; at most {scenario.get('max_steps', 10)} fetches; start at {scenario.get('entry')}.\n"
            "\n# JUDGE AS THIS PERSON\nYou are this reader in their real moment, not a tester. After your steps, fill `experience`: "
            "first impression, steps until you understood what the page gives you, interest 1–5, usefulness 1–5, would you come back, "
            "would you share, why in your own words, and one wish. File observations only for what should change "
            "(ux_friction, value_gap, reproducible_defect, need_hypothesis); successes go into `experience`, not observations.\n")
