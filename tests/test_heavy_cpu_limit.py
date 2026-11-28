"""`cycle.heavy_runs.cpu_quota` — one processor limit for every heavy run of the server, kept by the system.

28.11 12:07 UTC, ai-battles on 8 cores at a load of 38: two places for heavy runs and five of them going — the
dispatcher's suite, a build of the tower, two `npm test` of developers and a simulation. A run that waited `wait_s`
starts all the same (refusing it had cost the developers their tests that morning), so the queue is a delay, not a
limit. The limit is a systemd slice of the user that all of them share. systemd is not on the machine the tests run on:
a stand-in `systemd-run` / `systemctl` on PATH records what it was asked."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from team import cycle as cycle_mod
from team import heavy
from team.cycle import Cycle

from test_parallel_lanes import LaneRunner, _events, _product, _settings

REPO = Path(__file__).resolve().parent.parent
QUEUE = {"cycle": {"heavy_runs": {"queue": ["Bash(npm test:*)"], "roles": ["developer"], "slots": 2, "busy": "run", "cpu_quota": 600}}}

FAKE_RUN = """#!/bin/sh
# stand-in for systemd-run: needs the user's runtime directory as the real one does, records its arguments, runs the command
[ -n "$XDG_RUNTIME_DIR" ] || { echo "Failed to connect to bus: No medium found" >&2; exit 1; }
[ -f "$FAKE_DIR/broken" ] && { echo "Failed to start transient scope unit: Access denied" >&2; exit 1; }
echo "$@" >> "$FAKE_DIR/systemd-run.log"
while [ "$1" != "--" ]; do shift; done
shift
exec "$@"
"""
FAKE_CTL = """#!/bin/sh
[ -f "$FAKE_DIR/broken" ] && { echo "Failed to set unit properties: Access denied" >&2; exit 1; }
echo "$XDG_RUNTIME_DIR $@" >> "$FAKE_DIR/systemctl.log"
"""


@pytest.fixture
def fake(tmp_path: Path, monkeypatch):
    d = tmp_path / "fakebin"
    d.mkdir()
    for name, body in (("systemd-run", FAKE_RUN), ("systemctl", FAKE_CTL)):
        (d / name).write_text(body, encoding="utf-8")
        (d / name).chmod((d / name).stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DIR", str(d))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)             # cron gives a run none (28.11: «No medium found»)
    return d


def _log(d: Path, name: str) -> list[str]:
    f = d / name
    return f.read_text(encoding="utf-8").splitlines() if f.exists() else []


def _heavy(tmp_path: Path, *cmd: str, quota: int | None = 600) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(REPO), "TEAM_HEAVY_LOCK": str(tmp_path / "locks" / "heavy.lock"), "TEAM_HEAVY_NICE": "5",
           "TEAM_HEAVY_WAIT_S": "5", "TEAM_HEAVY_SLOTS": "2", "TEAM_HEAVY_BUSY": "run"}
    if quota is not None:
        env["TEAM_HEAVY_CPU_QUOTA"] = str(quota)
    return subprocess.run([sys.executable, "-m", "team.heavy", *cmd], env=env, cwd=tmp_path, capture_output=True, text=True, timeout=120)


def test_with_a_quota_the_command_runs_inside_the_shared_slice(tmp_path: Path, fake: Path):
    cp = _heavy(tmp_path, sys.executable, "-c", "import os,sys; print('ran', bool(os.environ.get('XDG_RUNTIME_DIR'))); sys.exit(3)")
    assert cp.returncode == 3 and "ran True" in cp.stdout, cp.stderr              # the exit code and the output come back as before
    assert f"inside {heavy.SLICE}" in cp.stderr
    calls = _log(fake, "systemd-run.log")
    assert len(calls) == 2 and calls[0].endswith("-- true")                       # the trial start, then the command
    assert all(f"--user --scope --quiet --collect --slice={heavy.SLICE} --" in c for c in calls)


def test_without_the_setting_nothing_changes(tmp_path: Path, fake: Path):
    for quota in (None, 0):
        cp = _heavy(tmp_path, sys.executable, "-c", "print('ran')", quota=quota)
        assert cp.returncode == 0 and "ran" in cp.stdout and "slice" not in cp.stderr
    assert _log(fake, "systemd-run.log") == []


def test_where_the_slice_cannot_be_used_the_command_still_runs_and_says_so(tmp_path: Path, fake: Path):
    (fake / "broken").write_text("x", encoding="utf-8")
    cp = _heavy(tmp_path, sys.executable, "-c", "print('ran')")
    assert cp.returncode == 0 and "ran" in cp.stdout                              # a limit that does not work never costs a role its tests
    assert "the shared processor limit is not in use (the trial start ended with 1: Failed to start transient scope unit" in cp.stderr
    (fake / "systemd-run").unlink()
    prefix, why = heavy.scope_prefix(600, {"PATH": str(fake)})
    assert prefix == [] and why == "systemd-run is not on this machine"
    assert heavy.scope_prefix(0) == ([], "off")


def test_the_cycle_keeps_the_slice_at_its_quota_and_says_whether_the_limit_works(tmp_path: Path, fake: Path):
    st = _settings(tmp_path, QUEUE)
    p = _product(st)
    assert p.heavy_runs_cfg["cpu_quota"] == 600
    cyc = Cycle(st, p, LaneRunner(), "c1")
    res = cyc.run(["measure"])
    assert "measure" in res["phases"]
    assert cyc.heavy_env()["TEAM_HEAVY_CPU_QUOTA"] == "600"                        # what `team-heavy` of a role's run reads
    cyc.store.close()
    ctl = _log(fake, "systemctl.log")
    assert len(ctl) == 1 and ctl[0].endswith(f"--user set-property --runtime {heavy.SLICE} CPUQuota=600%")
    assert ctl[0].startswith(f"/run/user/{os.getuid()} ")                         # found without cron's help
    ev = _events(p, "heavy.cpu_limit")
    assert len(ev) == 1 and ev[0]["detail"] == {"ok": True, "quota": 600, "slice": heavy.SLICE, "why": ""}
    (fake / "broken").write_text("x", encoding="utf-8")
    cyc = Cycle(st, p, LaneRunner(), "c2")
    res = cyc.run(["measure"])
    cyc.store.close()
    assert "measure" in res["phases"]                                             # said aloud; the cycle goes on
    bad = [e["detail"] for e in _events(p, "heavy.cpu_limit") if not e["detail"]["ok"]]
    assert len(bad) == 1 and "Access denied" in bad[0]["why"]


def test_off_by_default_the_cycle_asks_the_system_nothing(tmp_path: Path, fake: Path):
    st = _settings(tmp_path)
    p = _product(st)
    cyc = Cycle(st, p, LaneRunner(), "c1")
    cyc.run(["measure"])
    assert "TEAM_HEAVY_CPU_QUOTA" not in cyc.heavy_env()
    cyc.store.close()
    assert _log(fake, "systemctl.log") == [] and _log(fake, "systemd-run.log") == [] and _events(p, "heavy.cpu_limit") == []
    bad = _product(_settings(tmp_path / "b", {"cycle": {"heavy_runs": {"cpu_quota": "all"}}}))
    assert bad.heavy_runs_cfg["cpu_quota"] == 0


def test_the_dispatchers_own_suite_runs_inside_the_slice_too(tmp_path: Path, fake: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for args in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "one"]):
        subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    prefix, why = heavy.scope_prefix(600)
    assert prefix and not why
    out = cycle_mod.run_tests_for_review(repo, sha, tmp_path / "clone", "echo suite-ran && exit 4", wrap=prefix)
    assert "suite-ran" in out and "exit code 4" in out.replace("exit 4", "exit code 4")
    assert any(c.endswith("-- sh -c echo suite-ran && exit 4") for c in _log(fake, "systemd-run.log"))
