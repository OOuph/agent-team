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

log = logging.getLogger("team.config")

ROOT = Path(os.environ.get("TEAM_ROOT") or Path(__file__).resolve().parent.parent)

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
              "parallel": dict(PARALLEL_DEFAULTS)},
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
