"""Locks of the cycle entries (friction F17, 20.11; team/locks.py, SCHEME §3): the cycle of one product must not hold up
the cycles of the others, and whatever replaces the dispatcher's code must be alone on it.

The "other process" in these tests is a real one (a child python that takes the same flock), because flock is a lock
between open file descriptions and the point is what cron entries started side by side do to each other.
No role runs, no network: cycles are either a stand-in class or a `--dry-run` of an empty product.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from team import cadence, cli, config as C, gitops, locks
from team.cycle import Cycle
from team.store import Store

REPO = Path(__file__).resolve().parent.parent
FAST = {"start_wait_s": 0.3, "code_update_wait_s": 10.0, "restore_wait_s": 5.0, "canary_state_wait_s": 0.3, "poll_s": 0.05}

# argv: seconds, then pairs MODE PATH (MODE = sh | ex). Takes every lock, prints "held", keeps them for `seconds`.
HOLDER = """
import fcntl, sys, time
fhs = []
args = sys.argv[2:]
for mode, path in zip(args[::2], args[1::2]):
    fh = open(path, "a+")
    fcntl.flock(fh, fcntl.LOCK_SH if mode == "sh" else fcntl.LOCK_EX)
    fhs.append(fh)
print("held", flush=True)
time.sleep(float(sys.argv[1]))
"""
# argv: TEAM_ROOT is in the environment. Prints what a starting `team cycle` would get from the code lock.
PROBE = "from team import config as C, locks\nst = C.load()\nwith locks.cycles(st) as ok:\n    print('start' if ok else 'no-start')\n"


def _settings(tmp_path: Path, **lock_cfg: float) -> C.Settings:
    st = C.Settings(C._merge(C.DEFAULTS, {}), tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")
    st.raw["locks"] = {**FAST, **lock_cfg}
    return st


def _product(st: C.Settings, slug: str, cycle_yaml: str = "") -> C.Product:
    d = st.products_dir / slug
    d.mkdir(parents=True)
    (d / "product.yaml").write_text(f"slug: {slug}\nname: {slug}\nhost: ''\n{cycle_yaml}", encoding="utf-8")
    (d / "VISION.md").write_text(f"# {slug}\n", encoding="utf-8")
    return C.load_product(st, d)


class _Holder:
    """Another process holding locks: `with _Holder(2.0, ("ex", path), ("sh", other)):`."""

    def __init__(self, seconds: float, *locks_: tuple[str, Path]):
        argv = [sys.executable, "-c", HOLDER, str(seconds)]
        for mode, path in locks_:
            path.parent.mkdir(parents=True, exist_ok=True)
            argv += [mode, str(path)]
        self.proc = subprocess.Popen(argv, stdout=subprocess.PIPE, text=True)
        assert self.proc.stdout.readline().strip() == "held"

    def running(self) -> bool:
        return self.proc.poll() is None

    def ended(self, timeout: float = 5.0) -> bool:
        """True once the process is gone. A lock is released a moment before the process can be reaped, hence the wait."""
        try:
            self.proc.wait(timeout=timeout)
            return True
        except subprocess.TimeoutExpired:
            return False

    def __enter__(self) -> "_Holder":
        return self

    def __exit__(self, *exc) -> None:
        self.proc.kill()
        self.proc.wait()
        self.proc.stdout.close()


def _until(cond, timeout: float = 15.0) -> bool:
    """Wait for something another process is about to do; the tests never rely on a sleep being long enough."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return bool(cond())


def _child_env(root: Path) -> dict[str, str]:
    return {**os.environ, "TEAM_ROOT": str(root), "TEAM_CONFIG": str(root / "team.yaml"), "PYTHONPATH": str(REPO), "TEAM_PUBLISH_DRY_RUN": "1"}


def _probe(root: Path) -> str:
    """Would a `team cycle` started now, in a process of its own, get past the gate and the code lock?"""
    (root / "team.yaml").write_text("locks: " + json.dumps(FAST) + "\n", encoding="utf-8")
    return subprocess.run([sys.executable, "-c", PROBE], env=_child_env(root), capture_output=True, text=True, check=True).stdout.strip()


# ------------------------------------------------------------------ the locks themselves

def test_product_locks_are_exclusive_per_product_and_independent_of_each_other(tmp_path: Path):
    st = _settings(tmp_path)
    with _Holder(30, ("ex", locks.product_lock_path(st, "agent-team"))):
        assert locks.product_busy(st, "agent-team") and not locks.product_busy(st, "ai-battles")
        with locks.product_cycle(st, "agent-team") as free:
            assert not free                                   # its cycle runs elsewhere: skipped, not waited for
        with locks.product_cycle(st, "ai-battles", note="cycle=c1") as free:
            assert free                                       # another product is not held up
            assert locks.product_busy(st, "ai-battles")
            assert f"pid={os.getpid()}" in locks.product_lock_path(st, "ai-battles").read_text() and "cycle=c1" in locks.product_lock_path(st, "ai-battles").read_text()
        assert locks.product_lock_path(st, "ai-battles").read_text() == ""      # the note goes with the lock
    with locks.product_cycle(st, "agent-team") as free:
        assert free


def test_product_lock_can_wait_a_bounded_time(tmp_path: Path):
    st = _settings(tmp_path)
    with _Holder(2.0, ("ex", locks.product_lock_path(st, "release-radar"))):
        t0 = time.monotonic()
        with locks.product_cycle(st, "release-radar", wait_s=0.2) as free:
            assert not free and time.monotonic() - t0 >= 0.2
        with locks.product_cycle(st, "release-radar", wait_s=30) as free:      # the holder ends after 2 s
            assert free and time.monotonic() - t0 >= 1.5


def test_cycles_of_several_processes_share_the_code_lock_and_an_exclusive_try_sees_them(tmp_path: Path):
    st = _settings(tmp_path)
    assert not locks.busy(locks.code_lock_path(st))           # no file yet: nobody holds it
    with _Holder(30, ("sh", locks.code_lock_path(st))):       # the hourly pass, somewhere in its first product
        with locks.cycles(st) as ok:
            assert ok                                         # a fast cycle starts beside it
        assert locks.busy(locks.code_lock_path(st))           # what `flock -n data/cycle.lock` of watchdog.sh sees: a cycle is running
    assert not locks.busy(locks.code_lock_path(st))


def test_closed_gate_keeps_new_cycles_out_at_once(tmp_path: Path):
    st = _settings(tmp_path, start_wait_s=30)
    with _Holder(30, ("ex", locks.gate_path(st))):            # update-code.sh waiting for the running cycles
        t0 = time.monotonic()
        with locks.cycles(st) as ok:
            assert not ok and time.monotonic() - t0 < 5       # no waiting at the gate: the next cron tick looks again
    with locks.cycles(st) as ok:
        assert ok


def test_short_exclusive_hold_of_the_code_lock_is_waited_out_and_a_long_one_is_not(tmp_path: Path):
    st = _settings(tmp_path, start_wait_s=5)
    with _Holder(0.7, ("ex", locks.code_lock_path(st))):      # watchdog.sh in its rollback, or a plain `flock data/cycle.lock …`
        t0 = time.monotonic()
        with locks.cycles(st) as ok:
            assert ok and time.monotonic() - t0 >= 0.5
    st = _settings(tmp_path, start_wait_s=0.2)
    with _Holder(30, ("ex", locks.code_lock_path(st))):
        with locks.cycles(st) as ok:
            assert not ok


def test_code_update_waits_for_the_other_cycles_and_closes_the_gate_meanwhile(tmp_path: Path):
    st = _settings(tmp_path)
    with locks.cycles(st) as ok:                              # this process: the cycle of the framework product, about to self-merge
        assert ok
        with _Holder(1.2, ("sh", locks.code_lock_path(st))) as other:          # the cycle of another product, 1.2 s to go
            t0 = time.monotonic()
            with locks.code_update(st) as alone:
                assert alone and time.monotonic() - t0 >= 1.0 and other.ended()
                assert _probe(tmp_path) == "no-start"         # nothing starts on a checkout that is being replaced
            assert locks.busy(locks.code_lock_path(st))       # the shared hold is back: the cycle goes on protected
            assert _probe(tmp_path) == "start"                # and the others may start again
    assert not locks.busy(locks.code_lock_path(st)) and not locks.busy(locks.gate_path(st))


def test_code_update_gives_up_after_the_wait_and_keeps_its_shared_hold(tmp_path: Path):
    st = _settings(tmp_path, code_update_wait_s=0.3)
    with locks.cycles(st):
        with _Holder(30, ("sh", locks.code_lock_path(st))) as other:
            with locks.code_update(st) as alone:
                assert not alone and other.running()
            assert locks.busy(locks.code_lock_path(st))
        with locks.code_update(st) as alone:                  # the other cycle is gone
            assert alone


def test_code_update_does_not_deadlock_with_a_deploy_holding_the_gate(tmp_path: Path):
    """update-code.sh holds the gate and waits for the code lock; the self-merge gives its shared hold up while it
    waits for the gate, so the deploy gets through and the self-merge follows it."""
    st = _settings(tmp_path)
    with locks.cycles(st):
        with _Holder(0.8, ("ex", locks.gate_path(st))):
            t0 = time.monotonic()
            with locks.code_update(st) as alone:
                assert alone and 0.6 <= time.monotonic() - t0 < 8
                assert _probe(tmp_path) == "no-start"


def test_file_lock_serialises_a_shared_state_file(tmp_path: Path):
    f = tmp_path / "data" / "external-critic" / "seen.json"
    with _Holder(0.6, ("ex", f.with_name("seen.json.lock"))):
        t0 = time.monotonic()
        with locks.file_lock(f):
            assert time.monotonic() - t0 >= 0.4


# ------------------------------------------------------------------ team cycle: who runs, who is skipped

class _FakeCycle:
    ran: list[str] = []
    on_run = None                                # callable(slug) run inside the cycle

    def __init__(self, st, p, runner, cycle_id, dry_run=False):
        self.st, self.p, self.cycle_id = st, p, cycle_id

    def run(self, phases=None):
        _FakeCycle.ran.append(self.p.slug)
        assert locks.product_busy(self.st, self.p.slug) and locks.busy(locks.code_lock_path(self.st))   # both held while a cycle runs
        if _FakeCycle.on_run:
            _FakeCycle.on_run(self.p.slug)
        s = Store(self.p.dir); s.cycle_start(self.cycle_id); s.cycle_finish(self.cycle_id, [], 0.0); s.close()
        return {"phases": [], "runs": 0, "spent_usd": 0}


@pytest.fixture
def team(tmp_path: Path, monkeypatch):
    """Three products — two hourly sites and a game on a 5-minute cadence, due now — and `team cycle` with stand-in cycles."""
    from team import friction, retro
    st = _settings(tmp_path)
    for slug, cyc in (("a-site", ""), ("b-game", "cycle:\n  every_min: 5\n"), ("c-site", "")):
        _product(st, slug, cyc)
    calls: list[str] = []
    monkeypatch.setattr(cli.C, "load", lambda *a, **k: st)
    monkeypatch.setattr(cli, "Cycle", _FakeCycle)
    monkeypatch.setattr(friction, "check_rollback", lambda *a, **k: calls.append("check_rollback"))
    monkeypatch.setattr(friction, "collect_all", lambda *a, **k: calls.append("collect_all") or 0)
    monkeypatch.setattr(retro, "due", lambda *a, **k: False)
    _FakeCycle.ran, _FakeCycle.on_run = [], None
    st.framework_calls = calls                                # type: ignore[attr-defined]
    return st


def test_due_cycle_starts_while_the_hourly_pass_is_busy_with_another_product(team, capsys):
    """F17: 21:07 the hourly pass sits in agent-team for 15 minutes; the game on a 5-minute cadence must not wait for it."""
    st = team
    hourly_pass_in_a_site = (("sh", locks.code_lock_path(st)), ("ex", locks.hourly_pass_path(st)), ("ex", locks.product_lock_path(st, "a-site")))
    with _Holder(30, *hourly_pass_in_a_site) as hourly:
        assert cli.main(["cycle", "--due"]) == 0
        assert _FakeCycle.ran == ["b-game"] and hourly.running()
    assert st.framework_calls == []                           # the framework's hourly work stays with the hourly pass
    assert cadence.last_finished(C.load_product(st, st.products_dir / "b-game")) is not None


def test_due_entry_skips_a_product_whose_own_cycle_is_running(team, capsys):
    st = team
    with _Holder(30, ("sh", locks.code_lock_path(st)), ("ex", locks.product_lock_path(st, "b-game"))):
        assert cli.main(["cycle", "--due"]) == 0
    assert _FakeCycle.ran == []
    assert "b-game: skipped (its cycle is already running" in capsys.readouterr().out


def test_hourly_pass_skips_the_busy_product_and_runs_the_others(team, capsys):
    st = team
    with _Holder(30, ("sh", locks.code_lock_path(st)), ("ex", locks.product_lock_path(st, "b-game"))):     # a fast cycle of the game
        assert cli.main(["cycle", "--product", "all"]) == 0
    assert _FakeCycle.ran == ["a-site", "c-site"]
    out = capsys.readouterr().out
    assert "b-game: skipped (its cycle is already running" in out and "a-site: {" in out and "c-site: {" in out
    assert st.framework_calls == ["check_rollback", "collect_all"]          # the pass still does its framework work
    assert locks.describe(st) == {"code": False, "gate": False, "hourly_pass": False, "products": []}      # nothing left held


def test_second_hourly_pass_does_not_start(team):
    st = team
    with _Holder(30, ("sh", locks.code_lock_path(st)), ("ex", locks.hourly_pass_path(st))):
        assert cli.main(["cycle", "--product", "all"]) == locks.EX_TEMPFAIL
        assert _FakeCycle.ran == [] and st.framework_calls == []
        assert cli.main(["cycle", "--product", "c-site"]) == 0                # one product by hand is not a pass
        assert _FakeCycle.ran == ["c-site"]


@pytest.mark.parametrize("argv", [["cycle", "--product", "all"], ["cycle", "--due"], ["cycle", "--product", "a-site"]])
def test_no_cycle_starts_while_the_code_is_being_updated(team, argv):
    st = team
    with _Holder(30, ("ex", locks.gate_path(st))):                            # update-code.sh / the self-merge announced itself
        assert cli.main(argv) == locks.EX_TEMPFAIL
    with _Holder(30, ("ex", locks.code_lock_path(st))):                       # watchdog.sh rollback, plain `flock data/cycle.lock …`
        assert cli.main(argv) == locks.EX_TEMPFAIL
    assert _FakeCycle.ran == [] and st.framework_calls == []
    assert cli.main(argv) == 0 and _FakeCycle.ran                             # free again: it runs


def test_due_is_checked_again_under_the_product_lock(team, capsys):
    """Two due games: while this entry runs the first, another entry runs and finishes the second. It must not run twice."""
    st = team
    d = _product(st, "d-game", "cycle:\n  every_min: 5\n")

    def other_entry_finishes_d(slug: str) -> None:
        if slug == "b-game":
            s = Store(d.dir); s.cycle_start("by-another-entry"); s.cycle_finish("by-another-entry", [], 0.0); s.close()

    _FakeCycle.on_run = other_entry_finishes_d
    assert cli.main(["cycle", "--due"]) == 0
    assert _FakeCycle.ran == ["b-game"]
    assert "d-game: skipped (no longer due" in capsys.readouterr().out


def test_cycle_id_of_the_same_minute_is_not_run_twice_for_a_product(team, capsys):
    st = team
    a = C.load_product(st, st.products_dir / "a-site")
    now = datetime.now(timezone.utc)
    s = Store(a.dir)
    for t in (now, now + timedelta(minutes=1)):                # both: the test may cross a minute boundary
        s.cycle_start(t.strftime("%Y%m%dT%H%M"))
    s.close()
    assert cli.main(["cycle", "--product", "all"]) == 0
    assert _FakeCycle.ran == ["b-game", "c-site"]
    assert "a-site: skipped (cycle " in capsys.readouterr().out
    assert cli.main(["cycle", "--product", "a-site", "--cycle", now.strftime("%Y%m%dT%H%M")]) == 0      # an explicit id is the owner's call
    assert _FakeCycle.ran[-1] == "a-site"


def test_retro_waits_for_the_framework_products_own_cycle(team, monkeypatch):
    from team import retro
    st = team
    st.raw["framework"] = {"product": "a-site"}
    ran: list[str] = []
    monkeypatch.setattr(retro, "due", lambda *a, **k: True)
    monkeypatch.setattr(retro, "run_retro", lambda *a, **k: ran.append("retro") or "ok")
    with _Holder(30, ("sh", locks.code_lock_path(st)), ("ex", locks.product_lock_path(st, "a-site"))):
        assert cli.main(["cycle", "--product", "all"]) == 0
        assert ran == []                                      # its store is being written by its own cycle: next pass
    assert cli.main(["cycle", "--product", "all", "--cycle", "next-pass"]) == 0
    assert ran == ["retro"]


def test_status_says_when_a_cycle_is_running(team, capsys):
    st = team
    with _Holder(30, ("ex", locks.product_lock_path(st, "b-game"))):
        assert cli.main(["status", "b-game"]) == 0
        assert "cadence: cycle every 5 min · cycle running now" in capsys.readouterr().out
    assert cli.main(["status", "b-game"]) == 0
    assert "cadence: cycle every 5 min · due now" in capsys.readouterr().out


def test_real_processes_a_due_dry_run_cycle_finishes_beside_a_running_hourly_pass(tmp_path: Path):
    """The whole thing with real `team cycle` processes: an hourly pass is inside the first product (a process holding
    exactly its locks), and `team cycle --due --dry-run` started beside it runs the game's cycle to the end."""
    st = _settings(tmp_path)
    _product(st, "a-site")
    game = _product(st, "b-game", "cycle:\n  every_min: 5\n")
    (tmp_path / "team.yaml").write_text("locks: " + json.dumps(FAST) + "\n", encoding="utf-8")
    cmd = [sys.executable, "-m", "team.cli", "cycle", "--due", "--dry-run"]
    with _Holder(60, ("sh", locks.code_lock_path(st)), ("ex", locks.hourly_pass_path(st)), ("ex", locks.product_lock_path(st, "a-site"))) as hourly:
        r = subprocess.run(cmd, env=_child_env(tmp_path), cwd=REPO, capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        assert hourly.running()
        assert "b-game: {'phases': ['collect'" in r.stdout
        assert re.search(r"^\S+ \S+ INFO \[\d+\] team\.cycle: b-game cycle \S+: phases", r.stderr, re.M)      # the pid is in every log line
        assert cadence.last_finished(game) is not None
        # the second tick, five minutes "later": the game is busy in another process → skipped, exit 0
        with _Holder(60, ("ex", locks.product_lock_path(st, "b-game"))):
            s = Store(game.dir); s.db.execute("UPDATE cycles SET finished=?", ((datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="seconds"),)); s.db.commit(); s.close()
            r = subprocess.run(cmd, env=_child_env(tmp_path), cwd=REPO, capture_output=True, text=True, timeout=120)
            assert r.returncode == 0 and "b-game: skipped (its cycle is already running" in r.stdout, r.stderr
    with _Holder(60, ("ex", locks.gate_path(st))):                                        # a code update is waiting
        r = subprocess.run(cmd, env=_child_env(tmp_path), cwd=REPO, capture_output=True, text=True, timeout=120)
        assert r.returncode == locks.EX_TEMPFAIL and "the dispatcher code is being updated" in r.stderr


# ------------------------------------------------------------------ the self-merge: alone on the dispatcher's code

def _framework_cycle(st: C.Settings) -> Cycle:
    return Cycle(st, _product(st, "agent-team"), None, "c1")


def test_self_merge_updates_the_checkout_only_after_the_other_cycles_ended(tmp_path: Path, monkeypatch):
    st = _settings(tmp_path)
    seen: dict[str, object] = {}

    def update(root, branch="main"):
        seen.update(other_ended=other.ended(), new_cycle=_probe(tmp_path), root=root)
        return "f" * 40

    monkeypatch.setattr(gitops, "update_running_checkout", update)
    cyc = _framework_cycle(st)
    with locks.cycles(st):                                    # as `team cycle` holds it around every cycle
        with _Holder(1.0, ("sh", locks.code_lock_path(st))) as other:        # the game's cycle, one second to go
            cyc.update_own_code({"id": "T-0001"}, "a" * 40)
    assert seen == {"other_ended": True, "new_cycle": "no-start", "root": st.root}
    assert not [e for e in cyc.store.db.execute("SELECT kind FROM events") if e[0] == "code.update_not_exclusive"]


def test_self_merge_after_the_wait_ran_out_updates_anyway_and_records_it(tmp_path: Path, monkeypatch):
    st = _settings(tmp_path, code_update_wait_s=0.3)
    updated: list[bool] = []
    cyc = _framework_cycle(st)
    with locks.cycles(st):
        with _Holder(30, ("sh", locks.code_lock_path(st))) as other:
            monkeypatch.setattr(gitops, "update_running_checkout", lambda root, branch="main": updated.append(other.running()) or None)
            cyc.update_own_code({"id": "T-0001"}, "a" * 40)
    assert updated == [True]                                  # the merge is on GitHub already: the checkout must follow
    rows = list(cyc.store.db.execute("SELECT subject, detail FROM events WHERE kind='code.update_not_exclusive'"))
    assert len(rows) == 1 and rows[0][0] == "T-0001" and "a" * 40 in rows[0][1]


# ------------------------------------------------------------------ watchdog.sh and update-code.sh (need `flock`)

# util-linux `flock` is not on every developer machine. The stand-in is the same call on the same descriptor
# (flock(2) through python), for the forms the scripts use: `flock [-n | -w S] FD`, `flock -u FD`.
PY_FLOCK = """#!{python}
import fcntl, os, sys, time
args, nb, wait, unlock = sys.argv[1:], False, None, False
while args and args[0].startswith("-"):
    o = args.pop(0)
    if o == "-n": nb = True
    elif o == "-u": unlock = True
    elif o == "-w": wait = float(args.pop(0))
    else: sys.exit(64)
fd = int(args.pop(0))
if unlock:
    fcntl.flock(fd, fcntl.LOCK_UN); sys.exit(0)
deadline = time.monotonic() + (wait or 0)
while True:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | (fcntl.LOCK_NB if nb or wait is not None else 0)); break
    except OSError:
        if nb or time.monotonic() >= deadline: sys.exit(1)
        time.sleep(0.05)
"""
# .venv/bin/team of the test checkout: records its arguments and whether a cycle could take the code lock right now.
PY_TEAM = """#!{python}
import fcntl, sys
from pathlib import Path
root = Path(__file__).resolve().parents[2]
fh = open(root / "data" / "cycle.lock", "a+")
try:
    fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB); state = "lock-free"
except OSError:
    state = "lock-held"
with open(root / "team-calls.txt", "a") as f:
    f.write(" ".join(sys.argv[1:]) + " | " + state + "\\n")
"""
# `git` in front of the real one: notes whether the code lock is held while `git reset` rewrites the checkout.
GIT_WRAPPER = """#!/usr/bin/env bash
if [ "$1" = reset ]; then
  "{python}" -c 'import fcntl,sys
fh = open("data/cycle.lock", "a+")
try:
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB); print("reset: lock-free")
except OSError:
    print("reset: lock-held")' >> git-calls.txt
fi
exec "{git}" "$@"
"""
GNU_DATE = subprocess.run(["date", "-u", "-d", "2026-11-20 21:07:00", "+%s"], capture_output=True).returncode == 0


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _exe(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _server_checkout(tmp_path: Path) -> tuple[Path, dict[str, str], str, str]:
    """A dispatcher checkout as on the server: origin + clone with two commits, the cron scripts, a stand-in `team`.
    Returns (root, env, first sha, second sha)."""
    origin, root = tmp_path / "origin.git", tmp_path / "dispatcher"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "clone", "-q", str(origin), str(root)], check=True, capture_output=True)
    _git(root, "checkout", "-q", "-B", "main")
    shas = []
    for n in ("one", "two"):
        (root / "code.txt").write_text(n + "\n")
        _git(root, "add", "code.txt"); _git(root, "commit", "-q", "-m", n)
        shas.append(_git(root, "rev-parse", "HEAD"))
    _git(root, "push", "-q", "origin", "main")
    for script in ("watchdog.sh", "run-cycle.sh", "update-code.sh"):
        shutil.copy(REPO / script, root / script)
    (root / ".git" / "info" / "exclude").write_text("*\n")           # scripts, data/, .venv/: as untracked as data/ is on the server
    _exe(root / ".venv" / "bin" / "team", PY_TEAM.format(python=sys.executable))
    (root / "data" / "framework").mkdir(parents=True)
    fake_bin = tmp_path / "fakebin"
    if shutil.which("flock") is None:
        _exe(fake_bin / "flock", PY_FLOCK.format(python=sys.executable))
    _exe(fake_bin / "git", GIT_WRAPPER.format(python=sys.executable, git=shutil.which("git")))
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}", "UPDATE_PIP": str(tmp_path / "no-pip")})
    return root, env, shas[0], shas[1]


def _team_calls(root: Path) -> list[str]:
    f = root / "team-calls.txt"
    return f.read_text().splitlines() if f.exists() else []


def test_watchdog_rolls_back_alone_on_the_code_and_frees_the_lock_for_the_pass(tmp_path: Path):
    root, env, good, bad = _server_checkout(tmp_path)
    (root / "data" / "framework" / "last-good").write_text(good + "\n")
    r = subprocess.run(["bash", str(root / "watchdog.sh")], env=env, cwd=root, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert _git(root, "rev-parse", "HEAD") == good
    assert f"rolled back {bad} -> {good}" in (root / "data" / "watchdog.log").read_text()
    assert json.loads((root / "data" / "framework" / "friction.jsonl").read_text())["kind"] == "watchdog_rollback"
    assert (root / "git-calls.txt").read_text().split("\n")[0] == "reset: lock-held"      # exclusive while the checkout is rewritten
    assert _team_calls(root) == ["cycle --product all | lock-free"]                        # released before the pass starts
    assert not locks.busy(root / "data" / "cycle.lock")


def test_watchdog_does_nothing_while_any_cycle_is_running(tmp_path: Path):
    root, env, good, bad = _server_checkout(tmp_path)
    (root / "data" / "framework" / "last-good").write_text(good + "\n")
    with _Holder(30, ("sh", root / "data" / "cycle.lock")):            # one product's cycle, whichever entry started it
        r = subprocess.run(["bash", str(root / "watchdog.sh")], env=env, cwd=root, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert _git(root, "rev-parse", "HEAD") == bad and not (root / "data" / "watchdog.log").exists() and _team_calls(root) == []
    (root / "STOP").write_text("owner\n")
    subprocess.run(["bash", str(root / "watchdog.sh")], env=env, cwd=root, check=True, timeout=60)
    assert _git(root, "rev-parse", "HEAD") == bad and _team_calls(root) == []


@pytest.mark.skipif(not GNU_DATE, reason="watchdog.sh reads the log time with GNU date (-d), as on the server")
def test_watchdog_leaves_a_recently_finished_cycle_alone_and_reads_the_log_line_with_the_pid(tmp_path: Path):
    root, env, good, bad = _server_checkout(tmp_path)
    (root / "data" / "framework" / "last-good").write_text(good + "\n")
    now = datetime.now(timezone.utc)
    line = "{} INFO [4242] team.cycle: ai-battles cycle 20261120T2105: phases ['collect'], runs 1, spent $0.10, 200s\n"
    (root / "data" / "cron.log").write_text(line.format((now - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S,000")))
    subprocess.run(["bash", str(root / "watchdog.sh")], env=env, cwd=root, check=True, timeout=60)
    assert _git(root, "rev-parse", "HEAD") == bad and _team_calls(root) == []
    (root / "data" / "cron.log").write_text(line.format((now - timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S,000")))
    subprocess.run(["bash", str(root / "watchdog.sh")], env=env, cwd=root, check=True, timeout=60)
    assert _git(root, "rev-parse", "HEAD") == good and _team_calls(root) == ["cycle --product all | lock-free"]


def _new_commit_on_origin(tmp_path: Path) -> str:
    other = tmp_path / "dev"
    subprocess.run(["git", "clone", "-q", str(tmp_path / "origin.git"), str(other)], check=True, capture_output=True)
    (other / "code.txt").write_text("three\n")
    _git(other, "commit", "-q", "-am", "three"); _git(other, "push", "-q", "origin", "main")
    return _git(other, "rev-parse", "HEAD")


def test_update_code_waits_for_running_cycles_with_the_gate_closed_then_fast_forwards(tmp_path: Path):
    root, env, _, old = _server_checkout(tmp_path)
    new = _new_commit_on_origin(tmp_path)
    st = _settings(root)
    with _Holder(4.0, ("sh", root / "data" / "cycle.lock")) as cycle:  # a cycle with 4 s to go
        upd = subprocess.Popen(["bash", str(root / "update-code.sh")], env=env, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        assert _until(lambda: locks.busy(locks.gate_path(st)))         # the script announced itself at the gate
        with locks.cycles(st) as ok:
            assert not ok                                              # no new cycle starts meanwhile
        assert cycle.running() and upd.poll() is None and _git(root, "rev-parse", "HEAD") == old     # still waiting, nothing changed
        out, _ = upd.communicate(timeout=60)
        assert cycle.ended()                                           # it got the code only after the cycle was over
    assert upd.returncode == 0, out
    assert _git(root, "rev-parse", "HEAD") == new and f"{old[:7]} -> {new[:7]}" in out
    with locks.cycles(st) as ok:
        assert ok                                                      # gate and lock are free again


def test_update_code_gives_up_without_changes_when_cycles_do_not_end_in_time(tmp_path: Path):
    root, env, _, old = _server_checkout(tmp_path)
    _new_commit_on_origin(tmp_path)
    with _Holder(30, ("sh", root / "data" / "cycle.lock")):
        r = subprocess.run(["bash", str(root / "update-code.sh")], env={**env, "UPDATE_WAIT_S": "1"}, cwd=root, capture_output=True, text=True, timeout=60)
    assert r.returncode == 75 and "cycles still running after 1s" in r.stdout
    assert _git(root, "rev-parse", "HEAD") == old
    with locks.cycles(_settings(root)) as ok:
        assert ok                                                      # the gate did not stay closed


def test_cron_scripts_and_docs_name_the_same_lock_files():
    for script in ("watchdog.sh", "update-code.sh"):
        assert f"data/{locks.CODE_LOCK}" in (REPO / script).read_text(), script
    assert f"data/{locks.LOCKS_DIR}/{locks.GATE}" in (REPO / "update-code.sh").read_text()
    assert "update-code.sh" in (REPO / "sync.sh").read_text()
    scheme = (REPO / "SCHEME.md").read_text()
    for name in (locks.CODE_LOCK, locks.GATE, locks.HOURLY_PASS, "product-<slug>.lock"):
        assert name in scheme, name
