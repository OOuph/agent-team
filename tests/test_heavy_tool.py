"""`team-heavy` as a real command: one heavy run at a time on the server, at low priority (27.11: a balance simulation at
174 % of a core beside three test suites, on a server at a load of 33)."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _heavy(tmp_path: Path, *cmd: str, wait_s: int = 20, nice: int = 15, background: bool = False):
    env = {**os.environ, "PYTHONPATH": str(REPO), "TEAM_HEAVY_LOCK": str(tmp_path / "locks" / "heavy.lock"), "TEAM_HEAVY_NICE": str(nice),
           "TEAM_HEAVY_WAIT_S": str(wait_s)}
    argv = [sys.executable, "-m", "team.heavy", *cmd]
    if background:
        return subprocess.Popen(argv, env=env, cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    return subprocess.run(argv, env=env, cwd=tmp_path, capture_output=True, text=True, timeout=120)


def test_the_command_runs_at_low_priority_and_its_output_and_exit_code_come_back(tmp_path: Path):
    cp = _heavy(tmp_path, sys.executable, "-c", "import os,sys; print('nice', os.nice(0)); sys.exit(3)", nice=11)
    base = os.nice(0)
    assert cp.returncode == 3 and f"nice {min(19, base + 11)}" in cp.stdout
    assert "started after 0s of waiting, at nice 11" in cp.stderr


def test_two_heavy_runs_do_not_overlap(tmp_path: Path):
    mark = "import time,sys; open(sys.argv[1],'a').write('start %f\\n' % time.time()); time.sleep(1.5); open(sys.argv[1],'a').write('end %f\\n' % time.time())"
    a = _heavy(tmp_path, sys.executable, "-c", mark, str(tmp_path / "log.txt"), background=True)
    time.sleep(0.4)
    b = _heavy(tmp_path, sys.executable, "-c", mark, str(tmp_path / "log.txt"), background=True)
    assert a.wait(60) == 0 and b.wait(60) == 0
    lines = (tmp_path / "log.txt").read_text().split("\n")
    assert [ln.split()[0] for ln in lines if ln] == ["start", "end", "start", "end"]      # the second waited for the first
    assert "waiting for it to end" in b.stderr.read()


def test_it_gives_up_when_the_server_stays_busy_and_says_what_to_do(tmp_path: Path):
    a = _heavy(tmp_path, sys.executable, "-c", "import time; time.sleep(6)", background=True)
    time.sleep(0.5)
    cp = _heavy(tmp_path, sys.executable, "-c", "print('must not run')", wait_s=1)
    a.kill(); a.wait(10)
    assert cp.returncode == 75 and "must not run" not in cp.stdout and "Report the command in your result" in cp.stderr


def test_no_command_and_an_unknown_command(tmp_path: Path):
    assert _heavy(tmp_path).returncode == 2
    assert _heavy(tmp_path, "no-such-command-xyz").returncode == 127


def test_the_tool_is_on_the_path_the_roles_get():
    script = REPO / "bin" / "team-heavy"
    assert script.exists() and os.access(script, os.X_OK) and "team.heavy" in script.read_text()
    assert 'team-heavy = "team.heavy:main"' in (REPO / "pyproject.toml").read_text()
