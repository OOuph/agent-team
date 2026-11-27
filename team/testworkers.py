"""A limit on the worker processes of the test tools inside one cycle (27.11.2026).

Every developer run and every review starts its own test runner, and each runner starts its own workers: on the team
server (8 cores) six lanes at once gave a load of 9 in the median and 53 at the peak. The limit is passed the only way
that reaches every one of them — the role's run, the tests before a review, a play-test's build, the tests before a merge
— as environment variables of the cycle's process, for the time of the cycle.

product.yaml (or team.yaml) `cycle.test_workers`:
    count: 0            # workers per test run; 0 = off (as before), "auto" = cores // cycle.parallel.max_runs, at least 1
    env: [VITEST_MAX_FORKS, VITEST_MAX_THREADS]     # the variables that carry the number to the product's test tools
"""
from __future__ import annotations

import logging
import os
import re
from contextlib import contextmanager
from typing import Any, Iterator

log = logging.getLogger("team.testworkers")

# ------------------------------------------------------------------ parameters
DEFAULTS: dict[str, Any] = {
    "count": 0,                                              # 0 = off; "auto" = cores // max_runs
    "env": ["VITEST_MAX_FORKS", "VITEST_MAX_THREADS"],       # Vitest 1–3 read both; a product may name its own (E2E_WORKERS, …)
}
COUNT_MAX = 64
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
FORBIDDEN_PREFIXES = ("TEAM_", "ANTHROPIC_", "CLAUDE_", "GITHUB_", "GH_", "AWS_", "PATH", "HOME", "LD_", "NODE_OPTIONS")


def env_for(team_cycle: dict[str, Any] | None, product_raw: dict[str, Any] | None, max_runs: int, slug: str = "-",
            cores: int | None = None) -> dict[str, str]:
    """The variables to set for this product's cycle ({} = the limit is off)."""
    raw = dict(DEFAULTS)
    for block in ((team_cycle or {}).get("test_workers"), ((product_raw or {}).get("cycle") or {}).get("test_workers")):
        if isinstance(block, dict):
            raw.update({k: v for k, v in block.items() if k in DEFAULTS})
    count = raw["count"]
    if isinstance(count, str) and count.strip().lower() == "auto":
        count = max(1, (cores or os.cpu_count() or 1) // max(1, int(max_runs)))
    if isinstance(count, bool) or not isinstance(count, int) or count < 0 or count > COUNT_MAX:
        log.warning("%s: cycle.test_workers.count %r is not a number 0..%d or \"auto\" — the limit is off", slug, raw["count"], COUNT_MAX)
        return {}
    if count == 0:
        return {}
    names = raw["env"] if isinstance(raw["env"], list) else []
    good = [str(n) for n in names if isinstance(n, str) and NAME_RE.match(n) and not n.startswith(FORBIDDEN_PREFIXES)]
    if len(good) != len(names) or not good:
        log.warning("%s: cycle.test_workers.env: only plain variable names are taken (%s of %s)", slug, len(good), len(names))
    return {n: str(count) for n in good}


@contextmanager
def applied(env: dict[str, str], slug: str = "-") -> Iterator[None]:
    """Set the variables in this process for the block and put the old values back afterwards (a `team cycle --due`
    process runs the products one after another: the next product must not inherit this one's limit)."""
    if not env:
        yield
        return
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    log.info("%s: test workers limited for this cycle: %s", slug, ", ".join(f"{k}={v}" for k, v in sorted(env.items())))
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
