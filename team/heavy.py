"""`team-heavy <command …>` — a heavy run from inside a role's run: one at a time on the server, at low priority.

27.11.2026, ai-battles on an 8-core server at a load of 28–37: a balance simulation of a developer's task took 174 % of a
core for minutes beside three test suites of other runs. A ban would stop balance work (the simulations ARE the task), so
they are queued instead: the commands named in `cycle.heavy_runs.queue` are denied to the run as typed, and the role
starts them as `team-heavy npm run sim:weapons`. This tool waits for the server-wide lock, then runs the command under
`nice`, with its output and its exit code passed through.

The dispatcher gives the run: TEAM_HEAVY_LOCK (the lock file), TEAM_HEAVY_NICE, TEAM_HEAVY_WAIT_S.
"""
from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

# ------------------------------------------------------------------ parameters (overridden by the environment the dispatcher sets)
DEFAULT_NICE = 15                 # 0..19; the simulation gets the processor that nothing else wants
DEFAULT_WAIT_S = 1800             # how long to wait for another heavy run to end before giving up
POLL_S = 2.0
EX_TEMPFAIL = 75


def _num(name: str, default: int, lo: int, hi: int) -> int:
    try:
        v = int(os.environ.get(name, default))
        return v if lo <= v <= hi else default
    except ValueError:
        return default


def main(argv: list[str] | None = None) -> int:
    cmd = list(sys.argv[1:] if argv is None else argv)
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("team-heavy: give the command to run, e.g. `team-heavy npm run sim:weapons`", file=sys.stderr)
        return 2
    lock = os.environ.get("TEAM_HEAVY_LOCK")
    nice, wait_s = _num("TEAM_HEAVY_NICE", DEFAULT_NICE, 0, 19), _num("TEAM_HEAVY_WAIT_S", DEFAULT_WAIT_S, 0, 86400)
    fh = None
    t0 = time.monotonic()
    if lock:
        Path(lock).parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock, "a+", encoding="utf-8")
        said = False
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() - t0 >= wait_s:
                    print(f"team-heavy: another heavy run still holds the server after {wait_s}s — not started. Report the command in your "
                          "result so that it is run later; do not start it another way.", file=sys.stderr)
                    return EX_TEMPFAIL
                if not said:
                    said = True
                    print("team-heavy: another heavy run is going on this server; waiting for it to end …", file=sys.stderr, flush=True)
                time.sleep(POLL_S)
    waited = time.monotonic() - t0
    print(f"team-heavy: started after {waited:.0f}s of waiting, at nice {nice}: {' '.join(cmd)}", file=sys.stderr, flush=True)
    try:
        return subprocess.run(cmd, preexec_fn=(lambda: os.nice(nice)) if nice else None).returncode
    except FileNotFoundError:
        print(f"team-heavy: command not found: {cmd[0]}", file=sys.stderr)
        return 127
    finally:
        if fh is not None:
            fh.close()                             # closing the description releases the lock


if __name__ == "__main__":
    raise SystemExit(main())
