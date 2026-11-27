"""Continuous work of the seats (owner 25.11: «если у нас так простои постоянно, то надо что-то менять»; SCHEME §3
«Непрерывная работа мест»).

Measured on ai-battles, 21–25.11: the execute seats worked 18 % of the time of the four run slots while 8–28 ready
tasks waited the whole time. Two causes sat in the shape of the cycle, not in the backlog:

  one task per seat per cycle   a seat that finished after 8 minutes waited for the longest run of the cycle (25–45
                                minutes) and for the next cycle's triage before it took its next task;
  the pause between cycles      `cycle.every_min` is counted from the END of the last cycle and cron looks every five
                                minutes: 5–10 minutes of nothing after every cycle, 15–30 % of the day.

`cycle.flow` (team.yaml, product.yaml `cycle: {flow: {...}}` overrides key by key) switches two remedies on:

  refill   inside the execute phase a seat that finished its task takes the next startable one — read fresh from the
           store, in the usual order — until the phase is `refill_min` minutes old. `execute_slots` then means «places
           working at once», not «tasks per cycle». The cycle is not held longer than it would be by one long run:
           nothing new starts after `refill_min`. One exception, measured 24–25.11: in 23–29 % of the minutes inside a
           cycle only reviews were running — play-tests of 25 minutes queued for the run slots — and every execute seat
           waited for them. While reviews of the cycle still run or wait for a slot the cycle is held anyway, so refill
           stays open, up to `refill_max_min`.
  chain    `team cycle --due` starts the product's next cycle right after the previous one, in the same process, while
           the cycle did work and startable work is waiting — at most `chain_max` cycles per process. Between two
           cycles every lock is given up for `chain_pause_s` seconds: an operator script waiting for the product lock
           gets in there, a code update gets its turn at the gate; a changed checkout ends the chain.

Both are EXTRA work on top of the cadence the owner set, so both stop by themselves where they would cost what the owner
did not approve (`allowed` below): while the subscription's weekly window is above `max_utilization`, and while the team
runs on bought credits — real money — unless `on_credits: true`. The product's daily cap holds in any case
(`Cycle.role_run`). With both switches off (the default) the cycle is exactly the one of before 25.11.
"""
from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("team.flow")

# ------------------------------------------------------------------ parameters (team.yaml `cycle.flow`, product.yaml `cycle.flow` overrides)

DEFAULTS: dict[str, Any] = {
    "refill": False,              # a freed seat takes the next startable task inside the same execute phase
    "refill_min": 20,             # … until the execute phase is this many minutes old (1..180); later starts wait for the next cycle
    "refill_max_min": 60,         # … and beyond that while reviews of this cycle still run or wait for a slot — the cycle is held by
                                  # them anyway — but never after the phase is this old (refill_min..360)
    "refill_keep_on_failure": False,  # true = a run that failed for its own reason (its cap, the time limit, no answer) does not
                                  # close refill for the cycle; an outage or repeated failures still do (27.11)
    "refill_while_running": False,    # true = past refill_min refill stays open while ANY run of the cycle still goes — an execute
                                  # seat or a review — up to refill_max_min (27.11)
    "early_execute": False,       # true = the seats start on the tasks that are ready at the START of the cycle, beside collect
                                  # and the PO's triage, instead of after them (needs refill; 27.11)
    "retake": False,              # true = a task that became `ready` again inside the cycle (its question was answered, its run
                                  # failed) may be taken again in that cycle while refill is open (27.11)
    "retake_max": 1,              # … this many more times per task and cycle (0..3)
    "chain": False,               # `team cycle --due` starts the next cycle right after a cycle that did work, while work waits
    "chain_max": 6,               # cycles of one product in one `team cycle --due` process (1..50; 1 = no chaining)
    "chain_pause_s": 5,           # seconds without any lock between two chained cycles: the window of a waiting operator script
    "chain_lock_wait_s": 120,     # the next chained cycle waits this long for whoever took the product lock in that window
    "max_utilization": 1.0,       # share of the subscription's weekly window above which refill and chain are off (0.05..1.0)
    "on_credits": False,          # true = refill and chain also while the team runs on bought credits (real money)
}
GIT_TIMEOUT_S = 10                # `git rev-parse HEAD` of the dispatcher checkout, between two chained cycles


@dataclass(frozen=True)
class Config:
    refill: bool = False
    refill_min: float = 20.0
    refill_max_min: float = 60.0
    refill_keep_on_failure: bool = False
    refill_while_running: bool = False
    early_execute: bool = False
    retake: bool = False
    retake_max: int = 1
    chain: bool = False
    chain_max: int = 6
    chain_pause_s: float = 5.0
    chain_lock_wait_s: float = 120.0
    max_utilization: float = 1.0
    on_credits: bool = False

    def describe(self) -> str:
        parts = []
        if self.refill:
            parts.append(f"a freed seat takes the next task for the first {self.refill_min:g} min of the execute phase"
                         + (f" (up to {self.refill_max_min:g} min while reviews hold the cycle)" if self.refill_max_min > self.refill_min else ""))
        if self.chain:
            parts.append(f"up to {self.chain_max} cycles back to back while work waits")
        if not parts:
            return "one task per seat per cycle, a pause between cycles (cycle.flow is off)"
        limit = f"; off above {self.max_utilization:.0%} of the weekly limit" + ("" if self.on_credits else " and on credits")
        return "; ".join(parts) + limit


def _num(v: Any, default: float, lo: float, hi: float, where: str, whole: bool = False) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (whole and int(v) != v):
        log.warning("%s=%r is not a %snumber — %s in use", where, v, "whole " if whole else "", default)
        return default
    if not lo <= v <= hi:
        log.warning("%s=%r is outside %s..%s — clamped", where, v, lo, hi)
    return max(lo, min(hi, float(v)))


def _bool(v: Any, default: bool, where: str) -> bool:
    if not isinstance(v, bool):
        log.warning("%s=%r is not true/false — %s in use", where, v, default)
        return default
    return v


def settings(team_cycle: dict[str, Any] | None, product_raw: dict[str, Any] | None = None, slug: str = "-") -> Config:
    """`cycle.flow` of team.yaml with the product's own `cycle.flow` on top, validated like `cycle.parallel`: a wrong
    value is logged and replaced by the default, never an exception — a typo must not stop the product's cycle."""
    raw = dict(DEFAULTS)
    pcycle = (product_raw or {}).get("cycle")
    for where, block in (("team.yaml", (team_cycle or {}).get("flow")), ("product.yaml", pcycle.get("flow") if isinstance(pcycle, dict) else None)):
        if block is None:
            continue
        if isinstance(block, bool):                      # `flow: true` is the short form of `flow: {refill: true, chain: true}`
            block = {"refill": block, "chain": block}
        if not isinstance(block, dict):
            log.warning("%s: %s cycle.flow must be a mapping like {refill: true, chain: true}, got %r — ignored", slug, where, block)
            continue
        unknown = sorted(set(block) - set(DEFAULTS))
        if unknown:
            log.warning("%s: %s cycle.flow has unknown key(s) %s — ignored", slug, where, ", ".join(unknown))
        raw.update({k: v for k, v in block.items() if k in DEFAULTS})
    pre = f"{slug}: cycle.flow."
    refill_min = _num(raw["refill_min"], float(DEFAULTS["refill_min"]), 1, 180, pre + "refill_min")
    cfg = Config(refill=_bool(raw["refill"], bool(DEFAULTS["refill"]), pre + "refill"),
                 refill_min=refill_min,
                 refill_max_min=max(refill_min, _num(raw["refill_max_min"], float(DEFAULTS["refill_max_min"]), 1, 360, pre + "refill_max_min")),
                 refill_keep_on_failure=_bool(raw["refill_keep_on_failure"], False, pre + "refill_keep_on_failure"),
                 refill_while_running=_bool(raw["refill_while_running"], False, pre + "refill_while_running"),
                 early_execute=_bool(raw["early_execute"], False, pre + "early_execute"),
                 retake=_bool(raw["retake"], False, pre + "retake"),
                 retake_max=int(_num(raw["retake_max"], 1, 0, 3, pre + "retake_max", whole=True)),
                 chain=_bool(raw["chain"], bool(DEFAULTS["chain"]), pre + "chain"),
                 chain_max=int(_num(raw["chain_max"], int(DEFAULTS["chain_max"]), 1, 50, pre + "chain_max", whole=True)),
                 chain_pause_s=_num(raw["chain_pause_s"], float(DEFAULTS["chain_pause_s"]), 0, 600, pre + "chain_pause_s"),
                 chain_lock_wait_s=_num(raw["chain_lock_wait_s"], float(DEFAULTS["chain_lock_wait_s"]), 0, 3600, pre + "chain_lock_wait_s"),
                 max_utilization=_num(raw["max_utilization"], float(DEFAULTS["max_utilization"]), 0.05, 1.0, pre + "max_utilization"),
                 on_credits=_bool(raw["on_credits"], bool(DEFAULTS["on_credits"]), pre + "on_credits"))
    log.debug("%s: flow: %s", slug, cfg.describe())
    return cfg


def allowed(st: Any, cfg: Config) -> str | None:
    """None while extra work (refill, chain) may start; else the reason it may not. Reads data/billing.json — the state
    the last finished run of any product left there (team/billing.py); no state yet = nothing is known against it."""
    from . import billing
    s = billing.load(st)
    if s.get("using_credits") and not cfg.on_credits:
        return "the team runs on bought credits (cycle.flow.on_credits is false)"
    util = s.get("utilization")
    # at 100 % the CLI switches to credits by itself, so a ceiling of 1.0 means «no ceiling»: `using_credits` is the stop there
    if cfg.max_utilization < 1.0 and isinstance(util, (int, float)) and not isinstance(util, bool) and util >= cfg.max_utilization:
        return f"the weekly limit of the subscription is at {util:.0%}, cycle.flow.max_utilization is {cfg.max_utilization:.0%}"
    return None


def code_version(root: Path) -> str:
    """HEAD of the dispatcher checkout, "" when it cannot be read. A chain ends when it changes: the process holds the
    code of the commit it started on, and the role cards and prompts on disk are then of another one."""
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(root), capture_output=True, text=True, timeout=GIT_TIMEOUT_S)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("code version of %s not read: %s", root, exc)
        return ""
