"""Friction register (deterministic + reported), retro plumbing, public export."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from team import config as C
from team import export, friction, retro
from team.store import Store


def _settings(tmp_path: Path) -> C.Settings:
    raw = {**C.DEFAULTS, "framework": {"retro_every_h": 24, "repo": "", "issues": False, "product": ""}}
    return C.Settings(raw, tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")


def _product(st: C.Settings, slug: str = "p1") -> C.Product:
    d = st.products_dir / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "product.yaml").write_text(yaml.safe_dump({"slug": slug, "name": slug, "host": "p1.example"}), encoding="utf-8")
    (d / "VISION.md").write_text("# p1\n\n## Цели\n| id | метрика | опр | база | цель | как |\n|---|---|---|---|---|---|\n| G1 | визиты | люди | 0 | 10 | логи |\n\n## Каналы\n", encoding="utf-8")
    return C.Product(slug, d, {**C.PRODUCT_DEFAULTS, "slug": slug, "name": slug, "host": "p1.example"}, st)


def test_friction_collects_signals_and_dedupes(tmp_path: Path) -> None:
    st = _settings(tmp_path)
    p = _product(st)
    s = Store(p.dir)
    t = s.create_task(title="x", type_="feature", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po", state="ready")
    s.transition(t["id"], "blocked", "dispatcher", "merge/push failed: boom", force=True)
    s.add_result(t["id"], {"run_id": "r1", "status": "done", "friction": [{"kind": "missing_tool", "what": "no curl"}]})
    s.add_review(t["id"], {"verdict": "inconclusive", "summary": "site down", "friction": [{"kind": "missing_data", "what": "page cut"}]}, "r2")
    s.event("run", "r3", "developer", {"ok": False, "error": "timeout"})
    s.event("phase.error", "execute", "dispatcher", "KeyError")
    rd = p.runs_dir / "c1" / "dev"
    rd.mkdir(parents=True)
    (rd / "run.json").write_text(json.dumps({"run_id": "r4", "role": "developer", "denials": [{"tool_name": "WebFetch"}], "finished": "2026-11-02T10:00:00+00:00"}), encoding="utf-8")
    recs = friction.collect_product(p, s)
    kinds = sorted(r["kind"] for r in recs)
    assert kinds == ["phase_error", "reported", "reported", "review_inconclusive", "run_failed", "task_blocked", "tool_denied"]
    reg = friction.Register(st.data_dir / "framework")
    assert reg.add(recs) == 7
    assert reg.add(friction.collect_product(p, s)) == 0        # idempotent
    md = reg.register_md(7)
    assert "tool_denied" in md and "WebFetch" in md and "F-0001" in md
    out = reg.export_public(tmp_path / "pub")
    j = json.loads(out.read_text(encoding="utf-8"))
    assert j["total"] == 7 and j["by_kind"]["reported"] == 2
    s.close()


def test_friction_redacts_secrets() -> None:
    assert "ghp_abc" not in friction.redact("push https://x-access-token:ghp_abc123@github.com failed")


def test_retro_due_and_dry_run(tmp_path: Path) -> None:
    st = _settings(tmp_path)
    p = _product(st)
    s = Store(p.dir)
    s.event("run", "r1", "po", {"ok": False, "error": "schema"})
    s.close()
    assert retro.due(st)
    assert friction.collect_all(st, [p]) == 1
    assert retro.run_retro(st, None, dry_run=True) is None
    prompt = (st.data_dir / "framework" / "retro" / "prompt-dry-run.md").read_text(encoding="utf-8")
    assert "run_failed" in prompt and "## Role cards" in prompt
    (st.data_dir / "framework" / "last-retro").write_text("2099-01-01T00:00:00+00:00", encoding="utf-8")
    assert not retro.due(st)


def test_export_public_day_files(tmp_path: Path) -> None:
    st = _settings(tmp_path)
    p = _product(st)
    s = Store(p.dir)
    t = s.create_task(title="feed", type_="feature", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po", state="ready")
    s.ask(from_="po", to="owner", question="secret question text")
    s.close()
    files = export.export_product(p, tmp_path / "pub", days=2)
    assert len(files) == 1
    d = json.loads(files[0].read_text(encoding="utf-8"))
    assert d["tasks"][0]["id"] == t["id"] and d["owner_questions_open"] == 1
    assert "secret question text" not in files[0].read_text(encoding="utf-8")
    pj = json.loads((tmp_path / "pub" / "p1" / "product.json").read_text(encoding="utf-8"))
    assert pj["goals"][0]["id"] == "G1"


def test_pr_mode_config(tmp_path: Path) -> None:
    st = _settings(tmp_path)
    p = C.Product("fw", tmp_path / "fw", {**C.PRODUCT_DEFAULTS, "repo": {"url": "https://github.com/o/r.git", "mode": "pr", "test_cmd": "true"}}, st)
    assert p.repo_mode == "pr" and p.test_cmd == "true"
    assert C.Product("x", tmp_path / "x", dict(C.PRODUCT_DEFAULTS), st).repo_mode == "merge"
