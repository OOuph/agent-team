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

log = logging.getLogger("team.config")

ROOT = Path(os.environ.get("TEAM_ROOT") or Path(__file__).resolve().parent.parent)

DEFAULTS: dict[str, Any] = {
    "paths": {"products_dir": "products", "data_dir": "data", "repos_dir": "data/repos"},
    "claude": {"binary": "claude", "auth": "oauth_token", "timeout_s": 1500, "inactivity_s": 300, "max_turns": 60,
               "models": {"po": "opus", "developer": "sonnet", "reviewer": "sonnet", "analyst": "sonnet",
                          "marketer": "sonnet", "persona": "sonnet", "geo-probe": "sonnet"}},
    "budget": {"max_usd_per_product_per_day": 20.0, "max_usd_per_run": 3.0, "min_usd_per_run": 0.3, "unknown_cost_reserve_usd": 0.5, "warn_share": 0.8},
    "cycle": {"review_after_min": 6, "measure_after_h": 24, "max_review_fails": 2, "persona_every_n_cycles": 6,
              "digest_hour_utc": 7, "digest_hours_utc": [7, 19], "measure_every_h": 6, "seo_every_h": 12, "geo_every_h": 168,
              "max_ready": 3, "max_execute_per_cycle": 1},
    "publish": {"disclosure": {"default": "AI agent - formachines.ai"}, "max_posts_per_channel_per_day": 2,
                "dry_run": False},
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

    @property
    def repo_dir(self) -> Path:
        return self.st.repos_dir / self.slug

    @property
    def runs_dir(self) -> Path:
        return self.st.data_dir / "runs" / self.slug

    @property
    def work_dir(self) -> Path:
        return self.st.data_dir / "work" / self.slug

    @property
    def daily_cap(self) -> float:
        v = self.raw.get("budget_usd_per_day")
        return float(v) if v is not None else float(self.st.budget["max_usd_per_product_per_day"])

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
    return Product(str(raw["slug"]), d.resolve(), raw, st)


def load_products(st: Settings) -> list[Product]:
    return [load_product(st, d) for d in st.product_dirs()]
