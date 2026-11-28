"""Settings: team.yaml (global) + products/<slug>/product.yaml (per product) + .env (secrets).

Every configurable value lives in those files; this module only resolves paths and merges defaults.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .lanes import DEFAULTS as PARALLEL_DEFAULTS, Config as ParallelConfig, settings as parallel_settings
from .flow import DEFAULTS as FLOW_DEFAULTS, Config as FlowConfig, settings as flow_settings

log = logging.getLogger("team.config")

ROOT = Path(os.environ.get("TEAM_ROOT") or Path(__file__).resolve().parent.parent)

# `merged_since` (28.11, pokerbr К5): every new observation in the PO's prompt lists the tasks merged AFTER it was recorded —
# the PO cut a task from an observation that a merge had already answered
PO_PROMPT_DEFAULTS: dict[str, bool] = {"compact_finished": False, "stable_first": False, "merged_since": False}   # `cycle.po_prompt`, see DEFAULTS
ESCALATION_DEFAULTS: dict[str, bool] = {"needs_context": False, "review_return": False, "references": False, "budget_split": False, "review_exit": False,
                                        # 28.11 (pokerbr К7, К8): `owner_ping` true = every open question to the owner goes to
                                        # the owner's channel as one line, once; `operator_hold` true = a task taken with
                                        # `team task <product> take` is left alone by seats, reviews, replans and closings
                                        "owner_ping": False, "operator_hold": False}   # `cycle.escalation`, see DEFAULTS
DUPLICATES_DEFAULTS: dict[str, bool] = {"closed_list": False, "closed_check": False}   # `cycle.duplicates`, see DEFAULTS
VISIBLE_DEFAULTS: dict[str, bool] = {"review_before_merge": False, "redesign_gate": False}   # `cycle.visible`, see DEFAULTS
# `cycle.queue` (rework slice 9): the PO cuts tasks ahead
QUEUE_DEFAULTS: dict[str, Any] = {
    "ahead": False,               # true = the PO is woken when a seat's ready queue is low and may cut more tasks per run
    "target_ready": 6,            # ready tasks of a role with seats below which the PO is woken (1..50)
    "max_per_run": 6,             # Ready tasks the PO may propose in one run while `ahead` is on (1..12; off: cycle.max_ready)
    "wake_every_min": 30,         # the PO is not woken for a low queue more often than this (5..1440)
    "remind_parked": False,       # true = `proposed` tasks older than a day are listed for the PO («promote or close»), and the
                                  # daily digest names the `deferred` ones for the owner (the PO may not raise those) (27–28.11)
    "cut_on_design": False,       # true = the PO runs as soon as a design document is merged inside the execute phase (27.11)
}
_QUEUE_RANGE = {"target_ready": (1, 50), "max_per_run": (1, 12), "wake_every_min": (5, 1440)}
DEPENDENCIES_DEFAULTS: dict[str, Any] = {
    "at_merge": False,            # true = a dependency is met once it is MERGED (`released`), not only once it is accepted. 27.11,
                                  # ai-battles: reviews take an hour, all 23 tasks with a dependency waited for it (median 54 min)
    "roles": [],                  # … only for dependencies whose own role is in this list; [] = every role
    "report_stuck": False,        # true = a dependency that is finished without a usable result, or that leads back to the task
                                  # itself (a circle), is reported like a rejected one: an event and a question to the PO (27.11)
}
ANALYST_PROMPT_DEFAULTS: dict[str, bool] = {"compact_tasks": False}   # `cycle.analyst_prompt`, see DEFAULTS
MEASURE_DEFAULTS: dict[str, bool] = {"by_day": False}   # `cycle.measure`, see DEFAULTS
GAME_DESIGNER_DEFAULTS: dict[str, bool] = {"checkout": False}   # `cycle.game_designer`, see DEFAULTS
REVIEW_TESTS_DEFAULTS: dict[str, bool] = {
    "skip_for_docs": False,
    "once_per_batch": False,      # true = the test suite runs once on the head of the main branch and every review of a merged
                                  # task whose commit is in that head takes its result (27.11: ~113 of 360 suite runs a day, each
                                  # ~1000 CPU seconds, on a server at load 28–31 on 8 cores). A red head: every task gets its own run
    "changed_files": False,       # true = the reviewer's prompt lists the files the merged commit changed (28.11: reviewers and
                                  # testers of the game spent 13 turns in three cycles on `git show --stat`, which they may not run)
    "docs_outside_slots": False,  # true = the review of a commit that changes documents only takes no run slot (it is a 12-second
                                  # read; 27.11 such reviews waited 19 minutes for a slot)
}   # `cycle.review_tests`, see DEFAULTS
NODE_CACHE_DEFAULTS: dict[str, bool] = {"enabled": False}   # `cycle.node_cache`, see DEFAULTS
# `cycle.guards` (rework slice 4): what stops a run that goes wrong, beside the time limit and the inactivity watchdog
# `cycle.test_budget` (28.11): how often a role's run may run the product's WHOLE test suite. ai-battles 27.11: 247 runs of the
# suite inside developer runs, 2.6 per run, ~1000 CPU seconds each — a third of an 8-core server. `full_runs`: 0 = no rule (as
# before); N = the role is told to run the tests of what it touched while it works and the whole suite at most N times, at the end.
TEST_BUDGET_DEFAULTS: dict[str, Any] = {"full_runs": 0, "roles": ["developer", "designer"]}
# `cycle.heavy_runs` (27.11): commands a role's run must not start on the team's server — load tests, simulations of hundreds of
# battles. `deny`: Claude Code permission rules, e.g. "Bash(npx tsx tests/load/*)"; `roles`: whose runs ([] = every role).
# `queue`: rules denied as typed but allowed through `team-heavy <command>` — one such run at a time on the server, at `nice`.
# `slots`: how many such runs may go at once on the server, the dispatcher's own test runs before a review included (28.11: five
# whole-suite runs at once, 44 vitest processes, load 45 on 8 cores).
# `busy`: what `team-heavy` does when no place came free within `wait_s` — "refuse" (exit 75, as before) or "run" (the command runs
# all the same: the queue then only delays a run and never costs a role its tests).
# `cpu_quota`: 0 = off; N = every heavy run of the server (`team-heavy` and the dispatcher's own test runs) goes into one
# systemd slice of the user that may use at most N % of one core in all (600 = six cores) — the limit the queue cannot be
# since a run that waited `wait_s` starts all the same (28.11: five heavy runs at two places, load 38 on 8 cores).
HEAVY_RUNS_DEFAULTS: dict[str, Any] = {"deny": [], "queue": [], "roles": [], "nice": 15, "wait_s": 1800, "slots": 1, "busy": "refuse", "cpu_quota": 0}
GUARDS_DEFAULTS: dict[str, Any] = {
    "loop": False,                # true = a run that repeats one tool call with the same input and output is stopped
    "loop_repeats": 5,            # … after this many identical calls in a row (3..50)
    "cost": False,                # true = the cap of a run is at most cost_factor × the role's median spend per run
    "cost_factor": 8.0,           # (2..100)
    "cost_min_runs": 20,          # runs of the role in the window needed for a median (3..10000); fewer = no guard
    "cost_days": 7,               # the window of the median, days (1..90)
    "cost_floor_usd": 1.0,        # the guard never lowers a cap below this (0..100)
    "idle_reasons": False,        # true = at the end of the execute phase every task that is still `ready` gets its reason on record
                                  # (one event `execute.left` per cycle) — not only when the cycle executed nothing (26.11: a task of
                                  # a role without a seat lay three hours and nothing said why)
    "reap_orphans": False,        # true = before the seats start, processes whose working folder is a DELETED folder of this product's
                                  # work directory are ended (28.11: vitest of worktrees removed hours ago was still running)
    "no_name_kill": False,        # true = a role's run may not stop processes by name or pattern: pkill, killall, pgrep are denied
                                  # to it and its request says how to stop what it started (28.11: one run killed three others)
    "partial_limit": 0,           # N > 0 = after N partial results in a row (whatever their summaries say) the task is blocked
                                  # with a question to the PO (28.11, pokerbr К4: seven runs of one task, each «partial»)
    "budget_halt": False,         # true = once the day's budget is used up and nothing in flight can free any, the seats take
                                  # no further task in this cycle (27.11, found by a scenario: with retake and three passes
                                  # every ready task went ready → in_progress → ready three times per cycle)
    "crash_release": False,       # true = a task whose lane raised an exception goes back to `ready` (after CRASHES_MAX times: to
                                  # the PO with a question) instead of staying `in_progress` with a run id nobody holds (27.11)
}
_GUARDS_RANGE = {"loop_repeats": (3, 50, int), "cost_factor": (2.0, 100.0, float), "cost_min_runs": (3, 10000, int),
                 "cost_days": (1, 90, int), "cost_floor_usd": (0.0, 100.0, float), "partial_limit": (0, 20, int)}
DEFAULTS: dict[str, Any] = {
    "paths": {"products_dir": "products", "data_dir": "data", "repos_dir": "data/repos"},
    "claude": {"binary": "claude", "auth": "oauth_token", "timeout_s": 1500, "inactivity_s": 300, "max_turns": 60,
               # time budget per role (owner 21.11, F23/F30): {<role>: {timeout_s, inactivity_s}}; product.yaml `roles` overrides
               "roles": {},
               # shipped defaults by repository kind (data, not code): the developer of a service repository builds, starts
               # a server and plays e2e specs inside one run
               "roles_by_repo_kind": {"service": {"developer": {"timeout_s": 2700}}},
               "models": {"po": "opus", "developer": "sonnet", "reviewer": "sonnet", "analyst": "sonnet",
                          "marketer": "sonnet", "persona": "sonnet", "geo-probe": "sonnet", "onboarding": "opus",
                          "designer": "sonnet", "game-designer": "opus", "tester": "sonnet"}},
    "budget": {"max_usd_per_product_per_day": 20.0, "max_usd_per_run": 3.0, "min_usd_per_run": 0.3, "unknown_cost_reserve_usd": 0.5, "warn_share": 0.8},
    "cycle": {"review_after_min": 6, "measure_after_h": 24, "max_review_fails": 2, "persona_every_h": 12,
              "digest_hour_utc": 7, "digest_hours_utc": [7, 19], "measure_every_h": 6, "seo_every_h": 12, "geo_every_h": 168, "voice_every_h": 84, "dev_idle_po_every_h": 12,
              "max_ready": 3, "max_execute_per_cycle": 1,
              "max_continuations": 3,       # runs stopped by the time limit that may be continued per task before the PO splits it
              # parallel lanes inside one product's cycle (owner 21.11, team/lanes.py): reviews beside execution, one
              # execute lane per seat, one git worktree per task; product.yaml `cycle: {parallel: {...}}` overrides
              "parallel": dict(PARALLEL_DEFAULTS),
              # continuous work of the seats (owner 25.11, team/flow.py): a freed seat takes the next task inside the cycle,
              # the next cycle starts right after the previous one; off by default, product.yaml `cycle: {flow: {...}}` overrides
              "flow": dict(FLOW_DEFAULTS),
              # the PO's prompt (owner 26.11, rework slice 1): `compact_finished` gives accepted / released / measured tasks as a
              # short card instead of their full text, `stable_first` puts VISION first and the cycle number and the spend last;
              # both off by default, product.yaml `cycle: {po_prompt: {...}}` overrides key by key
              "po_prompt": dict(PO_PROMPT_DEFAULTS),
              # a role that lacks what its task needs (owner 26.11, rework slice 2): `needs_context` true = its `needs_context`
              # result becomes a question to the PO, the task waits and the attempt is not a failure; the answer travels to the
              # role with the task. Off = the old `blocked` + `question` path. product.yaml `cycle: {escalation: {...}}` overrides
              # `review_return` true = the third unproven review returns the SAME task to its role with the reason word and
              # what to do (slice 2b); an environment reason retries the review; off = blocked + a PO question, as before
              "escalation": dict(ESCALATION_DEFAULTS),
              # work that is re-created under a new id (owner 26.11, rework slice 3): `closed_list` true = the PO's prompt lists
              # the tasks closed in the last days (id, title, state, one line of why); `closed_check` true = a proposed task with
              # the title of finished work is skipped, one with the title of a rejected task is linked to it with `continues`
              "duplicates": dict(DUPLICATES_DEFAULTS),
              # loop and cost guards of a run (owner 26.11, rework slice 4); off by default, product.yaml `cycle: {guards: {...}}`
              "guards": dict(GUARDS_DEFAULTS),
              # a visible change is judged before players see it (owner 26.11, rework slice 5): `review_before_merge` true = a task
              # marked `visible` is reviewed on its branch, on a locally started server, and merged only by a pass;
              # `redesign_gate` true = a task marked `redesign` without a `new_function` is not created (the owner is not asked)
              "visible": dict(VISIBLE_DEFAULTS),
              # the PO cuts tasks ahead (owner 26.11, rework slice 9); off by default, product.yaml `cycle: {queue: {...}}`
              "queue": dict(QUEUE_DEFAULTS),
              # when a dependency counts as met (owner 27.11): product.yaml `cycle: {dependencies: {at_merge: true, roles: [...]}}`
              "dependencies": dict(DEPENDENCIES_DEFAULTS),
              # the analyst's prompt (26.11, rework slice 10): `compact_tasks` true = recently accepted tasks as a card (id, title,
              # type, goal, accepted_at, one line of the last result) instead of their full text; off by default
              "analyst_prompt": dict(ANALYST_PROMPT_DEFAULTS),
              # the day is measured, not every task (owner 26.11, rework slice 11): `by_day` true = accepted tasks close by
              # themselves after measure_after_h (no-metric tasks at once), the analyst gives a day-to-day summary only, a failed
              # analyst run is not repeated in the same window; off by default
              "measure": dict(MEASURE_DEFAULTS),
              # the game designer's run (26.11, rework slice 12): `checkout` true = a read-only checkout of the base branch is
              # its working folder and it has Read / Glob / Grep; off = no tools, an empty folder, documents in the prompt only
              "game_designer": dict(GAME_DESIGNER_DEFAULTS),
              # the test step before a review (27.11): `skip_for_docs` true = a commit that changes documents only is reviewed
              # without running the tests and without a play-test server; off by default
              "review_tests": dict(REVIEW_TESTS_DEFAULTS),
              "heavy_runs": dict(HEAVY_RUNS_DEFAULTS),
              "test_budget": dict(TEST_BUDGET_DEFAULTS),
              # a ready node_modules per lock file (27.11, team/nodecache.py): `enabled` true = a checkout without node_modules gets
              # a copy from the cache and the commands run without their `npm ci`; off by default
              "node_cache": dict(NODE_CACHE_DEFAULTS)},
    "publish": {"disclosure": {"default": "AI agent - formachines.ai"}, "max_posts_per_channel_per_day": 2,
                "channel_caps": {}, "dry_run": False},
    "deploy": {"mode": "git_pull", "droplet_host": "root@68.183.2.193"},
}


def _merge(base: dict[str, Any], over: dict[str, Any] | None) -> dict[str, Any]:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_env(path: Path | None = None) -> dict[str, str]:
    """KEY=VALUE lines of .env into os.environ (existing variables win). Returns what was set."""
    p = path or ROOT / ".env"
    out: dict[str, str] = {}
    if not p.exists():
        return out
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        k, v = k.strip(), v.split("#", 1)[0].strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v
            out[k] = v
    return out


@dataclass
class Settings:
    raw: dict[str, Any]
    root: Path
    products_dir: Path
    data_dir: Path
    repos_dir: Path

    @property
    def claude(self) -> dict[str, Any]:
        return self.raw["claude"]

    @property
    def budget(self) -> dict[str, Any]:
        return self.raw["budget"]

    @property
    def cycle(self) -> dict[str, Any]:
        return self.raw["cycle"]

    @property
    def publish(self) -> dict[str, Any]:
        return self.raw["publish"]

    def product_dirs(self) -> list[Path]:
        return sorted(p for p in self.products_dir.iterdir() if (p / "product.yaml").exists()) if self.products_dir.exists() else []


def load(path: Path | None = None) -> Settings:
    load_env()
    p = path or Path(os.environ.get("TEAM_CONFIG") or ROOT / "team.yaml")
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) if p.exists() else {}
    if not p.exists():
        log.warning("team.yaml not found at %s, defaults in use", p)
    st = _merge(DEFAULTS, raw or {})
    if os.environ.get("CLAUDE_BINARY"):
        st["claude"]["binary"] = os.environ["CLAUDE_BINARY"]
    paths = st["paths"]
    return Settings(st, ROOT, _abs(paths["products_dir"]), _abs(paths["data_dir"]), _abs(paths["repos_dir"]))


def _abs(p: str | Path) -> Path:
    p = Path(p)
    return (p if p.is_absolute() else ROOT / p).resolve()


MILESTONES = ("M0", "M1", "M2", "M3")      # game profile stages, in order (VISION.game.template.md)
HYPOTHESIS_FROM_STAGE = "M2"               # from this stage a `milestone` task is a `feature` under the hypothesis rule


def stage_index(stage: str) -> int:
    """Position of a milestone id in MILESTONES; unknown ids sort after the known ones (an unknown stage opens no gate)."""
    s = str(stage or "").strip().upper()
    return MILESTONES.index(s) if s in MILESTONES else (len(MILESTONES) if s else -1)


DEPLOY_BATCH_MAX_WAIT_MIN = 45.0           # product.yaml `deploy.batch_max_wait_min` default: the longest a merged commit waits for the batch deploy
CYCLE_EVERY_MIN_RANGE = (5, 60)            # product.yaml `cycle.every_min`: 5 = the fast cron step, 60 = the hourly cron itself


def parse_cycle_every_min(raw: dict[str, Any], where: str = "product.yaml") -> int | None:
    """`cycle: {every_min: N}` of a product.yaml → N, or None when the setting is absent (the product runs on the hourly
    cron only). N must be a whole number of minutes inside CYCLE_EVERY_MIN_RANGE; anything else is a ValueError naming
    the file, so a typo is seen at load time and not as a product that silently stays hourly."""
    block = raw.get("cycle")
    if block is None:
        return None
    if not isinstance(block, dict):
        raise ValueError(f"{where}: `cycle` must be a mapping like `cycle: {{every_min: 10}}`, got {block!r}")
    v = block.get("every_min")
    if v is None:
        return None
    lo, hi = CYCLE_EVERY_MIN_RANGE
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise ValueError(f"{where}: cycle.every_min must be a whole number of minutes {lo}..{hi}, got {v!r}")
    return v


WORKTREES_DIRNAME = "wt"                   # data/work/<slug>/wt/<task id>: the checkout of one task (git worktree of data/repos/<slug>)

RUN_TIMEOUT_CEILING_S = 3600               # hard ceiling of one role run, whatever team.yaml / product.yaml say (owner 21.11)
RUN_LIMIT_KEYS = ("timeout_s", "inactivity_s")
RUN_LIMIT_FALLBACK = {"timeout_s": 1500, "inactivity_s": 300}   # only when `claude.<key>` itself is missing or invalid


def _limit_value(v: Any, where: str) -> int | None:
    """A positive whole number of seconds, or None (absent). A wrong value is logged and skipped, so a typo in one
    layer falls back to the layer below instead of starting a run with no limit."""
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
        log.warning("%s: %r is not a positive number of seconds — ignored", where, v)
        return None
    return int(v)


def run_limits(st: "Settings", product: "Product | None", role: str) -> dict[str, Any]:
    """Time budget of one role run: `{timeout_s, inactivity_s, source: {key: where it came from}}`.

    Layers, the later one wins: `claude.<key>` (global) → `claude.roles.<role>` (team.yaml) →
    `claude.roles_by_repo_kind.<repo.kind>.<role>` (team.yaml; ships 2700 s for the developer of a service repository)
    → `roles.<role>` of product.yaml. `timeout_s` above RUN_TIMEOUT_CEILING_S is clamped with a WARNING; the
    inactivity watchdog is never longer than the hard limit."""
    claude = st.raw.get("claude") or {}
    out: dict[str, Any] = {"source": {}}
    kind = product.repo_kind if product is not None else ""
    slug = product.slug if product is not None else "-"
    layers: list[tuple[str, Any]] = [
        ("claude", claude),
        (f"claude.roles.{role}", (claude.get("roles") or {}).get(role)),
        (f"claude.roles_by_repo_kind.{kind}.{role}", ((claude.get("roles_by_repo_kind") or {}).get(kind) or {}).get(role)),
        (f"product.yaml roles.{role}", ((product.raw.get("roles") or {}).get(role) if product is not None else None)),
    ]
    for key in RUN_LIMIT_KEYS:
        out[key], out["source"][key] = RUN_LIMIT_FALLBACK[key], "built-in"
        for where, block in layers:
            if block is not None and not isinstance(block, dict):
                log.warning("%s: %s must be a mapping like {timeout_s: 2700}, got %r — ignored", slug, where, block)
                continue
            v = _limit_value((block or {}).get(key), f"{slug}: {where}.{key}")
            if v is not None:
                out[key], out["source"][key] = v, where
    if out["timeout_s"] > RUN_TIMEOUT_CEILING_S:
        log.warning("%s: %s timeout_s=%d (%s) is above the hard ceiling %d s — clamped", slug, role, out["timeout_s"],
                    out["source"]["timeout_s"], RUN_TIMEOUT_CEILING_S)
        out["timeout_s"], out["clamped_from"] = RUN_TIMEOUT_CEILING_S, out["timeout_s"]
    if out["inactivity_s"] > out["timeout_s"]:
        log.debug("%s: %s inactivity_s=%d is longer than timeout_s=%d — lowered to it", slug, role, out["inactivity_s"], out["timeout_s"])
        out["inactivity_s"] = out["timeout_s"]
    log.debug("%s: run limits for %s: %s", slug, role, out)
    return out


PRODUCT_DEFAULTS: dict[str, Any] = {
    "slug": "", "name": "", "host": "", "site_base": "", "repo": {"url": "", "branch": "main", "public_dir": "public"},
    "channels": {}, "targets": [], "budget_usd_per_day": None, "paused": False,
    "goals": [],
}


@dataclass
class Product:
    slug: str
    dir: Path
    raw: dict[str, Any]
    st: Settings

    @property
    def host(self) -> str:
        return str(self.raw.get("host") or "")

    @property
    def site_base(self) -> str:
        return str(self.raw.get("site_base") or (f"https://{self.host}" if self.host else ""))

    @property
    def repo_url(self) -> str:
        return str((self.raw.get("repo") or {}).get("url") or "")

    @property
    def repo_branch(self) -> str:
        return str((self.raw.get("repo") or {}).get("branch") or "main")

    @property
    def repo_mode(self) -> str:
        """merge (default: task branch merged into base and pushed) or pr (branch pushed, tests must pass, pull request opened, owner merges)."""
        return str((self.raw.get("repo") or {}).get("mode") or "merge")

    @property
    def test_cmd(self) -> str:
        return str((self.raw.get("repo") or {}).get("test_cmd") or "")

    @property
    def public_dir(self) -> str:
        return str((self.raw.get("repo") or {}).get("public_dir") or "public")

    # --- game profile (owner 20.11; SCHEME §11): a product that is a long-running service, not a static site ---------

    @property
    def profile(self) -> str:
        """`site` (default: static pages on the droplet) or `game` (networked game: service repository, milestones, phase gates)."""
        return str(self.raw.get("profile") or "site")

    @property
    def stage(self) -> str:
        """Current milestone id (M0…M3) from product.yaml; `team onboard` writes it, the PO asks the owner to advance it."""
        return str(self.raw.get("stage") or MILESTONES[0])

    @property
    def phase_gates(self) -> dict[str, str]:
        """check/phase name → the milestone from which it runs (e.g. marketing: M2). Missing name = always on."""
        g = self.raw.get("phase_gates") or {}
        return {str(k): str(v) for k, v in g.items()} if isinstance(g, dict) else {}

    def gate_open(self, name: str) -> bool:
        """True when the phase/check may run at the product's current stage. A site profile has no gates."""
        need = self.phase_gates.get(name)
        if not need:
            return True
        return stage_index(self.stage) >= stage_index(need)

    @property
    def repo_kind(self) -> str:
        """`static` (public/ is served as is) or `service` (a process: run_cmd, build_cmd, health_path, port)."""
        return str((self.raw.get("repo") or {}).get("kind") or "static")

    @property
    def run_cmd(self) -> str:
        return str((self.raw.get("repo") or {}).get("run_cmd") or "")

    @property
    def build_cmd(self) -> str:
        return str((self.raw.get("repo") or {}).get("build_cmd") or "")

    @property
    def health_path(self) -> str:
        return str((self.raw.get("repo") or {}).get("health_path") or "")

    @property
    def port(self) -> int | None:
        v = (self.raw.get("repo") or {}).get("port")
        return int(v) if isinstance(v, int) or (isinstance(v, str) and v.isdigit()) else None

    # --- deploy (slice 5): how the merged commit reaches the host ---------------------------------------------------

    @property
    def deploy_mode(self) -> str:
        """`pending` (no host yet: the tester plays a local server), `service` (the dispatcher runs deploy-service.sh
        after every merge) or `git_pull` (static site: the droplet timer pulls). Default: git_pull for a site, pending for a game."""
        mode = str((self.raw.get("deploy") or {}).get("mode") or "")
        return mode or ("pending" if self.profile == "game" else "git_pull")

    @property
    def deploy_host(self) -> str:
        """Empty / `local` = the dispatcher's own host (sudo); `user@host` = over ssh."""
        return str((self.raw.get("deploy") or {}).get("host") or "")

    @property
    def deploy_cmd(self) -> str:
        return str((self.raw.get("deploy") or {}).get("cmd") or "")

    @property
    def deploy_origin(self) -> str:
        """`deploy.origin`: path of a bare git repository on a remote game host (owner 24.11: the game gets its own server).
        When set together with a remote `deploy.host`, the dispatcher pushes the commit there over ssh before it calls the
        deploy script, so the game host needs no GitHub credential and no way back to the dispatcher. Empty = the deploy
        script fetches from its own `REPO` (the same-host setup)."""
        return str((self.raw.get("deploy") or {}).get("origin") or "")

    @property
    def deploy_batch(self) -> bool:
        """`deploy.batch: true` (owner 25.11, service deploys only): merges go on as tasks finish, the live host is
        switched once per cycle — at the end of the execute phase, to the last merged commit — instead of after every
        merge (67 deploys on 25.11, each one cutting the matches of live players). Default false: deploy after each merge."""
        v = (self.raw.get("deploy") or {}).get("batch")
        if v is not None and not isinstance(v, bool):
            log.warning("%s: deploy.batch=%r is not true/false — false in use", self.slug, v)
        return v is True

    @property
    def deploy_batch_max_wait_min(self) -> float:
        """`deploy.batch_max_wait_min` (default 45, 1..720): a merged commit never waits longer than this for the batch —
        a long cycle (refill, a 45-minute run) deploys in between."""
        v = (self.raw.get("deploy") or {}).get("batch_max_wait_min")
        if v is None:
            return DEPLOY_BATCH_MAX_WAIT_MIN
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 1 <= v <= 720:
            log.warning("%s: deploy.batch_max_wait_min=%r is not a number of minutes 1..720 — %s in use", self.slug, v, DEPLOY_BATCH_MAX_WAIT_MIN)
            return DEPLOY_BATCH_MAX_WAIT_MIN
        return float(v)

    @property
    def deploy_branch(self) -> str:
        """Branch of the game host's bare repository the deploy script reads (`BRANCH` in its conf, default main)."""
        return str((self.raw.get("deploy") or {}).get("branch") or "main")

    @property
    def repo_dir(self) -> Path:
        return self.st.repos_dir / self.slug

    @property
    def runs_dir(self) -> Path:
        return self.st.data_dir / "runs" / self.slug

    @property
    def work_dir(self) -> Path:
        return self.st.data_dir / "work" / self.slug

    # --- parallel lanes and per-task checkouts (owner 21.11, team/lanes.py) -------------------------------------------

    @property
    def worktrees_dir(self) -> Path:
        """Where the task checkouts of the repository roles live: one `git worktree` of `repo_dir` per task."""
        return self.work_dir / WORKTREES_DIRNAME

    def worktree_dir(self, task_id: str) -> Path:
        return self.worktrees_dir / str(task_id)

    @property
    def parallel(self) -> ParallelConfig:
        """`cycle.parallel` of team.yaml with this product's `cycle: {parallel: {...}}` on top (validated)."""
        return parallel_settings(self.st.cycle, self.raw, self.slug)

    @property
    def flow(self) -> FlowConfig:
        """`cycle.flow` of team.yaml with this product's `cycle: {flow: {...}}` on top (validated; team/flow.py)."""
        return flow_settings(self.st.cycle, self.raw, self.slug)

    def switches(self, block: str, defaults: dict[str, bool]) -> dict[str, bool]:
        """On/off switches of `cycle.<block>`: team.yaml first, this product's `cycle: {<block>: {...}}` on top, key by
        key. Only a real true/false counts; anything else is reported and the default stays (a typo never switches a
        behaviour on)."""
        out = dict(defaults)
        own = self.raw.get("cycle")
        for where, src in (("team.yaml", self.st.cycle.get(block)), (f"{self.slug} product.yaml", own.get(block) if isinstance(own, dict) else None)):
            if src is None:
                continue
            if not isinstance(src, dict):
                log.warning("%s: cycle.%s=%r is not a mapping — ignored", where, block, src)
                continue
            for k, v in src.items():
                if k in out and isinstance(v, bool):
                    out[k] = v
                elif k in out:
                    log.warning("%s: cycle.%s.%s=%r is not true/false — %s in use", where, block, k, v, out[k])
        return out

    @property
    def po_prompt_cfg(self) -> dict[str, bool]:
        """`cycle.po_prompt` (rework slice 1): {compact_finished, stable_first}, both off by default."""
        return self.switches("po_prompt", PO_PROMPT_DEFAULTS)

    @property
    def escalation_cfg(self) -> dict[str, bool]:
        """`cycle.escalation` (rework slice 2; 27.11): {needs_context, review_return, references, budget_split}, off by default."""
        return self.switches("escalation", ESCALATION_DEFAULTS)

    @property
    def duplicates_cfg(self) -> dict[str, bool]:
        """`cycle.duplicates` (rework slice 3): {closed_list, closed_check}, off by default."""
        return self.switches("duplicates", DUPLICATES_DEFAULTS)

    @property
    def guards_cfg(self) -> dict[str, Any]:
        """`cycle.guards` (rework slice 4): team.yaml first, this product's block on top, key by key; a value of the wrong
        type or outside its range is reported and the default stays."""
        out = dict(GUARDS_DEFAULTS)
        own = self.raw.get("cycle")
        for where, src in (("team.yaml", self.st.cycle.get("guards")), (f"{self.slug} product.yaml", own.get("guards") if isinstance(own, dict) else None)):
            if not isinstance(src, dict):
                if src is not None:
                    log.warning("%s: cycle.guards=%r is not a mapping — ignored", where, src)
                continue
            for k, v in src.items():
                if k not in out:
                    continue
                if k in ("loop", "cost", "crash_release", "idle_reasons", "budget_halt", "no_name_kill", "reap_orphans"):
                    if isinstance(v, bool):
                        out[k] = v
                    else:
                        log.warning("%s: cycle.guards.%s=%r is not true/false — %s in use", where, k, v, out[k])
                    continue
                lo, hi, typ = _GUARDS_RANGE[k]
                try:
                    num = typ(v)
                    if isinstance(v, bool) or not lo <= num <= hi:
                        raise ValueError
                    out[k] = num
                except (TypeError, ValueError):
                    log.warning("%s: cycle.guards.%s=%r is not a number in %s..%s — %s in use", where, k, v, lo, hi, out[k])
        return out

    @property
    def visible_cfg(self) -> dict[str, bool]:
        """`cycle.visible` (rework slice 5): {review_before_merge, redesign_gate}, off by default."""
        return self.switches("visible", VISIBLE_DEFAULTS)

    @property
    def dependencies_cfg(self) -> dict[str, Any]:
        """`cycle.dependencies`: team.yaml first, this product's block on top; a wrong value keeps the default."""
        out = {"at_merge": False, "roles": [], "report_stuck": False}
        own = self.raw.get("cycle")
        for where, src in (("team.yaml", self.st.cycle.get("dependencies")), (f"{self.slug} product.yaml", own.get("dependencies") if isinstance(own, dict) else None)):
            if not isinstance(src, dict):
                if src is not None:
                    log.warning("%s: cycle.dependencies=%r is not a mapping — ignored", where, src)
                continue
            for k, v in src.items():
                if k in ("at_merge", "report_stuck"):
                    if isinstance(v, bool):
                        out[k] = v
                    else:
                        log.warning("%s: cycle.dependencies.%s=%r is not true/false — %s in use", where, k, v, out[k])
                elif k == "roles":
                    if isinstance(v, list) and all(isinstance(x, str) for x in v):
                        out[k] = [x.strip() for x in v if x.strip()]
                    else:
                        log.warning("%s: cycle.dependencies.roles=%r is not a list of role names — %s in use", where, v, out[k])
        return out

    @property
    def test_budget_cfg(self) -> dict[str, Any]:
        """`cycle.test_budget`: team.yaml first, this product's block on top; a wrong value keeps the default."""
        out: dict[str, Any] = {"full_runs": 0, "roles": list(TEST_BUDGET_DEFAULTS["roles"])}
        own = self.raw.get("cycle")
        for where, src in (("team.yaml", self.st.cycle.get("test_budget")), (f"{self.slug} product.yaml", own.get("test_budget") if isinstance(own, dict) else None)):
            if not isinstance(src, dict):
                continue
            v = src.get("full_runs")
            if v is not None:
                if isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 10:
                    out["full_runs"] = v
                else:
                    log.warning("%s: cycle.test_budget.full_runs=%r is not a whole number in 0..10 — %s in use", where, v, out["full_runs"])
            r = src.get("roles")
            if r is not None:
                if isinstance(r, list) and all(isinstance(x, str) and x.strip() for x in r):
                    out["roles"] = [x.strip() for x in r]
                else:
                    log.warning("%s: cycle.test_budget.roles=%r is not a list of role names — ignored", where, r)
        return out

    @property
    def heavy_runs_cfg(self) -> dict[str, Any]:
        """`cycle.heavy_runs`: this product's block replaces team.yaml's, list by list; anything that is not a list of
        strings is ignored with a warning."""
        out: dict[str, Any] = {k: (list(v) if isinstance(v, list) else v) for k, v in HEAVY_RUNS_DEFAULTS.items()}
        own = self.raw.get("cycle")
        for where, src in (("team.yaml", self.st.cycle.get("heavy_runs")), (f"{self.slug} product.yaml", own.get("heavy_runs") if isinstance(own, dict) else None)):
            if not isinstance(src, dict):
                continue
            for k in ("deny", "queue", "roles"):
                v = src.get(k)
                if v is None:
                    continue
                if isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v):
                    out[k] = [x.strip() for x in v]
                else:
                    log.warning("%s: cycle.heavy_runs.%s=%r is not a list of strings — ignored", where, k, v)
            b = src.get("busy")
            if b is not None:
                if b in ("refuse", "run"):
                    out["busy"] = b
                else:
                    log.warning("%s: cycle.heavy_runs.busy=%r is not \"refuse\" or \"run\" — %s in use", where, b, out["busy"])
            for k, lo, hi in (("nice", 0, 19), ("wait_s", 0, 86400), ("slots", 1, 64), ("cpu_quota", 0, 6400)):
                v = src.get(k)
                if v is None:
                    continue
                if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi:
                    out[k] = v
                else:
                    log.warning("%s: cycle.heavy_runs.%s=%r is not a whole number in %d..%d — %s in use", where, k, v, lo, hi, out[k])
        return out

    @property
    def queue_cfg(self) -> dict[str, Any]:
        """`cycle.queue` (rework slice 9): team.yaml first, this product's block on top; a wrong value keeps the default."""
        out = dict(QUEUE_DEFAULTS)
        own = self.raw.get("cycle")
        for where, src in (("team.yaml", self.st.cycle.get("queue")), (f"{self.slug} product.yaml", own.get("queue") if isinstance(own, dict) else None)):
            if not isinstance(src, dict):
                if src is not None:
                    log.warning("%s: cycle.queue=%r is not a mapping — ignored", where, src)
                continue
            for k, v in src.items():
                if k in ("ahead", "cut_on_design", "remind_parked"):
                    if isinstance(v, bool):
                        out[k] = v
                    else:
                        log.warning("%s: cycle.queue.%s=%r is not true/false — %s in use", where, k, v, out[k])
                elif k in _QUEUE_RANGE:
                    lo, hi = _QUEUE_RANGE[k]
                    if isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi:
                        out[k] = v
                    else:
                        log.warning("%s: cycle.queue.%s=%r is not a whole number in %s..%s — %s in use", where, k, v, lo, hi, out[k])
        return out

    @property
    def analyst_prompt_cfg(self) -> dict[str, bool]:
        """`cycle.analyst_prompt` (rework slice 10): {compact_tasks}, off by default."""
        return self.switches("analyst_prompt", ANALYST_PROMPT_DEFAULTS)

    @property
    def measure_cfg(self) -> dict[str, bool]:
        """`cycle.measure` (rework slice 11): {by_day}, off by default."""
        return self.switches("measure", MEASURE_DEFAULTS)

    @property
    def game_designer_cfg(self) -> dict[str, bool]:
        """`cycle.game_designer` (rework slice 12): {checkout}, off by default."""
        return self.switches("game_designer", GAME_DESIGNER_DEFAULTS)

    @property
    def review_tests_cfg(self) -> dict[str, bool]:
        """`cycle.review_tests` (27.11): {skip_for_docs}, off by default."""
        return self.switches("review_tests", REVIEW_TESTS_DEFAULTS)

    @property
    def node_cache_cfg(self) -> dict[str, bool]:
        """`cycle.node_cache` (27.11): {enabled}, off by default."""
        return self.switches("node_cache", NODE_CACHE_DEFAULTS)

    # --- cadence (owner 20.11, friction F12): how often the product's cycle runs --------------------------------------

    @property
    def cycle_every_min(self) -> int | None:
        """Minutes between cycles from `cycle.every_min` (5..60), or None = hourly cron only. `team cycle --due`
        (run-cycle-fast.sh) starts a cycle when the last finished one is at least this old."""
        return parse_cycle_every_min(self.raw, f"{self.dir / 'product.yaml'}")

    @property
    def cadence(self) -> str:
        """Human-readable cadence for `team status`."""
        n = self.cycle_every_min
        return f"cycle every {n} min" if n else "hourly"

    # --- run time budget and continuation (owner 21.11, frictions F23/F30) ---------------------------------------------

    def run_limits(self, role: str) -> dict[str, Any]:
        """`{timeout_s, inactivity_s, source}` for one run of `role` on this product (see `run_limits` above)."""
        return run_limits(self.st, self, role)

    @property
    def max_continuations(self) -> int:
        """How many times a task whose run was stopped by the time limit is continued before it goes to the PO to be
        split: `cycle.max_continuations` of product.yaml, else of team.yaml. 0 = never continue."""
        own = self.raw.get("cycle")
        v = own.get("max_continuations") if isinstance(own, dict) else None
        if v is None:
            v = self.st.cycle.get("max_continuations")
        default = int(DEFAULTS["cycle"]["max_continuations"])
        if v is None:
            return default
        try:
            return max(0, int(v))
        except (TypeError, ValueError):
            log.warning("%s: cycle.max_continuations=%r is not a number — %d in use", self.slug, v, default)
            return default

    @property
    def daily_cap(self) -> float:
        v = self.raw.get("budget_usd_per_day")
        return float(v) if v is not None else float(self.st.budget["max_usd_per_product_per_day"])

    def run_cap(self, role: str) -> tuple[float, str] | None:
        """The owner's cap of ONE run of `role`, USD: product.yaml `run_caps.<role>`, else team.yaml
        `budget.run_caps.<role>`; None = the caller's own cap (the PO's $2, the task's `budget_usd`, `max_usd_per_run`).
        Owner 23.11: a role moved to a dearer model (`models: {po: claude-fable-5-2}`) needs a higher cap with it —
        the PO's $2 was written for Opus, and a run that hits `--max-budget-usd` is cut and brings no decision.
        A wrong value is logged and skipped. Returns (cap, where it came from)."""
        for where, block in ((f"product.yaml run_caps.{role}", self.raw.get("run_caps")),
                             (f"team.yaml budget.run_caps.{role}", self.st.budget.get("run_caps"))):
            if block is None:
                continue
            if not isinstance(block, dict):
                log.warning("%s: %s must be a mapping like {po: 6.0}, got %r — ignored", self.slug, where.rsplit(".", 1)[0], block)
                continue
            v = block.get(role)
            if v is None:
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
                log.warning("%s: %s=%r is not a positive number of dollars — ignored", self.slug, where, v)
                continue
            return float(v), where
        return None

    @property
    def stop_file(self) -> Path:
        return self.dir / "STOP"

    def vision(self) -> str:
        p = self.dir / "VISION.md"
        return p.read_text(encoding="utf-8") if p.exists() else ""


def load_product(st: Settings, slug_or_dir: str | Path) -> Product:
    d = Path(slug_or_dir) if isinstance(slug_or_dir, Path) or "/" in str(slug_or_dir) else st.products_dir / str(slug_or_dir)
    raw = _merge(PRODUCT_DEFAULTS, yaml.safe_load((d / "product.yaml").read_text(encoding="utf-8")) or {})
    raw["slug"] = raw.get("slug") or d.name
    every = parse_cycle_every_min(raw, str(d / "product.yaml"))      # validated here: a bad value fails the load, loudly
    log.debug("product %s: cadence %s", raw["slug"], f"every {every} min" if every else "hourly")
    return Product(str(raw["slug"]), d.resolve(), raw, st)


def load_products(st: Settings) -> list[Product]:
    return [load_product(st, d) for d in st.product_dirs()]
