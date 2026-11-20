"""Cycle cadence per product (owner 20.11, friction F12): `product.yaml` `cycle: {every_min: N}`, the choice of due
products for `team cycle --due` (last finished cycle in the `cycles` table of team.db, STOP files, no setting), the
cadence line of `team status`, and the cron entry run-cycle-fast.sh. No role runs, no network."""
from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from team import cadence, cli, config as C
from team.store import Store

REPO = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 20, 18, 30, tzinfo=timezone.utc)


def _settings(tmp_path: Path) -> C.Settings:
    return C.Settings(C._merge(C.DEFAULTS, {}), tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")


def _product(st: C.Settings, slug: str, cycle_yaml: str = "", extra: str = "") -> C.Product:
    d = st.products_dir / slug
    d.mkdir(parents=True)
    (d / "product.yaml").write_text(f"slug: {slug}\nname: {slug}\nhost: ''\n{cycle_yaml}{extra}", encoding="utf-8")
    (d / "VISION.md").write_text(f"# {slug}\n", encoding="utf-8")
    return C.load_product(st, d)


def _cycle_row(p: C.Product, cycle_id: str, finished_min_ago: float | None, started_min_ago: float | None = None) -> None:
    """One row of `cycles`, as Store.cycle_start / cycle_finish leave it; finished_min_ago=None = a cycle that never finished."""
    started = NOW - timedelta(minutes=started_min_ago if started_min_ago is not None else (finished_min_ago or 0) + 2)
    finished = (NOW - timedelta(minutes=finished_min_ago)).isoformat(timespec="seconds") if finished_min_ago is not None else None
    s = Store(p.dir)
    s.db.execute("INSERT INTO cycles(cycle_id, started, finished, phases, spent_usd) VALUES(?,?,?,?,0)",
                 (cycle_id, started.isoformat(timespec="seconds"), finished, "[]"))
    s.db.commit()
    s.close()


# ------------------------------------------------------------------ product.yaml: cycle.every_min

def test_no_setting_means_hourly(tmp_path: Path):
    p = _product(_settings(tmp_path), "site")
    assert p.cycle_every_min is None and p.cadence == "hourly"
    assert _product(_settings(tmp_path / "b"), "site", "cycle: {}\n").cycle_every_min is None


@pytest.mark.parametrize("n", [5, 10, 60])
def test_every_min_inside_range_is_read(tmp_path: Path, n: int):
    p = _product(_settings(tmp_path), "game", f"cycle:\n  every_min: {n}\n")
    assert p.cycle_every_min == n and p.cadence == f"cycle every {n} min"


@pytest.mark.parametrize("bad", ["4", "61", "0", "-10", "7.5", "'10'", "ten", "true", "[10]"])
def test_every_min_outside_range_or_not_an_integer_fails_the_load(tmp_path: Path, bad: str):
    with pytest.raises(ValueError, match=r"cycle\.every_min must be a whole number of minutes 5\.\.60"):
        _product(_settings(tmp_path), "game", f"cycle:\n  every_min: {bad}\n")


def test_cycle_block_must_be_a_mapping(tmp_path: Path):
    with pytest.raises(ValueError, match="`cycle` must be a mapping"):
        _product(_settings(tmp_path), "game", "cycle: 10\n")


# ------------------------------------------------------------------ which products are due

def test_product_without_setting_is_never_due_and_its_store_is_not_touched(tmp_path: Path):
    st = _settings(tmp_path)
    old = _product(st, "old-site")
    _cycle_row(old, "c-old", finished_min_ago=600)                 # long idle, still hourly only
    fresh = _product(st, "fresh-site")
    assert cadence.check(st, old, NOW).due is False and "hourly" in cadence.check(st, old, NOW).reason
    assert cadence.check(st, fresh, NOW).due is False
    assert not (fresh.dir / "team.db").exists()                    # the decision is read-only: no store created by a check
    assert cadence.due_products(st, [old, fresh], NOW) == []


def test_due_by_last_finished_cycle(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st, "game", "cycle:\n  every_min: 10\n")
    d = cadence.check(st, p, NOW)
    assert d.due and d.reason == "no finished cycle yet" and not (p.dir / "team.db").exists()

    _cycle_row(p, "c1", finished_min_ago=3)
    d = cadence.check(st, p, NOW)
    assert not d.due and d.every_min == 10 and d.wait_min == pytest.approx(7, abs=0.1)
    assert d.last_finished == NOW - timedelta(minutes=3)
    assert not cadence.check(st, p, NOW + timedelta(minutes=6, seconds=59)).due      # 9 min 59 s
    assert cadence.check(st, p, NOW + timedelta(minutes=7)).due                      # exactly every_min
    assert cadence.check(st, p, NOW + timedelta(hours=5)).due


def test_last_finished_is_the_newest_finished_row_whoever_ran_it(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st, "game", "cycle:\n  every_min: 10\n")
    _cycle_row(p, "20261120T1707", finished_min_ago=80)            # the hourly cron's cycle
    _cycle_row(p, "20261120T1820", finished_min_ago=None, started_min_ago=1)   # crashed / never finished: ignored
    assert cadence.check(st, p, NOW).due
    _cycle_row(p, "20261120T1825", finished_min_ago=4)             # hourly or fast, same table → both push the next start
    assert not cadence.check(st, p, NOW).due
    assert cadence.last_finished(p) == NOW - timedelta(minutes=4)


def test_stop_files_pause_and_onboarding_keep_a_due_product_out(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st, "game", "cycle:\n  every_min: 5\n")
    _cycle_row(p, "c1", finished_min_ago=30)
    assert cadence.check(st, p, NOW).due

    p.stop_file.write_text("owner\n")
    d = cadence.check(st, p, NOW)
    assert not d.due and "stopped" in d.reason and str(p.stop_file) in d.reason
    p.stop_file.unlink()

    (st.root / "STOP").write_text("owner\n")                       # global STOP
    assert not cadence.check(st, p, NOW).due
    (st.root / "STOP").unlink()
    assert cadence.check(st, p, NOW).due

    paused = _product(st, "paused-game", "cycle:\n  every_min: 5\n", "paused: true\n")
    assert not cadence.check(st, paused, NOW).due
    onb = _product(st, "new-game", "cycle:\n  every_min: 5\n", "onboarding:\n  state: questions\n")
    d = cadence.check(st, onb, NOW)
    assert not d.due and "onboarding not finished" in d.reason


def test_due_products_picks_only_the_due_ones(tmp_path: Path):
    st = _settings(tmp_path)
    hourly = _product(st, "a-site")
    due = _product(st, "b-game", "cycle:\n  every_min: 10\n")
    recent = _product(st, "c-game", "cycle:\n  every_min: 30\n")
    stopped = _product(st, "d-game", "cycle:\n  every_min: 10\n")
    for p, ago in ((hourly, 55), (due, 12), (recent, 12), (stopped, 50)):
        _cycle_row(p, "c1", finished_min_ago=ago)
    stopped.stop_file.write_text("owner\n")
    assert [p.slug for p in cadence.due_products(st, C.load_products(st), NOW)] == ["b-game"]


# ------------------------------------------------------------------ team cycle --due, team status

class _FakeCycle:
    ran: list[str] = []

    def __init__(self, st, p, runner, cycle_id, dry_run=False):
        self.p = p

    def run(self, phases=None):
        _FakeCycle.ran.append(self.p.slug)
        return {"phases": [], "runs": 0, "spent_usd": 0}


def test_cli_cycle_due_runs_only_due_products_and_skips_the_hourly_framework_work(tmp_path: Path, monkeypatch, capsys):
    st = _settings(tmp_path)
    _product(st, "a-site")
    game = _product(st, "b-game", "cycle:\n  every_min: 10\n")
    monkeypatch.setattr(cli.C, "load", lambda *a, **k: st)
    monkeypatch.setattr(cli, "Cycle", _FakeCycle)
    from team import friction

    def _boom(*a, **k):
        raise AssertionError("the hourly-only framework work must not run under --due")
    monkeypatch.setattr(friction, "collect_all", _boom)
    monkeypatch.setattr(friction, "check_rollback", _boom)

    _FakeCycle.ran = []
    assert cli.main(["cycle", "--due"]) == 0                       # no finished cycle yet → due
    assert _FakeCycle.ran == ["b-game"] and "b-game:" in capsys.readouterr().out

    s = Store(game.dir)                                            # a cycle that has just finished
    s.cycle_start("c1"); s.cycle_finish("c1", [], 0.0); s.close()
    _FakeCycle.ran = []
    assert cli.main(["cycle", "--due"]) == 0
    assert _FakeCycle.ran == [] and capsys.readouterr().out == ""  # nothing due: silent, exit 0

    game.stop_file.write_text("owner\n")
    s = Store(game.dir); s.db.execute("UPDATE cycles SET finished='2026-01-01T00:00:00+00:00'"); s.db.commit(); s.close()
    assert cli.main(["cycle", "--due"]) == 0 and _FakeCycle.ran == []      # old enough, but STOP


def test_status_shows_the_cadence(tmp_path: Path, monkeypatch, capsys):
    st = _settings(tmp_path)
    _product(st, "a-site")
    game = _product(st, "b-game", "cycle:\n  every_min: 10\n")
    monkeypatch.setattr(cli.C, "load", lambda *a, **k: st)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    site_block, game_block = out.split("== b-game")
    assert "cadence: hourly" in site_block
    assert "cadence: cycle every 10 min · due now" in game_block

    s = Store(game.dir); s.cycle_start("c1"); s.cycle_finish("c1", [], 0.0); s.close()
    assert cli.main(["status", "b-game"]) == 0
    assert "cadence: cycle every 10 min · next in 10 min" in capsys.readouterr().out
    game.stop_file.write_text("owner\n")
    assert cli.main(["status", "b-game"]) == 0
    assert "cadence: cycle every 10 min · stopped" in capsys.readouterr().out


# ------------------------------------------------------------------ run-cycle-fast.sh (the cron entry)

FAKE_TEAM = "#!/usr/bin/env bash\necho \"$@\" >> \"$(dirname \"$0\")/../../team-calls.txt\"\n"
# util-linux `flock` is not on every developer machine; the stub keeps the contract the script relies on:
# `flock -n LOCK cmd…` runs cmd, or exits 1 at once when the lock is held (here: when LOCK.held exists).
FAKE_FLOCK = "#!/usr/bin/env bash\n[ \"$1\" = -n ] || exit 64\nlock=\"$2\"; shift 2\necho \"$lock\" >> flock-locks.txt\n[ -e \"$lock.held\" ] && exit 1\nexec \"$@\"\n"


def _fast_checkout(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    root = tmp_path / "dispatcher"
    (root / ".venv" / "bin").mkdir(parents=True)
    shutil.copy(REPO / "run-cycle-fast.sh", root / "run-cycle-fast.sh")
    team = root / ".venv" / "bin" / "team"
    team.write_text(FAKE_TEAM); team.chmod(0o755)
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    (fake_bin / "flock").write_text(FAKE_FLOCK); (fake_bin / "flock").chmod(0o755)
    return root, {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}


def _calls(root: Path) -> list[str]:
    f = root / "team-calls.txt"
    return f.read_text().splitlines() if f.exists() else []


def test_fast_script_runs_due_cycle_under_the_hourly_lock(tmp_path: Path):
    root, env = _fast_checkout(tmp_path)
    r = subprocess.run(["bash", str(root / "run-cycle-fast.sh")], env=env, cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert _calls(root) == ["cycle --due --product all"]
    assert (root / "flock-locks.txt").read_text().split() == ["data/cycle.lock"]
    assert "flock -n data/cycle.lock" in (REPO / "run-cycle.sh").read_text()        # the very lock of the hourly entry
    assert not (root / "data" / "framework" / "last-good").exists()                 # the rollback point stays hourly

    subprocess.run(["bash", str(root / "run-cycle-fast.sh"), "ai-battles"], env=env, check=True)
    assert _calls(root)[-1] == "cycle --due --product ai-battles"


def test_fast_script_does_nothing_while_a_cycle_holds_the_lock_or_stop_is_set(tmp_path: Path):
    root, env = _fast_checkout(tmp_path)
    (root / "data").mkdir()
    (root / "data" / "cycle.lock.held").write_text("")              # the hourly cycle is running
    r = subprocess.run(["bash", str(root / "run-cycle-fast.sh")], env=env)
    assert r.returncode == 1 and _calls(root) == []
    (root / "data" / "cycle.lock.held").unlink()
    (root / "flock-locks.txt").unlink()

    (root / "STOP").write_text("owner\n")
    r = subprocess.run(["bash", str(root / "run-cycle-fast.sh")], env=env)
    assert r.returncode == 0 and _calls(root) == [] and not (root / "flock-locks.txt").exists()


@pytest.mark.skipif(shutil.which("flock") is None, reason="util-linux flock not installed")
def test_fast_script_with_real_flock_never_overlaps(tmp_path: Path):
    root, env = _fast_checkout(tmp_path)
    env = {**env, "PATH": os.environ["PATH"]}                       # the real flock
    (root / "data").mkdir()
    holder = subprocess.Popen(["flock", "-n", str(root / "data" / "cycle.lock"), "sleep", "5"])
    try:
        import time
        time.sleep(0.5)
        r = subprocess.run(["bash", str(root / "run-cycle-fast.sh")], env=env)
        assert r.returncode == 1 and _calls(root) == []
    finally:
        holder.kill(); holder.wait()
    assert subprocess.run(["bash", str(root / "run-cycle-fast.sh")], env=env).returncode == 0
    assert _calls(root) == ["cycle --due --product all"]


def test_server_crontab_has_one_fast_line_for_all_products():
    lines = [ln for ln in (REPO / "deploy" / "server" / "crontab.txt").read_text().splitlines() if "run-cycle-fast.sh" in ln]
    assert lines == ["*/5 * * * * bash /home/ubuntu/agent-team/dispatcher/run-cycle-fast.sh"]
