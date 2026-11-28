"""`team-heavy <command …>` — a heavy run from inside a role's run: one at a time on the server, at low priority.

27.11.2026, ai-battles on an 8-core server at a load of 28–37: a balance simulation of a developer's task took 174 % of a
core for minutes beside three test suites of other runs. A ban would stop balance work (the simulations ARE the task), so
they are queued instead: the commands named in `cycle.heavy_runs.queue` are denied to the run as typed, and the role
starts them as `team-heavy npm run sim:weapons`. This tool waits for the server-wide lock, then runs the command under
`nice`, with its output and its exit code passed through.

TEAM_HEAVY_BUSY=run: when no place comes free within the wait, the command runs all the same (default: it is refused, exit 75).

The dispatcher gives the run: TEAM_HEAVY_LOCK (the lock file), TEAM_HEAVY_SLOTS (how many such runs at once, 1 by default),
TEAM_HEAVY_NICE, TEAM_HEAVY_WAIT_S. The dispatcher's own test runs take a place of the same set (`take_slot`).

TEAM_HEAVY_CPU_QUOTA (`cycle.heavy_runs.cpu_quota`, percent of one core, 0 = off): the command is started inside the
systemd slice SLICE of the user, which the dispatcher keeps at that quota — every heavy run of the server shares it, so
however many of them start, together they get no more than that much processor (28.11: five heavy runs at two places,
because a run that waited `wait_s` starts all the same; load 38 on 8 cores). Where the slice cannot be used (no systemd
of the user, no `systemd-run`), the command runs as before and a line says why.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# ------------------------------------------------------------------ parameters (overridden by the environment the dispatcher sets)
DEFAULT_NICE = 15                 # 0..19; the simulation gets the processor that nothing else wants
DEFAULT_WAIT_S = 1800             # how long to wait for another heavy run to end before giving up
POLL_S = 2.0
EX_TEMPFAIL = 75
SLICE = "team-heavy.slice"        # the slice of the user's systemd all heavy runs share (it nests under team.slice)
PROBE_TIMEOUT_S = 15              # how long the trial start in the slice, and the setting of its quota, may take


def _num(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(name, default))
        return v if lo <= v <= hi else default
    except ValueError:
        return default


def scope_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """The environment for `systemd-run --user` / `systemctl --user`: cron gives a run no XDG_RUNTIME_DIR, and without
    it the user's systemd is not found («Failed to connect to bus: No medium found», seen 28.11)."""
    out = dict(os.environ if env is None else env)
    out.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return out


def scope_prefix(quota: int, env: dict[str, str] | None = None) -> tuple[list[str], str]:
    """(what to put in front of a command so that it runs in SLICE, "") — or ([], why not). Tried with a start of
    `true`: a prefix that does not work would otherwise look like a failed test run."""
    if int(quota or 0) <= 0:
        return [], "off"
    exe = shutil.which("systemd-run", path=(env or os.environ).get("PATH"))
    if not exe:
        return [], "systemd-run is not on this machine"
    prefix = [exe, "--user", "--scope", "--quiet", "--collect", f"--slice={SLICE}", "--"]
    try:
        cp = subprocess.run(prefix + ["true"], env=scope_env(env), capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"the trial start failed: {str(exc)[:160]}"
    if cp.returncode != 0:
        return [], f"the trial start ended with {cp.returncode}: {(cp.stderr or cp.stdout).strip()[:160]}"
    return prefix, ""


def set_slice_quota(quota: int, env: dict[str, str] | None = None) -> str:
    """Keep SLICE at `quota` percent of one core in all ("" = done, else why not). Runtime only: nothing is written into
    the user's configuration; the dispatcher sets it again at the start of every cycle."""
    exe = shutil.which("systemctl", path=(env or os.environ).get("PATH"))
    if not exe:
        return "systemctl is not on this machine"
    try:
        cp = subprocess.run([exe, "--user", "set-property", "--runtime", SLICE, f"CPUQuota={int(quota)}%"], env=scope_env(env),
                            capture_output=True, text=True, timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"the quota was not set: {str(exc)[:160]}"
    return "" if cp.returncode == 0 else f"the quota was not set ({cp.returncode}): {(cp.stderr or cp.stdout).strip()[:160]}"


def take_slot(lock: str, slots: int, wait_s: float, on_wait: Any = None) -> Any:
    """One of `slots` server-wide places for a heavy run: the open lock file (close it to give the place back), or None
    when none came free within `wait_s`. The places are the files `<lock>` (the first) and `<lock>.2 … <lock>.N`."""
    Path(lock).parent.mkdir(parents=True, exist_ok=True)
    names = [lock] + [f"{lock}.{i}" for i in range(2, max(1, int(slots)) + 1)]
    t0 = time.monotonic()
    said = False
    while True:
        for name in names:
            fh = open(name, "a+", encoding="utf-8")
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fh
            except OSError:
                fh.close()
        if time.monotonic() - t0 >= wait_s:
            return None
        if not said and on_wait is not None:
            said = True
            on_wait()
        time.sleep(POLL_S)


def main(argv: list[str] | None = None) -> int:
    cmd = list(sys.argv[1:] if argv is None else argv)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("team-heavy: give the command to run, e.g. `team-heavy npm run sim:weapons`", file=sys.stderr)
        return 2
    lock = os.environ.get("TEAM_HEAVY_LOCK")
    nice, wait_s = _num("TEAM_HEAVY_NICE", DEFAULT_NICE, 0, 19), _num("TEAM_HEAVY_WAIT_S", DEFAULT_WAIT_S, 0, 86400)
    slots = _num("TEAM_HEAVY_SLOTS", 1, 1, 64)
    fh = None
    t0 = time.monotonic()
    if lock:
        fh = take_slot(lock, slots, wait_s, on_wait=lambda: print(
            f"team-heavy: all {slots} place(s) for heavy runs on this server are taken; waiting for one …", file=sys.stderr, flush=True))
        if fh is None and os.environ.get("TEAM_HEAVY_BUSY", "refuse") != "run":
            print(f"team-heavy: no place came free in {wait_s}s — not started. Run the same `team-heavy` command again (it only "
                  "waits); do not start the work another way.", file=sys.stderr)
            return EX_TEMPFAIL
        if fh is None:
            # `cycle.heavy_runs.busy: run` — the queue delays a run, it never refuses one (28.11: with a refusal after the
            # wait 36–40 % of the developers' runs ended «partial: the tests could not be run»)
            print(f"team-heavy: no place came free in {wait_s}s — starting all the same.", file=sys.stderr, flush=True)
    waited = time.monotonic() - t0
    quota = _num("TEAM_HEAVY_CPU_QUOTA", 0, 0, 6400)
    prefix, why = scope_prefix(quota)
    if quota and not prefix:
        print(f"team-heavy: the shared processor limit is not in use ({why}); the command runs without it.", file=sys.stderr, flush=True)
    print(f"team-heavy: started after {waited:.0f}s of waiting, at nice {nice}"
          + (f", inside {SLICE}" if prefix else "") + f": {' '.join(cmd)}", file=sys.stderr, flush=True)
    try:
        return subprocess.run(prefix + cmd, env=scope_env() if prefix else None, preexec_fn=(lambda: os.nice(nice)) if nice else None).returncode
    except FileNotFoundError:
        print(f"team-heavy: command not found: {cmd[0]}", file=sys.stderr)
        return 127
    finally:
        if fh is not None:
            fh.close()                             # closing the description releases the lock


if __name__ == "__main__":
    raise SystemExit(main())
