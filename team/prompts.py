"""Prompt builders: what each role sees. Everything a role needs travels in the prompt; roles never read the store."""
from __future__ import annotations

import json
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
            "acceptance", "negative_cases", "observations", "links", "continues", "probe_of", "review_fails", "created")
    out = {k: t.get(k) for k in keys if t.get(k) not in (None, [], "")}
    if t.get("hypothesis"):
        from . import hypothesis
        out["hypothesis"] = hypothesis.brief(t["hypothesis"])
    return out


def _tail(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + f"\n… [cut {len(s) - n} chars]"


def po_prompt(product: Product, store: Store, *, cycle_id: str, observations: list[dict[str, Any]], questions: list[dict[str, Any]],
              metrics_md: str, spent_today: float, cap: float, max_ready: int, idle: bool, dev_idle: bool = False,
              channel_caps: dict[str, Any] | None = None, stuck_md: str = "") -> str:
    tasks = [t for t in store.tasks() if t.get("state") not in ("done", "rejected")]
    recent_done = [t for t in store.tasks(["done", "measured", "accepted", "released"])][-10:]
    obs_txt = "\n\n".join(_y({k: v for k, v in o.items() if k not in ("fingerprint",)}) for o in observations) or "(none)"
    q_txt = "\n\n".join(_y(q) for q in questions) or "(none)"
    answered = [q for q in store.questions(["answered"]) if q.get("to") == "owner" and str(q.get("answered") or "") >= _hours_ago(72)][-8:]
    answered_txt = "\n\n".join(_y({k: q.get(k) for k in ("id", "question", "answer", "answered_by", "answered")}) for q in answered) or "(none)"
    fw_txt = framework_context(product)
    mkt_txt = po_marketing_block(product, store, caps=channel_caps)
    game_txt = game_block(product)
    return (f"# Cycle {cycle_id} — PO triage for product `{product.slug}` ({product.site_base})\n\n"
            f"Spend today ${spent_today:.2f} of the daily cap ${cap:.2f}. Propose at most {max_ready} Ready tasks."
            + (" The team is idle: no task is ready or in progress — if VISION promises daily work, create it." if idle else "")
            + (" The developer has no ready work: queue 1–2 tasks of type `defect` or `quality` (see the quality rule) from failing "
               "SEO/GEO checks, review fails, persona `ux_friction`, critic findings or facts on the live site, each naming its source "
               "in `problem`; if nothing is broken or measurably improvable, say so in the digest and invent no feature."
               if dev_idle and not idle else "")
            + "\n\n# VISION\n" + product.vision()
            + (("\n\n" + stuck_md) if stuck_md else "")
            + "\n\n# BACKLOG (open tasks)\n" + (_y([_task_brief(t) for t in tasks]) if tasks else "(empty)")
            + "\n\n# RECENTLY FINISHED\n" + (_y([{k: t.get(k) for k in ("id", "title", "state")} for t in recent_done]) if recent_done else "(none)")
            + "\n\n# OPEN QUESTIONS TO YOU\n" + q_txt
            + "\n\n# QUESTIONS THE OWNER ALREADY ANSWERED (last 72 h — do not ask these again)\n" + answered_txt
            + (("\n\n# FRAMEWORK CONTEXT (this product is the framework itself)\n" + fw_txt) if fw_txt else "")
            + (("\n\n" + game_txt) if game_txt else "")
            + (("\n\n" + mkt_txt) if mkt_txt else "")
            + "\n\n# VOICE OF THE USER (weekly research: real people's words about the problem)\n" + _voice_block(product)
            + "\n\n# READER EXPERIENCE (personas, scores 1–5)\n" + _ux_block(product)
            + "\n\n# NEW OBSERVATIONS\n" + obs_txt
            + "\n\n# LATEST METRICS\n" + (_tail(metrics_md, 6000) or "(no metrics yet)") + "\n")


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
SERVICE_TEMPLATES = Path(__file__).parent / "templates" / "service-repo"   # starter playwright.config.ts, tests/e2e, tests/load, PROTOCOL.md
LOAD_SCRIPT_REL = "tests/load/clients.ts"   # the repository's load script (`team-play load`, slice 6)
GAME_METRICS_DOC = Path(__file__).parent.parent / "docs" / "game-metrics.md"   # the /_team/metrics.json contract a game server serves


def _read_tail(p: Path, n: int) -> str:
    try:
        return _tail(p.read_text(encoding="utf-8", errors="replace"), n) if p.exists() else ""
    except OSError:
        return ""


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
           "players, latency, rejected moves); the PO, analyst and game designer read nothing else about play. A load task (role tester) runs "
           f"`tests/load/clients.ts` (starter in `{SERVICE_TEMPLATES}/tests/load/`) with N WebSocket clients: keep it runnable."]
    if role == "designer":
        out.append(f"You are the designer: your files are `{DESIGN_FILE}`, styles and assets (SVG, CSS). Do not change server code or game rules; "
                   "a rule that looks wrong goes into `proposals`.")
    for name, n in ((GDD_FILE, 4000), (PROTOCOL_FILE, 3000), (DESIGN_FILE, 2000 if role == "designer" else 800)):
        txt = _read_tail(repo_dir / name, n)
        if txt:
            out.append(f"\n--- {name} ---\n{txt}")
    return "\n".join(out)


def developer_prompt(product: Product, task: dict[str, Any], repo_dir: Path, branch: str, previous: list[dict[str, Any]],
                     review: dict[str, Any] | None, role: str = "developer") -> str:
    readme = ""
    seen: set[Path] = set()
    for name in ("README.md", "readme.md", "AGENTS.md"):
        p = repo_dir / name
        if p.exists() and p.resolve() not in seen:          # case-insensitive file systems: README.md == readme.md
            seen.add(p.resolve())
            readme += f"\n\n--- {name} ---\n" + _tail(p.read_text(encoding="utf-8", errors="replace"), 8000)
    prev = ("\n\n# PREVIOUS ATTEMPTS ON THIS TASK\n" + _y(previous[-2:])) if previous else ""
    rev = ("\n\n# REVIEW THAT FAILED (fix this first)\n" + _y({k: review.get(k) for k in ("verdict", "first_failure", "criteria", "summary")})) if review else ""
    tests = (f"Tests: run `{product.test_cmd}` from the checkout root before reporting done (it creates its own venv; "
             "the checkout has no .venv). A task is not done while that command fails.\n\n") if product.test_cmd else ""
    probe = ("# PROBE (stuck-goal replan): answer one question, do not implement\n"
             f"Task {task.get('probe_of')} failed several times. This run tests ONE alternative approach: run the check in the "
             "acceptance (commands, fetches, comparisons in a scratch directory), do not change or commit product code; nothing "
             "you do here is merged. Report `done` with one self_check per criterion: `met: true` only when the evidence shows the "
             "approach gives what the goal needs, `met: false` with the output that rules it out. A clear «does not work» is a "
             "good result.\n\n") if task.get("probe_of") else ""
    return (probe + f"# TASK {task['id']} — product `{product.slug}` ({product.site_base or 'no live site yet'})\n\n"
            f"You are the `{role}` role in the repository checkout `{repo_dir}` on branch `{branch}`. Commit there; do not push.\n\n" + tests +
            "# TASK\n" + _y(_task_brief(task)) + prev + rev
            + "\n\n# VISION\n" + product.vision()
            + service_block(product, repo_dir, role)
            + "\n\n# REPOSITORY NOTES" + (readme or "\n(no README — follow the conventions you find in the tree)") + "\n")


def tester_prompt(product: Product, task: dict[str, Any], result: dict[str, Any] | None, sha: str | None, *, base_url: str,
                  local: bool, checkout: Path, out_dir: Path, tests: str, server_notes: list[str] | None = None) -> str:
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
               "takes a phone-viewport picture; `team-play logs` shows the server log.\n"
               + ("E2E specs present: " + ", ".join(specs) if specs else "No tests/e2e specs in the checkout: judge from screenshots, /healthz and the server log, and say in `limits` which spec file is missing."))
            + "\n\n# TEST RUN (unit tests, by the dispatcher)\n" + (tests or "(none)")
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
            "2. `team-play load --clients N --seconds S [--rate R] [--moves]` with the task's numbers (start smaller if the task allows a ramp); quote the summary line.\n"
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
    gdd = _read_tail(d / GDD_FILE, 6000) or _read_tail(product.dir / GDD_FILE, 6000)
    heads = [ln for ln in gdd.splitlines() if ln.startswith("#")][:30]
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


def game_designer_prompt(product: Product, task: dict[str, Any], gdd_dir: Path, store: Store) -> str:
    """The game designer's prompt: VISION, the task, the current GAME-DESIGN.md and balance tables, game metrics, what
    testers and the PO observed. No shell: everything travels here (owner 20.11)."""
    gdd = _read_tail(gdd_dir / GDD_FILE, 12000)
    tables = ""
    bdir = gdd_dir / BALANCE_DIR
    if bdir.exists():
        for f in sorted(bdir.glob("*"))[:8]:
            tables += f"\n--- {BALANCE_DIR}/{f.name} ---\n" + _read_tail(f, 3000)
    latest = product.dir / "metrics" / "latest.md"
    metrics = _read_tail(latest, 4000)
    obs = [o for o in store.observations() if o.get("status") in ("new", "triaged") and o.get("classification") in ("defect", "ux_friction", "value_gap", "feedback")][-15:]
    return (f"# DESIGN TASK {task['id']} — product `{product.slug}` — stage {product.stage}\n\n"
            "You return the design-spec schema: `files` are complete documents to write into the repository (paths relative to its root, "
            f"e.g. `{GDD_FILE}`, `{BALANCE_DIR}/ships.json`); `balance_changes` say what changed and how the effect will be read in the "
            "metrics; `proposed_tasks` are suggestions for the PO (not tasks yet); `open_questions` only for what changes the genre, "
            "the audience or money (the owner decides those).\n\n"
            "# TASK\n" + _y(_task_brief(task))
            + "\n\n# VISION\n" + product.vision()
            + "\n\n# CURRENT GAME-DESIGN.md\n" + (gdd or "(none yet — write v1)")
            + ("\n\n# BALANCE TABLES" + tables if tables else "")
            + "\n\n# GAME METRICS\n" + (metrics or "(no metrics yet: no rounds have been played)")
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


def game_metrics_digest(days: dict[str, Any], generated: str = "", max_days: int = 7) -> str:
    """Newest day first; the fields the PO, analyst and game designer read (docs/game-metrics.md). Missing fields print —."""
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
    return "\n\n".join(out)


def metrics_digest(files: list[Path], max_days: int = 7) -> str:
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
        return game_metrics_digest(days, generated, max_days=max_days)
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


def analyst_prompt(product: Product, store: Store, *, metrics: list[tuple[str, str]], accepted: list[dict[str, Any]],
                   posts: list[dict[str, Any]]) -> str:
    m = "\n\n".join(txt if day == "digest" else f"--- {day} ---\n{_tail(txt, 5000)}" for day, txt in metrics) or "(no metrics files yet)"
    return (f"# ANALYST — product `{product.slug}` ({product.site_base})\n\n# VISION (goals table)\n" + product.vision()
            + "\n\n# METRICS (latest first)\n" + m
            + "\n\n# TASKS ACCEPTED RECENTLY\n" + (_y([{**_task_brief(t), "accepted_at": t.get("accepted_at"), "released_sha": t.get("released_sha")} for t in accepted]) if accepted else "(none)")
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
