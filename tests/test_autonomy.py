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
