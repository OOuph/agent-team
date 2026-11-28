"""The dispatcher checkout moves only through the gate (owner 28.11). 28.11 10:34 UTC a session ran `git fetch && git
merge --ff-only origin/main` in the checkout on the server, in the middle of a cycle of the game: here that very command
must fail, the gate's own update must pass, and a move that happened all the same must be said aloud by the next cycle."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from team import codegate
from team.cycle import Cycle

from test_parallel_lanes import LaneRunner, _events, _product, _settings

REPO = Path(__file__).resolve().parent.parent
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
ENV.pop("TEAM_CODE_GATE", None)


def _git(cwd: Path, *args: str, gate: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, env={**ENV, **({"TEAM_CODE_GATE": "1"} if gate else {})}, capture_output=True, text=True, timeout=60)


def _sh(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "gate-lock.sh", *args], cwd=cwd, env=ENV, capture_output=True, text=True, timeout=60)


@pytest.fixture
def checkout(tmp_path: Path):
    """An origin with two commits and a checkout of the first one, with gate-lock.sh in it — the server in small."""
    origin = tmp_path / "origin"
    origin.mkdir()
    assert _git(origin, "init", "-q", "-b", "main").returncode == 0
    (origin / "a.txt").write_text("one\n", encoding="utf-8")
    shutil.copy(REPO / "gate-lock.sh", origin / "gate-lock.sh")
    _git(origin, "add", "-A"); _git(origin, "commit", "-q", "-m", "one")
    work = tmp_path / "dispatcher"
    assert _git(tmp_path, "clone", "-q", str(origin), str(work)).returncode == 0
    (origin / "a.txt").write_text("two\n", encoding="utf-8")
    _git(origin, "commit", "-q", "-am", "two")
    yield work, origin
    subprocess.run(["chmod", "-R", "u+w", str(work / ".git")])           # pytest must be able to remove the folder


def _head(d: Path) -> str:
    return _git(d, "rev-parse", "HEAD").stdout.strip()


def test_a_direct_pull_is_refused_and_the_gate_passes(checkout):
    work, origin = checkout
    first = _head(work)
    cp = _sh(work, "install")
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert (work / "data/locks/code.version").read_text(encoding="utf-8").split()[0] == first
    status = _sh(work, "status").stdout
    assert "hook: on" in status and (os.geteuid() == 0 or "lock: on" in status)
    # the command of 28.11, word for word
    fetch = _git(work, "fetch", "-q", "origin", "main")
    merge = _git(work, "merge", "-q", "--ff-only", "origin/main")
    assert _head(work) == first and (fetch.returncode != 0 or merge.returncode != 0)
    assert _git(work, "pull", "-q", "--ff-only").returncode != 0 and _head(work) == first
    assert _git(work, "reset", "-q", "--hard", "HEAD").returncode != 0 or os.geteuid() == 0
    # somebody gave the write bit back by hand: the hook answers, and names the gate
    assert _sh(work, "unlock").returncode == 0
    assert _git(work, "fetch", "-q", "origin", "main").returncode == 0     # reading what is new moves nothing
    merge = _git(work, "merge", "-q", "--ff-only", "origin/main")
    assert merge.returncode != 0 and "update-code.sh" in merge.stderr and _head(work) == first
    # the gate: unlock, move with its mark, record, lock — what update-code.sh does
    assert _git(work, "merge", "-q", "--ff-only", "origin/main", gate=True).returncode == 0
    assert _head(work) == _head(origin) != first
    assert _sh(work, "record", "update-code.sh").returncode == 0 and _sh(work, "lock").returncode == 0
    rec = (work / "data/locks/code.version").read_text(encoding="utf-8").split()
    assert rec[0] == _head(origin) and rec[2] == "update-code.sh"


def test_update_code_unlocks_marks_records_and_locks_again():
    """flock is not on every machine the tests run on, so the script is read, not run: the four steps and their order."""
    text = (REPO / "update-code.sh").read_text(encoding="utf-8")
    steps = ["bash gate-lock.sh unlock", "export TEAM_CODE_GATE=1", 'git merge -q --ff-only "origin/$BRANCH"', "bash gate-lock.sh record update-code.sh"]
    at = [text.index(s) for s in steps]
    assert at == sorted(at)
    assert "trap 'rm -f data/locks/update.pending; bash gate-lock.sh lock" in text      # locked again on ANY exit, a failed merge too


def test_a_checkout_moved_outside_the_gate_is_said_aloud_once_per_commit(tmp_path: Path, monkeypatch):
    st = _settings(tmp_path)
    p = _product(st)
    head = {"v": "a" * 40}
    monkeypatch.setattr(codegate.flow, "code_version", lambda root: head["v"])
    said: list[str] = []
    assert codegate.check(st, "fleets", None, said.append) is None and said == []       # no record: no gate, as before
    rec = st.data_dir / codegate.RECORD_FILE
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text("a" * 40 + " 2026-11-28T06:52:26Z update-code.sh\n", encoding="utf-8")
    assert codegate.check(st, "fleets", None, said.append) is None and said == []       # where the gate left it
    head["v"] = "b" * 40                                                                 # 10:34: moved by hand
    monkeypatch.setattr(Cycle, "tell_owner", lambda self, text: said.append(text))
    for i in range(2):
        cyc = Cycle(st, p, LaneRunner(), f"c{i}")
        res = cyc.run(["measure"])
        cyc.store.close()
        assert "measure" in res["phases"]                                               # said aloud; the cycle goes on
    ev = _events(p, codegate.EVENT)
    assert len(ev) == 2 and ev[0]["detail"]["head"] == "b" * 12 and ev[0]["detail"]["recorded"] == "a" * 12
    assert len(said) == 1 and "bbbbbbb" in said[0] and "aaaaaaa" in said[0] and "update-code.sh" in said[0]
    head["v"] = "c" * 40
    assert codegate.check(st, "fleets", None, said.append)["head"] == "c" * 40 and len(said) == 2      # another commit: another line
    monkeypatch.setattr(codegate.flow, "code_version", lambda root: "")
    assert codegate.check(st, "fleets", None, said.append) is None                      # HEAD unreadable: nothing is claimed
