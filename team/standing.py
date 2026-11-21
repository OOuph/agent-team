"""Standing directions: work the team must keep returning to instead of closing it with one task (owner 21.11, the
first game product: «постоянные направления — придумывать необычные механики и тестировать каждую до входа в игру;
автоматическая балансировка»; SCHEME §3 and §11).

`product.yaml: standing:` is a list of directions:

    standing:
      - id: balance            # unique; a task of the direction carries `standing: balance`
        role: game-designer    # who executes the task
        type: design
        every_h: 24            # a new task at most this often, counted from the creation of the previous one
        title: "…"
        problem: "…"
        scope: "…"
        acceptance: ["…", "…"]
        priority: P2
        # optional: size (S), goal, from_stage (M1), docs: [repository paths the role maintains and must see whole]

The key absent → `defaults(product)`: the built-in list for `profile: game`, nothing for any other profile. An explicit
`standing: []` switches the directions off. An entry whose `id` is a built-in one inherits every field it does not
set (so `- {id: balance, every_h: 12}` only changes the cadence); a listed key replaces the built-in list, so a
built-in direction that is not listed is off.

`due(product, store, now)` names the directions whose task must be created now: none of the direction's tasks is open
(any state other than CLOSED_STATES counts as open) and the last one was created at least `every_h` hours ago, or
never. The dispatcher creates the task itself at the start of the triage phase (`Cycle.create_standing_tasks`): state
`ready`, `created_by: dispatcher`, field `standing: <id>`, event `standing.created`. No PO run is needed, and the PO
sees the task in its backlog in the same phase. A task made by hand for the same purpose is counted as the direction's
task once its YAML carries `standing: <id>`.

Stage: a direction waits until the product's `stage` reaches its `from_stage`; `phase_gates: {standing: M1}` (all
directions) and `phase_gates: {standing.<id>: M2}` (one) work as for every other gated phase.
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from .config import Product, stage_index
from .store import PRIORITIES, TASK_TYPES, Store, parse_ts

log = logging.getLogger("team.standing")

# --- parameters ------------------------------------------------------------------------------------------------------
CONFIG_KEY = "standing"                 # product.yaml key and the task field that ties a task to its direction
TASK_FIELD = "standing"
CLOSED_STATES = ("accepted", "rejected", "done", "measured")   # every other task state means «the direction has a task open»
CREATED_BY = "dispatcher"
EVENT_CREATED = "standing.created"
TASK_STATE = "ready"
GATE = "standing"                       # phase_gates name; `standing.<id>` gates one direction
EVERY_H_RANGE = (1, 24 * 60)            # hours between two tasks of one direction
DEFAULT_PRIORITY = "P2"
DEFAULT_SIZE = "S"
DEFAULT_TYPE = "quality"                # for a custom direction that names no type
ROLES = ("developer", "designer", "game-designer", "tester", "marketer", "analyst")   # roles the cycle can execute
GAME_PROFILE = "game"
GAME_FROM_STAGE = "M1"                  # built-in game directions start once the playable core exists (M0 passed)

MECHANICS_ID, BALANCE_ID = "mechanics", "balance"
MECHANICS_EVERY_H = 60
BALANCE_EVERY_H = 24
MECHANICS_CATALOGUE = "design/ideas/mechanics.md"      # the living catalogue of mechanic ideas, in the game repository
BALANCE_REPORT = "design/reports/balance.md"           # the latest balance reading, in the game repository
BALANCE_TABLES_GLOB = "design/balance/*.json"
MECHANICS_MIN_NEW = 3                   # new ideas per run of the mechanics direction
MECHANICS_MAX_PROTOTYPES = 1            # mechanics recommended for a prototype per run (and in work at a time, PO card)
CATALOGUE_MAX_CHARS = 30000             # the role returns the file whole (design-spec: 40 000 chars per file): keep it below

GAME_DEFAULTS: list[dict[str, Any]] = [
    {
        "id": MECHANICS_ID, "role": "game-designer", "type": "design", "every_h": MECHANICS_EVERY_H, "priority": "P2", "size": "M",
        "from_stage": GAME_FROM_STAGE, "docs": [MECHANICS_CATALOGUE],
        "title": "Standing: mechanics catalogue — new ideas, re-rating, one candidate for a prototype",
        "problem": ("The game stays worth a second round only while it keeps getting mechanics players have not met: unusual mechanics "
                    "of units and of the options a player picks, and effects on the level. One task cannot close this, so the "
                    "catalogue of ideas is revisited on a cadence; and no mechanic enters the game untested (owner 21.11)."),
        "scope": (f"`{MECHANICS_CATALOGUE}` — the living catalogue; its whole current text is in your prompt, return it complete. "
                  "Add new ideas of two kinds: a mechanic of a unit or of an option a player picks, and an effect on the level "
                  "(zones, hazards, objects, events). Re-rate the older entries with the match data and the Balance table of the "
                  f"prompt. Recommend at most {MECHANICS_MAX_PROTOTYPES} mechanic for a prototype and put its developer task into "
                  "`proposed_tasks`: a prototype behind a flag in the balance tables (off by default), a simulation run inside the "
                  "product's balance corridor attached to the task result, a tester play-test, and only then on by default. "
                  "Estimate what an idea needs from the engine, the client and the bots from GAME-DESIGN.md and the protocol — you "
                  "have no code access. Do not change GAME-DESIGN.md or the balance tables in this task. Keep the file under "
                  f"{CATALOGUE_MAX_CHARS} characters: shipped and rejected ideas shrink to one line each in an archive list at the end."),
        "acceptance": [
            f"`{MECHANICS_CATALOGUE}` exists in the repository and holds at least {MECHANICS_MIN_NEW} entries added in this run (each "
            "marked with the run's date): at least one unit or option mechanic and at least one effect on the level.",
            "Every entry added in this run has all of: essence (what the player does and sees), counterplay, parameters and how they "
            "are priced in the balance tables, what it needs from engine / client / bots — each rated S, M or L, balance risk and "
            "readability risk, how to verify it by simulation (what is run and which numbers must stay inside the corridor).",
            "Every entry from earlier runs has a status line updated in this run: its rating and the match or simulation numbers "
            "behind it, or the words that no match data exists yet.",
            f"A section «Next prototype» names at most {MECHANICS_MAX_PROTOTYPES} mechanic with the reason, the flag in the balance "
            "tables that switches it (off by default) and the simulation check — or says that none is recommended and why.",
            "No entry names a source-code file: needs are stated as engine / client / bots with a size, not as file paths.",
        ],
    },
    {
        "id": BALANCE_ID, "role": "game-designer", "type": "design", "every_h": BALANCE_EVERY_H, "priority": "P2", "size": "S",
        "from_stage": GAME_FROM_STAGE, "docs": [BALANCE_REPORT],
        "title": "Standing: balance reading — within thresholds, or a one-lever patch of the balance tables",
        "problem": ("An overpowered or useless option must be found quickly — from the product's own simulation and from real "
                    "matches — and cured by a patch of data, not of code (owner 21.11). Nobody reads the balance unless the task "
                    "comes back on a cadence."),
        "scope": ("Read the Balance table of the GAME METRICS (picks, win rate, deviation per option; all matches and humans only), "
                  "the SIMULATION REPORT and the `balance` observations in the prompt. Apply the autobalance rules of GAME-DESIGN.md "
                  "(thresholds, which lever goes first); if the document has no such section, use the band printed in the table "
                  f"header and say in `friction` that the section is missing. Write the reading into `{BALANCE_REPORT}` (return it "
                  "complete; it replaces the previous reading). Outcome A — everything inside the thresholds: say so with the "
                  f"numbers and change no table. Outcome B — an option is outside: change ONE lever (one parameter of one option) in "
                  f"ONE file of `{BALANCE_TABLES_GLOB}`, raise the tables version (add a `version` field to that file if the tables "
                  "carry none), fill `balance_changes`, and put the check a developer must run into `proposed_tasks` when it needs "
                  "code or a simulation run. No new mechanics and no change of the rules in GAME-DESIGN.md here."),
        "acceptance": [
            f"`{BALANCE_REPORT}` exists in the repository and is dated by this run: the balance tables version, the days and the "
            "number of picks read, and for every group either the options outside the thresholds with their numbers (picks, win "
            "rate; from matches and from the simulation) or the line that the group is within thresholds, with the numbers.",
            "The report states the verdict in one line — «within thresholds» or «patch» — and names the autobalance rule of "
            "GAME-DESIGN.md it applied (or says that the document has none and which band was used).",
            "Verdict «patch»: the report lists exactly one changed lever (file under `design/balance/`, option, parameter, value "
            "before → after), that file holds the «after» value and a new tables version, and the report gives the expected win "
            "rate after the patch and after how many matches it is read.",
            "Verdict «patch»: the report names the simulation check a developer or reviewer must run (what is run, which numbers "
            "must fall inside the corridor) and the result that cancels the patch.",
            "Verdict «within thresholds»: the report says that no table was changed, and the tables version it quotes equals the "
            "version in the balance tables.",
        ],
    },
]


@dataclass(frozen=True)
class Direction:
    id: str
    role: str
    type: str
    every_h: float
    title: str
    problem: str
    scope: str
    acceptance: tuple[str, ...]
    priority: str = DEFAULT_PRIORITY
    size: str = DEFAULT_SIZE
    goal: str = ""
    from_stage: str = ""
    docs: tuple[str, ...] = ()

    @property
    def cadence(self) -> str:
        return f"every {self.every_h:g} h"


@dataclass
class Status:
    direction: Direction
    due: bool
    reason: str                                   # for the log and `team status`
    open_task: dict[str, Any] | None = None
    last_task: dict[str, Any] | None = None
    last_created: datetime | None = None
    next_due: datetime | None = None
    wait_h: float = 0.0
    problems: list[str] = field(default_factory=list)


# ------------------------------------------------------------------ configuration

def builtin(direction_id: str) -> dict[str, Any] | None:
    """A copy of the built-in direction with this id, or None."""
    for d in GAME_DEFAULTS:
        if d["id"] == direction_id:
            return copy.deepcopy(d)
    return None


def defaults(product: Product) -> list[dict[str, Any]]:
    """The built-in directions of the product's profile: two for `game`, none for anything else."""
    return copy.deepcopy(GAME_DEFAULTS) if product.profile == GAME_PROFILE else []


def _parse(entry: Any, where: str) -> tuple[Direction | None, str | None]:
    if not isinstance(entry, dict):
        return None, f"{where}: an entry must be a mapping, got {entry!r}"
    did = str(entry.get("id") or "").strip()
    if not did:
        return None, f"{where}: an entry has no `id`"
    merged = {**(builtin(did) or {}), **{k: v for k, v in entry.items() if v is not None}}
    role = str(merged.get("role") or "").strip()
    if role not in ROLES:
        return None, f"{where}: `{did}`: role {role!r} is not one of {', '.join(ROLES)}"
    every = merged.get("every_h")
    lo, hi = EVERY_H_RANGE
    if isinstance(every, bool) or not isinstance(every, (int, float)) or not lo <= every <= hi:
        return None, f"{where}: `{did}`: every_h must be a number of hours {lo}..{hi}, got {every!r}"
    title = str(merged.get("title") or "").strip()
    acceptance = merged.get("acceptance")
    if isinstance(acceptance, str):
        acceptance = [acceptance]
    acceptance = [str(a).strip() for a in (acceptance or []) if str(a).strip()]
    if not title or not acceptance:
        return None, f"{where}: `{did}`: `title` and a non-empty `acceptance` list are required"
    type_ = str(merged.get("type") or DEFAULT_TYPE)
    if type_ not in TASK_TYPES:
        return None, f"{where}: `{did}`: type {type_!r} is not one of {', '.join(TASK_TYPES)}"
    prio = str(merged.get("priority") or DEFAULT_PRIORITY)
    if prio not in PRIORITIES:
        return None, f"{where}: `{did}`: priority {prio!r} is not one of {', '.join(PRIORITIES)}"
    docs = merged.get("docs") or []
    docs = [docs] if isinstance(docs, str) else docs
    return Direction(id=did, role=role, type=type_, every_h=float(every), title=title[:140],
                     problem=str(merged.get("problem") or title).strip(), scope=str(merged.get("scope") or "").strip(),
                     acceptance=tuple(acceptance), priority=prio, size=str(merged.get("size") or DEFAULT_SIZE),
                     goal=str(merged.get("goal") or "").strip(), from_stage=str(merged.get("from_stage") or "").strip(),
                     docs=tuple(str(x) for x in docs if str(x).strip())), None


def load(product: Product) -> tuple[list[Direction], list[str]]:
    """The product's directions and the configuration problems found. A bad entry is skipped and reported (ERROR in the
    log, a line in `team status`): one typo must not stop the product's cycle, and must not be silent either."""
    where = f"{product.dir / 'product.yaml'}: {CONFIG_KEY}"
    raw = product.raw.get(CONFIG_KEY)
    if raw is None:
        entries = defaults(product)
    elif isinstance(raw, list):
        entries = raw
    else:
        return [], [f"{where}: must be a list of directions (or [] to switch them off), got {type(raw).__name__}"]
    out: list[Direction] = []
    problems: list[str] = []
    seen: set[str] = set()
    for e in entries:
        d, problem = _parse(e, where)
        if d and d.id in seen:
            d, problem = None, f"{where}: `{d.id}` is listed twice; the first entry is used"
        if problem:
            problems.append(problem)
            continue
        seen.add(d.id)
        out.append(d)
    return out, problems


def directions(product: Product) -> list[Direction]:
    ds, problems = load(product)
    for pr in problems:
        log.error("standing: %s", pr)
    return ds


def known_id(product: Product, direction_id: Any) -> bool:
    return any(d.id == str(direction_id) for d in load(product)[0])


# ------------------------------------------------------------------ the decision

def gate_reason(product: Product, d: Direction) -> str | None:
    """Why the direction must wait for a later stage, or None when it may run at the product's current stage."""
    if d.from_stage and stage_index(product.stage) < stage_index(d.from_stage):
        return f"waits for stage {d.from_stage} (now {product.stage})"
    for name in (GATE, f"{GATE}.{d.id}"):
        if not product.gate_open(name):
            return f"waits for stage {product.phase_gates.get(name)} (now {product.stage}; phase_gates.{name})"
    return None


def is_open(task: dict[str, Any]) -> bool:
    return str(task.get("state") or "") not in CLOSED_STATES


def tasks_of(tasks: list[dict[str, Any]], direction_id: str) -> list[dict[str, Any]]:
    return [t for t in tasks if str(t.get(TASK_FIELD) or "") == direction_id]


def _created(t: dict[str, Any]) -> datetime | None:
    ts = parse_ts(t.get("created"))
    return (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)) if ts else None


def check(product: Product, store: Store, now: datetime | None = None) -> list[Status]:
    """One Status per direction: due or not, and why. Read-only."""
    now = now or datetime.now(timezone.utc)
    ds, problems = load(product)
    for pr in problems:
        log.error("standing: %s", pr)
    if not ds:
        return []
    all_tasks = store.tasks()
    out: list[Status] = []
    for d in ds:
        mine = tasks_of(all_tasks, d.id)
        open_ = sorted((t for t in mine if is_open(t)), key=lambda t: str(t.get("created") or ""))
        dated = [(c, t) for c, t in ((_created(t), t) for t in mine) if c]
        last_created, last_task = max(dated, key=lambda ct: ct[0]) if dated else (None, None)
        gate = gate_reason(product, d)
        st = Status(d, False, "", open_task=open_[0] if open_ else None, last_task=last_task, last_created=last_created, problems=problems)
        if gate:
            st.reason = gate
        elif open_:
            st.reason = f"open {open_[0]['id']} ({open_[0].get('state')})"
        elif last_created is None:
            st.due, st.reason = True, "due now (never created)"
        else:
            st.next_due = last_created + timedelta(hours=d.every_h)
            age_h = (now - last_created).total_seconds() / 3600
            if now >= st.next_due:
                st.due, st.reason = True, f"due now (last {last_task['id']} created {age_h:.1f} h ago ≥ {d.every_h:g} h)"
            else:
                st.wait_h = round(d.every_h - age_h, 1)
                st.reason = f"next in {st.wait_h:g} h (last {last_task['id']} created {age_h:.1f} h ago)"
        out.append(st)
    return out


def due(product: Product, store: Store, now: datetime | None = None) -> list[Direction]:
    """The directions whose task must be created now. Every decision is logged (INFO when due, DEBUG otherwise)."""
    out = []
    for st in check(product, store, now):
        log.log(logging.INFO if st.due else logging.DEBUG, "standing %s/%s (%s): %s", product.slug, st.direction.id, st.direction.cadence, st.reason)
        if st.due:
            out.append(st.direction)
    return out


def create_task(product: Product, store: Store, d: Direction, *, cycle_id: str = "") -> dict[str, Any]:
    """The direction's next task: `ready`, created by the dispatcher, tied to the direction by `standing: <id>`."""
    t = store.create_task(title=d.title, type_=d.type, problem=d.problem, scope=d.scope, acceptance=list(d.acceptance),
                          goal=d.goal or f"standing: {d.id} ({product.stage})", created_by=CREATED_BY, priority=d.priority, size=d.size,
                          role=d.role, state=TASK_STATE, note=f"standing direction `{d.id}`, {d.cadence}")
    t[TASK_FIELD] = d.id
    if d.docs:
        t["docs"] = list(d.docs)
    store.save_task(t)
    store.event(EVENT_CREATED, t["id"], CREATED_BY, {"direction": d.id, "every_h": d.every_h, "role": d.role, "cycle": cycle_id})
    log.info("standing %s/%s: task %s created (%s, role %s, %s)", product.slug, d.id, t["id"], d.cadence, d.role, d.priority)
    return t


# ------------------------------------------------------------------ what people and roles read

def status_lines(product: Product, store: Store, now: datetime | None = None) -> list[str]:
    """One line per direction for `team status`: id, cadence, when the next task is due or which task is open."""
    sts = check(product, store, now)
    lines = [f"standing {st.direction.id}: {st.direction.cadence}, {st.direction.role} · {st.reason}" for st in sts]
    return lines + [f"standing config: {pr}" for pr in load(product)[1]]


def vision_section() -> str:
    """The «Постоянные направления» block for a game VISION (onboarding template and plan prompt): what the dispatcher
    creates by itself, so the first backlog does not duplicate it."""
    rows = "\n".join(f"- `{d['id']}` — роль {d['role']}, раз в {d['every_h']} ч, с этапа {d.get('from_stage') or 'M0'}: {d['title']}"
                     for d in GAME_DEFAULTS)
    return ("Задачи этих направлений диспетчер создаёт сам (`created_by: dispatcher`, поле задачи `standing: <id>`), когда "
            "предыдущая закрыта и прошёл интервал; PO их не дублирует и не закрывает как дубли.\n" + rows)
