"""One dispatcher cycle for one product: collect → review → triage (PO) → execute → measure → digest.

Each phase is idempotent and checkpointed through the store (task states, events); a cycle that dies mid-way is
continued by the next one. Every model run is capped (per run, per product per day) and booked; a run without a
reported cost is booked at the reserve. A STOP file (product or root) ends the cycle before any model run.

Since 21.11 the role runs of one cycle overlap (team/lanes.py holds the executor and its invariants): the reviews of
released tasks run beside the PO's triage and beside execution, every seat of `cycle.execute_slots` is its own lane,
and a repository role works in its own `git worktree` of the task. `cycle.parallel.enabled: false` makes the cycle
strictly sequential again; the worktrees stay.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from . import balance, geo, gitops, hypothesis, playtest, prompts, prfacts, publish, replan, servicedeploy, standing, voice
from .config import HYPOTHESIS_FROM_STAGE, Product, Settings, stage_index
from .config import WORKTREES_DIRNAME
from .runner import Runner, RunResult, front_matter
from .store import Store, parse_ts, utcnow
from . import runprofile                                  # own lines: the import lists above are edited by other branches
from .runner import KILLED_LOOP, killed_kind
from . import canary as canary_mod
from . import lanes as lanes_mod
from . import flow as flow_mod
from .lanes import LANE_EXECUTE, LANE_REVIEW

# parallel lanes and per-task checkouts (owner 21.11, frictions F21/F29; team/lanes.py)
EXECUTE_WAVES_MAX = 3                                     # passes of the execute phase in a parallel cycle: the first starts what is ready, a later one what the reviews and the other lanes of this cycle made startable
WORKTREE_LIVE_STATES = ("ready", "claimed", "in_progress")            # the task's checkout is kept: the next run works in it
WORKTREE_PARKED_STATES = ("blocked", "waiting_owner", "deferred")     # kept for `cycle.parallel.worktree_keep_h` hours, then removed (the branch stays)
MERGE_CONFLICT = "merge_conflict"                         # task field: the last merge into the base branch conflicted (cleared by a merge that went through)
CONFLICT_PROMPT_CHARS = 5000                              # of the conflict text kept on the task and shown to the author in the next prompt
CONFLICT_NOTE_CHARS = 200                                 # of the file list in the transition note
DEPLOY_PENDING = "deploy_pending"                         # task field (`deploy.batch: true`): merged at this ISO time, waits for the batch deploy
DEPLOY_ENV_EXITS = (servicedeploy.EXIT_PUSH_FAILED, servicedeploy.EXIT_NGINX_FAILED, 124, 127, 255)   # the host or the way to it failed, not a commit: push, nginx, timeout, no script, ssh
DEPLOY_ENV_RETRIES = 3                                    # batches that may fail that way before each waiting task is deployed (and blamed) on its own
RUN_PORT_ENV = "TEAM_PORT"                                # environment of a repository role's run on a service repository: a port of its own for a server it starts

# run continuation (owner 21.11, frictions F23/F30): a run stopped by the time limit is continued, not restarted
# what a review leaves on the disk (25.11: 154 GB full after five days — 803 node_modules in 226 cycle folders, team/cycle.py
# `Cycle.drop_review_dirs` and `Cycle.sweep_work_dirs`)
WORKDIR_HEAVY_DIRS = ("checkout", "tests-run")           # data/work/<slug>/<cycle>/<label>/<one of these>: clones made for one run
WORKDIR_HEAVY_NAME = "node_modules"                      # removed wherever it lies inside a cycle folder
WORKDIR_KEEP = (WORKTREES_DIRNAME, "deploy", "onboarding")   # folders of data/work/<slug> that are not cycle folders: never swept
WORKDIR_SWEPT_MARK = ".swept"                            # written into a swept cycle folder: the next sweeps skip it
WORKDIR_SWEEP_MAX_S = 120.0                              # one sweep stops after this long; the rest waits for the next cycle
CONTINUE_ROLES = ("developer", "designer")               # roles that work in a product repository checkout
INTERRUPTED = "interrupted"                               # result status of a run the dispatcher stopped: not a failed attempt for the replan counter
WIP_WHY = {"timeout": "run stopped by the time limit, continues next cycle",          # text of the service commit, by reason
           "inactivity": "run stopped by the inactivity watchdog, continues next cycle",
           "partial": "partial result, continues next cycle"}
QUESTION_PROFILES_MAX = 4                                 # stopped runs listed in the «split this task» question to the PO
QUESTION_PROFILE_CHARS = 230                              # characters per run in that question, at most
QUESTION_MAX_CHARS = 1480                                 # the store keeps 1500 characters of a question: the profile lines are cut to fit

INCONCLUSIVE_LIMIT = 3                 # `inconclusive` verdicts of one released task before the rule below applies
NEEDS_CONTEXT = "needs_context"            # result status: the role lacks what the task needs and says what (rework slice 2a)
NEEDS_CONTEXT_ITEMS = 5                    # of the role's `missing` list that go into the question to the PO
NEEDS_CONTEXT_EVENT = "task.needs_context"
RUN_LOOP_EVENT, RUN_COST_EVENT = "run.loop", "run.cost_guard"   # rework slice 4: a run stopped by the loop guard / the cost guard
QUEUE_ROLES = ("developer",)                # `cycle.queue.ahead`: the roles whose ready queue the PO keeps at `target_ready`
TASK_MARKS = ("visible", "redesign", "new_function")       # the PO's marks of a visible change (rework slice 5), kept on the task
PRE_MERGE = "pre_merge"                    # task field: in `review` with its commit on task/<id> only — not merged, not deployed
REVIEW_BEFORE_MERGE_EVENT, REDESIGN_HELD_EVENT = "review.before_merge", "task.redesign_held"
PRE_MERGE_PROMPT = (
    "\n# REVIEW BEFORE THE MERGE\nThis is a visible change and it is not on the live host: commit {sha} is on the task's branch only. It is "
    "merged and deployed only if you pass it, so players see nothing of it yet. Judge it on what you were given here — the server "
    "started from this commit, the checkout, the test run; the live site shows the game WITHOUT this change (useful as «before»). "
    "The owner's principle applies: «редизайн делаем только при новом функционале» — if the change re-lays or restyles a screen that "
    "already existed beyond what its new function needs, that is a `fail` (first_failure: «redesign without a new function»), however "
    "well it matches a mock-up.\n")
# Three unproven reviews (rework slice 2b, `cycle.escalation.review_return`): what the role is told per reason word of
# team/play.py INCONCLUSIVE_REASONS when the task comes back to it …
REVIEW_RETURN_TODO = {
    "no_tests_live": "No test of this task runs against the deployed host. Give every acceptance criterion a scenario the tester can run: "
                     "tag it for the live host, or make sure the spec runs on the local test server (`team-play test <spec> --local`).",
    "no_tests": "The spec the task names holds no test the runner finds. Add the spec (or fix its name and tags) so that "
                "`team-play test <spec>` runs at least one test per acceptance criterion.",
    "no_test_for_criterion": "At least one acceptance criterion has no scenario. Add a spec scenario for each criterion the review lists as "
                             "not met, with `data-testid` hooks on what it reads.",
    "not_observable": "The result cannot be seen from outside. Make it observable: a `data-testid`, a line in the server log or the metrics, "
                      "a screenshot the tester can take — and say in `how_to_verify` exactly where to look.",
    "run_timeout": "The check does not finish inside the tester's time limit. Make the scenario shorter or split it (one spec per "
                   "criterion, no loops over random starts) so one spec runs in well under ten minutes.",
    "other": "The review could not prove the result for the reason it gives below. Remove that obstacle in the repository (a test, a hook, "
             "a report the acceptance asks for) and say in `how_to_verify` how the proof is obtained.",
}
# … and the reasons that are the environment's, not the role's: the review gets another try instead
REVIEW_RETRY_REASONS = ("lock_timeout", "server_not_started", "test_tooling")
REVIEW_RETURN_MAX = 2                      # returns to work per task; the next third unproven review is the old rule (blocked + PO)
REVIEW_ENV_RETRIES = 2                     # extra reviews per task after an environment reason; then the old rule
REVIEW_RETURN_EVENT, REVIEW_RETRY_EVENT = "review.returned", "review.retry"
INCONCLUSIVE_ACCEPT, INCONCLUSIVE_ASK = "accept", "ask"   # product.yaml review.inconclusive; default by profile (Cycle.inconclusive_policy)
DESIGN_TEST_SUFFIXES = (".json", ".yaml", ".yml", ".csv")   # game-designer files the code reads: the repository tests run before their merge
DESIGN_TESTS_EVENT = "design.tests_failed"
DESIGN_TESTS_TAIL_CHARS = 900          # how much of the failing test output goes into the result and the PO question
GAME_ROLE_SLOTS = {"designer": 1, "game-designer": 1, "tester": 1}   # owner 20.11: seats the game profile adds to cycle.execute_slots
PENDING_ROLES: tuple[str, ...] = ()                      # roles without an execute path (none since slice 6: a tester task = load test)
LOAD_TASK_TYPES = ("load",)                               # tester tasks: N clients against a threshold (slice 6, docs/game-metrics.md)
PLAYTEST_ROLES = ("developer", "designer")               # whose released tasks the tester plays (game profile)
TESTER_CAP_USD = 1.5
TESTER_MAX_TURNS = 40
TESTER_TOOLS = ["Bash(team-play *)", "Bash(team-play:*)", "Bash(team-fetch *)", "Bash(team-fetch:*)", "Read", "Glob", "Grep"]


def _y_short(obj: Any, n: int = 400) -> str:
    s = yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=110)
    return s if len(s) <= n else s[:n] + "…"

log = logging.getLogger("team.cycle")

PHASES = ("collect", "review", "triage", "execute", "measure", "geo", "voice", "digest")
PRIO = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
SCHEMAS = Path(__file__).parent / "schemas"
# reviewer of a `repo_only` task (owner 15.11): read-only checkout + Read/Glob/Grep; the DISPATCHER runs the tests and puts
# their output into the prompt. The unittest allowlist it replaced never worked: Claude Code did not match patterns with
# `*` in the middle (`python3 -B -m unittest discover -s tests -t .` denied with that exact pattern listed), and six
# retro PRs (#218 #225 #238 #240 #244 #251) only added more patterns. Replan probe T-0146 (T-0143 stuck) proposed this.
REPO_ONLY_REVIEW_TOOLS = ["Read", "Glob", "Grep", "Bash(team-fetch *)", "Bash(team-fetch:*)"]
SECRET_ENV_PREFIXES = ("TELEGRAM_", "BLUESKY_", "DEVTO_", "MASTODON_", "THREADS_", "X_", "GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
                       "ANTHROPIC_API_KEY", "GSC_")
REVIEW_TEST_TIMEOUT_S = 900
REVIEW_TEST_OUTPUT_CHARS = 6000
UNIT_LOG_NAME = "unit-tests.log"           # the whole output of the dispatcher's test run, in the review folder (`team-play unit`)
TESTER_BASH_SLACK_S = 120   # over team-play's lock wait + run limits, for its own start and the teardown


def tester_bash_timeouts() -> dict[str, str]:
    """The tester's Bash tool: the agent CLI moves a command to the background after 2 minutes and stops it after 10
    (24-26.11: 4 team-play runs stopped there, and the agent polled the rest). A `team-play test` may wait for the host
    lock and then run (team/play.py LOCK_WAIT_S + TEST_TIMEOUT_S), so the default and the maximum cover both."""
    from .play import LOCK_WAIT_S, TEST_TIMEOUT_S
    ms = str((LOCK_WAIT_S + TEST_TIMEOUT_S + TESTER_BASH_SLACK_S) * 1000)
    return {"BASH_DEFAULT_TIMEOUT_MS": ms, "BASH_MAX_TIMEOUT_MS": ms}

REPO_ONLY_PROMPT = (
    "\n# READ-ONLY CHECKOUT\nThe working directory is a read-only checkout of commit {sha}. You have Read, Glob, Grep and "
    "team-fetch for the live site; no shell. The dispatcher already ran the repository's tests on this commit in a writable "
    "copy; their command, exit code and output are below. A criterion about tests is judged from that output: exit 0 and the "
    "named test present → pass; a failing or missing test → fail with its name. A criterion about code is judged by reading "
    "the files.\n\n## TEST RUN (by the dispatcher)\n{tests}\n")


def remove_tree(path: Path) -> bool:
    """Delete the directory `path` with everything in it, read-only files and folders included (`gitops.readonly_checkout`
    takes the write bit off). A symlink is never followed and never removed. Returns True when the directory is gone;
    a failure is logged, not raised — housekeeping must not cost a review its verdict."""
    path = Path(path)
    if path.is_symlink() or not path.is_dir():
        return False

    def _writable_then_retry(func: Any, failed: str, _exc: Any) -> None:
        for q in (os.path.dirname(failed), failed):
            try:
                if not os.path.islink(q):
                    os.chmod(q, 0o700)
            except OSError:
                pass
        func(failed)

    try:
        # `onerror` is deprecated since Python 3.12 in favour of `onexc`; the handler ignores its third argument
        shutil.rmtree(path, **{"onexc" if sys.version_info >= (3, 12) else "onerror": _writable_then_retry})
    except OSError as exc:
        log.warning("could not remove %s: %s", path, str(exc)[:200])
    return not path.exists()


def remove_named_dirs(root: Path, name: str = WORKDIR_HEAVY_NAME) -> list[Path]:
    """Delete every directory called `name` under `root` (not followed into, symlinks left alone). Returns what went."""
    gone: list[Path] = []
    if Path(root).is_symlink() or not Path(root).is_dir():
        return gone
    for r, ds, _fs in os.walk(root, followlinks=False):
        if name in ds:
            ds.remove(name)
            if remove_tree(Path(r) / name):
                gone.append(Path(r) / name)
    return gone


def run_tests_for_review(repo_dir: Path, sha: str, dest: Path, test_cmd: str = "", lock: Any = None, full_log: Path | None = None) -> str:
    """Runs the repository's tests on `sha` in a writable clone (tests may write scratch files, #163) with no secrets in the
    environment; returns «command / exit code / output tail» for the reviewer's prompt. Without `repo.test_cmd`:
    `python3 -m unittest discover -s <dir>` for every top-level directory that holds test_*.py files. `lock`: the
    cycle's repository lock, held only while the clone is copied (another lane may be merging into it); the tests
    themselves run outside it."""
    import shutil
    from contextlib import nullcontext
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with lock if lock is not None else nullcontext():
        gitops.git(repo_dir, "clone", "--quiet", "--no-hardlinks", "--no-checkout", str(repo_dir), str(dest))
    gitops.git(dest, "checkout", "--quiet", "--detach", sha)
    gitops.git(dest, "remote", "remove", "origin", check=False)
    if test_cmd:
        cmds = [test_cmd]
    else:
        dirs = sorted({p.relative_to(dest).parts[0] for p in dest.rglob("test_*.py")
                       if ".git" not in p.parts and len(p.relative_to(dest).parts) > 1})
        cmds = [f"python3 -m unittest discover -s {d}" for d in dirs]   # no -t: works without __init__.py
    if not cmds:
        return "(no tests found in the repository: no test_*.py files and no repo.test_cmd)"
    env = {k: v for k, v in os.environ.items() if not k.startswith(SECRET_ENV_PREFIXES)}
    env.update({"TEAM_PUBLISH_DRY_RUN": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    out, full = [], []
    for c in cmds:
        try:
            cp = subprocess.run(c, shell=True, cwd=str(dest), capture_output=True, text=True, timeout=REVIEW_TEST_TIMEOUT_S, env=env)
            text, code = (cp.stdout + "\n" + cp.stderr).strip(), cp.returncode
        except subprocess.TimeoutExpired:
            text, code = f"timed out after {REVIEW_TEST_TIMEOUT_S} s", "timeout"
        full.append(f"$ {c}\nexit code: {code}\n{text}")
        if len(text) > REVIEW_TEST_OUTPUT_CHARS:
            text = "… [head cut]\n" + text[-REVIEW_TEST_OUTPUT_CHARS:]
        out.append(f"$ {c}\nexit code: {code}\n{text}")
    if full_log is not None:                           # rework slice 6: the whole output, for `team-play unit --grep`
        try:
            full_log.parent.mkdir(parents=True, exist_ok=True)
            full_log.write_text("\n\n".join(full), encoding="utf-8")
        except OSError as exc:
            log.warning("full test output not kept in %s: %s", full_log, exc)
    return "\n\n".join(out)


def tests_passed(report: str) -> bool:
    """`run_tests_for_review` output → did every command exit with 0? No command at all (no tests in the repository) counts
    as passed: there is nothing the change could break."""
    codes = re.findall(r"^exit code: (\S+)$", report or "", re.M)
    return all(c == "0" for c in codes)


def schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def only_raw_github_urls(urls: Any) -> bool:
    """True when how_to_verify lists URLs and every one is raw.githubusercontent.com (a repo-only deliverable)."""
    urls = [u for u in (urls or []) if isinstance(u, str) and u.strip()]
    return bool(urls) and all(re.match(r"https://raw\.githubusercontent\.com/", u.strip()) for u in urls)


CANARY_EMPTY_REASON = "canary empty result"
CANARY_RESULT_DIRS = ("digests", "reviews", "observations", "backlog")   # reports and task results


def _canary_snapshot(pdir: Path) -> dict[str, str]:
    """relative path -> content hash of every non-blank file in the result directories."""
    import hashlib
    out: dict[str, str] = {}
    for d in CANARY_RESULT_DIRS:
        for f in sorted((pdir / d).rglob("*")) if (pdir / d).is_dir() else []:
            if f.is_file():
                data = f.read_bytes()
                if data.strip():
                    out[str(f.relative_to(pdir))] = hashlib.sha256(data).hexdigest()
    return out


class Cycle:
    def __init__(self, st: Settings, product: Product, runner: Runner | None, cycle_id: str, *, dry_run: bool = False):
        self.st, self.p, self.runner, self.cycle_id, self.dry_run = st, product, runner, cycle_id, dry_run
        self.store = Store(product.dir)
        self.run_dir = product.runs_dir / cycle_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.phases_done: list[str] = []
        self.n_runs = 0
        # parallel lanes (team/lanes.py): the executor, the repository lock, the live-host lock; what the lanes share in
        # this object is guarded here — the budget (cap reservations, `n_runs`) and the seats of the execute phase
        self.par = product.parallel
        self.lanes = lanes_mod.Lanes(self.par, product.slug, on_lane_exit=self.store.close_thread, on_working=self._task_mark)
        self.repo_lock = self.lanes.repo_lock                 # the product's clone: fetch, worktrees, merge, push, deploy
        self._budget_lock = threading.Lock()
        self._reserved: dict[str, float] = {}                 # run id -> the cap reserved for a run in flight
        self._seat_lock = threading.Lock()
        self._seat_running: dict[str, int] = {}               # seat key -> tasks being executed right now
        self._in_run = False                                  # True inside `run`: there the review lanes outlive their phase
        self._failed_phases: set[str] = set()                 # phases one of whose lanes raised
        self._tl = threading.local()                          # per thread: the run profile of its latest role_run
        # continuous work of the seats (owner 25.11, team/flow.py): refill inside the execute phase, the next cycle back to back
        self.flow = product.flow
        self._refill_deadline: float | None = None            # monotonic; set by a parallel execute phase with `cycle.flow.refill`
        self._refill_last: float | None = None                # monotonic; the latest moment refill may stay open for running reviews
        self._refill_closed: str | None = None                # why refill ended early in this cycle (logged once)
        self.canary_report: Path | None = None                # the report of this cycle's canary run (team/canary.py)
        self.canary_started: str | None = None                # ISO time the release that ran it began
        self._role_median: dict[str, float | None] = {}       # role → median spend of a run (`cost_guard_cap`), read once per cycle
        self._taken: set[str] = set()                         # tasks handed to a seat lane in this pass of the execute phase (guarded by `_seat_lock`)
        self._not_done: set[str] = set()                      # tasks this cycle looked at and did not carry through: not «waiting work» for a chain
        self._batch_timer: threading.Timer | None = None      # `deploy.batch`: fires the batch when a merged commit has waited too long
        self.n_executed = 0                                   # tasks an execute lane carried through in this cycle
        self.n_refilled = 0                                   # of them taken beyond the seat's places, by refill

    @property
    def last_profile(self) -> dict[str, Any] | None:
        """Run profile of the calling thread's latest role_run (team/runprofile.py); lanes do not see each other's."""
        return getattr(self._tl, "profile", None)

    @last_profile.setter
    def last_profile(self, prof: dict[str, Any] | None) -> None:
        self._tl.profile = prof

    # ------------------------------------------------------------ guards

    def _task_mark(self, task_id: str, lane: str | None) -> None:
        """A lane took the task (label) or let go of it (None): kept in the store, where `team task move` of another
        process looks before it changes a task (team/lanes.py invariant 2, seen from outside the cycle)."""
        if lane is None:
            self.store.unmark_working(task_id)
        else:
            self.store.mark_working(task_id, lane)

    def stopped(self) -> str | None:
        from . import cadence
        return cadence.stop_reason(self.st, self.p)      # STOP files, `paused`, onboarding not finished — shared with `team cycle --due`

    def budget_ok(self, cap_run: float, reserved: float = 0.0) -> bool:
        """`reserved`: the caps of the runs in flight in the other lanes of this cycle — money that may still be spent."""
        spent, cap = self.store.spent_today(), self.p.daily_cap
        if spent + reserved + cap_run > cap + 1e-9:
            self.store.event("budget.stop", self.p.slug, "dispatcher", {"spent": round(spent, 3), "cap": cap, "run_cap": cap_run,
                                                                        **({"reserved": round(reserved, 3)} if reserved else {})})
            log.warning("%s: budget stop — spent $%.2f + reserved $%.2f + $%.2f > cap $%.2f", self.p.slug, spent, reserved, cap_run, cap)
            return False
        if spent > cap * float(self.st.budget.get("warn_share", 0.8)):
            log.warning("%s: spend $%.2f above %.0f%% of cap $%.2f", self.p.slug, spent, 100 * float(self.st.budget.get("warn_share", 0.8)), cap)
        return True

    # ------------------------------------------------------------ one role run

    def role_run(self, role: str, prompt: str, schema_name: str | None, *, cwd: Path, label: str, cap: float | None = None,
                 allowed_tools: list[str] | None = None, env_extra: dict[str, str] | None = None,
                 max_turns: int | None = None, model: str | None = None, schema_obj: dict[str, Any] | None = None) -> RunResult | None:
        self._tl.run_ok = False                                     # until a run really finished well (read by `seat_lane` for refill)
        if self.runner is None:
            log.error("no runner: %s skipped", label)
            return None
        from . import billing
        stop = billing.paused(self.st)                              # owner 04.11: shared $100/day cap, plan + credits state
        if stop:
            self.store.event("budget.stop", self.p.slug, "dispatcher", {"reason": stop, "label": label})
            log.warning("%s: %s skipped: %s", self.p.slug, label, stop)
            return None
        run_id = f"{self.cycle_id}-{label}"
        with self._budget_lock:
            # Reserve before the run starts (team/lanes.py invariant 7): the caps of the runs in flight count as spent, so
            # N lanes starting together cannot each see the same «remaining» and overshoot the daily cap between them.
            reserved = sum(self._reserved.values())
            cap = float(cap or self.st.budget["max_usd_per_run"])
            own = self.p.run_cap(role)                                  # owner 23.11: the cap of one run follows the role's model
            if own is not None:
                log.info("%s: run %s: cap of one %s run $%.2f (%s) instead of $%.2f", self.p.slug, run_id, role, own[0], own[1], cap)
                cap = own[0]
            credit_left = billing.remaining_today(self.st)             # None inside the subscription
            remaining = self.p.daily_cap - self.store.spent_today() - reserved
            if credit_left is not None:
                remaining = min(remaining, credit_left - reserved)
            cap = min(cap, remaining)                                   # a run may use what is left of the day
            guards = self.p.guards_cfg                                  # rework slice 4: off by default
            guard_cap = self.cost_guard_cap(role, guards) if guards["cost"] else None
            guarded = guard_cap is not None and guard_cap < cap
            if guarded:
                log.info("%s: run %s: cost guard caps the %s run at $%.2f instead of $%.2f", self.p.slug, run_id, role, guard_cap, cap)
                cap = guard_cap
            if cap < float(self.st.budget.get("min_usd_per_run", 0.3)) or not self.budget_ok(cap, reserved):
                if cap < float(self.st.budget.get("min_usd_per_run", 0.3)):
                    self.store.event("budget.stop", self.p.slug, "dispatcher", {"remaining": round(remaining, 3), "cap": self.p.daily_cap,
                                                                                **({"reserved": round(reserved, 3)} if reserved else {})})
                return None
            if self.dry_run:
                log.info("[dry-run] would run %s (%s): prompt %d chars", role, label, len(prompt))
                return None
            self.n_runs += 1
            self._reserved[run_id] = cap
            if reserved:
                log.info("%s: run %s reserves $%.2f; $%.2f more is reserved by %d run(s) in flight", self.p.slug, run_id, cap, reserved,
                         len(self._reserved) - 1)
        rr: RunResult | None = None
        cost = 0.0
        try:
            rd = self.run_dir / label
            rd.mkdir(parents=True, exist_ok=True)
            (rd / "prompt.md").write_text(prompt, encoding="utf-8")
            env = {"TEAM_USER_AGENT": f"agent-team/{run_id} ({role}; {self.p.slug})", "TEAM_ALLOWED_HOSTS": self._allowed_hosts(role),
                   "TEAM_TRACE_FILE": str(rd / "fetch.jsonl"), "TEAM_PRODUCT": self.p.slug, "TEAM_RUN_ID": run_id,
                   "PATH": f"{self._bin_dir()}{os.pathsep}{os.environ.get('PATH', '')}", **(env_extra or {})}
            model = model or (self.p.raw.get("models") or {}).get(role)   # explicit override (series editing), else product.yaml per role
            limits = self.p.run_limits(role)                              # owner 21.11: the time budget is per role and per product
            with self.lanes.run_slot(label):                              # a lane holds its slot already; the main thread takes one here
                log.info("%s: run %s time budget: timeout %ds (%s), inactivity %ds (%s)", self.p.slug, run_id, limits["timeout_s"],
                         limits["source"]["timeout_s"], limits["inactivity_s"], limits["source"]["inactivity_s"])
                rr = self.runner.run(role, prompt, schema_obj or (schema(schema_name) if schema_name else None), max_usd=cap, cwd=cwd, model=str(model) if model else None,
                                     allowed_tools=allowed_tools, env_extra=env, run_dir=rd, run_id=run_id, max_turns=max_turns,
                                     timeout_s=int(limits["timeout_s"]), inactivity_s=int(limits["inactivity_s"]),
                                     **({"loop_repeats": int(guards["loop_repeats"])} if guards["loop"] else {}))
        finally:
            with self._budget_lock:                                       # the spend is booked and the reservation dropped in one step
                if rr is not None:
                    cost = rr.cost_usd if rr.cost_usd is not None else float(self.st.budget["unknown_cost_reserve_usd"])
                    self.store.book_spend(run_id, role, cost, rr.cost_usd is not None)
                self._reserved.pop(run_id, None)
        for kind in billing.observe(self.st, rr.limit_info, run_id, notify=billing.owner_notify(), cost=cost):
            self.store.event(f"billing.{kind}", self.p.slug, "dispatcher", {"run": run_id, "line": billing.status_line(self.st)})
        rec = {"run_id": run_id, "role": role, "ok": rr.ok, "error": rr.error, "cost_usd": cost, "cost_reported": rr.cost_usd is not None,
               "num_turns": rr.num_turns, "duration_s": round(rr.duration_s, 1), "session_id": rr.session_id,
               "denials": rr.denials, "structured": rr.structured, "finished": utcnow(),
               "limits": limits, "killed": killed_kind(rr), "profile": self.profile_run(rr, role, run_id, limits),
               "lane": lanes_mod.current_lane() or "main"}
        (rd / "run.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        self.store.event("run", run_id, role, {"ok": rr.ok, "cost": round(cost, 3), "turns": rr.num_turns, "error": (rr.error or "")[:200]})
        if killed_kind(rr) == KILLED_LOOP:                                # both go to the friction register (team/friction.py)
            self.store.event(RUN_LOOP_EVENT, run_id, role, {"what": str(rr.error or "")[:300], "duration_s": round(rr.duration_s, 1)})
        elif guarded and not rr.ok and over_budget(rr):
            self.store.event(RUN_COST_EVENT, run_id, role, {"cap": round(cap, 3), "median": self._role_median.get(role),
                                                            "factor": guards["cost_factor"], "cost": round(cost, 3)})
            log.warning("%s: run %s stopped by the cost guard at $%.2f (%s × the role's median $%s)", self.p.slug, run_id, cap,
                        guards["cost_factor"], self._role_median.get(role))
        self._tl.run_ok = bool(rr.ok)
        return rr

    def cost_guard_cap(self, role: str, guards: dict[str, Any] | None = None) -> float | None:
        """`cycle.guards.cost`: the most one run of `role` may spend — `cost_factor` × the median of the role's runs of
        the last `cost_days` days (the `spend` table), never below `cost_floor_usd`. None while the role has fewer than
        `cost_min_runs` runs there: no median, no guard. The median is read once per cycle and role."""
        g = guards or self.p.guards_cfg
        if role not in self._role_median:
            since = (_now() - timedelta(days=int(g["cost_days"]))).isoformat(timespec="seconds")
            rows = [float(r[0]) for r in self.store.db.execute("SELECT usd FROM spend WHERE role = ? AND ts >= ? AND reported = 1 AND usd > 0",
                                                                 (role, since)).fetchall()]
            self._role_median[role] = round(statistics.median(rows), 4) if len(rows) >= int(g["cost_min_runs"]) else None
        med = self._role_median[role]
        return None if med is None else max(float(g["cost_floor_usd"]), float(g["cost_factor"]) * med)

    def profile_run(self, rr: RunResult, role: str, run_id: str, limits: dict[str, Any]) -> dict[str, Any] | None:
        """Where the run's time went (team/runprofile.py): computed from the events of every run and kept in run.json; a
        run longer than `run_profile.event_min_s` or stopped by a limit also gets the compact `run.profile` event the PO
        prompt and the report read. No model call; a failure here never fails the run."""
        self.last_profile = None
        cfg = runprofile.settings_for(self.p)
        if not cfg.get("enabled", True):
            return None
        try:
            prof = runprofile.build(list(getattr(rr, "events", None) or []), wall_s=float(rr.duration_s or 0.0),
                                    started=float(getattr(rr, "started", 0.0) or 0.0) or None, killed=killed_kind(rr),
                                    limit_s=int(limits["timeout_s"]), cfg=cfg)
            if runprofile.worth_event(prof, cfg):
                self.store.event(runprofile.RUN_EVENT, run_id, role, runprofile.event_detail(prof, role=role, run_id=run_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: run profile of %s failed: %s", self.p.slug, run_id, exc)
            return None
        self.last_profile = prof
        return prof

    def _allowed_hosts(self, role: str | None = None) -> str:
        """Hosts team-fetch may open for a run: the product site, product extra_hosts, the publishing channels, and the
        per-role list from team.yaml `fetch.extra_hosts_by_role` (03.11: the reviewer reads repo-only deliverables on
        raw.githubusercontent.com; without this every such review was denied_permission and stayed inconclusive)."""
        hosts = [self.p.host] if self.p.host else []
        hosts += [str(h) for h in self.p.raw.get("extra_hosts") or []]
        hosts += ["bsky.app", "t.me", "dev.to"]
        by_role = (self.st.raw.get("fetch") or {}).get("extra_hosts_by_role") or {}
        if role:
            hosts += [str(h) for h in by_role.get(role) or []]
        return ",".join(dict.fromkeys(h for h in hosts if h))

    def _bin_dir(self) -> Path:
        return self.st.root / "bin"

    # ------------------------------------------------------------ phases

    def run(self, phases: list[str] | None = None) -> dict[str, Any]:
        self.store.cycle_start(self.cycle_id)
        self.store.clear_working()                # the caller holds the product lock: a mark found now is of a cycle that died
        reason = self.stopped()
        if reason:
            log.warning("%s: stopped (%s)", self.p.slug, reason)
            self.store.event("cycle.stopped", self.cycle_id, "dispatcher", reason)
            return {"stopped": reason}
        t0 = time.time()
        spent0 = self.store.spent_today()
        log.debug("%s cycle %s: lanes %s", self.p.slug, self.cycle_id, self.par.describe())      # `team status` shows the same line
        self._in_run = True
        try:
            for ph in phases or PHASES:
                try:
                    getattr(self, f"phase_{ph}")()
                    self.phases_done.append(ph)
                except Exception as exc:  # noqa: BLE001 — one phase failing must not kill the cycle
                    log.exception("%s: phase %s failed: %s", self.p.slug, ph, exc)
                    self.store.event("phase.error", ph, "dispatcher", str(exc)[:500])
                if self.stopped():
                    break
        finally:
            # lanes still in flight (the reviews, when the execute phase did not run or the cycle was stopped): their runs
            # finish and their results are applied before the cycle is summed up — nothing is left half-done behind it
            self.finish_lanes()
            self._in_run = False
        self.phases_done = [ph for ph in self.phases_done if ph not in self._failed_phases]      # a phase whose lane raised did not finish
        spent = self.store.spent_today() - spent0
        self.store.cycle_finish(self.cycle_id, self.phases_done, round(spent, 4))
        peak = self.lanes.gate.peak
        log.info("%s cycle %s: phases %s, runs %d, spent $%.2f, %.0fs%s%s", self.p.slug, self.cycle_id, self.phases_done, self.n_runs, spent,
                 time.time() - t0, f", {self.lanes.started} lane(s), up to {peak} at once" if self.lanes.started else "",
                 f", {self.n_executed} task(s) executed, {self.n_refilled} of them by refill" if self.n_refilled else "")
        return {"phases": self.phases_done, "runs": self.n_runs, "spent_usd": round(spent, 4)}

    # ------------------------------------------------------------ continuous work of the seats (team/flow.py)

    def refill_open(self) -> bool:
        """May a seat that finished its task take one more in this execute phase? Only in a parallel cycle with
        `cycle.flow.refill`, while the phase is younger than `refill_min`, nothing stopped the product, the last run of
        every refilling lane went well, and extra work is allowed (weekly limit, credits: `flow.allowed`). The reason it
        closed is logged once and kept as the `flow.refill_closed` event."""
        if self._refill_deadline is None or self._refill_closed:
            return False
        why = None
        now = time.monotonic()
        if now >= (self._refill_last or self._refill_deadline):
            why = f"the execute phase is older than cycle.flow.refill_max_min ({self.flow.refill_max_min:g} min)"
        elif now >= self._refill_deadline and not self.lanes.gate.in_flight(LANE_REVIEW):
            # past refill_min the window stays open only while reviews of this cycle hold the cycle anyway
            why = f"the execute phase is older than cycle.flow.refill_min ({self.flow.refill_min:g} min) and no review is running"
        else:
            why = self.stopped() or flow_mod.allowed(self.st, self.flow)
        if why:
            self.close_refill(why)
            return False
        return True

    def close_refill(self, why: str) -> None:
        if self._refill_deadline is None or self._refill_closed:
            return
        self._refill_closed = why
        log.info("%s: refill closed for this cycle: %s (%d task(s) were taken by refill)", self.p.slug, why, self.n_refilled)
        self.store.event("flow.refill_closed", self.cycle_id, "dispatcher", {"why": why[:300], "refilled": self.n_refilled})

    def next_for_seat(self, key: str, slots: dict[str, int], seen: set[str]) -> dict[str, Any] | None:
        """Refill: the next task of seat `key`, read from the store now — what a review of this cycle sent back and what
        another lane unblocked included — in the order of `execute_candidates`. Never a task a lane of this cycle has
        taken or is working on. Called under `_seat_lock`."""
        for t in self.execute_candidates():
            if t["id"] in seen or t["id"] in self._taken or self.lanes.busy(t["id"]):
                continue
            if self.seat_key(str(t.get("role") or "developer"), slots) == key:
                return t
        return None

    def work_waiting(self) -> list[str]:
        """Tasks an execute phase starting now could take: ready or in progress, the role has a seat, not scheduled for
        later, no open question, no unfinished dependency — and not one this cycle already looked at and did not carry
        through (it would be refused or fail again). Read-only: the checks that write (`startable`) are not called."""
        slots = self.execute_slots()
        out: list[str] = []
        for t in self.execute_candidates():
            if t["id"] in self._not_done or self.seat_key(str(t.get("role") or "developer"), slots) is None:
                continue
            if str(t.get("role") or "developer") in PENDING_ROLES:
                continue
            nb = parse_ts(t.get("not_before"))
            if nb and _now() < nb:
                continue
            if t.get("state") == "ready" and (self.open_question_blocks(t) or unmet_dependencies(self.store, t)):
                continue
            out.append(t["id"])
        return out

    def chain_reason(self, res: dict[str, Any]) -> tuple[bool, str]:
        """Should `team cycle --due` start this product's next cycle right away (`cycle.flow.chain`)? (yes/no, why) —
        yes only after a cycle that carried a task through, while startable work waits and extra work is allowed."""
        if not self.flow.chain:
            return False, "cycle.flow.chain is off"
        if self.dry_run:
            return False, "dry run"
        if res.get("stopped") or self.stopped():
            return False, f"stopped ({res.get('stopped') or self.stopped()})"
        if "execute" not in (res.get("phases") or []):
            return False, "the execute phase did not finish"
        if not self.n_executed:
            return False, "this cycle carried no task through"
        why = flow_mod.allowed(self.st, self.flow)
        if why:
            return False, why
        waiting = self.work_waiting()
        if not waiting:
            return False, "no startable task is waiting"
        return True, f"{len(waiting)} startable task(s) waiting ({', '.join(waiting[:5])}{'…' if len(waiting) > 5 else ''})"

    def finish_lanes(self) -> list[lanes_mod.LaneError]:
        """Wait for every lane in flight. A lane that raised is recorded like a phase that raised: `phase.error`, and
        `run` takes its phase out of the cycle's list of finished phases — the summary line and `team status` say what
        really happened. Returns the failures (empty in a sequential cycle: there an exception ends the phase itself).
        A phase method called on its own, outside `run`, raises the first failure instead: it is complete, or it failed,
        when it returns — like any other phase."""
        errors = self.lanes.join()
        for e in errors:
            log.error("%s: phase %s failed in lane %s: %s", self.p.slug, e.phase, e.label, e.error,
                      exc_info=(type(e.error), e.error, e.error.__traceback__))
            self.store.event("phase.error", e.phase, "dispatcher", f"lane {e.label}: {e.error}"[:500])
            self._failed_phases.add(e.phase)
        if errors and not self._in_run:
            raise errors[0].error
        return errors

    # collect: stale claims, metrics snapshot, personas ------------------------------------------------

    def phase_collect(self) -> None:
        for c in self.store.stale_claims():
            t = self.store.task(c["task_id"])
            if t and t.get("state") in ("claimed", "in_progress"):
                self.store.transition(t["id"], "ready", "dispatcher", f"lease of {c['run_id']} expired", force=True)
        self.fetch_metrics()
        self.collect_feedback()
        self.collect_critic()
        self.support_replies()
        self.seo_check()
        if self.personas_due():
            self.run_personas()

    def personas_due(self) -> bool:
        """Personas run by the clock: every `cycle.persona_every_h` hours per product (default 12), the last run is
        in products/<slug>/personas-last.json (team/ux.py). A game product starts them at the stage VISION names
        (gate `persona`)."""
        from . import ux
        every = ux.persona_every_h(self.st.cycle)
        if every <= 0 or not self.p.gate_open("persona"):
            log.debug("%s: personas not scheduled (every %s h, gate open: %s)", self.p.slug, every, self.p.gate_open("persona"))
            return False
        due = ux.personas_due(self.p.dir, every, _now(), ux.persona_retry_h(self.st.cycle))
        mark = ux.persona_mark(self.p.dir)
        log.log(logging.INFO if due else logging.DEBUG, "%s: personas %s (every %d h; last run started %s, results %s)", self.p.slug,
                "due" if due else "not due", every, mark.get("started") or "never", mark.get("ok", "—"))
        return due

    def collect_feedback(self) -> int:
        """External feedback -> observation cards: GitHub issues of the site repository, replies and mentions on Bluesky.
        Real people's words weigh above anything synthetic; each item becomes one card, deduped by its id."""
        if self.dry_run:
            return 0
        from . import feedback
        n = 0
        try:
            for card in feedback.collect(self.p):
                self.store.add_observation(card)
                n += 1
        except Exception as exc:  # noqa: BLE001 - a feedback source failing must not stop the cycle
            log.warning("%s: feedback collection failed: %s", self.p.slug, exc)
            self.store.event("feedback.error", self.p.slug, "dispatcher", str(exc)[:300])
        if n:
            self.store.event("feedback.collected", self.p.slug, "dispatcher", {"items": n})
        return n

    def collect_critic(self) -> int:
        """Reports of the external critic (owner 03.11) → observation cards for this product, like another reviewer."""
        from . import critic
        from .config import load_products
        try:
            n = critic.ingest(self.st, self.p, self.store, load_products(self.st))
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: critic ingest failed: %s", self.p.slug, exc)
            self.store.event("critic.error", self.p.slug, "dispatcher", str(exc)[:300])
            return 0
        return n

    def support_replies(self) -> int:
        """People who wrote to the product get an answer within VISION (role support); outside VISION → question to the PO."""
        if self.dry_run or self.runner is None:
            return 0
        from . import support
        cfg = dict(self.st.raw.get("support") or {})
        if not cfg.get("enabled", True):
            return 0
        cap_day = int(cfg.get("max_replies_per_day", support.DEFAULT_MAX_PER_DAY))
        todo = support.pending(self.p, self.store)[: int(cfg.get("max_per_cycle", support.DEFAULT_MAX_PER_CYCLE))]
        n = 0
        for o in todo:
            if support.replies_today(self.store) >= cap_day:
                log.info("%s: support daily cap %d reached", self.p.slug, cap_day)
                break
            try:
                channel, how = support.channel_for(o)
            except ValueError as exc:
                self.store.event("support.skip", str(o.get("fingerprint")), "dispatcher", str(exc))
                continue
            work = self.p.work_dir / self.cycle_id / f"support-{o['id']}"
            work.mkdir(parents=True, exist_ok=True)
            rr = self.role_run("support", support.support_prompt(self.p, o, self.p.vision()), "support-reply", cwd=work,
                               label=f"support-{o['id']}", cap=0.5, max_turns=12)
            if rr is None or not (rr.ok and rr.structured):
                continue
            res = rr.structured
            if res.get("action") == "escalate" and res.get("question"):
                self.store.ask(from_="support", to="po", question=f"{res['question']} (from {o.get('author')}: {o.get('url')})")
            if res.get("action") == "skip" or not (res.get("text") or "").strip():
                self.store.event("support.skip", str(o.get("fingerprint")), "support", str(res.get("reason"))[:200])
                continue
            disc = self.st.publish.get("disclosure", "")
            disc_s = (disc.get(channel) or disc.get("default")) if isinstance(disc, dict) else str(disc)
            r = publish.post(channel, str(res["text"]), disclosure=str(disc_s or ""), dry_run=bool(self.st.publish.get("dry_run")), **how)
            if r.ok:
                self.store.event("support.reply", str(o.get("fingerprint")), "support", {"channel": channel, "url": r.url, "action": res.get("action")})
                self.store.record_post(channel + publish.REPLY_SUFFIX, str(r.post_id or ""), str(r.url or ""), None)   # T-0071: not a post
                n += 1
            else:
                self.store.event("support.error", str(o.get("fingerprint")), "support", str(r.error)[:300])
        return n

    def seo_check(self) -> None:
        """Programmatic SEO/GEO page checks every `seo_every_h` hours: defects become observation cards, the full table
        goes to metrics/seo-<date>.json for the analyst."""
        if self.dry_run or not self.p.site_base or not self.p.gate_open("seo"):
            return
        from . import seocheck
        every = int(self.st.cycle.get("seo_every_h", 12))
        last = sorted((self.p.dir / "metrics").glob("seo-*.json"))
        if last:
            prev = parse_ts(json.loads(last[-1].read_text(encoding="utf-8")).get("generated") or "")
            if prev and _now() - prev < timedelta(hours=every):
                return
        langs = [str(x) for x in (self.p.raw.get("langs") or [])] or None
        try:
            rep = seocheck.report(self.p.site_base, seocheck.default_fetch, max_pages=8, langs=langs)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: seocheck failed: %s", self.p.slug, exc)
            return
        rep["generated"] = utcnow()
        (self.p.dir / "metrics" / f"seo-{_now().strftime('%Y-%m-%d')}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        for card in rep.get("cards") or []:
            self.store.add_observation({**card, "run_id": f"{self.cycle_id}-seo"})
        self.store.event("seo.checked", self.p.slug, "dispatcher", {"passed": rep.get("passed"), "failed": rep.get("failed")})

    def fetch_metrics(self) -> Path | None:
        if not self.p.site_base or self.dry_run:
            return None
        url = self.p.site_base.rstrip("/") + "/_team/metrics.json"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"agent-team/{self.cycle_id} (dispatcher; {self.p.slug})"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.info("%s: metrics not available at %s: %s", self.p.slug, url, exc)
            return None
        day = _now().strftime("%Y-%m-%d")
        out = self.p.dir / "metrics" / f"{day}.json"
        txt = json.dumps(data, ensure_ascii=False, indent=1)
        if not out.exists() or out.read_text(encoding="utf-8") != txt:
            out.write_text(txt, encoding="utf-8")
            md = self.p.dir / "metrics" / "latest.md"
            md.write_text(metrics_md(data, balance.settings(self.p.raw)), encoding="utf-8")
        return out

    def run_personas(self) -> None:
        """Personas judge the reader's experience (owner 04.11). A run takes `persona_scenarios_per_run` scenarios in
        rotation (not all of them every time), records the experience scores (team/ux.py) and files only what should
        change: successes live in the scores, not in the PO's triage."""
        from . import ux
        sdir, pdir = self.p.dir / "scenarios", self.p.dir / "personas"
        if not sdir.exists():
            return
        all_sc = sorted(sdir.glob("*.md"))
        if not all_sc:
            return
        per_run = max(1, int(self.st.cycle.get("persona_scenarios_per_run", 2)))
        prev = ux.persona_mark(self.p.dir)
        try:
            start = int(prev.get("next") or 0) % len(all_sc)             # the rotation continues where the last run stopped
        except (TypeError, ValueError):
            start = 0
        picked = [all_sc[(start + i) % len(all_sc)] for i in range(min(per_run, len(all_sc)))]
        mark = {"started": utcnow(), "cycle": self.cycle_id, "scenarios": [sp.stem for sp in picked],
                "next": (start + len(picked)) % len(all_sc), "finished": None, "ok": 0, "last_ok": prev.get("last_ok")}
        if not self.dry_run:
            ux.write_persona_mark(self.p.dir, mark)                      # before the runs: a cycle killed mid-run is not retried every cycle
        log.info("%s: persona run: scenarios %s (%d of %d, from index %d)%s", self.p.slug, ", ".join(mark["scenarios"]), len(picked),
                 len(all_sc), start, " [dry-run: no mark written]" if self.dry_run else "")
        try:
            mark["ok"] = self._run_persona_scenarios(picked, sdir, pdir)
        finally:
            if not self.dry_run:
                mark["finished"] = utcnow()
                if mark["ok"]:
                    mark["last_ok"] = mark["started"]
                ux.write_persona_mark(self.p.dir, mark)
                self.store.event("persona.run", self.p.slug, "dispatcher", {"scenarios": mark["scenarios"], "ok": mark["ok"]})

    def _run_persona_scenarios(self, picked: list[Path], sdir: Path, pdir: Path) -> int:
        """One persona run per picked scenario; returns how many returned a result."""
        from . import ux
        ok = 0
        for sp in picked:
            smeta, sbody = front_matter(sp.read_text(encoding="utf-8"))
            pp = pdir / f"{smeta.get('persona', '')}.md"
            if not smeta.get("id") or not pp.exists():
                continue
            pmeta, pbody = front_matter(pp.read_text(encoding="utf-8"))
            shown = {k: v for k, v in smeta.items() if k not in ("success", "expected_gaps", "title")}
            prompt = prompts.persona_prompt(pmeta, pbody.strip(), shown, sbody.strip())
            work = self.p.work_dir / self.cycle_id / f"persona-{smeta['id']}"
            run_id = f"{self.cycle_id}-persona-{smeta['id']}"
            rr = self.role_run("persona", prompt, "persona-result", cwd=work, label=f"persona-{smeta['id']}",
                               cap=float(self.st.cycle.get("persona_cap_usd", 1.0)), max_turns=25)
            if not (rr and rr.ok and rr.structured):
                if not self.dry_run:
                    log.warning("%s: persona scenario %s brought no result (%s)", self.p.slug, smeta["id"],
                                "not started: budget or cap" if rr is None else (rr.error or "empty result"))
                continue
            ok += 1
            res = rr.structured
            persona = str(smeta.get("persona"))
            exp = res.get("experience") or {}
            if exp:
                ux.record(self.p, persona, smeta["id"], run_id, exp, str(res.get("outcome", "")))
            filed = False
            for o in res.get("observations") or []:
                if o.get("classification") == "success":
                    continue
                filed = filed or o.get("classification") in ("value_gap", "ux_friction")
                self.store.add_observation({**o, "source": f"persona:{persona}", "scenario": smeta["id"], "run_id": run_id,
                                            "fingerprint": fingerprint(smeta["id"], o.get("classification", ""), o.get("url", ""), o.get("title", "")),
                                            "scenario_success": str(smeta.get("success", ""))[:400],
                                            "experience": {k: exp.get(k) for k in ("interest", "usefulness", "would_return")}})
            low = (exp.get("interest") or 5) <= ux.LOW_SCORE or (exp.get("usefulness") or 5) <= ux.LOW_SCORE
            if low and not filed:
                self.store.add_observation({
                    "title": f"{persona}: interest {exp.get('interest')}/5, usefulness {exp.get('usefulness')}/5 — {exp.get('why', '')}"[:140],
                    "classification": "value_gap", "source": f"persona:{persona}", "scenario": smeta["id"], "run_id": run_id,
                    "url": self.p.site_base, "expected": str(exp.get("wish") or "")[:300], "actual": str(exp.get("first_impression") or "")[:300],
                    "fingerprint": fingerprint(smeta["id"], "value_gap", "", persona)})
        return ok

    # review: released tasks → reviewer --------------------------------------------------------------

    def phase_review(self) -> None:
        if self.p.repo_mode == "pr":
            self.check_pull_requests()
        after = timedelta(minutes=int(self.st.cycle.get("review_after_min", 6)))
        # «review» = the developer answered «no change needed» (T-0041); it waited here for a reviewer that only read
        # «released», and 14 tasks sat unreviewed 06.11 → 18.11 (owner 18.11)
        try:
            self.deploy_batch("merged commits left by the previous cycle")      # a cycle that died or was stopped before its batch
        except Exception as exc:  # noqa: BLE001 — the reviews of what is live must not wait for a host that is down
            log.exception("%s: batch deploy at the start of the review phase failed: %s", self.p.slug, exc)
        for t in self.store.tasks(["released", "review"]):
            if t.get(DEPLOY_PENDING):
                # `deploy.batch`: merged, not live yet — the tester plays the live host, so the review waits for the batch
                log.info("%s: %s is not reviewed now: its commit waits for the batch deploy", self.p.slug, t["id"])
                continue
            rel = parse_ts(t.get("released_at"))
            if rel and _now() - rel < after:
                continue
            if self.lanes.busy(t["id"]):
                # its merge or deploy is in flight in another lane of this cycle: never reviewed before that is over
                log.info("%s: %s is not reviewed now: another lane of this cycle is still working on it", self.p.slug, t["id"])
                continue
            # Parallel cycle: one lane per task — the reviews overlap each other, the PO's triage and the execute phase
            # (which joins them). Sequential cycle: the review runs here and now, one after another, as before.
            self.lanes.submit("review", LANE_REVIEW, f"review-{t['id']}", self.review_task, t, task_id=t["id"])
        if not self._in_run:
            self.finish_lanes()                   # called on its own: the reviews are done when the phase returns

    def review_task(self, t: dict[str, Any]) -> None:
        result = (t.get("results") or [None])[-1]
        prompt = prompts.reviewer_prompt(self.p, t, result, t.get("released_sha"))
        work = self.p.work_dir / self.cycle_id / f"review-{t['id']}"
        tools = env = None
        sha = t.get("released_sha")
        tests_out = ""
        if not t.get("repo_only") and (only_raw_github_urls((result or {}).get("how_to_verify")) or not self.p.site_base):
            with self.store.lock:                    # the PO may be changing this task beside the review: read it again, then write
                t = self.store.task(t["id"]) or t
                t["repo_only"] = True                # T-0017: nothing on the live site to check → read-only checkout (also: no site at all yet, game profile before deploy)
                self.store.save_task(t)
        pre_merge = bool(t.get(PRE_MERGE))            # rework slice 5: the commit is on the task branch only — nothing of it is live
        if (t.get("repo_only") or (pre_merge and not self.playtest_due(t))) and sha:   # read-only checkout of the commit (T-0011)
            try:
                try:
                    tests = run_tests_for_review(self.p.repo_dir, sha, work / "tests-run", self.p.test_cmd, lock=self.repo_lock)
                except Exception as e:  # noqa: BLE001 — the review still runs on the checkout, with the reason
                    tests = f"(the dispatcher could not run the tests: {str(e)[:300]})"
                tests_out = tests
                with self.repo_lock:                 # the clone is copied while no lane is merging into it
                    work = gitops.readonly_checkout(self.p.repo_dir, sha, work / "checkout")
                tools = list(REPO_ONLY_REVIEW_TOOLS)
                prompt += REPO_ONLY_PROMPT.format(sha=sha, tests=tests)
            except Exception as e:               # no checkout → the old URL-only review
                log.warning("%s: read-only checkout failed: %s", t["id"], e)
                work = self.p.work_dir / self.cycle_id / f"review-{t['id']}"
        role, schema_name, cap, turns = "reviewer", "review-verdict", 1.0, 30
        server = None
        if self.playtest_due(t) and sha:
            server, prompt, tools, env = self.start_playtest(t, result, sha, prompt, tests_out, force_local=pre_merge)
            if server is not None:
                role, schema_name, cap, turns = "tester", "test-report", TESTER_CAP_USD, TESTER_MAX_TURNS
        if pre_merge:
            prompt += PRE_MERGE_PROMPT.format(sha=sha)
        try:
            # a play-test against the DEPLOYED host shares the host with the other play-tests and excludes a deploy of this
            # product for the length of the tester's run (team/lanes.py invariant 6); a local server needs nothing
            with self.lanes.host.shared(server is not None and not server.local):
                rr = self.role_run(role, prompt, schema_name, cwd=work, label=f"review-{t['id']}", cap=cap, max_turns=turns,
                                   allowed_tools=tools, env_extra=env)
        finally:
            if server is not None:
                playtest.stop(server)
            self.drop_review_dirs(t["id"])           # the verdict needs none of it: the run is over, the evidence lies elsewhere
        if rr is None:
            return
        with self.store.lock:                        # applied as the run finishes, in one piece (team/lanes.py invariant 4)
            now = self.store.task(t["id"]) or t
            reviewed = self.apply_review(now, rr)
        if reviewed:
            # A pass of a task reviewed BEFORE its merge (rework slice 5) merges it — outside the store lock: the merge takes
            # the repository lock first and the store lock inside it, the order every execute lane uses. Taken the other way
            # round here, a review that passes while a developer's lane is merging would wait for it for ever, and it for us.
            self.merge_task(now, self.p.worktree_dir(now["id"]), str(now.get("role") or "developer"), f"{self.cycle_id}-review-{now['id']}",
                            reviewed=reviewed)

    def drop_review_dirs(self, task_id: str, labels: tuple[str, ...] = ("review", "playtest")) -> list[Path]:
        """Free what one review (or load test) put on the disk, as soon as its run is over: the clones the tests ran in
        (`tests-run`, with their node_modules), the reviewer's read-only checkout, and every node_modules of the
        play-test checkout. What stays: `server.log`, the play-test checkout itself (the files the server wrote; it goes
        with `sweep_work_dirs` later) and everything the run wrote beside them. The tester's screenshots and reports are
        not here at all — they are in products/<slug>/reviews/<task>. Never raises. Returns the directories removed."""
        gone: list[Path] = []
        try:
            for label in labels:
                work = self.p.work_dir / self.cycle_id / f"{label}-{task_id}"
                if work.is_symlink() or not work.is_dir():
                    continue
                for sub in ("tests-run",) + (("checkout",) if label == "review" else ()):
                    if remove_tree(work / sub):
                        gone.append(work / sub)
                gone += remove_named_dirs(work)
            if gone:
                log.info("%s: %s: %d working director%s of the finished review removed: %s", self.p.slug, task_id, len(gone),
                         "y" if len(gone) == 1 else "ies", ", ".join(str(g.relative_to(self.p.work_dir / self.cycle_id)) for g in gone))
        except Exception as exc:  # noqa: BLE001 — housekeeping must not cost the review its verdict
            log.warning("%s: %s: review working directories not removed: %s", self.p.slug, task_id, str(exc)[:300])
        return gone

    def apply_review(self, t: dict[str, Any], rr: RunResult) -> str | None:
        """The verdict of a finished review run → the store. `t` is the task as it is NOW (read after the run).
        Returns the reviewer's summary when the verdict is a pass of a task that is not merged yet (`pre_merge`): the
        caller merges it after it has left the store lock (`review_task`); else None."""
        if not (rr.ok and rr.structured):
            t["review_inconclusive"] = int(t.get("review_inconclusive") or 0) + 1
            self.store.save_task(t)
            if t["review_inconclusive"] >= 3 and not self.retry_review(t, "test_tooling", f"the review run failed: {rr.error}"):
                self.store.transition(t["id"], "blocked", "reviewer", f"review failed to run 3×: {rr.error}", force=True)
            return
        v = rr.structured
        if v.get("verdict") == "inconclusive":
            v = {**v, "inconclusive_reason": inconclusive_reason(v)}
        self.store.add_review(t["id"], v, f"{self.cycle_id}-review-{t['id']}")
        for o in v.get("observations") or []:
            self.store.add_observation({**o, "source": "reviewer", "task": t["id"], "run_id": f"{self.cycle_id}-review-{t['id']}",
                                        "fingerprint": fingerprint("review", o.get("classification", ""), o.get("url", ""), o.get("title", ""))})
        verdict = v.get("verdict")
        if verdict in ("pass", None, "inconclusive") and self.content_without_post(t):
            # a marketing task is accepted only by a recorded publication (release-radar T-0006 went `accepted` on
            # "inconclusive 3×" with posted 0); the PO re-plans it instead
            self.store.transition(t["id"], "blocked", "reviewer", "content task without a recorded publication: nothing to accept",
                                  force=True, blocked_by="po")
            self.store.ask(from_="reviewer", to="po", question=f"Task {t['id']} ({t.get('title', '')[:80]}) has no recorded publication; "
                           "re-plan it (new slot with not_before) or close it with close_tasks.", task_id=t["id"], blocks=[t["id"]])
            return
        if verdict == "pass" and t.get(PRE_MERGE):
            # reviewed on its branch: the pass is what merges it (by the caller, outside the store lock); a conflict returns it
            # to its author — nothing was live
            return str(v.get("summary") or "passed")
        elif verdict == "pass":
            self.store.transition(t["id"], "accepted", "reviewer", v.get("summary", "")[:300], accepted_at=utcnow())
        elif verdict == "fail":
            fails = int(t.get("review_fails") or 0) + 1
            if fails >= int(self.st.cycle.get("max_review_fails", 2)):
                self.store.transition(t["id"], "blocked", "reviewer", f"failed review {fails}×: {v.get('first_failure', '')[:200]}",
                                      review_fails=fails, blocked_by="review")
                self.store.ask(from_="reviewer", to="po", question=f"Task {t['id']} failed review {fails} times: {v.get('first_failure', '')}. "
                               f"Narrow the task, drop it, or re-specify?", task_id=None)
            else:
                self.store.transition(t["id"], "in_progress", "reviewer", f"failed: {v.get('first_failure', '')[:200]}",
                                      review_fails=fails, last_review=v)
        else:
            t["review_inconclusive"] = int(t.get("review_inconclusive") or 0) + 1
            self.store.save_task(t)
            if t["review_inconclusive"] >= INCONCLUSIVE_LIMIT:
                if self.inconclusive_policy() == INCONCLUSIVE_ASK:
                    # owner 22.11 (ai-battles day review): on 22.11 ten tasks were accepted by this rule with nobody having
                    # seen the result, the description field among them. No proof → no acceptance: the PO decides.
                    why = _short_text(v.get("first_failure") or v.get("limits") or v.get("summary"), 300)
                    if self.return_from_review(t, v, why):
                        return
                    returned = int(t.get("review_returns") or 0)
                    if returned:
                        why = f"{why} (already returned to work {returned} time(s) for this, and still unproven)"
                    self.store.transition(t["id"], "blocked", "reviewer",
                                          f"inconclusive {INCONCLUSIVE_LIMIT}× [{v['inconclusive_reason']}]: not accepted without proof: {why}"[:400],
                                          force=True, blocked_by="review")
                    self.store.ask(from_="reviewer", to="po", task_id=None,
                                   question=f"Task {t['id']} ({str(t.get('title') or '')[:80]}) could not be checked {INCONCLUSIVE_LIMIT} times: {why}. "
                                            "Nobody has seen its result. Create a check that can run (a scenario, a screenshot, a narrower task) "
                                            "or close the task with close_tasks; do not treat it as accepted.")
                else:
                    self.store.transition(t["id"], "accepted", "reviewer", "inconclusive 3×: accepted without external proof", accepted_at=utcnow())

    # three unproven reviews: back to work with a named reason (owner 26.11, rework slice 2b) ---------------------------

    def retry_review(self, t: dict[str, Any], reason: str, why: str) -> bool:
        """An unproven review whose reason is the environment's (the e2e lock, a server that did not start, the tooling):
        the task stays `released` and is reviewed again — `REVIEW_ENV_RETRIES` times per task. True = handled."""
        if not self.p.escalation_cfg["review_return"]:
            return False
        used = int(t.get("review_env_retries") or 0)
        if used >= REVIEW_ENV_RETRIES:
            return False
        t = self.store.task(t["id"]) or t
        t["review_env_retries"], t["review_inconclusive"] = used + 1, INCONCLUSIVE_LIMIT - 1
        t.setdefault("history", []).append({"ts": utcnow(), "actor": "dispatcher", "from": t.get("state"), "to": t.get("state"),
                                            "note": f"review retried ({used + 1} of {REVIEW_ENV_RETRIES}): unproven 3× [{reason}], the environment's: {why}"[:500]})
        self.store.save_task(t)
        self.store.event(REVIEW_RETRY_EVENT, t["id"], "dispatcher", {"reason": reason, "n": used + 1, "why": why[:200]})
        log.info("%s: %s: unproven 3× [%s] is the environment's — review retried (%d of %d)", self.p.slug, t["id"], reason, used + 1, REVIEW_ENV_RETRIES)
        return True

    def return_from_review(self, t: dict[str, Any], v: dict[str, Any], why: str) -> bool:
        """The third `inconclusive` of a released task, `cycle.escalation.review_return` on. The SAME task goes back to
        its role (`in_progress`, like a failed review) with the reason word and what to do about it — no block, no PO
        question, no new «proof» task. True = handled; False = the old rule applies (switch off, or the task was already
        returned `REVIEW_RETURN_MAX` times)."""
        if not self.p.escalation_cfg["review_return"]:
            return False
        reason = str(v.get("inconclusive_reason") or "other")
        if reason in REVIEW_RETRY_REASONS:
            return self.retry_review(t, reason, why)
        used = int(t.get("review_returns") or 0)
        if used >= REVIEW_RETURN_MAX:
            return False
        todo = REVIEW_RETURN_TODO.get(reason) or REVIEW_RETURN_TODO["other"]
        last = {**{k: v.get(k) for k in ("criteria", "summary")}, "verdict": "unproven", "return_reason": reason,
                "first_failure": f"NOT PROVEN, reason `{reason}`: three reviews could not confirm this task, so it is back with you. {todo} "
                                 f"The reviewer's words: {why}"}
        self.store.transition(t["id"], "in_progress", "reviewer", f"returned from review: unproven {INCONCLUSIVE_LIMIT}× [{reason}]: {why}"[:400],
                              force=True, review_returns=used + 1, review_inconclusive=0, last_review=last, assignee_run=None)
        self.store.event(REVIEW_RETURN_EVENT, t["id"], "reviewer", {"reason": reason, "n": used + 1, "why": why[:200]})
        log.info("%s: %s: unproven 3× [%s] → back to work (%d of %d)", self.p.slug, t["id"], reason, used + 1, REVIEW_RETURN_MAX)
        return True

    def inconclusive_policy(self) -> str:
        """What the third `inconclusive` verdict of a released task does. product.yaml `review: {inconclusive: accept | ask}`;
        without the key: `ask` for the game profile (blocked + a PO question, owner 22.11), `accept` for every other
        profile (the pilot's rule: a site task the reviewer cannot observe is accepted without external proof)."""
        v = str(((self.p.raw.get("review") or {}) if isinstance(self.p.raw.get("review"), dict) else {}).get("inconclusive") or "").strip().lower()
        if v in (INCONCLUSIVE_ACCEPT, INCONCLUSIVE_ASK):
            return v
        return INCONCLUSIVE_ASK if self.p.profile == "game" else INCONCLUSIVE_ACCEPT

    def playtest_due(self, t: dict[str, Any]) -> bool:
        """Game profile (owner 20.11, slice 4): a developer's or designer's change is accepted by the tester playing it; a
        document task (game-designer) and anything the task marks `playtest: false` stay with the reviewer."""
        return (self.p.profile == "game" and str(t.get("role") or "developer") in PLAYTEST_ROLES and t.get("playtest") is not False
                and not self.dry_run and self.runner is not None)

    def start_playtest(self, t: dict[str, Any], result: dict[str, Any] | None, sha: str, fallback_prompt: str, tests: str = "",
                       force_local: bool = False):
        """Start the server from the merged commit for the tester. Returns (server, prompt, tools, env); on failure
        (no Node, run_cmd missing, health never answered) returns (None, reviewer prompt + the reason, …) so the reviewer
        still judges what it can, and the PO gets one environment_error observation. `tests` is the dispatcher's unit-test
        run from the repo_only path (not repeated here)."""
        work = self.p.work_dir / self.cycle_id / f"playtest-{t['id']}"      # its own folder: the review checkout is read-only
        out_dir = self.p.dir / "reviews" / t["id"]
        out_dir.mkdir(parents=True, exist_ok=True)
        unit_log = out_dir / UNIT_LOG_NAME
        if not tests:
            try:
                tests = run_tests_for_review(self.p.repo_dir, sha, work / "tests-run", self.p.test_cmd, lock=self.repo_lock, full_log=unit_log)
            except Exception as e:  # noqa: BLE001
                tests = f"(the dispatcher could not run the tests: {str(e)[:300]})"
        try:
            server = playtest.start(self.p, self.p.repo_dir, sha, work, lock=self.repo_lock, **({"force_local": True} if force_local else {}))
        except (playtest.PlaytestError, gitops.GitError, OSError) as exc:
            reason = str(exc)[:1500]
            log.warning("%s: play-test server for %s not started: %s", self.p.slug, t["id"], reason[:200])
            self.store.event("playtest.failed", t["id"], "dispatcher", {"reason": reason[:400]})
            self.store.add_observation({"title": f"Play-test server could not start for {t['id']}: {reason.splitlines()[0][:90]}",
                                        "classification": "environment_error", "source": "dispatcher", "task": t["id"], "url": self.p.site_base,
                                        "actual": reason[:400], "fingerprint": f"playtest-failed-{t['id']}", "run_id": f"{self.cycle_id}-review-{t['id']}"})
            prompt = fallback_prompt + f"\n# PLAY-TEST SERVER — NOT AVAILABLE\nThe dispatcher could not start the server from commit {sha}:\n{reason}\n" \
                                       "Judge what the checkout and the test run show; put the rest into `limits` as `not observable: server not started`.\n"
            return None, prompt, None, None
        hosts = self._allowed_hosts("tester").split(",") + [playtest.HOST]
        env = {"TEAM_PLAY_CWD": str(server.dest), "TEAM_PLAY_BASE_URL": server.base_url, "TEAM_PLAY_OUT": str(out_dir),
               "TEAM_PLAY_SERVER_LOG": str(server.log_path or ""), "TEAM_ALLOWED_HOSTS": ",".join(dict.fromkeys(h for h in hosts if h)),
               **({"TEAM_PLAY_UNIT_LOG": str(unit_log)} if unit_log.exists() else {}), **self.test_server_env(),
               **tester_bash_timeouts()}
        prompt = prompts.tester_prompt(self.p, t, result, sha, base_url=server.base_url, local=server.local, checkout=server.dest,
                                       out_dir=out_dir, tests=tests, server_notes=server.notes,
                                       test_flags=sorted(f for f in env.get("TEAM_PLAY_TEST_FLAGS", "").split(",") if f) if env.get("TEAM_PLAY_TEST_ENV") else None,
                                       unit_log=unit_log.exists())
        self.store.event("playtest.started", t["id"], "dispatcher", {"base_url": server.base_url, "local": server.local})
        return server, prompt, list(TESTER_TOOLS), env

    def test_server_env(self) -> dict[str, str]:
        """product.yaml `playtest: {test_env: {NAME: value}, test_flags: [NAME, …]}` (rework slice 6) for `team-play
        shots|screenshot --local`: the variables of a test server and the names the tester may set with `--env`. Empty
        when the product has no `test_env` — the tools then say that the local mode is not set up."""
        cfg = self.p.raw.get("playtest")
        cfg = cfg if isinstance(cfg, dict) else {}
        te = cfg.get("test_env")
        if not isinstance(te, dict) or not te or not self.p.run_cmd:
            return {}
        name = re.compile(r"^[A-Z][A-Z0-9_]{1,60}$")
        bad = [str(k) for k in te if not name.match(str(k))] + [str(f) for f in (cfg.get("test_flags") or []) if not name.match(str(f))]
        if bad:
            log.warning("%s: product.yaml playtest: %s is not a variable name (A-Z, 0-9, _) — dropped", self.p.slug, ", ".join(bad))
        return {"TEAM_PLAY_TEST_ENV": json.dumps({str(k): str(v) for k, v in te.items() if name.match(str(k))}),
                "TEAM_PLAY_TEST_FLAGS": ",".join(str(f) for f in (cfg.get("test_flags") or []) if name.match(str(f))),
                "TEAM_PLAY_RUN_CMD": self.p.run_cmd, "TEAM_PLAY_HEALTH_PATH": self.p.health_path or playtest.DEFAULT_HEALTH_PATH}

    def content_without_post(self, t: dict[str, Any]) -> bool:
        if str(t.get("role") or "") != "marketer":
            return False
        if is_plan_task(t):             # a marketing plan is accepted by its file, never by a post
            return not prompts.plan_path(self.p).exists()
        n = self.store.db.execute("SELECT COUNT(*) FROM posts WHERE task_id=?", (t["id"],)).fetchone()[0]
        return int(n or 0) == 0

    # triage: PO ------------------------------------------------------------------------------------

    DEV_IDLE_FILE = "dev-idle-po"         # products/<slug>/dev-idle-po: ISO time the PO was last woken for an idle developer

    def dev_idle_due(self, now: datetime | None = None) -> bool:
        """The developer has nothing ready: wake the PO for defect/quality work at most every `cycle.dev_idle_po_every_h`."""
        every = float(self.st.cycle.get("dev_idle_po_every_h", 12) or 0)
        if every <= 0:
            return False
        try:
            prev = datetime.fromisoformat((self.p.dir / self.DEV_IDLE_FILE).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return True
        return (now or datetime.now(timezone.utc)) - prev >= timedelta(hours=every)

    def mark_dev_idle(self, now: datetime | None = None) -> None:
        (self.p.dir / self.DEV_IDLE_FILE).write_text((now or datetime.now(timezone.utc)).isoformat(timespec="seconds"), encoding="utf-8")
        log.info("%s: developer idle, PO woken for defect/quality work", self.p.slug)

    QUEUE_WAKE_FILE = "queue-wake-po"     # products/<slug>/queue-wake-po: ISO time the PO was last woken for a low ready queue

    def ready_queue(self) -> dict[str, tuple[int, int]]:
        """role → (ready tasks, seats) for the roles whose queue the PO keeps filled (`QUEUE_ROLES`: the developer — the
        other roles' work is cut from what the developer's tasks need, not kept in stock) and that have a seat
        (rework slice 9)."""
        slots = self.execute_slots()
        ready = self.store.tasks(["ready"])
        out = {}
        for r in QUEUE_ROLES:
            seats = int(slots.get(r) or slots.get("*") or 0)
            if seats > 0:
                out[r] = (sum(1 for t in ready if str(t.get("role") or "developer") == r), seats)
        return out

    def queue_wake_due(self, now: datetime | None = None) -> bool:
        """The PO was not woken for a low queue within `cycle.queue.wake_every_min`."""
        try:
            prev = datetime.fromisoformat((self.p.dir / self.QUEUE_WAKE_FILE).read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return True
        return (now or datetime.now(timezone.utc)) - prev >= timedelta(minutes=int(self.p.queue_cfg["wake_every_min"]))

    def mark_queue_wake(self, now: datetime | None = None) -> None:
        (self.p.dir / self.QUEUE_WAKE_FILE).write_text((now or datetime.now(timezone.utc)).isoformat(timespec="seconds"), encoding="utf-8")

    def create_standing_tasks(self) -> list[str]:
        """Standing directions (owner 21.11, team/standing.py): the dispatcher itself creates the next task of every
        direction that is due — none of its tasks open and `every_h` passed since the last one was created. No PO run is
        needed; the PO finds the task in its backlog in the same phase. Nothing is created in a dry run or while the
        product is stopped (STOP file, `paused`, onboarding not finished); a direction gated to a later stage waits."""
        stop = self.stopped()
        if stop:
            log.info("%s: standing directions skipped: stopped (%s)", self.p.slug, stop)
            return []
        created: list[str] = []
        for d in standing.due(self.p, self.store, _now()):
            if self.dry_run:
                log.info("[dry-run] %s: would create the standing task `%s` (%s, role %s)", self.p.slug, d.id, d.cadence, d.role)
                continue
            created.append(standing.create_task(self.p, self.store, d, cycle_id=self.cycle_id)["id"])
        return created

    def phase_triage(self) -> None:
        from . import deputy
        try:
            self.create_standing_tasks()          # before the PO reads the backlog: it sees the new tasks, not a gap to fill
        except Exception as exc:  # noqa: BLE001 — a bad `standing:` entry must not cost the product its triage
            log.exception("%s: standing directions failed: %s", self.p.slug, exc)
            self.store.event("standing.error", self.p.slug, "dispatcher", str(exc)[:300])
        try:
            deputy.run_for_product(self)          # owner away: answer owner questions by mandate before the PO reads them
        except Exception as exc:  # noqa: BLE001
            log.exception("%s: deputy failed: %s", self.p.slug, exc)
        with self.store.lock:                     # review lanes may be writing tasks beside the triage
            self.hand_over_readouts()
            self.settle_replans()
        stuck_md = replan.prompt_block(self.store, self.replan_cfg())
        new_obs = self.store.observations("new")
        qs = [q for q in self.store.questions(["open"]) if q.get("to") == "po"]
        active = self.store.tasks(["ready", "claimed", "in_progress", "review", "released"])
        idle = not active
        dev_idle = not any(str(t.get("role") or "") == "developer" for t in active)
        dev_wake = dev_idle and self.dev_idle_due()
        qcfg = self.p.queue_cfg                          # rework slice 9: the PO cuts tasks ahead (off by default)
        queue = self.ready_queue() if qcfg["ahead"] else {}
        low = {r: n for r, (n, seats) in queue.items() if n < int(qcfg["target_ready"])}
        queue_wake = bool(low) and not idle and self.queue_wake_due()
        if not new_obs and not qs and not idle and not dev_wake and not stuck_md and not queue_wake:
            log.info("%s: triage skipped (nothing new, team busy)", self.p.slug)
            return
        max_ready = int(qcfg["max_per_run"]) if qcfg["ahead"] else int(self.st.cycle.get("max_ready", 3))
        queue_md = ""
        if qcfg["ahead"] and queue:
            queue_md = ("# READY QUEUE (the seats take the next task the moment one is finished)\n"
                        + "\n".join(f"- {r}: {n} ready, {seats} seat(s)" for r, (n, seats) in sorted(queue.items()))
                        + f"\nKeep at least {qcfg['target_ready']} ready tasks for every role above: cut the next tasks of the current milestone and "
                          f"of the owner's approved plan now, in the order they should be done — up to {max_ready} in this run. Each must be able to "
                          "run beside the others (different files or screens; name `depends_on` where the order matters). Do not invent work to "
                          "fill the queue: when nothing more is certainly needed, propose fewer and say so in the digest.")
        if queue_wake:
            self.mark_queue_wake()
            self.store.event("queue.low", self.cycle_id, "dispatcher", {"low": low, "target": qcfg["target_ready"]})
            log.info("%s: the ready queue is low (%s; target %s): the PO is woken to cut ahead", self.p.slug,
                     ", ".join(f"{r} {n}" for r, n in low.items()), qcfg["target_ready"])
        latest = self.p.dir / "metrics" / "latest.md"
        gsc_md = self.p.dir / "metrics" / "gsc.md"               # owner 10.11: Search Console next to the log metrics
        seen_upto = runprofile.last_event_id(self.store)         # what this prompt can include; reviews go on writing beside it
        prompt = prompts.po_prompt(self.p, self.store, cycle_id=self.cycle_id, observations=new_obs[:25], questions=qs,
                                   metrics_md=(latest.read_text(encoding="utf-8") if latest.exists() else "")
                                   + ("\n\n" + gsc_md.read_text(encoding="utf-8")[:3000] if gsc_md.exists() else ""),
                                   spent_today=self.store.spent_today(), cap=self.p.daily_cap,
                                   max_ready=max_ready, idle=idle, dev_idle=dev_idle,
                                   channel_caps=self.channel_caps(), stuck_md=stuck_md, queue_md=queue_md)
        if dev_wake:
            self.mark_dev_idle()
        work = self.p.work_dir / self.cycle_id / "po"
        po_schema = schema("po-decision")
        if max_ready > int(po_schema["properties"]["ready_tasks"].get("maxItems") or 0):
            po_schema["properties"]["ready_tasks"]["maxItems"] = max_ready      # the schema's own limit is the default of three
        rr = self.role_run("po", prompt, "po-decision", cwd=work, label="po", cap=2.0, allowed_tools=[], max_turns=8, schema_obj=po_schema)
        if rr is not None:                                       # a run profile written while the PO ran is shown to it next time
            self.store.event(runprofile.PO_SEEN_EVENT, self.cycle_id, "dispatcher", {"upto": seen_upto})
        if rr is None or not (rr.ok and rr.structured):
            return
        self.apply_po(rr.structured, new_obs)

    def apply_po(self, res: dict[str, Any], new_obs: list[dict[str, Any]]) -> None:
        """The PO's decisions → the store, in one piece: reviews of this cycle may still be running in their lanes."""
        with self.store.lock:
            self._apply_po(res, new_obs)

    def _apply_po(self, res: dict[str, Any], new_obs: list[dict[str, Any]]) -> None:
        ids = {o["id"] for o in new_obs}
        for d in res.get("decisions") or []:
            oid = str(d.get("observation_id"))
            if oid in ids:
                self.store.update_observation(oid, status="triaged", decision={**d, "cycle": self.cycle_id})
        for a in res.get("answers") or []:
            try:
                self.store.answer(str(a["question_id"]), str(a["answer"]), "po", not_before=a.get("not_before"))
            except FileNotFoundError:
                pass
        for rp in res.get("reprioritize") or []:
            t = self.store.task(str(rp.get("task_id")))
            if t and rp.get("priority") in PRIO:
                t["priority"] = rp["priority"]
                t.setdefault("history", []).append({"ts": utcnow(), "actor": "po", "from": t["state"], "to": t["state"], "note": f"priority → {rp['priority']}: {rp.get('reason', '')[:200]}"})
                self.store.save_task(t)
        for c in res.get("close_tasks") or []:
            t = self.store.task(str(c.get("task_id")))
            if t and t.get("state") in CLOSABLE_BY_PO:
                self.store.transition(t["id"], "rejected", "po", f"closed by PO: {c.get('reason', '')[:300]}", force=True)
        skipped, held = [], []
        for pr in res.get("promote_tasks") or []:
            t = self.store.task(str(pr.get("task_id")))
            if not t or t.get("state") not in ("proposed", "ready", "deferred"):
                continue
            t["hypothesis"] = pr.get("hypothesis")
            problems = self.hypothesis_problems(t)
            t.setdefault("history", []).append({"ts": utcnow(), "actor": "po", "from": t["state"], "to": t["state"],
                                                "note": "hypothesis set" + (f"; rule not met: {'; '.join(problems)}" if problems else "")})
            self.store.save_task(t)
            if problems:
                held.append(f"{t['id']} stays {t['state']}: {'; '.join(problems)[:160]}")
            elif t["state"] != "ready":
                self.store.transition(t["id"], "ready", "po", "hypothesis rule met", force=True)
        closed_check = self.p.duplicates_cfg["closed_check"]
        for rt in res.get("ready_tasks") or []:
            # rework slice 3: the same title as a task closed in the last days — finished work is not created again, a
            # rejected task's successor is linked to it (the chain of one goal, for the stuck-goal counter)
            twin = closed_twin(prompts.recently_closed(self.store, CLOSED_TWIN_DAYS, CLOSED_TWIN_STATES), rt) if closed_check else None
            if twin is not None and twin.get("state") != "rejected":
                self.store.event("task.duplicate_skipped", twin["id"], "po", {"title": str(rt.get("title"))[:140], "closed": twin.get("state")})
                skipped.append(f"{str(rt.get('title'))[:80]} → уже сделана: {twin['id']} ({twin.get('state')})")
                log.info("%s: PO task %r not created: the work is done as %s (%s)", self.p.slug, str(rt.get("title"))[:60], twin["id"], twin.get("state"))
                continue
            dup = duplicate_task(self.store.tasks(OPEN_TASK_STATES), rt)
            sid = str(rt.get("standing") or "")
            if sid and not standing.known_id(self.p, sid):
                log.warning("%s: PO task %r names an unknown standing direction %r — field dropped", self.p.slug, rt["title"][:60], sid)
                sid = ""
            if sid and not dup:
                # one open task per standing direction (owner 21.11): the PO's task for a direction that has one open IS that task
                dup = next((t for t in standing.tasks_of(self.store.tasks(), sid) if standing.is_open(t)), None)
            if dup:
                # the PO re-proposed a task that is already open (same title, or the same role and start moment): one slot, one task
                self.store.event("task.duplicate_skipped", dup["id"], "po", {"title": rt["title"][:140], "not_before": rt.get("not_before")})
                skipped.append(f"{rt['title'][:80]} → already open as {dup['id']}")
                continue
            new = self.store.create_task(title=rt["title"], type_=self.task_type_for_stage(str(rt["type"])), problem=rt["problem"], scope=rt["scope"], acceptance=rt["acceptance"],
                                   negative_cases=rt.get("negative_cases"), goal=rt["goal"], priority=rt["priority"], size=rt["size"],
                                   role=rt["role"], observations=rt.get("observations"), links=rt.get("links"), created_by="po", state="ready",
                                   not_before=rt.get("not_before"), plan=bool(rt.get("plan")) and rt["role"] == "marketer",
                                   depends_on=[str(x) for x in (rt.get("depends_on") or [])])
            marks = {k: rt[k] for k in TASK_MARKS if rt.get(k) not in (None, "", False)}
            if marks:                                 # rework slice 5: the PO's marks of a visible change stay on the task
                new.update(marks)
                self.store.save_task(new)
            if self.p.visible_cfg["redesign_gate"] and rt.get("redesign") and not str(rt.get("new_function") or "").strip():
                # owner 26.11 «редизайн делаем только при новом функционале»: not started without his word
                self.store.transition(new["id"], "proposed", "dispatcher", "redesign without a new function: waits for the owner's word "
                                      f"(`team task move {new['id']} --to ready --by owner`)", force=True)
                self.store.ask(from_="dispatcher", to="owner", task_id=None, question=(
                    f"The PO proposed {new['id']} «{new['title'][:100]}»: it changes the layout or look of an existing screen and names no new "
                    "function for the player. By your principle (26.11: редизайн делаем только при новом функционале) it is not started. "
                    f"To allow it: `team task move {new['id']} --to ready --by owner`. Otherwise leave it, the PO will close it."))
                self.store.event(REDESIGN_HELD_EVENT, new["id"], "dispatcher", {"title": new["title"][:140]})
                held.append(f"{new['id']} → proposed: редизайн существующего экрана без новой функции, ждёт слова владельца")
            if rt.get("continues"):                   # owner 11.11: the chain of one goal, for the stuck-goal counter
                new["continues"] = [str(x) for x in rt["continues"]][:5]
                self.store.save_task(new)
            if twin is not None:                      # the PO re-created a rejected task without naming it: the dispatcher links them
                new["continues"] = ([twin["id"]] + [x for x in (new.get("continues") or []) if x != twin["id"]])[:5]
                self.store.save_task(new)
                self.store.event("task.recreated", new["id"], "po", {"continues": twin["id"], "title": new["title"][:140]})
            if rt.get("repo_only"):                   # T-0015: reviewer gets the read-only checkout (task.schema.json)
                new["repo_only"] = True
                self.store.save_task(new)
            if sid:                                   # owner 21.11: counted as the direction's task — no second one, the cadence restarts
                new[standing.TASK_FIELD] = sid
                docs = next((list(d.docs) for d in standing.directions(self.p) if d.id == sid and d.docs), None)
                if docs:
                    new["docs"] = docs
                self.store.save_task(new)
            if rt.get("hypothesis") or hypothesis.applies(new):
                new["hypothesis"] = rt.get("hypothesis")
                self.store.save_task(new)
                problems = self.hypothesis_problems(new)
                if problems:                          # owner 07.11: no real evidence → the feature waits as an idea
                    self.store.transition(new["id"], "proposed", "dispatcher", f"hypothesis rule: {'; '.join(problems)}"[:500], force=True)
                    held.append(f"{new['id']} → proposed: {'; '.join(problems)[:160]}")
        for q in res.get("owner_questions") or []:
            self.store.ask(from_="po", to="owner", question=f"{q['question']} (why the owner: {q.get('why_owner', '')})", task_id=q.get("task_id"))
        replan_notes = self.apply_replans(res.get("replans") or [])
        digest = res.get("digest_md", "")
        if replan_notes:
            digest += "\n\n_Перепланирование застрявших целей: " + "; ".join(replan_notes) + "._"
        if skipped:
            digest += "\n\n_Диспетчер не создал дубли (задача уже открыта или уже сделана): " + "; ".join(skipped) + "._"
        if held:
            digest += "\n\n_Правило гипотез (фича без доказательств от живых людей ждёт в proposed): " + "; ".join(held) + "._"
        (self.p.dir / "digests" / f"{self.cycle_id}.md").write_text(f"# PO digest {self.cycle_id}\n\n{digest}\n", encoding="utf-8")

    # execute: one ready task → developer / marketer / analyst ------------------------------------

    def ensure_local_repo(self) -> bool:
        """Game profile without `repo.url` yet (owner 20.11: the owner creates GitHub repositories, the team must not wait):
        the dispatcher starts a local git repository under data/repos/<slug> so design and code tasks can begin; one
        observation asks the PO for the URL (the owner adds `repo.url`, then the local history is pushed by hand). A site
        product keeps the old behaviour (blocked: no repository configured)."""
        if self.p.profile != "game" or self.p.repo_url:
            return False
        repo = self.p.repo_dir
        with self.repo_lock:                      # the developer's and the game designer's lanes may both get here first
            if (repo / ".git").exists():
                return True
            gitops.init_local(repo, self.p.repo_branch)
            (repo / "README.md").write_text(f"# {self.p.raw.get('name') or self.p.slug}\n\nStarted by agent-team; no remote yet.\n", encoding="utf-8")
            gitops.git(repo, "add", "-A")
            gitops.git(repo, "commit", "-q", "-m", "init: local repository started by the dispatcher")
        self.store.event("repo.local_init", self.p.slug, "dispatcher", {"path": str(repo)})
        self.store.add_observation({"title": "Repository is local only: ask the owner for a GitHub URL (repo.url) and a host",
                                    "classification": "need_hypothesis", "source": "dispatcher", "url": self.p.site_base,
                                    "actual": f"work happens in {repo}; nothing is pushed until repo.url is set", "fingerprint": f"local-repo-{self.p.slug}"})
        return True

    def pending_role(self, t: dict[str, Any]) -> bool:
        """True for a task whose role has no execute path yet (PENDING_ROLES); the first time an event is written."""
        role = str(t.get("role") or "developer")
        if role not in PENDING_ROLES:
            return False
        if not t.get("role_pending_noted"):
            self.store.event("task.role_not_available", t["id"], "dispatcher", {"role": role})
            t["role_pending_noted"] = True
            self.store.save_task(t)
        return True

    def task_type_for_stage(self, type_: str) -> str:
        """Owner 20.11: a `milestone` task (the playable core, outside the hypothesis rule) exists only before stage M2; from
        M2 on the PO's `milestone` is a `feature` and the hypothesis rule applies as for any product."""
        if type_ == "milestone" and self.p.profile == "game" and stage_index(self.p.stage) >= stage_index(HYPOTHESIS_FROM_STAGE):
            return "feature"
        return type_

    def execute_slots(self) -> dict[str, int]:
        """How many tasks per role one cycle may start. `cycle.execute_slots` (team.yaml, overridable in product.yaml) —
        03.11: with a single `max_execute_per_cycle` the daily content task starved behind P1/P2 developer tasks
        (release-radar T-0009 missed its 06:30 slot); content and measurement now have their own seats."""
        slots = self.p.raw.get("execute_slots") or self.st.cycle.get("execute_slots")
        if isinstance(slots, dict) and slots:
            out = {str(k): int(v) for k, v in slots.items()}
            if self.p.profile == "game":              # owner 20.11: the design roles always have a seat on a game product
                for r, n in GAME_ROLE_SLOTS.items():
                    out.setdefault(r, n)
            return out
        return {"*": int(self.st.cycle.get("max_execute_per_cycle", 1))}

    def phase_execute(self) -> None:
        """The execute phase, then — `deploy.batch: true` — the one deploy of the cycle: every lane has merged what it
        had, the reviews are over, the live host is switched once to the last merged commit (`deploy_batch`)."""
        try:
            self._phase_execute()
        finally:
            self._cancel_batch_timer()
            try:
                self.deploy_batch("end of the execute phase")
            except Exception as exc:  # noqa: BLE001 — the merged tasks stay pending; the next cycle deploys them first
                log.exception("%s: batch deploy at the end of the execute phase failed: %s", self.p.slug, exc)
                self.store.event("phase.error", "execute", "dispatcher", f"batch deploy: {exc}"[:500])

    def _phase_execute(self) -> None:
        """Start the tasks of this cycle — one per seat, picked exactly as before — and wait for everything in flight.

        Sequential cycle (`cycle.parallel.enabled: false`): the loop below visits the candidates in order and runs each
        task before it looks at the next one. Parallel cycle: the same candidates in the same order, split by seat —
        every seat is a lane that works through its own tasks one after another, so the seats overlap each other and
        the reviews started by `phase_review`; then everything is joined. A later pass starts what became startable
        meanwhile and still has a free seat: a task the review of this cycle sent back, a task whose `depends_on` was
        finished by another lane."""
        try:
            self.sweep_worktrees()
        except Exception as exc:  # noqa: BLE001 — housekeeping must not cost the product its execute phase
            log.warning("%s: worktree sweep failed: %s", self.p.slug, gitops.redact(str(exc))[:300])
        try:
            self.sweep_work_dirs()
        except Exception as exc:  # noqa: BLE001 — the same: a full disk is worse, but the tasks come first
            log.warning("%s: sweep of old cycle folders failed: %s", self.p.slug, gitops.redact(str(exc))[:300])
        slots = self.execute_slots()
        used: dict[str, int] = {}
        if self.lanes.parallel and self.flow.refill and not self.dry_run:
            # owner 25.11 (team/flow.py): a seat that finishes takes the next task until the phase is `refill_min` old
            self._refill_deadline = time.monotonic() + self.flow.refill_min * 60
            self._refill_last = time.monotonic() + self.flow.refill_max_min * 60
            log.info("%s: execute: refill is on — a freed seat takes the next task for %g min, up to %g min while reviews hold the cycle",
                     self.p.slug, self.flow.refill_min, self.flow.refill_max_min)
        if not self.lanes.parallel:
            for t in self.execute_candidates():
                if all(used.get(k, 0) >= v for k, v in slots.items()):
                    break
                if self.pending_role(t):
                    continue        # a role the cycle cannot run yet (tester: slice 4): the task waits in ready, noted once
                key = self.seat_key(str(t.get("role") or "developer"), slots)
                if key is None or used.get(key, 0) >= slots[key]:
                    continue        # this role's seat for the cycle is taken
                if not self.startable(t):
                    continue
                if self.execute_task(t):
                    used[key] = used.get(key, 0) + 1
                    self.n_executed += 1
                else:
                    self._not_done.add(t["id"])
            return
        seen: set[str] = set()                    # tasks a lane of this cycle has dealt with: not offered again in a later pass
        for wave in range(1, EXECUTE_WAVES_MAX + 1):
            with self._seat_lock:
                self._taken.clear()               # no seat lane is running here: a task that waited for a dependency may be offered again
            groups: dict[str, list[dict[str, Any]]] = {}
            for t in self.execute_candidates():
                key = self.seat_key(str(t.get("role") or "developer"), slots)
                if key is None or t["id"] in seen or self.lanes.busy(t["id"]):
                    continue
                if used.get(key, 0) >= slots[key] and not self.refill_open():
                    continue        # the seat's places for this cycle are used (refill: «places at once», no such count)
                groups.setdefault(key, []).append(t)
            started = 0
            for key, tasks in groups.items():
                # one lane per seat: the tasks of a role run one after another; only a role named in
                # `cycle.parallel.overlap_roles` gets a lane per place of its seat
                n = min(slots[key], len(tasks)) if key in self.par.overlap_roles else 1
                feed = _Feed(tasks)
                for i in range(n):
                    self.lanes.submit("execute", LANE_EXECUTE, f"exec-{key}" + (f"-{i + 1}" if n > 1 else ""), self.seat_lane, key, feed, slots, used, seen)
                started += n
            in_flight = self.lanes.pending()      # the new seat lanes and whatever `phase_review` started
            log.log(logging.INFO if in_flight else logging.DEBUG, "%s: execute pass %d: %d seat lane(s) (%s), %d lane(s) in flight with the reviews",
                    self.p.slug, wave, started, ", ".join(f"{k}: {len(v)} task(s)" for k, v in groups.items()) or "nothing to start", in_flight)
            self.finish_lanes()
            if not in_flight:
                break               # nothing ran in this pass, so nothing can have become startable

    def execute_candidates(self) -> list[dict[str, Any]]:
        """Tasks the execute phase looks at, in its order: in_progress (a failed review or a partial result) before
        new ones, then by priority, then by age."""
        return sorted(self.store.tasks(["in_progress", "ready"]), key=lambda x: (x["state"] != "in_progress", PRIO.get(x.get("priority"), 9), x.get("created", "")))

    @staticmethod
    def seat_key(role: str, slots: dict[str, int]) -> str | None:
        """The seat a role's task takes: its own entry of `execute_slots`, else the shared `*`, else none."""
        return role if role in slots else ("*" if "*" in slots else None)

    def startable(self, t: dict[str, Any]) -> bool | None:
        """May the execute phase start this task now? False = no (some of the checks record why: a transition, an
        observation, a question); None = not yet — a dependency is not finished, which another lane of this cycle may
        still change."""
        if t.get("state") == "in_progress" and t.get("assignee_run") and not t.get("last_review") and not (t.get("results") or [{}])[-1].get("status") == "partial":
            return False        # a run is on it (lease alive); stale leases are handled in collect
        if t.get("state") == "ready" and self.open_question_blocks(t):
            return False        # T-0091: re-opened while its question is still unanswered: not leased again
        nb = parse_ts(t.get("not_before"))
        if nb and _now() < nb:
            return False        # scheduled for later (daily posts): the PO set not_before
        if t.get("state") == "ready" and unmet_dependencies(self.store, t):
            return None         # `depends_on`: a dependency is not done yet or its result is a stub
        if t.get("state") == "ready" and hypothesis.applies(t):
            problems = self.hypothesis_problems(t)
            if problems:        # owner 07.11: a feature without real evidence does not start; the PO promotes it with one
                self.store.transition(t["id"], "proposed", "dispatcher", f"hypothesis rule: {'; '.join(problems)}"[:500], force=True)
                return False
        if t.get("state") == "ready" and not t.get("not_before") and future_dated_title(str(t.get("title", "")), _now()):
            self.store.add_observation({
                "title": f"Task {t['id']} names a future date in its title but has no not_before; not started: {t.get('title', '')}"[:140],
                "classification": "need_hypothesis", "source": "dispatcher", "task": t["id"],
                "fingerprint": f"future-dated-{t['id']}", "run_id": f"{self.cycle_id}-execute",
                "detail": "PO: set not_before on this task (or fix the title)."})
            return False
        if self.guard_replan(t) or self.guard_no_commit_loop(t):
            return False
        return True

    def seat_lane(self, key: str, feed: "_Feed", slots: dict[str, int], used: dict[str, int], seen: set[str]) -> None:
        """One execute lane: the tasks of seat `key`, in order, one after another, until the seat's places for this cycle
        are used — the body of the sequential loop for one seat. Several lanes of one seat (`overlap_roles`) share the
        feed and the count; a task being executed holds a place, so the seat is never over-filled."""
        healthy = True                            # refill: this lane's last task was carried through and its run went well
        while True:
            with self._seat_lock:
                # Refill (`cycle.flow.refill`, team/flow.py): the seat's count of tasks per cycle does not end the lane
                # while the refill window is open — the number of lanes of the seat is what bounds the work at once.
                extra = used.get(key, 0) + self._seat_running.get(key, 0) >= slots[key]
                if extra and not (healthy and self.refill_open()):
                    return
                if healthy and self.refill_open():
                    # the feed is the list made at the start of the pass; with refill the backlog is read again for every
                    # pick — what was added, sent back or moved meanwhile is seen, in the usual order
                    t = self.next_for_seat(key, slots, seen)
                else:
                    t = feed.next()
                    while t is not None and t["id"] in self._taken:      # a refill pick took it before its turn in the feed
                        t = feed.next()
                if t is None:
                    return
                self._taken.add(t["id"])
                self._seat_running[key] = self._seat_running.get(key, 0) + 1
            ok, verdict = False, False            # an exception below counts as «dealt with»: the task is not offered again this cycle
            self._tl.run_ok = None                # no run of this task yet (`role_run` sets it)
            try:
                if not self.pending_role(t):
                    # The mark first, then the task read again: whatever another process (`team task move`) or a lane wrote
                    # since the pass listed it is seen here, and from the mark on that process leaves the task alone.
                    with self.lanes.working(t["id"]):
                        now = self.store.task(t["id"])
                        if now is None or now.get("state") not in ("ready", "in_progress"):
                            log.info("%s: %s is not started: it is %s now, not %s as when the pass listed it", self.p.slug, t["id"],
                                     (now or {}).get("state") or "gone", t.get("state"))
                        else:
                            t = now
                            verdict = self.startable(t)
                            if verdict:
                                ok = bool(self.execute_task(t))
            finally:
                run_ok = getattr(self._tl, "run_ok", None)
                if verdict:                       # a task that was not startable says nothing about the health of the runs
                    healthy = ok and run_ok is not False
                with self._seat_lock:
                    self._seat_running[key] -= 1
                    if ok:
                        used[key] = used.get(key, 0) + 1
                        self.n_executed += 1
                        self.n_refilled += 1 if extra else 0
                    if not (ok and verdict):
                        self._not_done.add(t["id"])
                    if verdict is not None:      # None = waits for a dependency: a later pass of this cycle may start it
                        seen.add(t["id"])
                if verdict and not healthy:
                    # a run that failed or did not start (budget, limits, an outage): the next task would meet the same —
                    # nothing more is taken beyond the seat's places in this cycle, by any lane
                    self.close_refill(f"{t['id']}: the run {'failed' if run_ok is False and ok else 'did not finish'}")

    # the checkouts of the repository roles: one worktree per task (owner 21.11) ---------------------------------------

    def sweep_worktrees(self) -> list[str]:
        """Remove the checkouts nobody will work in again: the task is finished, closed or gone (a merged task loses its
        checkout at the merge already), or it has been blocked / deferred / waiting for the owner for longer than
        `cycle.parallel.worktree_keep_h`. Only the directory goes — `task/<id>` and its commits stay in the clone, and
        a task that comes back gets a new checkout of that branch. Returns the ids whose checkout was removed."""
        root = self.p.worktrees_dir
        if self.dry_run or not root.is_dir() or not (self.p.repo_dir / ".git").exists():
            return []
        removed: list[str] = []
        for d in sorted(x for x in root.iterdir() if x.is_dir()):
            t = self.store.task(d.name)
            state = str((t or {}).get("state") or "gone")
            if state in WORKTREE_LIVE_STATES or self.lanes.busy(d.name):
                continue
            if state in WORKTREE_PARKED_STATES:
                upd = parse_ts((t or {}).get("updated"))
                age_h = (_now() - upd).total_seconds() / 3600 if upd else None
                if age_h is None or age_h < self.par.worktree_keep_h:
                    continue
            with self.repo_lock:
                gitops.remove_task_worktree(self.p.repo_dir, d)
            log.info("%s: checkout of %s removed (task is %s); branch task/%s stays", self.p.slug, d.name, state, d.name)
            self.store.event("worktree.removed", d.name, "dispatcher", {"state": state, "path": str(d)})
            removed.append(d.name)
        return removed

    def sweep_work_dirs(self) -> list[str]:
        """Free the folders of finished cycles: in every data/work/<slug>/<cycle> whose newest entry is older than
        `cycle.parallel.workdir_keep_h` hours, remove the clones made for one run (`<label>/checkout`, `<label>/tests-run`)
        and every node_modules. The folder itself stays with its logs (`server.log`, `deploy-*.log`) and the files the
        runs wrote. Never touched: the folder of this cycle, `wt` (the task checkouts, `sweep_worktrees`), `deploy`,
        `onboarding`. A swept folder gets the mark `.swept` and is skipped from then on. One sweep works for at most
        WORKDIR_SWEEP_MAX_S; what is left waits for the next cycle. Returns the names of the folders swept."""
        root = self.p.work_dir
        if self.dry_run or not root.is_dir():
            return []
        keep_s = self.par.workdir_keep_h * 3600
        started, now = time.monotonic(), time.time()
        swept: list[str] = []
        removed = 0
        free_before = shutil.disk_usage(root).free
        for d in sorted(x for x in root.iterdir() if x.is_dir() and not x.is_symlink()):
            if d.name in WORKDIR_KEEP or d.name == self.cycle_id or (d / WORKDIR_SWEPT_MARK).exists():
                continue
            subs = [x for x in d.iterdir() if x.is_dir() and not x.is_symlink()]
            newest = max(x.lstat().st_mtime for x in [d, *subs])
            if now - newest < keep_s:
                continue
            if time.monotonic() - started > WORKDIR_SWEEP_MAX_S:
                log.info("%s: sweep of old cycle folders stopped after %.0fs; the rest waits for the next cycle", self.p.slug, WORKDIR_SWEEP_MAX_S)
                break
            gone = [sub / h for sub in subs for h in WORKDIR_HEAVY_DIRS if remove_tree(sub / h)]
            gone += remove_named_dirs(d)
            (d / WORKDIR_SWEPT_MARK).write_text(f"{utcnow()} {len(gone)} director{'y' if len(gone) == 1 else 'ies'} removed\n", encoding="utf-8")
            log.info("%s: cycle folder %s (%.0f h old): %d working director%s removed", self.p.slug, d.name, (now - newest) / 3600,
                     len(gone), "y" if len(gone) == 1 else "ies")
            swept.append(d.name)
            removed += len(gone)
        if swept:
            freed_mb = max(0, shutil.disk_usage(root).free - free_before) / 1e6
            log.info("%s: %d old cycle folder(s) swept, %d director%s removed, about %.0f MB freed", self.p.slug, len(swept), removed,
                     "y" if removed == 1 else "ies", freed_mb)
            self.store.event("workdir.swept", self.p.slug, "dispatcher", {"folders": len(swept), "removed": removed, "freed_mb": round(freed_mb)})
        return swept

    def task_checkout(self, t: dict[str, Any], start_point: str | None = None) -> tuple[Path, str]:
        """The task's own checkout and its branch (created, reused, or taken over from the layout before 21.11, where
        `task/<id>` was checked out in the clone itself). Under the repository lock: it fetches and may move the clone."""
        wt = self.p.worktree_dir(t["id"])
        with self.repo_lock:
            fresh = not wt.exists()
            branch = gitops.start_task_worktree(self.p.repo_dir, wt, t["id"], self.p.repo_branch, start_point=start_point)
        if fresh:
            self.store.event("worktree.created", t["id"], "dispatcher", {"path": str(wt), "branch": branch,
                                                                         "ahead": gitops.commits_ahead(wt, self.p.repo_branch)})
        return wt, branch

    def merge_conflict_back(self, t: dict[str, Any], exc: gitops.MergeConflict, role: str, run_id: str, *, rerun: bool = False) -> None:
        """The task's branch does not merge into the base branch: another lane merged a change of the same lines first.
        Nothing was merged and the cycle goes on. The task returns to its author with the conflict text — `in_progress`
        (the next run starts with the base merged into its branch and a MERGE CONFLICT block in the prompt), or `ready`
        with `rerun` (a role that returns whole files simply writes them again on the new base) — and one observation
        is recorded. After `cycle.parallel.max_merge_conflicts` conflicts in a row the task is blocked for the PO."""
        prev = t.get(MERGE_CONFLICT) if isinstance(t.get(MERGE_CONFLICT), dict) else {}
        n = int((prev or {}).get("count") or 0) + 1
        files = exc.files[:gitops.CONFLICT_FILES_MAX]
        rec = {"at": utcnow(), "base": self.p.repo_branch, "files": files, "text": exc.text[:CONFLICT_PROMPT_CHARS], "count": n, "run": run_id}
        note = f"merge conflict with {self.p.repo_branch} in {_short_text(', '.join(files), CONFLICT_NOTE_CHARS)} (nothing merged)"
        log.warning("%s: %s (%s) %s — conflict %d of %d", self.p.slug, t["id"], role, note, n, self.par.max_merge_conflicts)
        self.store.event("merge.conflict", t["id"], "dispatcher", {"role": role, "files": files, "count": n, "base": self.p.repo_branch})
        self.store.add_observation({"title": f"Merge conflict: {t['id']} ({role}) and {self.p.repo_branch} changed the same lines in {', '.join(files)}"[:140],
                                    "classification": "environment_error", "source": "dispatcher", "task": t["id"], "url": self.p.site_base,
                                    "actual": exc.text[:400], "expected": "tasks that run in the same cycle touch different files, or one depends_on the other",
                                    "fingerprint": f"merge-conflict-{t['id']}", "run_id": run_id})
        if n >= self.par.max_merge_conflicts:
            # the question first: it moves the task to `blocked` and sets `blocked_by`; then the note that says why
            self.store.ask(from_="dispatcher", to="po", question=f"Task {t['id']} ({_short_text(t.get('title'), 80)}) conflicted with "
                           f"{self.p.repo_branch} {n} times in a row in {_short_text(', '.join(files), 200)}: another task keeps changing the same "
                           f"lines. Its work is on the local branch task/{t['id']}. Order the two tasks with `depends_on`, narrow one of them, or close it.",
                           task_id=t["id"], blocks=[t["id"]])
            self.store.transition(t["id"], "blocked", "dispatcher", f"{note}; {n} conflict(s) in a row: the PO decides", force=True,
                                  assignee_run=None, **{MERGE_CONFLICT: rec})
            return
        if rerun:
            self.store.transition(t["id"], "ready", "dispatcher", f"{note}: runs again on the new {self.p.repo_branch}", force=True,
                                  assignee_run=None, **{MERGE_CONFLICT: rec})
        else:
            self.store.transition(t["id"], "in_progress", "dispatcher", f"{note}: back to the {role} to reconcile", force=True,
                                  assignee_run=None, last_review=None, **{MERGE_CONFLICT: rec})

    # stuck goals: replan with 3 alternatives, probe them, then the owner (owner 11.11, team/replan.py) ----------------

    def replan_cfg(self) -> dict[str, Any]:
        return replan.settings(self.st.raw.get("replan"))

    def guard_replan(self, t: dict[str, Any]) -> bool:
        """True = the task was stopped before a run: its goal failed `replan.threshold` times over the task chain, so the
        PO replans it (first time) or the owner gets it (stuck again after a replan)."""
        rp = t.get("replan") or {}
        if rp.get("state") == "exhausted" and t.get("state") == "ready":
            # the owner (or the deputy) answered: the developer gets the answer and no further automatic replan
            q = self.store.questions()
            ans = next((x for x in q if x.get("id") == rp.get("question")), None)
            t["replan"] = {**rp, "state": "owner"}
            if ans and ans.get("answer"):
                t["scope"] = f"{t.get('scope') or ''}\n\nOWNER'S ANSWER ({ans.get('answered_by')}) after the replan: {ans['answer']}"[:3000]
            self.store.save_task(t)
            return False
        cfg = self.replan_cfg()
        what = replan.needs_attention(self.store, t, int(cfg["threshold"]))
        if what is None:
            return False
        n = replan.chain_failures(self.store, t)
        if what == "replan":
            self.store.transition(t["id"], "blocked", "dispatcher", f"stuck: {n} failed attempts on this goal; the PO replans it with 3 alternatives",
                                  force=True, assignee_run=None, replan={"state": "asked", "at": utcnow(), "failures": n})
            self.store.event("task.replan_asked", t["id"], "dispatcher", {"failures": n})
        else:
            hand = ""
            if replan.is_framework_fix(t):
                hand = f"; framework-fix task: hand-apply the change to the file named in its scope ({_short_text(t.get('scope'), 160)})"
            self.escalate_stuck(t, f"stuck again after a replan on this chain ({n} failed attempts){hand}", n)
        return True

    def escalate_stuck(self, t: dict[str, Any], why: str, n: int, probes: list[dict[str, Any]] | None = None) -> None:
        rp = dict(t.get("replan") or {})
        lines = "\n".join(f"- {x}" for x in replan.tried(self.store, t, int(self.replan_cfg()["tried_max"])))
        ptxt = "\n".join(f"- {p['id']} {p.get('title')}: {_short_text((p.get('results') or [{}])[-1].get('summary'), 200)}" for p in probes or [])
        question = (f"Task {t['id']} ({_short_text(t.get('title'), 120)}) is stuck: {why}. "
                    + (f"PO diagnosis: {_short_text(rp.get('diagnosis'), 300)} " if rp.get("diagnosis") else "")
                    + (f"Probes that did not work:\n{ptxt}\n" if ptxt else "")
                    + f"Tried:\n{lines}\nDecide: a direction for the developer, a different source, or close the goal.")
        t = self.store.transition(t["id"], "blocked", "dispatcher", f"stuck: {why}", force=True, assignee_run=None,
                                  replan={**rp, "state": "exhausted", "failures": n, "at": utcnow()})
        q = self.store.ask(from_="dispatcher", to="owner", question=question, task_id=t["id"])
        t = self.store.task(t["id"]) or t
        t["replan"] = {**t.get("replan", {}), "question": q["id"]}
        self.store.save_task(t)
        self.store.event("task.replan_exhausted", t["id"], "dispatcher", {"question": q["id"]})

    def apply_replans(self, items: list[dict[str, Any]]) -> list[str]:
        """The PO's `replans`: three alternatives per stuck task become three probe tasks (developer, no commit)."""
        cfg, notes = self.replan_cfg(), []
        for rp in items or []:
            t = self.store.task(str(rp.get("task_id")))
            if not t or (t.get("replan") or {}).get("state") != "asked":
                continue
            problems = replan.validate(rp)
            if problems:
                notes.append(f"{t['id']}: замена не принята ({'; '.join(problems)})")
                continue
            ids = []
            for i, a in enumerate(rp["alternatives"][:3]):
                pt = self.store.create_task(
                    title=f"Probe {i + 1}/3 for {t['id']}: {a.get('title') or a.get('approach')}"[:140], type_="research",
                    problem=f"Stuck goal {t['id']}: {_short_text(t.get('problem'), 400)}\nPO diagnosis: {_short_text(rp.get('diagnosis'), 300)}",
                    scope=(f"PROBE, not an implementation. Approach: {_short_text(a.get('approach'), 400)}\nHow it differs from what was tried: "
                           f"{_short_text(a.get('differs_from_tried'), 300)}\nRun the check, do not change or commit product code."),
                    acceptance=[f"The check answers whether this approach gives what {t['id']} needs: {_short_text(a.get('probe_check'), 240)}",
                                "Evidence: the exact command or URL and the output that decides it"],
                    goal=str(t.get("goal") or ""), created_by="po", priority=str(t.get("priority") or "P1"), size="XS", role="developer",
                    state="ready", budget_usd=float(cfg["probe_budget_usd"]), links=[t["id"]], note=f"replan probe for {t['id']}")
                pt["probe_of"] = t["id"]
                self.store.save_task(pt)
                ids.append(pt["id"])
            t["replan"] = {**t["replan"], "state": "probing", "diagnosis": _short_text(rp.get("diagnosis"), 600),
                           "alternatives": rp["alternatives"][:3], "probes": ids}
            self.store.save_task(t)
            self.store.event("task.replan_probing", t["id"], "po", {"probes": ids})
            notes.append(f"{t['id']} застряла → пробы {', '.join(ids)}")
        return notes

    def finish_probe(self, t: dict[str, Any], res: dict[str, Any]) -> bool:
        works = replan.probe_verdict(res)
        self.store.transition(t["id"], "done", "dispatcher", f"probe {'works' if works else 'does not work'}: {_short_text(res.get('summary'), 300)}",
                              force=True, assignee_run=None, probe_works=works)
        return True

    def settle_replans(self) -> None:
        """Stuck tasks whose probes all finished: a working probe → the PO creates the implementation task; none → the owner."""
        for t in self.store.tasks(["blocked", "waiting_owner"]):
            rp = t.get("replan") or {}
            if rp.get("state") != "probing":
                continue
            probes = [p for p in (self.store.task(i) for i in rp.get("probes") or []) if p]
            if any(p.get("state") not in ("done", "rejected", "accepted", "blocked", "waiting_owner") for p in probes):
                continue
            works = [p for p in probes if p.get("probe_works")]
            if not works:
                self.escalate_stuck(t, "none of the 3 probes of the PO's replan worked", int(rp.get("failures") or 0), probes)
                continue
            best = works[0]
            ev = _short_text((best.get("results") or [{}])[-1].get("summary"), 500)
            t["replan"] = {**rp, "state": "done", "winner": best["id"]}
            self.store.save_task(t)
            self.store.transition(t["id"], "rejected", "dispatcher", f"replanned: probe {best['id']} works; the PO creates the implementation task", force=True)
            self.store.ask(from_="dispatcher", to="po", question=(
                f"Stuck goal {t['id']} ({_short_text(t.get('title'), 120)}): probe {best['id']} worked — {_short_text(best.get('scope'), 300)} "
                f"Evidence: {ev} Create ONE implementation task (role developer) for this approach with `continues: [\"{t['id']}\"]`, "
                f"keeping the original acceptance: {'; '.join(_short_text(a, 120) for a in t.get('acceptance') or [])}"))
            self.store.event("task.replan_done", t["id"], "dispatcher", {"winner": best["id"]})
    def open_question_blocks(self, t: dict[str, Any]) -> bool:
        """T-0091: True while the question the task was blocked on (`blocked_by`) is still open."""
        qid = str(t.get("blocked_by") or "")
        return qid.startswith("Q-") and any(q["id"] == qid for q in self.store.questions(["open"]))

    def guard_no_commit_loop(self, t: dict[str, Any]) -> bool:
        """T-0046: the single check for «never run the developer on a task with ≥2 no-commit records», whatever state (ready,
        in_progress, claimed) or missing role the task was re-readied to. True = the task was stopped and must not be executed."""
        if (t.get("role") or "developer") == "developer" and no_commit_count(t) >= 2:
            self.stop_no_commit_loop(t)
            return True
        return False

    def stop_no_commit_loop(self, t: dict[str, Any]) -> None:
        """T-0043: a developer task that already came back with no commit twice (counted over its whole history) and was re-readied
        is not started again: a title `Check: …` becomes a `check` task for the analyst, anything else goes back to `blocked`."""
        if str(t.get("title") or "").lstrip().lower().startswith("check:"):
            t["type"], t["role"] = "check", "analyst"
            self.store.save_task(t)
            self.store.transition(t["id"], "ready", "dispatcher", "title starts with 'Check:' after repeated no-commit runs: routed to the analyst as type check", force=True)
            return
        self.store.transition(t["id"], "blocked", "dispatcher", "re-readied after repeated developer runs that committed nothing: not started; name the file to change or close it", force=True, assignee_run=None)
        self.store.ask(from_="dispatcher", to="po", question=f"Task {t['id']} returned 'done' without a commit {no_commit_count(t)} times and was re-readied. "
                       "Name the file the developer must change, or make it a check task for the analyst, or close it.", task_id=t["id"], blocks=[t["id"]])

    def execute_task(self, t: dict[str, Any]) -> bool:
        if self.guard_replan(t) or self.guard_no_commit_loop(t):
            return False
        role = str(t.get("role") or "developer")
        if self.pending_role(t):
            return False
        run_id = f"{self.cycle_id}-exec-{t['id']}"
        if not self.store.claim(t["id"], str(t.get("role")), run_id):
            return False
        if t["state"] == "ready":
            self.store.transition(t["id"], "claimed", "dispatcher", run_id, assignee_run=run_id)
            self.store.transition(t["id"], "in_progress", "dispatcher", "started", assignee_run=run_id)
        else:
            t["assignee_run"] = run_id
            self.store.save_task(t)
        try:
            if role == "developer":
                return self.exec_developer(t, run_id)
            if role == "designer":                    # owner 20.11: same path as the developer, its own card and model
                return self.exec_developer(t, run_id, role="designer")
            if role == "game-designer":
                return self.exec_game_designer(t, run_id)
            if role == "tester":                      # slice 6: a load task — the tester plays N clients against a threshold
                return self.exec_tester(t, run_id)
            if role == "marketer":
                return self.exec_marketer(t, run_id)
            if role == "analyst":
                return self.exec_analyst_task(t, run_id)
            self.store.transition(t["id"], "blocked", "dispatcher", f"unknown role {role}", force=True)
            return True
        finally:
            self.store.release(t["id"])

    # a role without what the task needs: a question to the PO instead of a guess (owner 26.11, rework slice 2a) -------

    def context_result(self, res: dict[str, Any]) -> dict[str, Any]:
        """One meaning for «I cannot go on without …», whichever way the role said it. With `cycle.escalation.needs_context`
        on: `needs_context`, and the old `blocked` + `question`, both become `needs_context` with the list `missing`. Off:
        `needs_context` becomes the old `blocked` + `question`, so the result is handled exactly as before."""
        status = str(res.get("status") or "")
        missing = [" ".join(str(m).split())[:300] for m in (res.get("missing") or []) if str(m).strip()][:NEEDS_CONTEXT_ITEMS]
        if self.p.escalation_cfg["needs_context"]:
            if status == NEEDS_CONTEXT or (status == "blocked" and (missing or str(res.get("question") or "").strip())):
                return {**res, "status": NEEDS_CONTEXT,
                        "missing": missing or [" ".join(str(res.get("question") or res.get("summary") or "").split())[:300]]}
            return res
        if status == NEEDS_CONTEXT:
            return {**res, "status": "blocked", "question": str(res.get("question") or "; ".join(missing) or res.get("summary") or "")[:800]}
        return res

    def ask_for_context(self, t: dict[str, Any], role: str, res: dict[str, Any]) -> dict[str, Any]:
        """The role's `missing` list → ONE question to the PO that blocks the task until it is answered. The attempt is
        not a failure: nothing is added to `attempts`, and `needs_context` is not among `replan.FAILED_STATUSES`."""
        missing = [str(m) for m in (res.get("missing") or [])][:NEEDS_CONTEXT_ITEMS]
        question = (f"Task {t['id']} ({_short_text(t.get('title'), 100)}): the {role} stopped without guessing. It lacks:\n"
                    + "\n".join(f"- {m}" for m in missing)
                    + "\nAnswer every point from VISION, the design documents and the owner's recorded decisions: your answer is "
                      f"shown to the {role} when the task runs again. A point only the owner can decide goes into `owner_questions` "
                      f"with `task_id: {t['id']}` (the task then waits for the owner); never answer such a point with a guess.")
        q = self.store.ask(from_=role, to="po", question=question[:1500], task_id=t["id"], blocks=[t["id"]])
        self.store.event(NEEDS_CONTEXT_EVENT, t["id"], role, {"question": q["id"], "missing": missing})
        log.info("%s: %s: %s needs context (%d point(s)) → %s to the PO, the task waits", self.p.slug, t["id"], role, len(missing), q["id"])
        return q

    def task_answers(self, t: dict[str, Any]) -> list[dict[str, Any]]:
        """Answered questions this task was stopped on, oldest first — for the role's prompt. Empty while the switch is off."""
        if not self.p.escalation_cfg["needs_context"]:
            return []
        qs = [q for q in self.store.questions(["answered"]) if t["id"] in (q.get("blocks") or []) or q.get("task") == t["id"]]
        return sorted(qs, key=lambda q: str(q.get("answered") or ""))

    def exec_developer(self, t: dict[str, Any], run_id: str, role: str = "developer") -> bool:
        clone = self.p.repo_dir
        with self.repo_lock:
            if self.p.repo_url:
                gitops.ensure_clone(clone, self.p.repo_url, self.p.repo_branch)
            elif not (clone / ".git").exists():
                if not self.ensure_local_repo():
                    self.store.transition(t["id"], "blocked", "dispatcher", "no repository configured", force=True)
                    return True
            # the task's own checkout (owner 21.11): the developer and the designer of one product work at the same time,
            # each in a worktree of its task; the clone itself stays on the base branch for the merges
            repo, branch = self.task_checkout(t)
        conflict = t.get(MERGE_CONFLICT) if isinstance(t.get(MERGE_CONFLICT), dict) else None
        # a service repository: two roles may each start the server in their own checkout at the same moment — each run is
        # told a port of its own (TEAM_PORT; not PORT itself, which would silently move a server the repository's own
        # test configuration expects elsewhere)
        port = playtest.free_port() if (self.p.repo_kind == "service" or self.p.profile == "game") else None
        prompt = prompts.developer_prompt(self.p, t, repo, branch, t.get("results") or [], t.get("last_review"), role=role,
                                          budget_s=int(self.p.run_limits(role)["timeout_s"]), continuation=self.continuation_context(t, repo),
                                          conflict=conflict, base=self.p.repo_branch, port=port, answers=self.task_answers(t))
        cap = float(t.get("budget_usd") or self.st.budget["max_usd_per_run"])
        rr = self.role_run(role, prompt, "task-result", cwd=repo, label=f"{'dev' if role == 'developer' else role}-{t['id']}", cap=cap,
                           allowed_tools=["Bash", "Read", "Edit", "Write", "Glob", "Grep"],
                           env_extra={RUN_PORT_ENV: str(port)} if port else None)
        if rr is None:
            self.store.transition(t["id"], "ready", "dispatcher", "no run (budget or dry-run)", force=True, assignee_run=None)
            return False
        if not (rr.ok and rr.structured):
            if self.continue_interrupted(t, rr, run_id, repo, role):
                return True                                 # owner 21.11: stopped by a limit → wip commit, the next cycle continues
            attempts = int(t.get("attempts") or 0) + 1
            note = f"run failed: {rr.error}"
            self.store.add_result(t["id"], {"run_id": run_id, "status": "error", "summary": note[:400]})
            if attempts >= 3:
                self.store.transition(t["id"], "blocked", "dispatcher", note[:300], force=True, attempts=attempts)
            else:
                self.store.transition(t["id"], "ready", "dispatcher", note[:300], force=True, attempts=attempts, assignee_run=None)
            return True
        res = self.context_result(rr.structured)
        res["run_id"] = run_id
        if res.get("status") == "partial" and not replan.is_probe(t):
            try:                                        # uncommitted work of a partial result survives the next attempt's reset
                wip = self.save_wip(t, repo, "partial", run_id)
            except gitops.GitError as exc:
                log.warning("%s: %s partial result — uncommitted work not saved: %s", self.p.slug, t["id"], gitops.redact(str(exc))[:300])
                wip = None
            if wip:
                res["wip_commit"], res["wip_files"] = wip["commit"], wip["files"]
        res["commit"] = gitops.head(repo)
        self.store.add_result(t["id"], res)
        if replan.is_probe(t):
            return self.finish_probe(t, res)            # a probe never merges: its answer goes to the stuck task
        for pr in res.get("proposals") or []:
            self.store.add_observation({"title": str(pr)[:140], "classification": "need_hypothesis", "source": "developer", "task": t["id"],
                                        "actual": str(pr)[:400], "url": self.p.site_base, "fingerprint": fingerprint("dev", "proposal", "", str(pr))})
        if res.get("status") == NEEDS_CONTEXT:
            self.ask_for_context(t, role, res)
            return True
        if res.get("status") == "blocked":
            self.store.ask(from_="developer", to="po", question=str(res.get("question") or res.get("summary"))[:1200], task_id=t["id"])
            return True
        if res.get("status") == "partial":
            if repeated_partial(t, res):
                # the same partial twice in a row = the task is stuck, not progressing (release-radar T-0033 burned three cycles
                # on a report that did not exist); the PO re-scopes or closes it
                self.store.transition(t["id"], "blocked", "dispatcher", "partial repeated with the same summary: stuck", force=True, assignee_run=None)
                self.store.ask(from_="developer", to="po", question=f"Task {t['id']} returned the same partial result twice: "
                               f"{_short_text(res.get('summary'), 300)} Re-scope it, split it, or close it.", task_id=t["id"], blocks=[t["id"]])
                return True
            self.store.transition(t["id"], "in_progress", "dispatcher", "partial — continues next cycle", force=True, last_review=None)
            return True
        ahead = gitops.commits_ahead(repo, self.p.repo_branch) or (1 if gitops.dirty(repo) else 0)
        if ahead == 0 and str(res.get("no_change_reason") or "").strip() and any(
                isinstance(c, dict) and c.get("met") and str(c.get("evidence") or "").strip() for c in res.get("self_check") or []):
            # T-0041: «already satisfied» with a reason and evidence is a result, not a failed run: no no_commit record
            reason = _short_text(res.get("no_change_reason"), 300)
            sha = gitops.head(repo)
            if t.get("type") == "check":
                self.store.transition(t["id"], "accepted", "dispatcher", f"no change needed: {reason}", force=True, accepted_at=utcnow(), assignee_run=None)
            else:
                self.store.transition(t["id"], "review", "dispatcher", f"no change needed: {reason}", force=True, released_at=utcnow(),
                                      released_sha=sha, last_review=None, assignee_run=None)
            return True
        if ahead == 0:
            attempts = int(t.get("attempts") or 0) + 1
            checks = "; ".join(f"{'✓' if c.get('met') else '✗'} {_short_text(c.get('criterion'), 60)}: {_short_text(c.get('evidence'), 80)}"
                               for c in (res.get("self_check") or [])[:3] if isinstance(c, dict))
            note = f"developer reported done but committed nothing — summary: {_short_text(res.get('summary'), 300)}" + (f" | self_check: {checks}" if checks else "")
            if previous_no_commit(t):
                # the second no-commit in a row = the task cannot be finished as written (F-0103..F-0109): stop, ask the PO to re-scope
                self.store.transition(t["id"], "blocked", "dispatcher", note, force=True, attempts=attempts, assignee_run=None)
                self.store.ask(from_="developer", to="po", question=f"Task {t['id']} came back 'done' twice without a single commit. "
                               f"Last summary: {_short_text(res.get('summary'), 300)} Re-scope it, split it, or close it.", task_id=t["id"], blocks=[t["id"]])
                return True
            self.store.transition(t["id"], "ready", "dispatcher", note, force=True, attempts=attempts, assignee_run=None)
            return True
        if self.p.repo_mode == "pr":
            return self.release_as_pull_request(t, repo, res)
        if self.review_first(t):
            return self.hold_for_review(t, repo)
        self.merge_task(t, repo, role, run_id)
        return True

    def merge_task(self, t: dict[str, Any], repo: Path | None, role: str, run_id: str, *, reviewed: str = "") -> bool:
        """task/<id> → the base branch, push, deploy. `reviewed` (rework slice 5): the task passed its review BEFORE the
        merge — it becomes `accepted` here (with the reviewer's words) instead of `released`. True = merged."""
        clone = self.p.repo_dir
        # merge → push → deploy is ONE critical section of the product (team/lanes.py invariant 5): two lanes that finish
        # together merge one after the other, and the host always ends on the last merged commit
        with self.repo_lock:
            try:
                sha = gitops.merge_and_push(clone, t["id"], self.p.repo_branch, worktree=repo if repo is not None and Path(repo).exists() else None)
            except gitops.MergeConflict as exc:
                self.merge_conflict_back(t, exc, role, run_id)
                return False
            except gitops.GitError as exc:
                self.store.transition(t["id"], "blocked", "dispatcher", f"merge/push failed: {gitops.redact(str(exc))[:300]}", force=True)
                return False
            clear = {MERGE_CONFLICT: None} if t.get(MERGE_CONFLICT) else {}
            if reviewed:
                self.store.transition(t["id"], "accepted", "reviewer", f"reviewed before the merge, merged {sha[:10]}: {reviewed}"[:300], force=True,
                                      released_at=utcnow(), released_sha=sha, accepted_at=utcnow(), last_review=None, **{PRE_MERGE: None}, **clear)
            else:
                self.store.transition(t["id"], "released", "dispatcher", f"merged {sha[:10]}", force=True, released_at=utcnow(), released_sha=sha,
                                      last_review=None, **clear)
            self.store.event("worktree.removed", t["id"], "dispatcher", {"state": "accepted" if reviewed else "released", "path": str(repo)})
            self.deploy_after_merge(t, sha)
        return True

    # a visible change is reviewed before it is merged and deployed (owner 26.11, rework slice 5) -----------------------

    def review_first(self, t: dict[str, Any]) -> bool:
        """`cycle.visible.review_before_merge` and the task carries the PO's mark `visible`: its commit stays on the task
        branch until a review passes it. Only where the dispatcher merges itself (`repo.mode: merge`); never a probe."""
        return bool(self.p.visible_cfg["review_before_merge"] and t.get("visible") and self.p.repo_mode != "pr" and not replan.is_probe(t))

    def hold_for_review(self, t: dict[str, Any], repo: Path) -> bool:
        """The author reported done: nothing is merged. Leftovers are committed, the task goes to `review` with
        `released_sha` = the head of task/<id> and the mark `pre_merge`; the review phase plays that commit on a server it
        starts locally (`review_task`), a pass merges it (`merge_task(reviewed=…)`), a fail returns it to the author."""
        with self.repo_lock:
            if gitops.dirty(repo):
                gitops.git(repo, "add", "-A")
                gitops.git(repo, "commit", "-q", "-m", f"{t['id']}: leftover changes committed by the dispatcher")
            sha = gitops.head(repo)
        self.store.transition(t["id"], "review", "dispatcher", f"visible change: reviewed before the merge ({sha[:10]} on task/{t['id']})", force=True,
                              released_at=utcnow(), released_sha=sha, last_review=None, assignee_run=None, **{PRE_MERGE: True})
        self.store.event(REVIEW_BEFORE_MERGE_EVENT, t["id"], "dispatcher", {"sha": sha, "branch": f"task/{t['id']}"})
        log.info("%s: %s is a visible change: not merged, waits for its review on %s", self.p.slug, t["id"], sha[:10])
        return True

    # run continuation: a run stopped by the time limit is continued, not restarted (owner 21.11, frictions F23/F30) ------

    def save_wip(self, t: dict[str, Any], repo: Path, reason: str, run_id: str) -> dict[str, Any] | None:
        """Commit whatever the run left in the working tree of the task's checkout `repo` to `task/<id>` as a service
        commit and record `run.wip_saved`. Returns `{commit, files, stat}`; None when the tree was clean. The commit
        stays on the task branch, and the checkout (the task's worktree) is kept as it is — the next run continues in it:
        nothing is pushed, merged or deployed here — only a later normal run releases the task.
        Raises gitops.GitError when the work could not be saved (wrong branch, git failure)."""
        wip = gitops.save_wip(repo, t["id"], WIP_WHY.get(reason, f"{reason}, continues next cycle"))
        if wip:
            self.store.event("run.wip_saved", t["id"], "dispatcher", {"run": run_id, "files": wip["files"], "commit": wip["commit"],
                                                                      "branch": f"task/{t['id']}", "reason": reason})
        return wip

    def continue_interrupted(self, t: dict[str, Any], rr: RunResult, run_id: str, repo: Path, role: str) -> bool:
        """A repository role's run that the dispatcher stopped (hard time limit or inactivity watchdog): the unfinished work
        is saved as a wip commit on the task branch and the task stays `in_progress`, so the next cycle CONTINUES it (the
        prompt names the commit and the run profile and forbids starting over). After `cycle.max_continuations`
        continuations the task is blocked with a question to the PO that carries the profiles of the stopped runs, so
        the PO splits it instead of burning more runs. Returns False when this is not such a run or the work could not be
        saved — the caller then handles it as any failed run."""
        kind = killed_kind(rr)
        if kind is None or role not in CONTINUE_ROLES:
            return False
        try:
            wip = self.save_wip(t, repo, kind, run_id)
        except (gitops.GitError, OSError, subprocess.SubprocessError) as exc:
            log.error("%s: %s — unfinished work of run %s NOT saved: %s", self.p.slug, t["id"], run_id, gitops.redact(str(exc))[:300])
            self.store.event("run.wip_failed", t["id"], "dispatcher", {"run": run_id, "error": gitops.redact(str(exc))[:300]})
            return False
        prof = self.last_profile
        secs = int(round(rr.duration_s or 0))
        limit_s = int(self.p.run_limits(role)["timeout_s"])
        what = runprofile.KILLED_TEXT.get(kind, kind).replace("killed by", "stopped by")
        summary = (f"run {what} after {secs} s; "
                   + (f"{wip['files']} file(s) of unfinished work saved on task/{t['id']} as wip commit {wip['commit'][:10]}" if wip
                      else "the working tree was clean, nothing uncommitted to save"))
        result = {"run_id": run_id, "role": role, "status": INTERRUPTED, "killed": kind, "summary": summary, "duration_s": secs, "limit_s": limit_s}
        if wip:
            result.update(wip_commit=wip["commit"], wip_files=wip["files"])
        if prof:
            result.update(profile_line=runprofile.line(prof, role=role, task=t["id"]), profile_md=runprofile.render(prof))
        self.store.add_result(t["id"], result)
        used, allowed = int(t.get("continuations") or 0), self.p.max_continuations
        if used >= allowed:
            log.warning("%s: %s stopped by a limit again after %d continuation(s) — blocked, the PO splits it", self.p.slug, t["id"], used)
            # the question first: it moves the task to `blocked` and sets `blocked_by`; then the note that says why
            self.store.ask(from_="dispatcher", to="po", question=self.split_question(t["id"], limit_s), task_id=t["id"], blocks=[t["id"]])
            self.store.transition(t["id"], "blocked", "dispatcher", f"{summary}; {used} continuation(s) used (max {allowed}): the PO splits it",
                                  force=True, assignee_run=None)
            self.store.event("task.continuations_exhausted", t["id"], "dispatcher", {"continuations": used, "max": allowed})
            return True
        log.info("%s: %s %s — continuation %d of %d next cycle", self.p.slug, t["id"], summary, used + 1, allowed)
        self.store.transition(t["id"], "in_progress", "dispatcher", f"{summary} — continues next cycle (continuation {used + 1}/{allowed})",
                              force=True, continuations=used + 1, assignee_run=None)
        return True

    def split_question(self, task_id: str, limit_s: int) -> str:
        """The question that blocks a task after its last allowed continuation: what the stopped runs spent their time on."""
        t = self.store.task(task_id) or {"id": task_id}
        stopped = [r for r in t.get("results") or [] if r.get("status") == INTERRUPTED]
        shown = stopped[-QUESTION_PROFILES_MAX:]
        ahead = gitops.git(self.p.repo_dir, "rev-list", "--count", f"{self.p.repo_branch}..task/{task_id}", check=False) or "?"
        head = (f"Task {task_id} ({_short_text(t.get('title'), 80)}) was stopped by the run time limit {len(stopped)} times "
                f"(limit {limit_s} s per run) and is not finished: it is too big for one run. Split it into smaller tasks or cut its "
                f"scope, then close it; do not re-ready it unchanged. The unfinished work is on the local branch task/{task_id} "
                f"({ahead} commit(s) ahead of {self.p.repo_branch}): a successor names it in `continues` and its scope starts with "
                f"«run `git merge task/{task_id}` first». Where the time went ("
                + (f"last {len(shown)} of {len(stopped)} stopped runs; " if len(stopped) > len(shown) else "")
                + f"full profiles: `team task {self.p.slug} show --id {task_id}`):")
        per_line = max(60, min(QUESTION_PROFILE_CHARS, (QUESTION_MAX_CHARS - len(head)) // max(1, len(shown)) - 3))   # the lines must fit
        return head + "".join(f"\n- {_short_text(r.get('profile_line') or r.get('summary'), per_line)}" for r in shown)

    def continuation_context(self, t: dict[str, Any], repo: Path) -> dict[str, Any] | None:
        """What the next prompt of a continued task says about the earlier run: the wip commit and its `--stat`, the run
        profile, how many continuations are left. None when the task's latest attempt was not cut short."""
        last = None
        for r in reversed(t.get("results") or []):
            if r.get("status") == "done":
                break                                   # a normal run came after: nothing to continue
            if r.get("status") == INTERRUPTED or r.get("wip_commit"):
                last = r
                break
        if last is None:
            return None
        sha = str(last.get("wip_commit") or "")
        held = bool(sha) and gitops.is_ancestor(repo, sha)
        if sha and not held:
            log.warning("%s: %s wip commit %s is not on the task branch any more", self.p.slug, t["id"], sha[:10])
        return {"checkout": str(repo),
                "reason": str(last.get("killed") or last.get("status") or ""), "duration_s": last.get("duration_s"), "limit_s": last.get("limit_s"),
                "wip_commit": sha if held else "", "wip_lost": bool(sha) and not held, "wip_files": last.get("wip_files"),
                "stat": gitops.commit_stat(repo, sha) if held else "", "profile_md": str(last.get("profile_md") or ""),
                "used": int(t.get("continuations") or 0), "max": self.p.max_continuations,
                "ahead": gitops.commits_ahead(repo, self.p.repo_branch), "base": self.p.repo_branch}

    def exec_tester(self, t: dict[str, Any], run_id: str) -> bool:
        """A tester task (type `load`, slice 6): the dispatcher starts the server from the repository's HEAD (or uses the
        deployed host), the tester runs the repository's load script through `team-play load`, reads metrics and the log,
        and returns a `test-report`. pass → `accepted` (the numbers are in the result); fail → `done` with the first missed
        threshold and the tester's observations for the PO; inconclusive or no server → `blocked` with a PO question."""
        repo = self.p.repo_dir
        with self.repo_lock:
            if self.p.repo_url:
                gitops.ensure_clone(repo, self.p.repo_url, self.p.repo_branch)
            has_repo = (repo / ".git").exists()
            if has_repo:
                gitops.park_on_base(repo, self.p.repo_branch)      # the base branch is what is load-tested, never a task branch
                sha = gitops.head(repo)
        if not has_repo:
            self.store.transition(t["id"], "blocked", "dispatcher", "no repository to load-test", force=True)
            self.store.ask(from_="tester", to="po", question=f"Task {t['id']}: there is no repository yet, nothing to load-test. Defer it until the server exists.",
                           task_id=t["id"], blocks=[t["id"]])
            return True
        work = self.p.work_dir / self.cycle_id / f"load-{t['id']}"
        out_dir = self.p.dir / "reviews" / t["id"]
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            server = playtest.start(self.p, repo, sha, work, lock=self.repo_lock)
        except (playtest.PlaytestError, gitops.GitError, OSError) as exc:
            reason = str(exc)[:1200]
            self.store.event("playtest.failed", t["id"], "dispatcher", {"reason": reason[:400], "load": True})
            self.store.add_observation({"title": f"Server could not start for load task {t['id']}: {reason.splitlines()[0][:90]}",
                                        "classification": "environment_error", "source": "dispatcher", "task": t["id"], "url": self.p.site_base,
                                        "actual": reason[:400], "fingerprint": f"load-failed-{t['id']}", "run_id": run_id})
            self.store.transition(t["id"], "blocked", "dispatcher", f"server not started: {reason.splitlines()[0][:200]}", force=True, assignee_run=None)
            self.store.ask(from_="tester", to="po", question=f"Load task {t['id']}: the dispatcher could not start the server from {sha[:10]}: "
                           f"{reason.splitlines()[0][:300]}. Fix the run_cmd/health (developer task) or defer the load test.", task_id=t["id"], blocks=[t["id"]])
            return True
        hosts = self._allowed_hosts("tester").split(",") + [playtest.HOST]
        env = {"TEAM_PLAY_CWD": str(server.dest), "TEAM_PLAY_BASE_URL": server.base_url, "TEAM_PLAY_OUT": str(out_dir),
               "TEAM_PLAY_SERVER_LOG": str(server.log_path or ""), "TEAM_ALLOWED_HOSTS": ",".join(dict.fromkeys(h for h in hosts if h))}
        prompt = prompts.load_prompt(self.p, t, sha, base_url=server.base_url, local=server.local, checkout=server.dest, out_dir=out_dir,
                                     server_notes=server.notes)
        self.store.event("playtest.started", t["id"], "dispatcher", {"base_url": server.base_url, "local": server.local, "load": True})
        cap = float(t.get("budget_usd") or TESTER_CAP_USD)
        try:
            with self.lanes.host.shared(not server.local):      # no deploy switches the live host under the load test
                rr = self.role_run("tester", prompt, "test-report", cwd=work, label=f"load-{t['id']}", cap=cap, max_turns=TESTER_MAX_TURNS,
                                   allowed_tools=list(TESTER_TOOLS), env_extra=env)
        finally:
            playtest.stop(server)
            self.drop_review_dirs(t["id"], labels=("load",))
        if rr is None:
            self.store.transition(t["id"], "ready", "dispatcher", "no run (budget or dry-run)", force=True, assignee_run=None)
            return False
        if not (rr.ok and rr.structured):
            attempts = int(t.get("attempts") or 0) + 1
            self.store.add_result(t["id"], {"run_id": run_id, "status": "error", "summary": f"run failed: {rr.error}"[:400]})
            self.store.transition(t["id"], "blocked" if attempts >= 3 else "ready", "dispatcher", f"run failed: {rr.error}"[:300], force=True,
                                  attempts=attempts, assignee_run=None)
            return True
        rep = rr.structured
        verdict = str(rep.get("verdict") or "inconclusive")
        for o in rep.get("observations") or []:
            self.store.add_observation({**o, "source": "tester", "task": t["id"], "run_id": run_id,
                                        "fingerprint": fingerprint("tester", o.get("classification", ""), o.get("url", ""), o.get("title", ""))})
        self.store.add_result(t["id"], {"run_id": run_id, "status": "done" if verdict != "inconclusive" else "blocked", "verdict": verdict,
                                        "summary": str(rep.get("summary") or "")[:1200], "latency_ms": rep.get("latency_ms") or {},
                                        "scenarios": rep.get("scenarios") or [], "criteria": rep.get("criteria") or [],
                                        "first_failure": rep.get("first_failure") or "", "limits": rep.get("limits") or "", "commit": sha,
                                        "friction": rep.get("friction") or []})
        (out_dir / f"load-report-{run_id}.yaml").write_text(yaml.safe_dump(rep, allow_unicode=True, sort_keys=False), encoding="utf-8")
        lat = rep.get("latency_ms") or {}
        note = f"load {verdict}: p50 {lat.get('p50', '?')} p95 {lat.get('p95', '?')} ms — {_short_text(rep.get('summary'), 200)}"
        if verdict == "pass":
            self.store.transition(t["id"], "accepted", "tester", note, force=True, accepted_at=utcnow(), released_sha=sha, assignee_run=None)
        elif verdict == "fail":
            self.store.transition(t["id"], "done", "tester", f"{note} | first failure: {_short_text(rep.get('first_failure'), 200)}", force=True, assignee_run=None)
        else:
            self.store.transition(t["id"], "blocked", "tester", note, force=True, assignee_run=None)
            self.store.ask(from_="tester", to="po", question=f"Load task {t['id']} is inconclusive: {_short_text(rep.get('summary'), 300)} "
                           f"Limits: {_short_text(rep.get('limits'), 200)}. Add the load script (developer task) or re-state the thresholds.",
                           task_id=t["id"], blocks=[t["id"]])
        return True

    def deploy_after_merge(self, t: dict[str, Any], sha: str) -> bool:
        """Slice 5 (owner 20.11): a game product with `deploy.mode: service` gets the merged commit deployed at once
        (deploy-service.sh on the droplet: release, health, nginx switch, rollback on failure). Success → the task stays
        `released` and the tester plays the deployed host. Failure → the live release is untouched (the script never
        switches before health), the task is `blocked` with a PO question and one environment_error observation; the
        deploy log is kept under data/work. `pending` / `git_pull` / dry run deploy nothing. Returns True when deployed.

        With `deploy.batch: true` (owner 25.11) nothing is deployed here: the task is marked `deploy_pending` and the
        batch (`deploy_batch`) takes it — at the end of the execute phase, or earlier when the oldest merged commit has
        waited `deploy.batch_max_wait_min`. Called under `repo_lock`, right after the merge."""
        if self.dry_run or not servicedeploy.enabled(self.p):
            return False
        if self.p.deploy_batch:
            now = utcnow()
            with self.store.lock:
                t = self.store.task(t["id"]) or t
                t[DEPLOY_PENDING], t["deploy_failures"] = now, 0
                self.store.save_task(t)
            self.store.event("deploy.queued", t["id"], "dispatcher", {"sha": sha[:12], "max_wait_min": self.p.deploy_batch_max_wait_min})
            waited = self.deploy_waiting_s()
            if waited >= self.p.deploy_batch_max_wait_min * 60:
                return self.deploy_batch(f"a merged commit waited {waited / 60:.0f} min (deploy.batch_max_wait_min)")
            self._arm_batch_timer(self.p.deploy_batch_max_wait_min * 60 - waited)
            return False
        log_path = self.p.work_dir / self.cycle_id / f"deploy-{t['id']}.log"
        with self.lanes.host.exclusive():         # waits for the play-tests on the live host; none starts while it switches
            res = servicedeploy.run(self.p, sha=sha, log_path=log_path)
        return self._deploy_outcome(t, sha, res, log_path)

    def _deploy_outcome(self, t: dict[str, Any], sha: str, res: "servicedeploy.DeployResult", log_path: Path, extra: dict[str, Any] | None = None) -> bool:
        """What one task's deploy means for the task: live (`deployed_sha`) or blocked with a PO question."""
        if res.ok or res.skipped:
            self.store.event("deploy.ok", t["id"], "dispatcher", {"sha": sha[:12], "url": res.url, "port": res.port, "colour": res.colour,
                                                                  "skipped": res.skipped, "duration_s": round(res.duration_s, 1), **(extra or {})})
            self._mark_deployed(t, sha, res.url)
            return True
        self.store.event("deploy.failed", t["id"], "dispatcher", {"sha": sha[:12], "exit": res.exit_code, "error": res.error[:300], "log": str(log_path), **(extra or {})})
        self.store.add_observation({**servicedeploy.result_card(self.p, t, res), "run_id": f"{self.cycle_id}-deploy",
                                    "fingerprint": fingerprint("deploy", "failed", "", t["id"])})
        self.store.transition(t["id"], "blocked", "dispatcher", f"deploy failed: {res.summary()[:260]}", force=True, **{DEPLOY_PENDING: None})
        self.store.ask(from_="developer", to="po", question=f"Task {t['id']} merged ({sha[:10]}) but did not deploy: {res.summary()[:500]} "
                       f"The previous release stays live. Re-open the task for the developer (build or health path), or ask the owner if the host is the problem "
                       f"(log: {log_path}).", task_id=t["id"], blocks=[t["id"]])
        return False

    def _mark_deployed(self, t: dict[str, Any], sha: str, url: str) -> None:
        with self.store.lock:
            t = self.store.task(t["id"]) or t
            t["deployed_sha"], t["deployed_url"] = sha, url or self.p.site_base
            if t.get(DEPLOY_PENDING):
                t["deployed_at"] = utcnow()
            t.pop(DEPLOY_PENDING, None)
            t.pop("deploy_failures", None)
            self.store.save_task(t)

    # the batch deploy: one switch of the live host per cycle (owner 25.11; team/lanes.py invariant 5) --------------------

    def deploy_pending_tasks(self) -> list[dict[str, Any]]:
        """Merged tasks that wait for the batch deploy, oldest merge first — whatever their state is by now."""
        return sorted((t for t in self.store.tasks() if t.get(DEPLOY_PENDING)), key=lambda t: (str(t.get(DEPLOY_PENDING)), t["id"]))

    def deploy_waiting_s(self) -> float:
        """How long the oldest merged, not yet deployed commit has waited (0 when nothing waits)."""
        stamps = [parse_ts(str(t.get(DEPLOY_PENDING))) for t in self.deploy_pending_tasks()]
        stamps = [s for s in stamps if s]
        return max(0.0, (_now() - min(stamps)).total_seconds()) if stamps else 0.0

    def _arm_batch_timer(self, in_s: float) -> None:
        """The upper bound of the wait holds even when nothing else is merged in a long cycle: one timer per cycle, set
        by the first merge that waits, fires `deploy_batch` in its own thread. The end of the execute phase cancels it."""
        with self._budget_lock:
            if self._batch_timer is not None:
                return

            def fire() -> None:
                lanes_mod._ctx.label = "deploy-batch"
                try:
                    self.deploy_batch("a merged commit waited deploy.batch_max_wait_min")
                except Exception as exc:  # noqa: BLE001 — the end of the execute phase tries again
                    log.exception("%s: batch deploy from the timer failed: %s", self.p.slug, exc)
                finally:
                    self.store.close_thread()
            self._batch_timer = threading.Timer(max(1.0, in_s), fire)
            self._batch_timer.daemon = True
            self._batch_timer.name = f"deploy-batch-{self.p.slug}"
            self._batch_timer.start()
        log.info("%s: batch deploy at the end of the execute phase, or in %.0f min at the latest", self.p.slug, in_s / 60)

    def _cancel_batch_timer(self) -> None:
        with self._budget_lock:
            timer, self._batch_timer = self._batch_timer, None
        if timer is not None:
            timer.cancel()

    def deploy_batch(self, why: str) -> bool:
        """`deploy.batch: true`: deploy the last merged commit once for every task merged since the last deploy.

        Under `repo_lock`, so no merge moves the branch meanwhile: at the moment of the deploy the commit that goes live
        IS the last merged one (team/lanes.py invariant 5), and it holds the work of every waiting task. Success → all of
        them are marked `deployed_sha`; only then are they reviewed (the tester plays the live host). Failure of the host
        or the transport (push, ssh, nginx, timeout) → nothing is blamed: the tasks stay pending and the next batch tries
        again, `DEPLOY_ENV_RETRIES` times. Any other failure, or the retries used up → the existing per-task path finds
        the culprit: every waiting task's own merge commit is deployed in merge order, the ones that go live are live,
        the first that does not is blocked with a PO question exactly as without the batch (the script never switches
        the host before health, so the last good release stays live). Returns True when the host is on the last merge."""
        if self.dry_run or not (servicedeploy.enabled(self.p) and self.p.deploy_batch):
            return False
        with self.repo_lock:
            pending = self.deploy_pending_tasks()
            if not pending:
                return False
            try:
                sha = gitops.git(self.p.repo_dir, "rev-parse", f"refs/heads/{self.p.repo_branch}").strip()
            except gitops.GitError as exc:
                log.error("%s: batch deploy: the last merged commit cannot be read: %s", self.p.slug, gitops.redact(str(exc))[:300])
                return False
            ids = [t["id"] for t in pending]
            waited = self.deploy_waiting_s()
            log_path = self.p.work_dir / self.cycle_id / f"deploy-batch-{datetime.now(timezone.utc).strftime('%H%M%S')}.log"
            log.info("%s: batch deploy of %s for %d task(s) (%s): %s; the oldest merge waited %.0f min", self.p.slug, sha[:12], len(ids),
                     ", ".join(ids), why, waited / 60)
            t0 = time.monotonic()
            with self.lanes.host.exclusive():     # waits for the play-tests on the live host; none starts while it switches
                host_wait = time.monotonic() - t0
                res = servicedeploy.run(self.p, sha=sha, log_path=log_path)
            extra = {"batch": len(ids), "tasks": ids, "waited_s": round(waited), "host_wait_s": round(host_wait, 1), "why": why[:120]}
            if res.ok or res.skipped:
                self.store.event("deploy.ok", self.cycle_id, "dispatcher", {"sha": sha[:12], "url": res.url, "port": res.port, "colour": res.colour,
                                                                           "skipped": res.skipped, "duration_s": round(res.duration_s, 1), **extra})
                for t in pending:
                    self._mark_deployed(t, sha, res.url)
                return True
            tries = 1 + max(int(t.get("deploy_failures") or 0) for t in pending)
            env = res.exit_code in DEPLOY_ENV_EXITS
            self.store.event("deploy.failed", self.cycle_id, "dispatcher", {"sha": sha[:12], "exit": res.exit_code, "error": res.error[:300], "log": str(log_path),
                                                                           "tries": tries, "blamed": not env or tries >= DEPLOY_ENV_RETRIES, **extra})
            if env and tries < DEPLOY_ENV_RETRIES:
                # the host or the way to it, not a commit: the release that is live stays live, the batch waits for the next try
                with self.store.lock:
                    for t in pending:
                        t = self.store.task(t["id"]) or t
                        t["deploy_failures"] = tries
                        self.store.save_task(t)
                self.store.add_observation({**servicedeploy.result_card(self.p, {"id": f"batch of {len(ids)} task(s)"}, res), "task": None,
                                            "run_id": f"{self.cycle_id}-deploy", "fingerprint": fingerprint("deploy", "batch-env", "", self.p.slug),
                                            "detail": f"Tasks waiting for the deploy: {', '.join(ids)}. Try {tries} of {DEPLOY_ENV_RETRIES}; the next cycle tries again."})
                return False
            ok_all = True
            for i, t in enumerate(pending):       # which commit breaks the release: each task's own merge, in merge order
                own = str(t.get("released_sha") or sha)
                one_log = self.p.work_dir / self.cycle_id / f"deploy-{t['id']}.log"
                if own == sha:
                    one, one_log = res, log_path  # this is the commit that just failed: not deployed twice
                else:
                    with self.lanes.host.exclusive():
                        one = servicedeploy.run(self.p, sha=own, log_path=one_log)
                ok_all = self._deploy_outcome(t, own, one, one_log, {"batch": len(ids), "after_failed_batch": sha[:12]}) and ok_all
            return ok_all

    def exec_game_designer(self, t: dict[str, Any], run_id: str) -> bool:
        """Game designer (owner 20.11, game profile): writes GAME-DESIGN.md and balance tables as a `design-spec`; the
        dispatcher commits the files to the repository on the task branch and merges them (released, repo_only → the reviewer
        reads the checkout), or into the product folder when there is no repository yet (accepted). Proposed tasks become
        observations for the PO; questions outside VISION go to the owner."""
        repo = self.p.repo_dir
        with self.repo_lock:                         # the documents are read while no other lane is merging into the clone
            if self.p.repo_url:
                gitops.ensure_clone(repo, self.p.repo_url, self.p.repo_branch)
            has_repo = (repo / ".git").exists() or self.ensure_local_repo()
            if has_repo:
                gitops.park_on_base(repo, self.p.repo_branch)
            gdd_dir = repo if has_repo else self.p.dir
            read_sha = gitops.head(repo) if has_repo else None     # the base commit whose documents the role is shown
            prompt = prompts.game_designer_prompt(self.p, t, gdd_dir, self.store, answers=self.task_answers(t))
            partial = prompts.truncated_design_docs(self.p, gdd_dir, t)     # F18: never overwrite a document the role saw only in part
        cap = float(t.get("budget_usd") or self.st.budget["max_usd_per_run"])
        work = self.p.work_dir / self.cycle_id / f"gd-{t['id']}"
        rr = self.role_run("game-designer", prompt, "design-spec", cwd=work, label=f"gd-{t['id']}", cap=cap, allowed_tools=[], max_turns=8)
        if rr is None:
            self.store.transition(t["id"], "ready", "dispatcher", "no run (budget or dry-run)", force=True, assignee_run=None)
            return False
        if not (rr.ok and rr.structured):
            attempts = int(t.get("attempts") or 0) + 1
            self.store.add_result(t["id"], {"run_id": run_id, "status": "error", "summary": f"run failed: {rr.error}"[:400]})
            self.store.transition(t["id"], "blocked" if attempts >= 3 else "ready", "dispatcher", f"run failed: {rr.error}"[:300], force=True,
                                  attempts=attempts, assignee_run=None)
            return True
        res = rr.structured
        files = [f for f in (res.get("files") or []) if isinstance(f, dict) and f.get("path") and prompts.safe_rel_path(str(f["path"]))]
        refused = [str(f["path"]) for f in files if str(f["path"]) in partial]
        if refused:
            files = [f for f in files if str(f["path"]) not in partial]
            log.warning("%s: %s: game designer's rewrite of %s refused (the prompt carried a truncated copy)", self.p.slug, t["id"], refused)
            self.store.event("design.rewrite_refused", t["id"], "dispatcher", {"paths": refused})
        for q in res.get("open_questions") or []:
            self.store.ask(from_="game-designer", to="owner", question=f"{q.get('question', '')} (why the owner: {q.get('why_owner', '')})"[:1500], task_id=None)
        for pt in res.get("proposed_tasks") or []:
            title = str(pt.get("title") or "")[:140]
            self.store.add_observation({"title": title, "classification": "need_hypothesis", "source": "game-designer", "task": t["id"],
                                        "actual": _y_short(pt), "url": self.p.site_base, "fingerprint": fingerprint("gd", "proposal", "", title)})
        result = {"run_id": run_id, "status": "done" if files else "blocked", "summary": str(res.get("summary") or "")[:1200],
                  "changes": [f["path"] for f in files], "how_to_verify": [], "self_check": [],
                  "balance_changes": res.get("balance_changes") or [], "friction": res.get("friction") or []}
        if not files and self.p.escalation_cfg["needs_context"] and [m for m in (res.get("missing") or []) if str(m).strip()]:
            result = self.context_result({**result, "status": NEEDS_CONTEXT, "missing": res.get("missing")})
            self.store.add_result(t["id"], result)
            self.ask_for_context(t, "game-designer", result)
            return True
        if not files:
            self.store.add_result(t["id"], result)
            self.store.ask(from_="game-designer", to="po", question=f"Task {t['id']}: the game designer returned no files "
                           f"({_short_text(res.get('summary'), 200)}). Re-state what document or table it must produce, or close the task.",
                           task_id=t["id"], blocks=[t["id"]])
            return True
        if has_repo:
            # The files are whole documents written from the base the role READ (`read_sha`): they are committed on a branch
            # that starts there and merged like any task, so a change another lane merged meanwhile is merged with them —
            # or conflicts and the task runs again on the new documents — instead of being silently overwritten.
            with self.repo_lock:
                wt, branch = self.task_checkout(t, start_point=read_sha)
                for f in files:
                    p = wt / f["path"]
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(str(f.get("content") or ""), encoding="utf-8")
                gitops.git(wt, "add", "-A")
                gitops.git(wt, "commit", "-q", "-m", f"{t['id']}: game design — {', '.join(f['path'] for f in files)[:120]}")
                result["branch"], result["commit"] = branch, gitops.head(wt)
                self.store.add_result(t["id"], result)
            # Owner 22.11 (ai-battles day review): the role writes balance tables without running anything and the dispatcher
            # merged them at once — three windows of a red base branch in one day (2 h 20 min), T-0119 turned rockets into
            # bullets. A change of a data file is tested before the merge, outside the repository lock (the tests take
            # minutes); documents (.md) are merged as before.
            data = [str(f["path"]) for f in files if str(f["path"]).lower().endswith(DESIGN_TEST_SUFFIXES)]
            if data and self.p.test_cmd and not self.dry_run:
                report = run_tests_for_review(repo, result["commit"], work / "tests-pre-merge", self.p.test_cmd, lock=self.repo_lock)
                if not tests_passed(report):
                    tail = _short_text(report[-DESIGN_TESTS_TAIL_CHARS:], DESIGN_TESTS_TAIL_CHARS)
                    log.warning("%s: %s: tests fail with the game designer's data change %s: not merged", self.p.slug, t["id"], data)
                    with self.repo_lock:
                        gitops.remove_task_worktree(repo, wt, t["id"], delete_branch=True)     # the files are written again after the fix
                    self.store.event(DESIGN_TESTS_EVENT, t["id"], "dispatcher", {"paths": data, "tail": tail[-400:]})
                    self.store.add_result(t["id"], {"run_id": run_id, "status": "blocked", "changes": [],
                                                    "summary": f"NOT merged: the repository tests fail with this data change ({', '.join(data)[:200]}). {tail}"[:1200]})
                    self.store.ask(from_="dispatcher", to="po", task_id=t["id"], blocks=[t["id"]],
                                   question=f"Task {t['id']}: the game designer's data change ({', '.join(data)[:200]}) breaks the repository tests, so it "
                                            f"was NOT merged. Test output: {tail[-600:]} Create the developer task that makes the code or the tests ready "
                                            "for this data (it lands first), then answer this question to run the designer's task again; or re-state the task.")
                    return True
            with self.repo_lock:
                try:
                    sha = gitops.merge_and_push(repo, t["id"], self.p.repo_branch, worktree=wt)
                except gitops.MergeConflict as exc:
                    gitops.remove_task_worktree(repo, wt, t["id"], delete_branch=True)     # the files are written again on the new base
                    self.merge_conflict_back(t, exc, "game-designer", run_id, rerun=True)
                    return True
                except gitops.GitError as exc:
                    self.store.transition(t["id"], "blocked", "dispatcher", f"merge/push failed: {gitops.redact(str(exc))[:300]}", force=True)
                    return True
                t = self.store.task(t["id"]) or t
                t["repo_only"] = True                    # the deliverable is a document in the repository: the reviewer reads the checkout
                t.pop(MERGE_CONFLICT, None)
                self.store.save_task(t)
                self.store.transition(t["id"], "released", "dispatcher", f"merged {sha[:10]}", force=True, released_at=utcnow(), released_sha=sha, last_review=None)
            return True
        for f in files:                              # no repository yet: the documents live in the product folder until one exists
            p = self.p.dir / f["path"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(str(f.get("content") or ""), encoding="utf-8")
        self.store.add_result(t["id"], result)
        self.store.transition(t["id"], "accepted", "dispatcher", f"design files written to the product folder: {', '.join(f['path'] for f in files)[:200]}",
                              force=True, accepted_at=utcnow())
        return True

    def release_as_pull_request(self, t: dict[str, Any], repo: Path, res: dict[str, Any]) -> bool:
        """repo.mode: pr — the tests of the repository are the reviewer; a green branch becomes a pull request the owner merges
        (waiting_owner → accepted once merged). Used for the framework's own repository: nothing lands there without a human."""
        test_output: str | None = None
        if self.p.test_cmd:
            # the test suite never gets the dispatcher's secrets: a test that posts would post for real (Demo $25, 02.11)
            test_env = {k: v for k, v in os.environ.items() if not k.startswith(SECRET_ENV_PREFIXES)}
            test_env["TEAM_PUBLISH_DRY_RUN"] = "1"
            try:
                cp = subprocess.run(self.p.test_cmd, shell=True, cwd=str(repo), capture_output=True, text=True, timeout=900, env=test_env)
            except subprocess.TimeoutExpired:
                cp = None
            test_output = (cp.stdout + "\n" + cp.stderr) if cp else None
            if cp is None or cp.returncode != 0:
                tail = (cp.stdout + cp.stderr)[-600:] if cp else "tests timed out"
                attempts = int(t.get("attempts") or 0) + 1
                self.store.add_result(t["id"], {"run_id": res.get("run_id"), "status": "tests_failed", "summary": tail})
                self.store.transition(t["id"], "blocked" if attempts >= 2 else "ready", "dispatcher", f"tests failed: {tail[:300]}", force=True,
                                      attempts=attempts, assignee_run=None)
                return True
        fw = dict(self.st.raw.get("framework") or {})
        touched = gitops.changed_files(repo, self.p.repo_branch)
        protected = [f for f in touched if any(f == pp or f.startswith(pp.rstrip("/") + "/") for pp in fw.get("protected_paths") or [])]
        from . import instructions as instructions_mod
        inst_cfg = instructions_mod.settings(self.st)      # rework slice 8: instructions change only through the owner
        inst_files = instructions_mod.protected(touched, inst_cfg["paths"]) if inst_cfg["owner_only"] else []
        tests_changed = gitops.tests_modified_or_deleted(repo, self.p.repo_branch)
        try:
            with self.repo_lock:                    # the push writes refs of the clone the other lanes fetch into
                gitops.push_branch(repo, t["id"])
            # the diff is the truth (T-0132): if `changes` names a file absent from it, the recorded list is the diff; a log note only
            new_changes, absent = prfacts.changes_from_diff(res.get("changes"), touched)
            if absent:
                log.warning("%s: developer `changes` named files absent from the git diff (%s); recorded the diff list instead", t["id"], "; ".join(absent[:8]))
                res = {**res, "changes": new_changes}
            facts, _ = prfacts.build(gitops.diff_stat(repo, self.p.repo_branch), touched, test_output, res, scope=t.get("scope"))
            extra_files = prfacts.out_of_scope(t.get("scope"), touched)
            if extra_files:   # T-0167: warn-only, never blocks the merge or the canary
                try:
                    self.store.event("pr.out_of_scope", self.p.slug, "dispatcher", {"text": "; ".join(extra_files[:12]), "task": t["id"]})
                except Exception as exc:
                    log.warning("could not record pr.out_of_scope: %s", exc)
            body = (f"{res.get('summary', '')}\n\n**Changes:** " + "; ".join(res.get("changes") or []) +
                    "\n\n**Self-check:**\n" + "\n".join(f"- {'✓' if c.get('met') else '✗'} {c.get('criterion')} — {c.get('evidence')}" for c in res.get("self_check") or []) +
                    "\n\n" + "\n".join(f"Evidence: {l}" for l in t.get("links") or []) +
                    "".join("\n" + l for l in gitops.closes_lines(t.get("links"), self.p.repo_url)) +
                    "\n\n" + facts +
                    "\n\n_Opened by the agent-team dispatcher for task " + t["id"] + "; tests passed on the branch. A human merges._")
            url = gitops.open_pull_request(self.p.repo_url, t["id"], self.p.repo_branch, f"{t['id']}: {t.get('title', '')}", body)
        except gitops.GitError as exc:
            if gitops.is_transient(exc):
                # GitHub 5xx after the retries (F-0100): the commit is intact, so the task goes back to ready, not blocked;
                # but only MAX_TRANSIENT times in a row, then a persistent outage blocks it instead of looping forever
                n = int(t.get("transient_failures") or 0) + 1
                self.store.event("pr.transient_failure", t["id"], "dispatcher", {"error": gitops.redact(str(exc))[:300], "consecutive": n})
                if n >= gitops.MAX_TRANSIENT:
                    self.store.transition(t["id"], "blocked", "dispatcher", f"push/PR failed on GitHub 5xx {n} times in a row, giving up: {gitops.redact(str(exc))[:200]}",
                                          force=True, transient_failures=n, assignee_run=None)
                    return True
                self.store.transition(t["id"], "ready", "dispatcher", f"push/PR failed on GitHub 5xx after {gitops.RETRIES} tries ({n}/{gitops.MAX_TRANSIENT}), will retry: {gitops.redact(str(exc))[:200]}",
                                      force=True, transient_failures=n, assignee_run=None)
                return True
            self.store.transition(t["id"], "blocked", "dispatcher", f"push/PR failed: {gitops.redact(str(exc))[:300]}", force=True)
            return True
        no_reason = bool(tests_changed) and not gitops.has_tests_change_reason(body)
        if inst_files:
            # the change is kept as «было → стало» for the owner's weekly list; the pull request stays open and is not merged
            for f in inst_files:
                was, becomes = self.instruction_diff(repo, f)
                instructions_mod.propose(self.st, source=str(t.get("role") or "developer"), file=f, was=was, becomes=becomes,
                                         why=f"{t['id']} {t.get('title', '')}: {_short_text(res.get('summary'), 300)}", product=self.p.slug, ref=url)
            self.store.transition(t["id"], "waiting_owner", "dispatcher", f"pull request {url} (instructions change only through the owner: "
                                  f"{', '.join(inst_files[:5])}; kept as a proposal for his weekly list)", force=True, pr_url=url, assignee_run=None)
            self.store.event("instructions.proposed", t["id"], "dispatcher", {"files": inst_files[:10], "pr": url})
            return True
        guard = canary_mod.guard_settings(fw)                          # rework slice 7: off by default
        strict = self.tests_guard(repo, tests_changed) if guard["tests_strict"] and fw.get("auto_merge") and not protected else ""
        if not fw.get("auto_merge") or protected or no_reason or strict:
            why = (f"protected files {', '.join(protected[:5])}" if protected else strict if strict else
                   f"tests edited/deleted without a 'tests-change-reason: F-xxxx' line: {', '.join(tests_changed[:5])}" if no_reason else "auto_merge off")
            self.store.transition(t["id"], "waiting_owner", "dispatcher", f"pull request {url} ({why})", force=True, pr_url=url, assignee_run=None)
            if strict:
                self.tell_owner(f"agent-team {t['id']}: PR {url} ждёт вас, само не сольётся — {strict}")
            return True
        self.canary_report, self.canary_started = None, utcnow()
        ok, note = self.canary(repo, str(fw.get("canary_product") or ""))
        if ok and guard["canary_report"] and str(fw.get("canary_product") or ""):
            # the verdict is read back from the report the dispatcher wrote: none, unparsable, another revision, older than
            # this run, failed or empty → no merge (an empty report is not «all is well»)
            ok, why_rep = canary_mod.check_report(self.canary_report, gitops.head(repo), self.canary_started)
            note = why_rep if not ok else f"{note}; {why_rep}"
            if not ok:
                self.store.event("canary.report_refused", t["id"], "dispatcher", {"text": why_rep[:300], "pr": url})
                self.tell_owner(f"agent-team {t['id']}: PR {url} не слит — {why_rep}")
        try:
            gitops.update_pull_request_body(url, prfacts.with_canary(body, ("passed" if ok else "FAILED") + f": {note[:200]}"))
        except gitops.GitError as exc:
            log.warning("could not write the canary result into %s: %s", url, gitops.redact(str(exc)))
        if not ok:
            self.store.transition(t["id"], "waiting_owner", "dispatcher", f"pull request {url}; canary failed: {note[:200]}", force=True, pr_url=url, assignee_run=None)
            gitops.comment_pull_request(url, f"Canary cycle failed, left for a human:\n\n```\n{note[:1500]}\n```")
            return True
        try:
            sha = gitops.merge_pull_request(url, auto=True)
            self.update_own_code(t, sha)
        except gitops.GitError as exc:
            self.store.transition(t["id"], "waiting_owner", "dispatcher", f"pull request {url}; merge failed: {gitops.redact(str(exc))[:200]}", force=True, pr_url=url, assignee_run=None)
            return True
        self.store.transition(t["id"], "accepted", "dispatcher", f"canary passed ({note[:120]}); merged {sha[:10]}", force=True, pr_url=url, accepted_at=utcnow(), released_sha=sha)
        self.store.transition(t["id"], "done", "dispatcher", "framework change live; the watchdog rolls back if the next cycles fail", force=True)
        self.store.event("framework.merged", t["id"], "dispatcher", {"sha": sha, "pr": url, "files": touched[:20]})
        return True

    def update_own_code(self, t: dict[str, Any], sha: str) -> None:
        """Fast-forward the dispatcher's own checkout after a self-merge, alone on the code (friction F17): the gate is
        closed for new cycles and the running cycles of the other products are waited for (team.yaml
        `locks.code_update_wait_s`). If one of them outlives the wait the update is made anyway — the merge is already on
        GitHub, and a checkout left behind would be the worse state — and the event `code.update_not_exclusive` says so."""
        from . import locks
        with locks.code_update(self.st) as alone:
            if not alone:
                wait_s = locks.cfg(self.st)["code_update_wait_s"]
                log.warning("%s: %s merged as %s; another cycle was still running after %.0fs, the checkout is updated under it", self.p.slug, t["id"], sha[:10], wait_s)
                self.store.event("code.update_not_exclusive", t["id"], "dispatcher", {"sha": sha, "waited_s": wait_s})
            head = gitops.update_running_checkout(self.st.root, self.p.repo_branch)
            log.info("%s: dispatcher checkout %s after the merge of %s (exclusive=%s)", self.p.slug, (head or "not updated")[:10], t["id"], alone)

    def canary(self, repo: Path, product: str) -> tuple[bool, str]:
        """A dry-run cycle of one product on the candidate code, on a COPY of the product's state: every phase must
        finish and none may error. Returns (passed, note).

        03.11: the first version ran a REAL cycle on the live state — it claimed and executed real tasks on the candidate
        code (release-radar T-0009 was published by the 08:07 canary, T-0019 merged), doubling the product's work and
        letting untested code touch live state. Now the canary gets its own root (copied team.yaml, DEPUTY.md and the
        product directory with its store), no .env, publishing forced to dry-run, `--dry-run` for the roles: it proves
        the candidate's phases run end to end on real data without doing anything to the world."""
        if not product:
            return True, "no canary product configured"
        ok, note, facts = self._canary_run(repo, product)
        try:                                        # the report the merge gate reads back (team/canary.py); written by THIS code, not the candidate
            self.canary_report = canary_mod.report_path(self.st.data_dir, f"{self.cycle_id}-canary")
            canary_mod.write_report(self.canary_report, cycle=f"{self.cycle_id}-canary", product=product, sha=gitops.head(repo),
                                    started=getattr(self, "canary_started", None) or facts["started"], ok=ok, note=note,
                                    artefacts=facts.get("artefacts") or [], phases=facts.get("phases") or [], rc=facts.get("rc"))
        except Exception as exc:  # noqa: BLE001 — without a report the gate (when on) refuses the merge; the verdict itself stands
            log.warning("canary report not written: %s", exc)
            self.canary_report = None
        return ok, note

    def _canary_run(self, repo: Path, product: str) -> tuple[bool, str, dict[str, Any]]:
        """The canary cycle itself: (passed, note, facts for the report: started, rc, phases, artefacts)."""
        facts: dict[str, Any] = {"started": utcnow(), "rc": None, "phases": [], "artefacts": []}
        cyc = f"{self.cycle_id}-canary"
        root = self.st.data_dir / "canary" / cyc
        try:
            src = next(p for p in self.st.product_dirs() if p.name == product)
        except StopIteration:
            return False, f"canary product {product!r} not found", facts
        if root.exists():
            shutil.rmtree(root)
        (root / "products").mkdir(parents=True)
        (root / "data").mkdir()
        from . import locks
        # the state is copied while the canary product's own cycle is not writing it (it may run in another process, F17)
        own = product == self.p.slug                # then this very cycle holds the lock
        with locks.product_cycle(self.st, product, note=f"canary state copy {cyc}", wait_s=0.0 if own else locks.cfg(self.st)["canary_state_wait_s"]) as quiet:
            if not quiet and not own:
                log.warning("canary: the cycle of %s is still running; its state is copied as it is", product)
            shutil.copytree(src, root / "products" / product, ignore=shutil.ignore_patterns("*.lock", "work", "runs"))
        before = _canary_snapshot(root / "products" / product)
        for name in ("team.yaml", "DEPUTY.md"):
            if (self.st.root / name).exists():
                shutil.copy2(self.st.root / name, root / name)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TELEGRAM_", "BLUESKY_", "DEVTO_", "MASTODON_", "THREADS_", "X_"))}
        env.update({"PYTHONPATH": str(repo), "TEAM_ROOT": str(root), "TEAM_CONFIG": str(root / "team.yaml"), "TEAM_PUBLISH_DRY_RUN": "1"})
        try:
            cp = subprocess.run([sys.executable, "-m", "team.cli", "cycle", "--product", product, "--cycle", cyc, "--dry-run"], cwd=str(repo),
                                env=env, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            shutil.rmtree(root, ignore_errors=True)
            return False, "canary timed out", facts
        try:
            artefacts = self._canary_artefacts(root / "products" / product, before)
        finally:
            shutil.rmtree(root, ignore_errors=True)
        out = (cp.stdout + cp.stderr)
        facts.update(rc=cp.returncode, artefacts=artefacts)
        if cp.returncode != 0 or "Traceback" in out:
            return False, f"rc={cp.returncode} " + out[-800:], facts
        m = re.search(r"phases \[([^\]]*)\]", out)
        phases = [x.strip(" '") for x in m.group(1).split(",")] if m else []
        facts["phases"] = phases
        missing = [ph for ph in PHASES if ph not in phases]
        if missing:
            return False, f"phases missing: {missing}; " + out[-500:], facts
        # only a real phase failure counts: the event the cycle writes, or the log line of a phase that raised.
        # (12:07: a dry-run warning "deputy run failed: no run" matched the old substring check and failed PR #32 falsely)
        if "phase.error" in out or re.search(r"phase \w+ failed", out):
            return False, "phase error: " + out[-800:], facts
        # T-0165 (voice:2026-11-18:P1): a cycle that merely ended is not enough; it must have produced a real result
        if not artefacts:
            note = f"{CANARY_EMPTY_REASON}: cycle {cyc} of {product} ended, but wrote no non-empty report or task result"
            try:
                self.store.event("canary.empty", product, "dispatcher", {"text": note})
            except Exception as exc:  # recording must never mask the verdict
                log.warning("could not record canary.empty: %s", exc)
            return False, note, facts
        return True, f"dry-run cycle {cyc} of {product} on a state copy: all phases, {len(artefacts)} result artefact(s)", facts

    def instruction_diff(self, repo: Path, path: str) -> tuple[str, str]:
        """(«было», «стало») of an instruction file the candidate branch changes: the removed and the added lines of its
        diff against the base branch (the whole new file when it did not exist)."""
        ref = f"origin/{self.p.repo_branch}" if gitops.has_remote(repo) else self.p.repo_branch
        diff = gitops.git(repo, "diff", "-U0", f"{ref}...HEAD", "--", path, check=False)
        was = [ln[1:] for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---")]
        becomes = [ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
        return "\n".join(was), "\n".join(becomes)

    def keep_lessons(self, lessons: list[str]) -> None:
        """The editor's lessons. `framework.instructions.owner_only` (rework slice 8): a lesson is an instruction the role
        writes for itself, so it is kept as a proposal for the owner's weekly list and does not take effect; off — it is
        appended to the product's editorial-lessons.md, as before."""
        from . import editorial, instructions as instructions_mod
        if not instructions_mod.settings(self.st)["owner_only"]:
            editorial.add_lessons(self.p, lessons, "editor")
            return
        for ls in lessons:
            if str(ls).strip() and not editorial.is_label_lesson(str(ls)):
                instructions_mod.propose(self.st, source="editor", file=f"products/{self.p.slug}/{editorial.LESSONS_FILE}", was="",
                                         becomes=str(ls).strip(), why="the editor's lesson after a review", product=self.p.slug)

    def protected_functions_touched(self, repo: Path) -> list[str]:
        """`path:function` of the protected functions (the canary, the merge gate: team/canary.py PROTECTED_FUNCTIONS,
        team.yaml `framework.protected_functions`) whose text the candidate branch changes against the base branch."""
        fw = dict(self.st.raw.get("framework") or {})
        prot = fw.get("protected_functions") if isinstance(fw.get("protected_functions"), dict) else canary_mod.PROTECTED_FUNCTIONS
        ref = f"origin/{self.p.repo_branch}" if gitops.has_remote(repo) else self.p.repo_branch
        base_ref = gitops.git(repo, "merge-base", ref, "HEAD", check=False).strip() or ref

        def text(rev: str, path: str) -> str | None:
            try:
                return gitops.git(repo, "show", f"{rev}:{path}")
            except gitops.GitError:
                return None
        return canary_mod.protected_functions_changed({p: text(base_ref, p) for p in prot}, {p: text("HEAD", p) for p in prot}, prot)

    def tests_guard(self, repo: Path, tests_changed: list[str]) -> str:
        """`framework.guard.tests_strict` (owner 26.11: «новые тесты сливаются сами; изменение и удаление существующих тестов
        и кода пробного прогона ждёт владельца»): "" when the change only adds — new files, new lines under tests/ — else
        one line saying what waits for the owner. A `tests-change-reason` line does not replace him here."""
        if tests_changed:
            return f"existing tests changed or deleted: {', '.join(tests_changed[:5])}"
        funcs = self.protected_functions_touched(repo)
        if funcs:
            return f"canary code changed: {', '.join(funcs[:5])}"
        return ""

    def tell_owner(self, text: str) -> None:
        """One line to the owner's ops chat (the bot of the 12-hour report); never raises, silent without its settings."""
        line = " ".join(str(text).split())[:600]
        try:
            from . import billing
            if not self.dry_run:
                billing.owner_notify()(line)
            self.store.event("owner.told", self.p.slug, "dispatcher", {"text": line[:300]})
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: could not tell the owner: %s", self.p.slug, str(exc)[:200])

    @staticmethod
    def _canary_artefacts(pdir: Path, before: dict[str, str]) -> list[str]:
        """Non-empty report / task-result files that the canary cycle created or changed in the state copy."""
        return [k for k, v in _canary_snapshot(pdir).items() if before.get(k) != v]

    def check_pull_requests(self) -> None:
        for t in self.store.tasks(["waiting_owner"]):
            url = t.get("pr_url")
            if not url or self.dry_run:
                continue
            try:
                state = gitops.pull_request_state(url)
            except gitops.GitError as exc:
                log.warning("%s: PR state of %s: %s", self.p.slug, url, exc)
                continue
            if state == "merged":
                self.store.transition(t["id"], "accepted", "owner", f"pull request merged {url}", force=True, accepted_at=utcnow())
                self.store.transition(t["id"], "done", "dispatcher", "merged by the owner; framework tasks are not measured", force=True)
            elif state == "closed":
                self.store.transition(t["id"], "rejected", "owner", f"pull request closed without merge {url}", force=True)

    def exec_marketer(self, t: dict[str, Any], run_id: str) -> bool:
        channels = self.p.raw.get("channels") or {}
        if not channels:
            self.store.transition(t["id"], "blocked", "dispatcher", "no channels configured in product.yaml", force=True)
            return True
        work = self.p.work_dir / self.cycle_id / f"mkt-{t['id']}"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        ledger = self.run_dir / f"mkt-{t['id']}" / "ledger.jsonl"
        from . import editorial
        lessons = editorial.read_lessons(self.p)
        memory = prompts.marketing_memory(self.p, self.store, caps=self.channel_caps())      # posts of the week, visits by channel, feedback, the plan
        if is_plan_task(t):
            return self.exec_marketing_plan(t, run_id, work, channels, lessons, memory)
        series = editorial.series_mode(self.st, t)                 # owner 04.11: the daily series is edited very critically
        if series:
            lessons = [f"SERIES RUBRIC (binding, the editor rejects any draft that breaks it):\n{series['rubric']}"] + lessons
        prompt = prompts.marketer_prompt(self.p, t, channels, t.get("results") or [], lessons=lessons, memory=memory)
        disc = self.st.publish.get("disclosure", "")
        disc_map = disc if isinstance(disc, dict) else {"default": str(disc)}
        drafts_dir = work / "drafts"
        env = {"TEAM_PUBLISH_LEDGER": str(ledger), "TEAM_PUBLISH_DISCLOSURES": json.dumps(disc_map, ensure_ascii=False),
               "TEAM_PUBLISH_MAX_PER_DAY": str(self.st.publish.get("max_posts_per_channel_per_day", 2)),
               "TEAM_PUBLISH_PRODUCT": self.p.slug, "TEAM_PUBLISH_COUNTS": json.dumps({c: self.store.posts_today(c) for c in channels}),
               "TEAM_PUBLISH_CAPS": json.dumps(self.channel_caps() or {}),
               "TEAM_PUBLISH_COUNTS_WEEK": json.dumps({c: publish.week_count(self.store, c) for c in channels}),
               "TEAM_PUBLISH_REPLY_COUNTS": json.dumps({c: self.store.posts_today(c + publish.REPLY_SUFFIX) for c in channels}),
               "TEAM_PUBLISH_DRAFT_DIR": str(drafts_dir),
               **({"TEAM_PUBLISH_DRY_RUN": "1"} if self.st.publish.get("dry_run") else {})}
        mkt_tools = ["Bash(team-publish *)", "Bash(team-publish:*)", "Bash(team-fetch *)", "Bash(team-fetch:*)", "Read", "Write"]
        rr = self.role_run("marketer", prompt, "task-result", cwd=work, label=f"mkt-{t['id']}", cap=float(series["marketer_cap"]) if series else 1.0,
                           env_extra=env, allowed_tools=mkt_tools, model=series["marketer_model"] if series else None)
        posted, editor_summary = 0, ""
        if rr is not None and rr.ok and rr.structured and self.runner is not None:
            posted, editor_summary = self.editorial_loop(t, work, drafts_dir, channels, env, mkt_tools, lessons)
        if rr is None or not (rr.ok and rr.structured):
            self.store.transition(t["id"], "ready" if posted == 0 else "accepted", "dispatcher", f"run failed: {rr.error if rr else 'no run'}; posted {posted}", force=True, assignee_run=None)
            return True
        res = rr.structured
        res["run_id"] = run_id
        res["posted"] = posted
        if editor_summary:
            res["editor"] = editor_summary[:600]
        self.store.add_result(t["id"], res)
        if res.get("status") in ("blocked", NEEDS_CONTEXT) or posted == 0:
            why = str(res.get("question") or f"nothing was posted: {res.get('summary', '')}")
            if editor_summary:
                why = f"editor: {editor_summary} / marketer: {res.get('summary', '')}"
            self.store.ask(from_="marketer", to="po", question=why[:1200], task_id=t["id"])
            return True
        self.store.transition(t["id"], "accepted", "dispatcher", f"posted {posted} (editor-approved)", force=True, accepted_at=utcnow())
        return True

    def channel_caps(self) -> dict:
        """Owner's per-channel caps: `publish.channel_caps` in team.yaml, overridden per channel by the product's own
        `channel_caps` (e.g. Luz promises a daily Telegram post on its site, so its Telegram is not capped weekly)."""
        caps = {k: dict(v or {}) for k, v in (self.st.publish.get("channel_caps") or {}).items()}
        for k, v in (self.p.raw.get("channel_caps") or {}).items():
            caps[k] = dict(v or {})
        return caps

    def exec_marketing_plan(self, t: dict[str, Any], run_id: str, work: Path, channels: dict[str, Any], lessons: list[str],
                            memory: dict[str, Any]) -> bool:
        """A `plan: true` marketing task (03.11: the marketer was reactive — one task, one post, no memory, no plan): the
        marketer writes `marketing-plan.md` (7 days of slots per channel, rubrics, goals, what to drop) in its work
        directory; the dispatcher copies it to products/<slug>/ and accepts the task by the file. No post is expected,
        so `posted == 0` is the norm here and the PO is asked only when no plan was written."""
        prompt = prompts.marketing_plan_prompt(self.p, t, channels, lessons=lessons, memory=memory)
        tools = ["Bash(team-fetch *)", "Bash(team-fetch:*)", "Read", "Write"]
        rr = self.role_run("marketer", prompt, "task-result", cwd=work, label=f"plan-{t['id']}", cap=1.0, allowed_tools=tools, max_turns=30)
        if rr is None or not (rr.ok and rr.structured):
            self.store.transition(t["id"], "ready", "dispatcher", f"run failed: {rr.error if rr else 'no run'}", force=True, assignee_run=None)
            return True
        res = rr.structured
        res["run_id"] = run_id
        res["posted"] = 0
        src = work / prompts.MARKETING_PLAN
        body = src.read_text(encoding="utf-8").strip() if src.exists() else ""
        if res.get("status") in ("blocked", NEEDS_CONTEXT) or len(body) < MIN_PLAN_CHARS:
            self.store.add_result(t["id"], res)
            why = str(res.get("question") or f"no usable {prompts.MARKETING_PLAN} written ({len(body)} chars): {res.get('summary', '')}")
            self.store.ask(from_="marketer", to="po", question=why[:1200], task_id=t["id"])
            return True
        dst = prompts.plan_path(self.p)
        dst.write_text(f"<!-- marketing plan · task {t['id']} · written {utcnow()} by the marketer; the PO cuts it into post tasks "
                       f"with not_before; a new plan task replaces it after {prompts.PLAN_MAX_AGE_DAYS} days -->\n{body}\n", encoding="utf-8")
        res["plan_file"] = str(dst)
        self.store.add_result(t["id"], res)
        self.store.event("marketing.plan", t["id"], "marketer", {"chars": len(body), "file": dst.name})
        self.store.transition(t["id"], "accepted", "dispatcher", f"marketing plan written ({len(body)} chars); no post expected",
                              force=True, accepted_at=utcnow())
        return True

    def editorial_loop(self, t: dict[str, Any], work: Path, drafts_dir: Path, channels: dict[str, Any], env: dict[str, str],
                       mkt_tools: list[str], lessons: list[str]) -> tuple[int, str]:
        """draft → editor → (one revision by the marketer) → editor → publish approved. Returns (posted, editor summary)."""
        from . import editorial
        drafts = self._load_drafts(drafts_dir)
        if not drafts:
            return 0, ""
        _, _, recent_issues = editorial.rolling(self.st, self.p.slug)
        verdicts: dict[str, dict[str, Any]] = {}
        summary = ""
        prev_issues: list[str] = []
        series = editorial.series_mode(self.st, t)
        max_rounds = int(series["rounds"]) if series else 2
        for round_no in range(1, max_rounds + 1):
            prompt = editorial.editor_prompt(self.p, t, drafts, lessons, recent_issues, round_no, prev_issues or None, series=series)
            ework = work / f"editor-{round_no}"
            ework.mkdir(parents=True, exist_ok=True)
            rr = self.role_run("editor", prompt, "editor-review", cwd=ework, label=f"editor-{t['id']}-{round_no}",
                               cap=float(series["editor_cap"]) if series else 0.5, max_turns=30 if series else 16,
                               model=series["editor_model"] if series else None)
            if rr is None or not (rr.ok and rr.structured):
                log.warning("%s: editor run failed in round %d; drafts not published", self.p.slug, round_no)
                return 0, f"editor run failed in round {round_no}"
            rev = rr.structured
            summary = str(rev.get("summary") or "")
            verdicts = {str(r.get("draft")): r for r in rev.get("reviews") or []}
            if series:                                            # the bar is enforced in code, not only asked for
                verdicts = {n: editorial.enforce_series_bar(r, series, last_round=round_no == max_rounds) for n, r in verdicts.items()}
            new_lessons = [ls for r in verdicts.values() for ls in (r.get("lessons") or [])][: editorial.MAX_LESSONS_PER_REVIEW]
            if new_lessons:
                self.keep_lessons(new_lessons)
            revise = [(n, r) for n, r in verdicts.items() if r.get("verdict") == "revise"]
            final = round_no == max_rounds or not revise
            for name, d in drafts:
                r = verdicts.get(name) or {"verdict": "reject", "scores": {}, "issues": ["no verdict from the editor"]}
                editorial.record_score(self.st, {"product": self.p.slug, "task": t["id"], "channel": d.get("channel"), "round": round_no,
                                                 "verdict": r.get("verdict"), "scores": r.get("scores") or {}, "mean": editorial.mean_score(r.get("scores") or {}),
                                                 "issues": r.get("issues") or [], "final": final or r.get("verdict") != "revise"})
            self.store.event("editorial", t["id"], "editor", {"round": round_no, "verdicts": {n: r.get("verdict") for n, r in verdicts.items()}, "summary": summary[:200]})
            if final:
                break
            prev_issues = [i for _, r in revise for i in (r.get("issues") or [])]
            notes = [{"draft": n, "issues": r.get("issues"), "scores": r.get("scores")} for n, r in revise]
            drafts_dir2 = work / f"drafts-{round_no + 1}"
            prompt2 = prompts.marketer_prompt(self.p, t, channels, [], lessons=lessons if series else editorial.read_lessons(self.p),
                                              editor_notes=notes, memory=prompts.marketing_memory(self.p, self.store, caps=self.channel_caps()))
            rr2 = self.role_run("marketer", prompt2, "task-result", cwd=work, label=f"mkt-{t['id']}-rev{round_no}",
                                cap=float(series["marketer_cap"]) if series else 1.0,
                                env_extra={**env, "TEAM_PUBLISH_DRAFT_DIR": str(drafts_dir2)}, allowed_tools=mkt_tools,
                                model=series["marketer_model"] if series else None)
            drafts2 = self._load_drafts(drafts_dir2) if rr2 is not None else []
            if not drafts2:
                return 0, summary or "marketer produced no revised drafts"
            approved_r1 = [(n, d) for n, d in drafts if (verdicts.get(n) or {}).get("verdict") == "approve"]
            drafts = approved_r1 + drafts2 if approved_r1 else drafts2
        posted, errors = 0, []
        for name, d in drafts:
            r = verdicts.get(name) or {}
            if r.get("verdict") != "approve":
                continue
            text = str(r.get("final_text") or "").strip() or None
            res = publish.publish_draft(Path(d["_path"]), text=text, dry_run=bool(self.st.publish.get("dry_run")))
            if res.ok and not res.dry_run:
                self.store.record_post(str(res.channel) + (publish.REPLY_SUFFIX if d.get("reply_to") else ""), str(res.post_id or ""), str(res.url or ""), t["id"], text=text or str(d.get("text") or ""))
                posted += 1
            elif not res.ok:
                self.store.event("post.error", t["id"], "dispatcher", {"channel": d.get("channel"), "error": str(res.error)[:200]})
                errors.append(f"{d.get('channel')}: {str(res.error)[:160]}")
        if editorial.degrading(self.st, self.p.slug):
            m, n, top = editorial.rolling(self.st, self.p.slug)
            self.store.add_observation({"title": f"Editorial quality falling: mean {m}/5 over the last {n} posts", "classification": "need_hypothesis",
                                        "source": "editor", "url": self.p.site_base, "actual": "; ".join(top)[:400], "expected": f"≥ {editorial.DEGRADE_BELOW}",
                                        "fingerprint": fingerprint("editor", "degrading", self.p.slug, _now().strftime("%Y-%m-%d"))})
        if errors:  # 07.11 T-0057: the PO saw only the marketer's "dry-run" and closed the task; the real send errors go first
            summary = f"publish failed on the real send (not dry-run): {'; '.join(errors)} / {summary}"
        return posted, summary

    @staticmethod
    def _load_drafts(dir_: Path) -> list[tuple[str, dict[str, Any]]]:
        out = []
        for p in sorted(dir_.glob("*.json")) if dir_.exists() else []:
            try:
                d = publish.load_draft(p)
            except (json.JSONDecodeError, OSError):
                continue
            d["_path"] = str(p)
            out.append((p.name, d))
        return out

    def exec_analyst_task(self, t: dict[str, Any], run_id: str) -> bool:
        rr = self.analyst_run(label=f"an-{t['id']}", question=f"Task {t['id']}: {t.get('problem')}\nScope: {t.get('scope')}")
        if rr is None or not (rr.ok and rr.structured):
            self.store.transition(t["id"], "ready", "dispatcher", "analyst run failed", force=True, assignee_run=None)
            return True
        summary = str(rr.structured.get("summary_md", ""))
        if is_stub_result(summary):
            # a stub ("duplicate call; see the previous report") must not close the task (release-radar T-0029, 03.11)
            self.store.add_result(t["id"], {"run_id": run_id, "status": "blocked", "summary": summary[:1200]})
            self.store.transition(t["id"], "blocked", "dispatcher", "analyst returned a stub, not a measurement", force=True, assignee_run=None)
            self.store.ask(from_="analyst", to="po", question=f"Task {t['id']}: the analyst returned a stub instead of a measurement "
                           f"({_short_text(summary, 160)}). Re-state the question with the data field it needs, or close the task.",
                           task_id=t["id"], blocks=[t["id"]])
            return True
        self.store.add_result(t["id"], {"run_id": run_id, "status": "done", "summary": summary[:1200]})
        self.store.transition(t["id"], "done", "dispatcher", "measurement delivered", force=True)
        return True

    # measure: daily analyst ---------------------------------------------------------------------------

    def balance_check(self) -> list[str]:
        """Automatic balance reading (owner 21.11, team/balance.py): every option of the metrics' optional `balance` block
        whose win rate left the product's band with enough picks becomes ONE observation of class `balance` for the PO. The
        same option of the same tables version is not raised again for `balance.reraise_h` hours. No block → nothing."""
        data = prompts.latest_metrics(self.p)
        days = (data or {}).get("days") or {}
        if not balance.has_block(days):
            log.debug("%s: balance check skipped: the metrics carry no balance block", self.p.slug)
            return []
        cfg = balance.settings(self.p.raw)
        found = balance.findings(days, cfg)
        cards = balance.observation_cards(found, self.store.observations(), cfg, now=_now(), url=self.p.site_base,
                                          run_id=f"{self.cycle_id}-balance")
        log.info("%s: balance check: band %s, min picks %d — %d option(s) out of band, %d new observation(s)%s", self.p.slug, cfg.band,
                 cfg.min_picks, len(found), len(cards), " [dry-run: not written]" if self.dry_run and cards else "")
        if self.dry_run:
            return []
        return [self.store.add_observation(c)["id"] for c in cards]

    def phase_measure(self) -> None:
        try:
            self.balance_check()                  # every cycle, no model run: an overpowered option does not wait for the analyst's slot
        except Exception as exc:  # noqa: BLE001 — a malformed balance block must not cost the product its measure phase
            log.exception("%s: balance check failed: %s", self.p.slug, exc)
        day = _now().strftime("%Y-%m-%d")
        every = int(self.st.cycle.get("measure_every_h", 24))
        slot = _now().hour // max(1, every)
        marker = self.p.dir / "digests" / (f"analyst-{day}.md" if every >= 24 else f"analyst-{day}-{slot:02d}.md")
        if marker.exists():
            return
        if not list((self.p.dir / "metrics").glob("*.json")):
            return
        rr = self.analyst_run(label="analyst")
        if rr is None or not (rr.ok and rr.structured):
            return
        res = rr.structured
        marker.write_text(f"# Analyst {day}\n\n{res.get('summary_md', '')}\n\n```yaml\n{yaml.safe_dump({k: res.get(k) for k in ('task_effects', 'goal_status')}, allow_unicode=True, sort_keys=False)}```\n", encoding="utf-8")
        for o in res.get("observations") or []:
            self.store.add_observation({**o, "source": "analyst", "run_id": f"{self.cycle_id}-analyst",
                                        "fingerprint": fingerprint("analyst", o.get("classification", ""), o.get("url", ""), o.get("title", ""))})
        effects = {e.get("task_id"): e for e in res.get("task_effects") or []}
        after = timedelta(hours=int(self.st.cycle.get("measure_after_h", 24)))
        for t in self.store.tasks(["accepted"]):
            acc = parse_ts(t.get("accepted_at"))
            if acc and _now() - acc >= after:
                e = effects.get(t["id"], {})
                self.store.transition(t["id"], "measured", "analyst", f"effect: {e.get('effect', 'insufficient_data')} — {e.get('evidence', '')[:200]}", effect=e.get("effect", "insufficient_data"))
                self.store.transition(t["id"], "done", "dispatcher", "measured")

    # geo: weekly control questions to AI search -------------------------------------------------

    def phase_geo(self) -> None:
        if not self.p.gate_open("geo"):
            return
        every_h = int(self.st.cycle.get("geo_every_h", 168))
        last = sorted((self.p.dir / "metrics").glob("geo-*.json"))
        if last:
            prev = parse_ts(json.loads(last[-1].read_text(encoding="utf-8")).get("generated") or "")
            if prev and _now() - prev < timedelta(hours=every_h):
                return
        qs = geo.queries_for(self.p.raw)
        if not qs:
            return
        prompt = geo.build_prompt(str(self.p.raw.get("name") or self.p.slug), self.p.site_base, qs, geo.audience_section(self.p.vision()))
        rr = self.role_run("geo-probe", prompt, "geo-report", cwd=self.p.work_dir / self.cycle_id / "geo", label="geo", cap=1.5,
                           allowed_tools=["WebSearch", "WebFetch"], max_turns=40)
        if rr is None or not (rr.ok and rr.structured):
            return
        day = _now().strftime("%Y-%m-%d")
        m = geo.to_metrics(rr.structured, day)
        m["generated"] = utcnow()
        (self.p.dir / "metrics" / f"geo-{day}.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
        (self.p.dir / "digests" / f"geo-{day}.md").write_text(
            f"# GEO probe {day}\n\n{rr.structured.get('summary_md', '')}\n\n```yaml\n{yaml.safe_dump(m, allow_unicode=True, sort_keys=False)}```\n", encoding="utf-8")
        for card in geo.to_observations(rr.structured, self.p.slug):
            self.store.add_observation({**card, "run_id": f"{self.cycle_id}-geo"})

    # voice: weekly voice-of-user research ---------------------------------------------------------

    def phase_voice(self) -> None:
        if not self.p.gate_open("voice"):
            return
        if not voice.due(self.p.dir, int(self.st.cycle.get("voice_every_h", voice.EVERY_H_DEFAULT)), _now()):
            return
        if self.runner is None or self.dry_run or not voice.claim_cycle(self.st.data_dir, self.cycle_id):
            return
        voice.mark_attempt(self.p.dir, _now())
        prompt = voice.build_prompt(str(self.p.raw.get("name") or self.p.slug), self.p.site_base, self.p.vision(),
                                    voice.topics_for(self.p.raw), voice.latest(self.p.dir))
        rr = self.role_run(voice.ROLE, prompt, voice.SCHEMA, cwd=self.p.work_dir / self.cycle_id / "voice", label="voice",
                           cap=float(self.st.cycle.get("voice_cap_usd", 3.0)), allowed_tools=["WebSearch", "WebFetch"], max_turns=60)
        if rr is None or not (rr.ok and rr.structured):
            return
        day = _now().strftime("%Y-%m-%d")
        voice.save(self.p.dir, rr.structured, day, utcnow())
        for card in voice.to_observations(rr.structured, self.p.slug, day):
            self.store.add_observation({**card, "run_id": f"{self.cycle_id}-voice"})

    def hypothesis_problems(self, t: dict[str, Any]) -> list[str]:
        obs = {o["id"]: o for o in self.store.observations()}
        return hypothesis.check(t, voice_ids=voice.known_evidence_ids(self.p.dir), observations=obs, site_bases=[self.p.site_base],
                                today=_now().date())

    def hand_over_readouts(self) -> None:
        """A finished feature whose hypothesis is due gets one readout card for the PO (confirmed / refuted / too few people)."""
        for t in hypothesis.readouts_due(self.store.tasks(list(hypothesis.READ_STATES)), _now().date()):
            self.store.add_observation({**hypothesis.readout_card(t, self.p.slug), "run_id": f"{self.cycle_id}-triage"})
            t["hypothesis_readout"] = utcnow()
            self.store.save_task(t)

    def analyst_run(self, *, label: str, question: str | None = None) -> RunResult | None:
        files = sorted((self.p.dir / "metrics").glob("????-??-??.json"), reverse=True)[:4]
        metrics = [("digest", prompts.metrics_digest(files, balance_cfg=balance.settings(self.p.raw)))]     # ops 04.11: compact per-day view, not cut raw files
        gsc_md = self.p.dir / "metrics" / "gsc.md"
        if gsc_md.exists():
            metrics.append(("Google Search Console (team gsc, daily)", gsc_md.read_text(encoding="utf-8")))
        since = (_now() - timedelta(hours=72)).isoformat()
        accepted = [t for t in self.store.tasks(["accepted", "measured", "done", "released"]) if (t.get("accepted_at") or t.get("released_at") or "") >= since]
        posts = [{"day": r[0], "channel": r[1], "url": r[3], "ts": r[5]} for r in self.store.db.execute("SELECT * FROM posts ORDER BY ts DESC LIMIT 20")]
        prompt = prompts.analyst_prompt(self.p, self.store, metrics=metrics, accepted=accepted, posts=posts)
        if question:
            prompt += f"\n\n# QUESTION TO ANSWER\n{question}\n"
        return self.role_run("analyst", prompt, "analyst-report", cwd=self.p.work_dir / self.cycle_id / label, label=label, cap=1.0, allowed_tools=[], max_turns=6)

    # digest: once a day to the owner's channel ----------------------------------------------------

    def phase_digest(self) -> None:
        day = _now().strftime("%Y-%m-%d")
        hours = sorted(int(h) for h in (self.st.cycle.get("digest_hours_utc") or [self.st.cycle.get("digest_hour_utc", 7)]))
        due = [h for h in hours if _now().hour >= h]
        if not due:
            return
        out = self.p.dir / "digests" / f"daily-{day}-{due[-1]:02d}.md"
        if out.exists():
            return
        text = daily_digest(self.p, self.store, day)
        out.write_text(text, encoding="utf-8")
        chat = os.environ.get("TELEGRAM_OPS_CHAT_ID")
        if chat and os.environ.get("TELEGRAM_BOT_TOKEN") and not self.dry_run and os.environ.get("TEAM_PUBLISH_DRY_RUN") != "1":
            r = publish.post("telegram", text[:3500], target=chat, disclosure=" ", dry_run=False)
            self.store.event("digest.sent", day, "dispatcher", {"ok": r.ok, "error": r.error})


# ------------------------------------------------------------------ helpers

STUB_MARKERS = ("дубликат вызова", "duplicate call", "см. предыдущий отчёт", "see the previous report", "see previous report",
                "no new data", "нет новых данных", "placeholder", "заглушка")
MIN_RESULT_CHARS = 120


def _friction_items(v: Any) -> list[Any]:
    """A result's `friction` as a list: a role may return one string or one object instead of the list the contract asks for."""
    if v is None or v == "":
        return []
    return list(v) if isinstance(v, (list, tuple)) else [v]


def _friction_text(f: Any) -> str:
    """Comparable text of one friction item, whatever its shape ({kind, what} or a bare string — ai-battles 21.11)."""
    return str((f.get("what") if isinstance(f, dict) else None) or f)[:120]


def _short_text(s: Any, n: int) -> str:
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


# Words for an inconclusive review whose reviewer gave no `inconclusive_reason` (older prompts, a model that skipped the
# field): the first pattern found in first_failure / limits / summary wins. Order matters: a lock wait before a timeout.
INCONCLUSIVE_PATTERNS = (
    ("lock_timeout", r"no e2e host lock within|lock_timeout|waited for the (e2e host )?lock \d{3,}"),
    ("run_timeout", r"timed out after|run_timeout|ran \d+s after the lock|limit \d+ ?s|time limit"),
    ("no_tests_live", r"no_tests_live|no test for the deployed host|no @live|not @live|live host.*(no tests|0 tests)"),
    ("no_tests", r"no tests found|no_tests|0 tests in 0 files"),
    ("server_not_started", r"server not started|server_not_started|could not start|did not answer"),
    ("test_tooling", r"no json report|test_tooling|not installed|unknown --project|cannot find module"),
    ("no_test_for_criterion", r"no (e2e )?(test|spec|scenario) (covers|for)|no_test_for_criterion"),
    ("not_observable", r"not observable|not_observable|cannot be seen|could not observe"),
)


def inconclusive_reason(v: dict[str, Any]) -> str:
    """The fixed word for an inconclusive verdict (team/play.py INCONCLUSIVE_REASONS): the reviewer's own if it is one of
    the words, else read from its text, else `other`."""
    from .play import INCONCLUSIVE_REASONS
    own = str(v.get("inconclusive_reason") or "").strip()
    if own in INCONCLUSIVE_REASONS:
        return own
    text = " ".join(str(v.get(k) or "") for k in ("first_failure", "limits", "summary")).lower()
    for word, pat in INCONCLUSIVE_PATTERNS:
        if re.search(pat, text):
            return word
    return "other"


def is_stub_result(summary: str) -> bool:
    """A result that says nothing: shorter than MIN_RESULT_CHARS or a known 'duplicate / see previous' phrase."""
    low = " ".join(str(summary or "").lower().split())
    return len(low) < MIN_RESULT_CHARS or any(m in low for m in STUB_MARKERS)


DEPENDENCY_DONE = ("accepted", "measured", "done")


def unmet_dependencies(store: Any, t: dict[str, Any]) -> list[str]:
    """Ids from `depends_on` that are not finished (accepted/measured/done) with a non-stub last result; unknown ids count as unmet."""
    out = []
    for dep in t.get("depends_on") or []:
        d = store.task(str(dep))
        res = (d.get("results") or []) if d else []
        if not d or d.get("state") not in DEPENDENCY_DONE or not res or is_stub_result(str(res[-1].get("summary") or "")):
            out.append(str(dep))
    return out


def no_commit_count(t: dict[str, Any]) -> int:
    """Number of «developer reported done but committed nothing» records in the task's whole history (T-0043)."""
    return sum(1 for h in (t.get("history") or []) if "committed nothing" in str(h.get("note") or ""))


def over_budget(rr: RunResult) -> bool:
    """True when the CLI ended the run because it reached `--max-budget-usd`."""
    text = f"{(rr.raw or {}).get('subtype') or ''} {rr.error or ''}".lower()
    return "max_budget" in text or "budget" in text and "exceed" in text


def previous_no_commit(t: dict[str, Any]) -> bool:
    """True when the task has any earlier no-commit record, however many re-readies lie in between."""
    return no_commit_count(t) > 0


def repeated_partial(t: dict[str, Any], res: dict[str, Any]) -> bool:
    """True when the previous recorded result was also partial with the same summary (first 200 chars) or the same frictions."""
    prev = [r for r in (t.get("results") or []) if r.get("status") in ("partial", "blocked")]
    if not prev:
        return False
    last = prev[-1]
    k_old = {str(f.get("kind")) for f in (last.get("friction") or []) if isinstance(f, dict) and f.get("kind")}
    k_new = {str(f.get("kind")) for f in (res.get("friction") or []) if isinstance(f, dict) and f.get("kind")}
    # a shared friction category on two consecutive attempts = stuck (T-0024); `environment` is ignored when other kinds are present,
    # because it shows up incidentally between rounds (T-0025); if a side has only `environment`, it is compared as is
    o, n = k_old - {"environment"} or k_old, k_new - {"environment"} or k_new
    if o & n:
        return True
    same_summary = _short_text(last.get("summary"), 200) == _short_text(res.get("summary"), 200)
    f_old = sorted(_friction_text(f) for f in _friction_items(last.get("friction")))
    f_new = sorted(_friction_text(f) for f in _friction_items(res.get("friction")))
    same_friction = bool(f_old) and f_old == f_new
    return same_summary or same_friction


class _Feed:
    """The tasks of one seat, handed out one at a time to the lane(s) of that seat."""

    def __init__(self, tasks: list[dict[str, Any]]):
        self._tasks, self._lock = list(tasks), threading.Lock()

    def next(self) -> dict[str, Any] | None:
        with self._lock:
            return self._tasks.pop(0) if self._tasks else None


OPEN_TASK_STATES = ("ready", "claimed", "in_progress", "review", "released", "blocked", "waiting_owner")
MIN_PLAN_CHARS = 200        # a marketing-plan.md shorter than this is "no plan": the PO is asked instead of accepting it


def is_plan_task(t: dict[str, Any]) -> bool:
    """A marketer task whose deliverable is products/<slug>/marketing-plan.md, not a post (`plan: true` from the PO)."""
    return str(t.get("role") or "") == "marketer" and bool(t.get("plan"))
CLOSABLE_BY_PO = ("proposed", "ready", "blocked", "waiting_owner", "deferred")


def _norm_title(s: str) -> str:
    return re.sub(r"[^\w]+", " ", str(s or "").lower()).strip()


def _norm_ts(s: Any) -> str:
    d = parse_ts(str(s)) if s else None
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M") if d else ""


CHANNEL_WORDS = ("telegram", "bluesky", "mastodon", "threads", "devto", "dev.to", "x post", "x trial", " x ", "x:", "twitter")


def _channels_in(title: str) -> frozenset[str]:
    low = " " + str(title or "").lower() + " "
    out = set()
    for w in CHANNEL_WORDS:
        if w in low:
            out.add("x" if w.startswith("x") or w == " x " or w == "twitter" else ("devto" if w in ("devto", "dev.to") else w))
    return frozenset(out)


CLOSED_TWIN_STATES = ("done", "rejected", "accepted", "measured")   # `cycle.duplicates.closed_check`: states a proposal is compared with
CLOSED_TWIN_DAYS = 14                      # … closed within this many days


def closed_twin(closed: list[dict[str, Any]], rt: dict[str, Any]) -> dict[str, Any] | None:
    """The closed task a proposed ready task repeats by its normalized title, newest first, or None. Not compared: a
    proposal scheduled with `not_before` or belonging to a standing direction (a slot or a cadence repeats its title by
    design), one for another role, and one that names the closed task in `continues` (the PO builds on it knowingly)."""
    title = _norm_title(rt.get("title"))
    if not title or rt.get("not_before") or rt.get("standing"):
        return None
    named = {str(x) for x in (rt.get("continues") or [])} if isinstance(rt.get("continues"), list) else {str(rt.get("continues") or "")}
    for t in reversed(closed):
        if _norm_title(t.get("title")) == title and str(t.get("role") or "developer") == str(rt.get("role") or "developer") and t["id"] not in named:
            return t
    return None


def duplicate_task(open_tasks: list[dict[str, Any]], rt: dict[str, Any]) -> dict[str, Any] | None:
    """The open task a proposed ready task duplicates, or None.

    Same normalized title → duplicate. Same role, the same `not_before` moment and the same channel(s) named in the
    title → duplicate too: a scheduled post is one slot, and the PO wrote the same slot under a slightly different title in
    every cycle (luz-amanha T-0006/T-0011, release-radar T-0009/T-0012/T-0015 on 03.11). Two posts for DIFFERENT channels
    at one moment are two slots (16:13: a Threads trial was dropped as a duplicate of an X trial at the same minute)."""
    title = _norm_title(rt.get("title"))
    nb = _norm_ts(rt.get("not_before"))
    ch = _channels_in(rt.get("title", ""))
    for t in open_tasks:
        if title and _norm_title(t.get("title")) == title:
            return t
        if nb and t.get("role") == rt.get("role") and _norm_ts(t.get("not_before")) == nb:
            tch = _channels_in(t.get("title", ""))
            if not ch or not tch or ch & tch:
                return t
    return None


DATE_IN_TITLE_RE = re.compile(r"(?<![\w.])(\d{2})\.(\d{2})(?:\.(\d{4}))?(?!\w|\.\d)")


def future_dated_title(title: str, now: datetime) -> bool:
    """True if the title contains a standalone DD.MM or DD.MM.YYYY date later than today (UTC).

    Only two-digit day and month count (``03.11``), so version numbers (``3.12``, ``v1.12``, ``2.1.4``) and decimals
    are not dates. Without a year, December → January is read as the next year."""
    for d, m, y in DATE_IN_TITLE_RE.findall(title):
        d, m = int(d), int(m)
        if not (1 <= m <= 12 and 1 <= d <= 31):
            continue
        if y:
            if (int(y), m, d) > (now.year, now.month, now.day):
                return True
        elif (m, d) > (now.month, now.day) or (now.month == 12 and m == 1):
            return True
    return False


def fingerprint(scope: str, cls: str, url: str, title: str) -> str:
    import hashlib
    import re
    path = re.sub(r"^https?://[^/]+", "", url or "")
    t = re.sub(r"[^a-z0-9а-я]+", " ", (title or "").lower()).strip()[:60]
    return hashlib.sha1(f"{scope}|{cls}|{path}|{t}".encode()).hexdigest()[:12]


def game_metrics_md(data: dict[str, Any], balance_cfg: balance.Config | None = None) -> str:
    """metrics/latest.md for a game product (docs/game-metrics.md): one row per day, newest last, plus the live counters,
    and the balance table when the server sends the optional `balance` block (owner 21.11, team/balance.py)."""
    days = data.get("days") or {}
    now = data.get("now") or {}

    def g(d: dict[str, Any], k: str, sub: str | None = None) -> Any:
        v = d.get(k)
        if sub is not None:
            v = v.get(sub) if isinstance(v, dict) else None
        return "—" if v is None else v

    lines = [f"# game metrics {data.get('host', '')} — generated {data.get('generated', '')} (version {data.get('version', '?')})", "",
             f"Now: online {now.get('online', '—')}, rooms open {now.get('rooms_open', '—')}, playing {now.get('rooms_playing', '—')}", "",
             "| day | started | finished | abandoned | players | returning | online peak | round median s | move p50/p95 ms | rejected | disconnects | errors | first-mover wins |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for day in sorted(days)[-10:]:
        d = days[day] if isinstance(days[day], dict) else {}
        lines.append(f"| {day} | {g(d, 'rounds_started')} | {g(d, 'rounds_finished')} | {g(d, 'rounds_abandoned')} | {g(d, 'players')} | "
                     f"{g(d, 'returning_players')} | {g(d, 'online_peak')} | {g(d, 'round_length_s', 'median')} | "
                     f"{g(d, 'move_latency_ms', 'p50')}/{g(d, 'move_latency_ms', 'p95')} | {g(d, 'rejected_moves')} | {g(d, 'disconnects')} | "
                     f"{g(d, 'errors')} | {g(d, 'first_mover_wins')} |")
    bal = balance.render_md(days, balance_cfg)
    return "\n".join(lines) + "\n" + (("\n" + bal) if bal else "")


def metrics_md(data: dict[str, Any], balance_cfg: balance.Config | None = None) -> str:
    if prompts.is_game_metrics(data):
        return game_metrics_md(data, balance_cfg)
    days = data.get("days") or {}
    lines = [f"# metrics {data.get('host', '')} — generated {data.get('generated', '')}", "",
             "| day | humans | engaged | returns | agents | crawlers | targets |", "|---|---|---|---|---|---|---|"]
    for day in sorted(days)[-10:]:
        d = days[day]
        h = d.get("humans") or {}
        tg = d.get("targets") or {}
        tg_s = ", ".join(f"{k}:{(v.get('requests') if isinstance(v, dict) else v)}" for k, v in list(tg.items())[:4])
        lines.append(f"| {day} | {h.get('sources', 0)} | {h.get('engaged', 0)} | {h.get('returns', 0)} | "
                     f"{(d.get('agents') or {}).get('requests', d.get('agents', 0)) if isinstance(d.get('agents'), dict) else d.get('agents', 0)} | "
                     f"{(d.get('crawlers') or {}).get('requests', 0) if isinstance(d.get('crawlers'), dict) else d.get('crawlers', 0)} | {tg_s} |")
    top = (data.get("totals") or {}).get("top_pages") or (days.get(max(days), {}) if days else {}).get("top_pages") or []
    if top:
        lines += ["", "Top pages (humans): " + ", ".join(f"{p.get('path') if isinstance(p, dict) else p[0]} ({p.get('requests') if isinstance(p, dict) else p[1]})" for p in top[:8])]
    by_src = (data.get("totals") or {}).get("by_src") or {}
    if by_src:          # which channel brought people: team-publish adds ?src=<channel> to every linked page
        lines += ["", "Visits by ?src= (channel: human sources / engaged / requests, whole window): "
                  + ", ".join(f"{k}: {(v or {}).get('human_sources', 0)}/{(v or {}).get('engaged_sources', 0)}/{(v or {}).get('requests', 0)}"
                              for k, v in by_src.items())]
    return "\n".join(lines) + "\n"


def daily_digest(p: Product, store: Store, day: str) -> str:
    from .store import summary
    s = summary(store)
    po = sorted((p.dir / "digests").glob("20*.md"))
    po_txt = po[-1].read_text(encoding="utf-8").split("\n", 2)[-1].strip() if po else "(PO не запускался)"
    ans = sorted((p.dir / "digests").glob(f"analyst-{day}*.md"))
    an_txt = ans[-1].read_text(encoding="utf-8").split("\n", 2)[-1].split("```")[0].strip() if ans else "(аналитик сегодня не запускался)"
    qs = store.questions(["open"])
    owner_q = [q for q in qs if q.get("to") == "owner"]
    from . import editorial
    em, en, _ = editorial.rolling(p.st, p.slug)
    from . import ux
    ux_line = ux.digest_line(p)
    ed_line = f"Тексты: средний балл редактора {em}/5 за последние {en} постов" + (" ⚠ ниже планки" if em is not None and en >= 5 and em < editorial.DEGRADE_BELOW else "") if em is not None else "Тексты: редактор ещё не оценивал"
    lines = [f"📦 {p.raw.get('name') or p.slug} — {day}", "", ux_line, ed_line,
             f"Задачи: {s['tasks']} всего · " + ", ".join(f"{k} {v}" for k, v in sorted(s["by_state"].items())),
             f"Расход сегодня: ${s['spent_today']:.2f} (кап ${p.daily_cap:.0f}) · открытых вопросов: {len(qs)}, владельцу: {len(owner_q)}", "",
             "PO: " + po_txt[:1200], "", "Аналитик: " + an_txt[:1200]]
    if owner_q:
        lines += ["", "❓ Владельцу:"] + [f"- {q['id']}: {q['question'][:300]}" for q in owner_q[:5]]
    return "\n".join(lines) + "\n"
