"""Autonomy check: cron completeness, grading and the chat summary never crash and say what is wrong."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from team import autonomy
from team import config as C


def _st(tmp_path: Path) -> C.Settings:
    (tmp_path / "products").mkdir()
    return C.Settings(dict(C.DEFAULTS), tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")


def test_cron_missing_and_complete(tmp_path: Path) -> None:
    st = _st(tmp_path)
    full = "\n".join(f"0 * * * * bash {p}" for p in autonomy.CRON_REQUIRED.values())
    assert autonomy.check_cron(st, full).status == autonomy.OK
    c = autonomy.check_cron(st, full.replace("watchdog.sh >>", "gone"))
    assert c.status == autonomy.FAIL and "watchdog" in c.evidence
    assert autonomy.check_cron(st, "# 7 * * * * bash run-cycle.sh").status == autonomy.FAIL      # commented lines do not count


def test_stop_and_backup(tmp_path: Path) -> None:
    st = _st(tmp_path)
    assert autonomy.check_stop(st).status == autonomy.OK
    (tmp_path / "STOP").write_text("x")
    assert autonomy.check_stop(st).status == autonomy.FAIL
    assert autonomy.check_backup(st, datetime.now(timezone.utc)).status == autonomy.FAIL
    (tmp_path / "data" / "backups").mkdir(parents=True)
    (tmp_path / "data" / "backups" / "products-1.tgz").write_text("x")
    assert autonomy.check_backup(st, datetime.now(timezone.utc)).status == autonomy.OK


def test_run_all_never_crashes_and_summary(tmp_path: Path) -> None:
    st = _st(tmp_path)
    checks = autonomy.run_all(st, tests=False)
    assert len(checks) >= 15 and all(c.status in (autonomy.OK, autonomy.WARN, autonomy.FAIL) for c in checks)
    s = autonomy.summary_for_chat(checks)
    assert s.startswith("Проверка автономности") and "✅" not in s


def test_sites_grading(tmp_path: Path) -> None:
    st = _st(tmp_path)
    d = st.products_dir / "x"
    d.mkdir()
    (d / "VISION.md").write_text("# x")
    p = C.Product("x", d, {**C.PRODUCT_DEFAULTS, "slug": "x", "host": "x.example", "site_base": "https://x.example"}, st)
    assert autonomy.check_sites([p], fetch=lambda u: (200, 80, "")).status == autonomy.OK
    assert autonomy.check_sites([p], fetch=lambda u: (200, 5, "")).status == autonomy.WARN
    assert autonomy.check_sites([p], fetch=lambda u: (None, None, "timeout")).status == autonomy.FAIL


def test_persona_runs_grading(tmp_path: Path) -> None:
    """C18: WARN when a product with scenarios and an open `persona` gate had no persona result for more than 26 h."""
    import json
    from datetime import timedelta
    from team import ux
    st = _st(tmp_path)
    st = C.Settings(json.loads(json.dumps(C.DEFAULTS)), st.root, st.products_dir, st.data_dir, st.repos_dir)
    now = datetime.now(timezone.utc)

    def product(slug: str, scenarios: bool = True, **raw) -> C.Product:
        d = st.products_dir / slug
        (d / "scenarios").mkdir(parents=True)
        if scenarios:
            (d / "scenarios" / "s1.md").write_text("---\nid: s1\npersona: reader\n---\nread\n", encoding="utf-8")
        return C.Product(slug, d, {**C.PRODUCT_DEFAULTS, "slug": slug, **raw}, st)

    def ago(h: float) -> str:
        return (now - timedelta(hours=h)).isoformat(timespec="seconds")

    fresh, stale, never = product("fresh"), product("stale"), product("never")
    old_scores = product("old-scores")
    skipped = [product("no-scenarios", scenarios=False), product("paused", paused=True),
               product("game-m0", profile="game", stage="M0", phase_gates={"persona": "M1"})]
    ux.write_persona_mark(fresh.dir, {"started": ago(1), "ok": 2, "last_ok": ago(13)})
    ux.write_persona_mark(stale.dir, {"started": ago(2), "ok": 0, "last_ok": ago(27)})     # attempts go on, results do not
    ux.scores_path(old_scores).write_text(json.dumps({"ts": ago(24 * 18), "persona": "reader"}) + "\n", encoding="utf-8")

    assert autonomy.check_personas(st, [fresh] + skipped, now).status == autonomy.OK
    c = autonomy.check_personas(st, [fresh, stale, never, old_scores] + skipped, now)
    assert c.id == "C18" and c.status == autonomy.WARN
    assert "fresh 13.0 h" in c.evidence and "stale 27.0 h" in c.evidence and "never never" in c.evidence and "old-scores 432.0 h" in c.evidence
    assert not any(p.slug in c.evidence for p in skipped)
    assert autonomy.check_personas(st, skipped, now).status == autonomy.OK
    st.raw["cycle"]["persona_every_h"] = 0
    off = autonomy.check_personas(st, [stale], now)
    assert off.status == autonomy.OK and "switched off" in off.evidence
    assert any(ch.id == "C18" for ch in autonomy.run_all(st, tests=False))
