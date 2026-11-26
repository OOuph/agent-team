"""`team-play` — the tester's only shell command (game profile, slice 4; owner 20.11). Like `team-fetch` for the reviewer:
one allow-listed binary that runs Playwright in a fixed checkout against a fixed base URL and prints a compact result.

  team-play test [--grep TEXT ...] [--spec PATH] [--project NAME] [--local]   run the repository's Playwright tests (tests/e2e)
      against BASE_URL, or with --local against the server the repository's playwright config starts from this checkout
      (its test settings). A selection with no test for BASE_URL is run once more with --local by itself. The time limit
      counts from the moment the repository's e2e host lock is held; the wait for the lock has its own limit.
  team-play screenshot URL --out NAME [--width W --height H] [--full-page]   a PNG of a page into the review folder
  team-play shots [--weapon KEY] [--ability KEY] [--prefix NAME]  frames of a battle (the repository's tests/tools/battle-shots.mjs) into the review folder
  team-play logs [--tail N]                                       the tail of the play-test server log
  team-play shots|screenshot … --local [--env NAME=VALUE]   the same, against a server of this checkout started with the
      product's test settings (product.yaml `playtest.test_env`; `--env` only for names in `playtest.test_flags`)
  team-play unit [--grep TEXT] [--tail N]   lines of the WHOLE output of the dispatcher's unit-test run (the prompt shows its tail)
  team-play load --clients N --seconds S [--clients-per-room K] [--rate R] [--script tests/load/clients.ts] [--moves]   N WebSocket clients against BASE_URL

Environment set by the dispatcher: TEAM_PLAY_CWD (the checkout), TEAM_PLAY_BASE_URL (the server), TEAM_PLAY_OUT (where
screenshots and reports go), TEAM_PLAY_SERVER_LOG, TEAM_PLAY_NPX (default `npx`), TEAM_PLAY_TIMEOUT_S. URLs must be on
the base URL's host (or TEAM_ALLOWED_HOSTS); file names are restricted; nothing else can be run through this command.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

TEST_TIMEOUT_S = int(os.environ.get("TEAM_PLAY_TIMEOUT_S") or 600)          # the tests themselves, counted from the lock
LOCK_WAIT_S = int(os.environ.get("TEAM_PLAY_LOCK_WAIT_S") or 600)          # waiting for the repository's e2e host lock
# Both fit the review run's own limit (1500 s for ai-battles): wait 600 + run 600 + the agent's own work.
LOCAL_FALLBACK = (os.environ.get("TEAM_PLAY_LOCAL_FALLBACK") or "1") != "0"  # no test for the deployed host → once on a local server
LOCAL_PORT_ENV = "E2E_PORT"                    # the port the repository's playwright.config gives its own server (template convention)
REPORTERS = "line,json"                        # one line per test on stdout as it runs; the JSON report into PLAYWRIGHT_JSON_OUTPUT_NAME
RUNS_LOG = "team-play.jsonl"                   # one line per run in the review folder: reason, waited_s, ran_s, totals
POLL_S = 1.0
KILL_GRACE_S = 15                              # SIGTERM → this long for Playwright's teardown (server stop, lock release) → SIGKILL
TAIL_CHARS = 2500
# The repository's host lock (ai-battles tests/e2e/host-lock.ts) says on stderr when it waits and when it got the lock.
LOCK_WAIT_RE = re.compile(os.environ.get("TEAM_PLAY_LOCK_WAIT_RE") or r"waiting for the [\w .-]*lock: (\d+) s")
LOCK_ACQUIRED_RE = re.compile(os.environ.get("TEAM_PLAY_LOCK_ACQUIRED_RE") or r"lock acquired after (\d+) s")
LOCK_SKIPPED_RE = re.compile(r"running without the lock")
LOCK_HOLDER_RE = re.compile(r"holds the lock \(([^)]*)\)")
TEST_LINE_RE = re.compile(r"^\s*\[(\d+)/(\d+)\]")   # the line reporter: «[3/9] [desktop] › file.spec.ts:12:1 › title»
# Why a check could not be made: the fixed words of review-verdict `inconclusive_reason` (owner 26.11: the PO and the
# operator count them). Keep in sync with team/schemas/review-verdict.schema.json.
INCONCLUSIVE_REASONS = ("lock_timeout", "run_timeout", "no_tests_live", "no_tests", "server_not_started", "test_tooling",
                        "no_test_for_criterion", "not_observable", "other")
INCONCLUSIVE_FOR = {"lock_timeout": "lock_timeout", "run_timeout": "run_timeout", "no_tests_live": "no_tests_live",
                    "no_tests": "no_tests", "server_not_started": "server_not_started", "no_report": "test_tooling",
                    "tool_missing": "test_tooling", "bad_arguments": "test_tooling", "tests_failed": "other"}
SCREENSHOT_TIMEOUT_S = 60
MAX_ERROR_CHARS = 400
MAX_TESTS_SHOWN = 60
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,60}$")
PROGRESS_EVERY_S = 60
GREP_DENIED = set('|;&$`"<>()')
DEFAULT_VIEWPORT = (360, 740)        # the phone first (VISION: portrait, 360 px)
MAX_VIEWPORT = 4000
RESULTS_DIR = "test-results"         # Playwright's default output folder inside the checkout
LOAD_SCRIPT = "tests/load/clients.ts"   # the repository's load script (starter: team/templates/service-repo/tests/load/clients.ts)
LOAD_RUNNER = ["tsx"]                   # `npx tsx <script>`; TEAM_PLAY_LOAD_RUNNER overrides (e.g. "node" for a .js script)
MAX_LOAD_CLIENTS = 200
MAX_LOAD_SECONDS = 300
MAX_LOAD_PER_ROOM = 16                  # --clients-per-room: how many clients share one room (the script decides what a room is)
LOAD_VERSION_TIMEOUT_S = 5              # reading /healthz before and after a load run
LOAD_EXTRA_TIMEOUT_S = 120
SHOTS_SCRIPT = "tests/tools/battle-shots.mjs"   # the repository's battle-frames script (owner 22.11, ai-battles day review: «глаза в бою»)
SHOTS_TIMEOUT_S = 180
SHOTS_VIEWPORT = (1280, 720)           # the desktop first for the frames of a battle
SHOTS_KEY_RE = re.compile(r"^[a-z_]{2,24}$")   # --weapon / --ability: a catalogue key of the game, nothing else reaches the script
SHOTS_FACTS_CHARS = 260                # how much of a frame's facts line is printed
LOAD_KEYS = ("clients", "connected", "seconds", "messages", "samples", "p50_ms", "p95_ms", "max_ms", "errors", "disconnects", "rejected")


# ---- a local server with the product's test settings (rework slice 6; set by the dispatcher from product.yaml `playtest`)
LOCAL_START_TIMEOUT_S = float(os.environ.get("TEAM_PLAY_LOCAL_START_S") or 90)   # the server must answer its health path within this
LOCAL_STOP_GRACE_S = 10                        # SIGTERM, then SIGKILL after this many seconds
LOCAL_LOG_TAIL = 1200                          # characters of the server's log shown when it does not start
ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,60}$")
ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9_.,:/@+-]{0,200}$")
UNIT_MAX_LINES = 200                           # of `team-play unit` output
UNIT_CONTEXT_LINES = 3                         # lines after a match (the assertion text follows the test's name)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name) or default


def allowed_url(url: str) -> bool:
    base = _env("TEAM_PLAY_BASE_URL")
    hosts = {h.strip().lower() for h in _env("TEAM_ALLOWED_HOSTS").split(",") if h.strip()}
    if base:
        hosts.add(urlparse(base).hostname or "")
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        return False
    host = u.hostname.lower()
    return host in hosts or any(host.endswith("." + h) for h in hosts if h)


def npx() -> list[str]:
    bin_ = _env("TEAM_PLAY_NPX", "npx")
    return bin_.split() if " " in bin_ else [bin_]


def cwd() -> Path:
    d = _env("TEAM_PLAY_CWD")
    if not d or not Path(d).is_dir():
        sys.exit("team-play: TEAM_PLAY_CWD is not set or not a directory (the dispatcher sets it)")
    return Path(d)


def out_dir() -> Path:
    d = Path(_env("TEAM_PLAY_OUT") or (cwd() / RESULTS_DIR))
    d.mkdir(parents=True, exist_ok=True)
    return d


def summarize(report: dict) -> tuple[list[str], dict[str, int]]:
    """Playwright JSON reporter → one line per test, plus totals."""
    lines: list[str] = []
    totals = {"passed": 0, "failed": 0, "skipped": 0, "flaky": 0, "timedOut": 0}

    def walk(suite: dict, prefix: str) -> None:
        title = suite.get("title") or ""
        pre = f"{prefix}{title} › " if title else prefix
        for spec in suite.get("specs") or []:
            for t in spec.get("tests") or []:
                status = t.get("status") or "unknown"              # expected | unexpected | flaky | skipped
                res = (t.get("results") or [{}])[-1]
                outcome = res.get("status") or status               # passed | failed | timedOut | skipped
                key = {"expected": "passed", "unexpected": "failed", "flaky": "flaky", "skipped": "skipped"}.get(status, outcome)
                if key in totals:
                    totals[key] += 1
                else:
                    totals["failed"] += 1
                mark = {"passed": "✓", "flaky": "~", "skipped": "-"}.get(key, "✗")
                line = f"{mark} {pre}{spec.get('title', '')} [{t.get('projectName') or ''}] {res.get('duration', 0)} ms"
                err = (res.get("error") or {}).get("message") or ""
                if err and key not in ("passed", "skipped"):
                    line += " — " + re.sub(r"\x1b\[[0-9;]*m", "", err).strip().replace("\n", " ")[:MAX_ERROR_CHARS]
                lines.append(line)
        for s in suite.get("suites") or []:
            walk(s, pre)

    for s in report.get("suites") or []:
        walk(s, "")
    return lines, totals


def build_test_argv(a: argparse.Namespace) -> list[str]:
    """The spec first as a positional filter, then options in their `--name=value` form.

    26.11 (T-0416, T-0435 and 18 more blocked «inconclusive 3×»): the spec used to go after `--`, and Playwright 1.65 drops
    every argument after `--` from the test filters (lib/program.js), so `--spec cups.spec.ts` ran the WHOLE @live suite
    (80 tests in 34 files) and hit the time limit in 159 of 188 calls on 24-26.11. `--project desktop <spec>` does not work
    either: --project is variadic and would take the spec as a project name; `--project=desktop` does."""
    argv = [*npx(), "playwright", "test"]
    if a.spec:
        argv.append(a.spec)
    argv.append("--reporter=" + REPORTERS)
    if a.project:
        argv.append("--project=" + a.project)
    if a.grep:
        argv.append("--grep=" + "|".join(a.grep))
    return argv


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def test_env(a: argparse.Namespace, base: str, local: bool, report_path: Path) -> dict[str, str]:
    """Live: BASE_URL points the repository's config at the server the dispatcher gave. Local: no BASE_URL, so the
    repository's config starts its own server from this (built) checkout with its test settings, on a free E2E_PORT."""
    env = dict(os.environ)
    env.update({"PW_TEST_HTML_REPORT_OPEN": "never", "CI": "1", "PLAYWRIGHT_JSON_OUTPUT_NAME": str(report_path),
                "PLAYWRIGHT_FORCE_TTY": "0", "FORCE_COLOR": "0"})
    if local:
        for k in ("BASE_URL", "PLAYWRIGHT_BASE_URL"):
            env.pop(k, None)
        env[LOCAL_PORT_ENV] = str(free_port())
    else:
        env.update({"BASE_URL": base, "PLAYWRIGHT_BASE_URL": base})
    if a.project and a.project.lower().startswith("mobile"):
        env["E2E_MOBILE"] = "1"  # the repository's playwright.config enables its mobile projects on this
    return env


class Run:
    """One Playwright process: its output read as it comes, the host lock's lines parsed from stderr, and two clocks.
    The run's time limit (TEST_TIMEOUT_S) counts from the moment the lock was acquired — or from the start when the
    repository prints no lock lines; the wait for the lock has its own limit (LOCK_WAIT_S)."""

    def __init__(self, argv: list[str], cwd_: Path, env: dict[str, str]):
        self.t0 = time.time()
        self.p = subprocess.Popen(argv, cwd=str(cwd_), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                  errors="replace", start_new_session=True)   # own group: a stop takes Playwright, its browsers and its server
        self.out: list[str] = []
        self.err: list[str] = []
        self.waiting_since: float | None = None   # first «waiting for the lock» line
        self.acquired_at: float | None = None
        self.waited_s: float = 0.0
        self.holder = ""
        self.lock_skipped = False
        self.tests_started = 0
        self.tests_total = 0
        self.stopped: str | None = None           # None | "run_timeout" | "lock_timeout"
        self._threads = [threading.Thread(target=self._read, args=(self.p.stdout, self.out, False), daemon=True),
                         threading.Thread(target=self._read, args=(self.p.stderr, self.err, True), daemon=True)]
        for t in self._threads:
            t.start()

    def _read(self, fh: Any, sink: list[str], is_err: bool) -> None:
        for line in iter(fh.readline, ""):
            sink.append(line)
            self._parse(line)

    def _parse(self, line: str) -> None:
        now = time.time()
        m = LOCK_ACQUIRED_RE.search(line)
        if m and self.acquired_at is None:
            self.acquired_at = now
            self.waited_s = float(m.group(1)) if self.waiting_since is None else max(float(m.group(1)), now - self.waiting_since)
            return
        m = LOCK_WAIT_RE.search(line)
        if m and self.acquired_at is None:
            if self.waiting_since is None:
                self.waiting_since = now - float(m.group(1))
            h = LOCK_HOLDER_RE.search(line)
            self.holder = h.group(1) if h else self.holder
            return
        if LOCK_SKIPPED_RE.search(line) and self.acquired_at is None:
            self.lock_skipped = True
            self.acquired_at = now
            self.waited_s = now - (self.waiting_since or self.t0)
            return
        m = TEST_LINE_RE.match(line)
        if m:
            self.tests_started = max(self.tests_started, int(m.group(1)))
            self.tests_total = int(m.group(2))

    @property
    def phase(self) -> str:
        return "waiting" if self.waiting_since is not None and self.acquired_at is None else "running"

    def ran_s(self, now: float | None = None) -> float:
        now = now or time.time()
        return now - (self.acquired_at or self.t0)

    def waited(self, now: float | None = None) -> float:
        if self.acquired_at is not None or self.waiting_since is None:
            return self.waited_s
        return (now or time.time()) - self.waiting_since

    def progress_line(self) -> str:
        if self.phase == "waiting":
            return (f"team-play: waiting for the e2e host lock: {self.waited():.0f}s of {LOCK_WAIT_S}s (another Playwright run holds it"
                    + (f": {self.holder}" if self.holder else "") + "); the test limit has not started")
        tests = f", test {self.tests_started}/{self.tests_total}" if self.tests_total else ""
        return f"team-play: playwright test running {self.ran_s():.0f}s of {TEST_TIMEOUT_S}s{tests} (waited for the lock {self.waited():.0f}s)"

    def wait(self) -> int:
        last = time.time()
        while self.p.poll() is None:
            time.sleep(POLL_S)
            now = time.time()
            if self.phase == "waiting" and self.waited(now) > LOCK_WAIT_S:
                self.stop("lock_timeout")
            elif self.phase == "running" and self.ran_s(now) > TEST_TIMEOUT_S:
                self.stop("run_timeout")
            elif now - last >= PROGRESS_EVERY_S:
                print(self.progress_line(), file=sys.stderr, flush=True)
                last = now
        for t in self._threads:
            t.join(timeout=5)
        self.ended = time.time()
        return self.p.returncode

    def stop(self, why: str) -> None:
        """SIGTERM to the whole group (Playwright's teardown stops the repository's server and frees the lock), SIGKILL after
        a grace. Before 26.11 only `npm exec` was killed: Playwright went on (ppid 1), holding the lock for the next run."""
        self.stopped = why
        for sig, grace in ((signal.SIGTERM, KILL_GRACE_S), (signal.SIGKILL, 5)):
            try:
                os.killpg(self.p.pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                self.p.wait(timeout=grace)
                return
            except subprocess.TimeoutExpired:
                continue


def read_report(path: Path, stdout: str) -> dict | None:
    """The JSON reporter's file (PLAYWRIGHT_JSON_OUTPUT_NAME); a repository or a fake that prints JSON on stdout still works."""
    try:
        if path.exists() and path.stat().st_size:
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    for i, line in enumerate(stdout.splitlines()):
        if line.lstrip().startswith("{"):
            try:
                return json.loads("\n".join(stdout.splitlines()[i:]))
            except json.JSONDecodeError:
                continue
    return None


def record(entry: dict[str, Any]) -> None:
    """One line per run in <review folder>/team-play.jsonl: what the operator and the PO count (reason, wait, run)."""
    try:
        with (out_dir() / RUNS_LOG).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError:
        pass


def run_once(a: argparse.Namespace, base: str, local: bool) -> tuple[int, str, dict[str, Any]]:
    """One Playwright run → (exit code for team-play, reason word, facts). Prints the run's own report."""
    out = out_dir()
    stamp = time.strftime("%Y%m%dT%H%M%S")
    raw_path = out / f"playwright-{stamp}{'-local' if local else ''}.json"
    raw_path.unlink(missing_ok=True)
    argv = build_test_argv(a)
    env = test_env(a, base, local, raw_path)
    target = f"a local server of this checkout (port {env[LOCAL_PORT_ENV]}, the repository's test settings)" if local else base
    print(f"team-play: playwright test started against {target}; the {TEST_TIMEOUT_S}s limit counts from the moment the e2e host lock "
          f"is held, the wait for the lock has its own {LOCK_WAIT_S}s (progress every {PROGRESS_EVERY_S}s)", file=sys.stderr, flush=True)
    try:
        r = Run(argv, cwd(), env)
    except FileNotFoundError:
        print(f"team-play: {argv[0]} not found — Node and Playwright are not installed on this machine (friction for the retro)")
        return 2, "tool_missing", {}
    rc = r.wait()
    facts = {"waited_s": round(r.waited(r.ended)), "ran_s": round(r.ran_s(r.ended)), "local": local, "lock_skipped": r.lock_skipped}
    timing = f"waited for the lock {facts['waited_s']}s, ran {facts['ran_s']}s"
    stdout, stderr = "".join(r.out), "".join(r.err)
    if r.stopped:
        what = (f"stopped: no e2e host lock within {LOCK_WAIT_S}s (holder: {r.holder or '?'}); the tests did not start" if r.stopped == "lock_timeout"
                else f"stopped: the tests ran {facts['ran_s']}s after the lock, limit {TEST_TIMEOUT_S}s"
                     + (f"; {r.tests_started} of {r.tests_total} tests had started" if r.tests_total else ""))
        print(f"team-play: playwright test {what} · {timing}")
        tail = _clean(stdout)[-TAIL_CHARS:]
        if tail.strip():
            print("output tail (one line per test that started):\n" + tail)
        return 124, r.stopped, facts
    report = read_report(raw_path, stdout)
    if report is None:
        tail = _clean(stdout + stderr)
        print(f"team-play: no JSON report (exit {rc}) · {timing}; output tail:\n{tail[-2000:]}")
        if a.project and re.search(r"project.*not found", tail, re.I):
            m = re.search(r"Available projects?:[^\n]*", tail, re.I)
            print("team-play: unknown --project " + repr(a.project) + ". " + (m.group(0) if m else
                  "List the names with `npx playwright test --list` (project names are in its output) or read playwright.config.ts; projects starting with 'mobile' get E2E_MOBILE=1."))
            return 2, "bad_arguments", facts
        if re.search(r"No tests found", tail):
            return 2, ("no_tests_live" if not local else "no_tests"), facts
        return 2, ("server_not_started" if local and re.search(r"e2e server|did not answer|exited with code", tail) else "no_report"), facts
    if not raw_path.exists():
        raw_path.write_text(json.dumps(report), encoding="utf-8")
    lines, totals = summarize(report)
    facts["totals"] = {k: v for k, v in totals.items() if v}
    copied = copy_artifacts(cwd() / RESULTS_DIR, out)
    where = "a local server of this checkout" if local else base
    print(f"playwright test against {where}: " + (", ".join(f"{k} {v}" for k, v in totals.items() if v) or "no tests")
          + f" · {timing} · exit {rc}")
    for ln in lines[:MAX_TESTS_SHOWN]:
        print(ln)
    if len(lines) > MAX_TESTS_SHOWN:
        print(f"… {len(lines) - MAX_TESTS_SHOWN} more tests in {raw_path}")
    errs = [e for e in (report.get("errors") or []) if isinstance(e, dict) and e.get("message")]
    for e in errs[:5]:
        print("error: " + _clean(e["message"]).strip()[:MAX_ERROR_CHARS])
    if copied:
        print("artifacts: " + ", ".join(copied[:20]))
    print(f"report: {raw_path}")
    if not lines:
        return 2, ("no_tests_live" if not local else "no_tests"), facts
    if totals["failed"] or totals["timedOut"]:
        return 1, "tests_failed", facts
    return (0, "passed", facts) if rc == 0 else (1, "tests_failed", facts)


def cmd_test(a: argparse.Namespace) -> int:
    for g in a.grep or []:
        if set(g) & GREP_DENIED or g.startswith("-"):
            sys.exit("team-play: --grep takes plain text (no | ; & $ ` \" < > ( ) and no leading -); repeat --grep to select several tests")
    if a.spec and (".." in Path(a.spec).parts or Path(a.spec).is_absolute()):
        sys.exit("team-play: --spec is a path inside the checkout")
    base = _env("TEAM_PLAY_BASE_URL")
    local = bool(getattr(a, "local", False))
    if not base and not local:
        sys.exit("team-play: TEAM_PLAY_BASE_URL is not set: no server to test against (or pass --local)")
    code, reason, facts = run_once(a, base, local)
    runs = [{"local": local, "reason": reason, **facts}]
    if reason == "no_tests_live" and LOCAL_FALLBACK:
        # 19 of 53 specs of ai-battles (26.11) have no @live test: they need the server's test settings (dictionary text
        # generation, short matches, own garage). The same commit is checked out and built here, so its own server proves it.
        print("team-play: this selection has no test for the deployed host; running it once more against a local server of the "
              "same checkout, started by the repository's playwright config with its test settings")
        code, reason, facts = run_once(a, base, True)
        runs.append({"local": True, "reason": reason, **facts})
    print(f"team-play: result {reason}" + ("" if reason == "passed" else
          f" (if this is why a criterion could not be checked, write `inconclusive_reason: {INCONCLUSIVE_FOR.get(reason, 'other')}`)"))
    for r in runs:
        record({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "spec": a.spec or "", "project": a.project or "",
                "grep": "|".join(a.grep or []), **r})
    return code


def _clean(s: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def copy_artifacts(results: Path, out: Path) -> list[str]:
    """Screenshots, traces and videos Playwright left under test-results/ → the review folder, flat names."""
    names: list[str] = []
    if not results.is_dir():
        return names
    for p in sorted(results.rglob("*")):
        if p.is_file() and p.suffix.lower() in (".png", ".webm", ".zip", ".txt"):
            name = "__".join(p.relative_to(results).parts)[-120:]
            shutil.copy2(p, out / name)
            names.append(name)
    return names


def cmd_screenshot(a: argparse.Namespace) -> int:
    if getattr(a, "local", False) and not getattr(a, "_base", None):
        if not str(a.url).startswith("/"):
            sys.exit("team-play: with --local give the path on the server, e.g. `team-play screenshot /?render=test --local --out x.png`")
        with local_server(getattr(a, "env", None) or []) as base_local:
            a._base, a.url = base_local, base_local + str(a.url)
            return cmd_screenshot(a)
    if not getattr(a, "_base", None) and not allowed_url(a.url):
        sys.exit(f"team-play: {a.url} is not on an allowed host (the play-test server or TEAM_ALLOWED_HOSTS)")
    name = a.out if a.out.lower().endswith(".png") else a.out + ".png"
    if not NAME_RE.match(name):
        sys.exit("team-play: --out is a plain file name (letters, digits, - _ .), e.g. lobby-360.png")
    w, h = int(a.width or DEFAULT_VIEWPORT[0]), int(a.height or DEFAULT_VIEWPORT[1])
    if not (100 <= w <= MAX_VIEWPORT and 100 <= h <= MAX_VIEWPORT):
        sys.exit("team-play: viewport is 100..4000 px")
    path = out_dir() / name
    argv = [*npx(), "playwright", "screenshot", f"--viewport-size={w},{h}", "--wait-for-timeout", str(int(a.wait_ms))]
    if a.full_page:
        argv.append("--full-page")
    argv += [a.url, str(path)]
    try:
        cp = subprocess.run(argv, cwd=str(cwd()), capture_output=True, text=True, timeout=SCREENSHOT_TIMEOUT_S)
    except FileNotFoundError:
        sys.exit(f"team-play: {argv[0]} not found — Node and Playwright are not installed on this machine")
    except subprocess.TimeoutExpired:
        sys.exit(f"team-play: screenshot timed out after {SCREENSHOT_TIMEOUT_S}s")
    if cp.returncode != 0 or not path.exists():
        print(f"team-play: screenshot failed (exit {cp.returncode}): {(cp.stdout + cp.stderr)[-800:]}")
        return 1
    print(f"screenshot {w}x{h}{' full page' if a.full_page else ''} of {a.url} → {path} ({path.stat().st_size} bytes)")
    return 0


def cmd_logs(a: argparse.Namespace) -> int:
    p = _env("TEAM_PLAY_SERVER_LOG")
    if not p or not Path(p).exists():
        print("team-play: no server log (deployed host in use, or the server was not started by the dispatcher)")
        return 0
    text = Path(p).read_text(encoding="utf-8", errors="replace")
    n = max(200, int(a.tail))
    print(text[-n:])
    return 0


def parse_load_output(text: str) -> dict | None:
    """The load script's last JSON object line (`{"clients": …}`); None when there is none."""
    for line in reversed((text or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(d, dict):
                return d
    return None


def load_summary(d: dict, base: str, seconds: float) -> str:
    parts = [f"{k} {d[k]}" for k in LOAD_KEYS if k in d]
    return f"load against {base}: " + ", ".join(parts) + f" · {seconds:.0f}s"


def server_version(base: str) -> str:
    """The `version` of `<base>/healthz`, "" when the server has no such field or does not answer. A load run reads it before
    and after: a deploy in the middle restarts the server and drops every client, and those disconnects are not the game's
    (ai-battles 23.11: a load task got `fail` for exactly this)."""
    import urllib.request
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/healthz", timeout=LOAD_VERSION_TIMEOUT_S) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return str(d.get("version") or "")[:64] if isinstance(d, dict) else ""
    except Exception:  # noqa: BLE001 — best effort: no version is not an error of the run
        return ""


def _test_settings() -> tuple[dict[str, str], set[str]]:
    """(`playtest.test_env`, `playtest.test_flags`) of product.yaml as the dispatcher passed them; exits when the product
    has none — a local server without the test settings would show the live behaviour, not the tested one."""
    raw = _env("TEAM_PLAY_TEST_ENV")
    if not raw:
        sys.exit("team-play: --local is not set up for this product: product.yaml has no `playtest.test_env` (the variables of a test "
                 "server). Say so in `limits` with `inconclusive_reason: test_tooling` and file one observation for the PO.")
    try:
        d = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit("team-play: TEAM_PLAY_TEST_ENV is not JSON (dispatcher error; friction for the retro)")
    env = {str(k): str(v) for k, v in (d if isinstance(d, dict) else {}).items() if ENV_NAME_RE.match(str(k))}
    return env, {f.strip() for f in _env("TEAM_PLAY_TEST_FLAGS").split(",") if f.strip()}


class local_server:
    """`with local_server(["SAW_ENABLED=1"]) as base:` — a server of the checkout on a free port, started with the
    product's run command, a clean environment (no secrets), the test settings and the flags asked for; stopped with
    its whole process group on exit. `{port}` and `{tmp}` in a setting's value are filled in (`{tmp}` = a folder of
    this server under the review folder: replays, a garage, a database of its own)."""

    def __init__(self, pairs: list[str] | None = None):
        self.pairs, self.proc, self.log = list(pairs or []), None, None
        self.shown: list[str] = []

    def __enter__(self) -> str:
        from . import playtest
        settings, flags = _test_settings()
        asked: dict[str, str] = {}
        for pair in self.pairs:
            name, _, value = str(pair).partition("=")
            if not ENV_NAME_RE.match(name) or not ENV_VALUE_RE.match(value):
                sys.exit("team-play: --env takes NAME=VALUE with a plain value (letters, digits, _ . , : / @ + -), e.g. --env SAW_ENABLED=1")
            if name not in flags:
                sys.exit(f"team-play: --env {name} is not a test flag of this product; allowed: {', '.join(sorted(flags)) or '(none: product.yaml playtest.test_flags is empty)'}")
            asked[name] = value
        run_cmd = _env("TEAM_PLAY_RUN_CMD")
        if not run_cmd:
            sys.exit("team-play: TEAM_PLAY_RUN_CMD is not set: the dispatcher did not say how the server starts")
        port = playtest.free_port()
        tmp = out_dir() / f"local-server-{port}"
        tmp.mkdir(parents=True, exist_ok=True)
        env = playtest.clean_env()
        for k in ("BASE_URL", "PLAYWRIGHT_BASE_URL"):
            env.pop(k, None)
        fill = lambda v: v.replace("{port}", str(port)).replace("{tmp}", str(tmp))   # noqa: E731
        env.update({k: fill(v) for k, v in settings.items()})
        env.update(asked)
        env.update({"PORT": str(port), "HOST": playtest.HOST})
        self.shown = [f"{k}={v}" for k, v in asked.items()]
        self.log = (tmp / "server.log").open("w", encoding="utf-8")
        self.proc = subprocess.Popen(playtest.command_for_port(run_cmd, port), shell=True, cwd=str(cwd()), stdout=self.log, stderr=subprocess.STDOUT,
                                     env=env, start_new_session=True)
        base = f"http://{playtest.HOST}:{port}"
        health = base + (_env("TEAM_PLAY_HEALTH_PATH") or playtest.DEFAULT_HEALTH_PATH)
        t0 = time.time()
        while time.time() - t0 < LOCAL_START_TIMEOUT_S:
            if self.proc.poll() is not None:
                break
            if playtest.health_ok(health):
                return base
            time.sleep(0.3)
        code = self.proc.poll()
        self.__exit__(None, None, None)
        tail = (tmp / "server.log").read_text(encoding="utf-8", errors="replace")[-LOCAL_LOG_TAIL:]
        sys.exit(f"team-play: result server_not_started — the local server {'exited with code ' + str(code) if code is not None else 'did not answer ' + health} "
                 f"within {LOCAL_START_TIMEOUT_S:.0f}s. Log tail:\n{tail}")

    def __exit__(self, *exc: Any) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=LOCAL_STOP_GRACE_S)
            except (ProcessLookupError, PermissionError):
                pass
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        if self.log is not None:
            self.log.close()


def cmd_unit(a: argparse.Namespace) -> int:
    """`team-play unit`: lines of the whole output of the dispatcher's unit-test run. The prompt shows only its tail
    (ai-battles T-0435: the test the acceptance named was cut off, and the tester may not run the tests itself)."""
    p = _env("TEAM_PLAY_UNIT_LOG")
    if not p or not Path(p).exists():
        print("team-play: no full test output was kept for this review; judge from the TEST RUN section of the prompt")
        return 0
    lines = Path(p).read_text(encoding="utf-8", errors="replace").splitlines()
    if a.grep:
        want = str(a.grep).lower()
        keep: set[int] = set()
        for i, ln in enumerate(lines):
            if want in ln.lower():
                keep.update(range(i, min(len(lines), i + 1 + UNIT_CONTEXT_LINES)))
        picked = [lines[i] for i in sorted(keep)]
        if not picked:
            print(f"team-play: no line of the test output ({len(lines)} lines) contains «{a.grep}»")
            return 0
    else:
        picked = lines[-max(1, int(a.tail)):]
    print(f"unit-test output: {len(picked[:UNIT_MAX_LINES])} of {len(lines)} lines" + (f" matching «{a.grep}» (+{UNIT_CONTEXT_LINES} after each)" if a.grep else " (tail)"))
    print("\n".join(picked[:UNIT_MAX_LINES]))
    if len(picked) > UNIT_MAX_LINES:
        print(f"… {len(picked) - UNIT_MAX_LINES} more line(s): narrow --grep")
    return 0


def cmd_shots(a: argparse.Namespace) -> int:
    """`team-play shots`: run the repository's battle-frames script against BASE_URL. The script plays one battle in one
    browser and writes PNG frames into the review folder; its last stdout line is a JSON summary {frames: [{file, what, facts}]}.
    The tester then reads the frames (Read) and compares them with the design. What a battle is and what is worth a
    frame is the game's knowledge, so it lives in the repository, like the load script."""
    script = Path(a.script or SHOTS_SCRIPT)
    if ".." in script.parts or script.is_absolute():
        sys.exit("team-play: --script is a path inside the checkout")
    if getattr(a, "local", False) and not getattr(a, "_base", None):
        # rework slice 6: a feature that is off on the live host (the saw, T-0238) is played on a server of this checkout
        with local_server(getattr(a, "env", None) or []) as base_local:
            a._base = base_local
            print(f"team-play: playing on a local server of this checkout ({base_local}), test settings on"
                  + (f", flags {' '.join(a.env)}" if getattr(a, "env", None) else ""))
            return cmd_shots(a)
    base = getattr(a, "_base", None) or _env("TEAM_PLAY_BASE_URL")
    if not base:
        sys.exit("team-play: TEAM_PLAY_BASE_URL is not set: no server to play on (or pass --local)")
    if not (cwd() / script).exists():
        sys.exit(f"team-play: {script} not found in the checkout — the repository needs a battle-frames script (see the tester card); "
                 "say so in `limits` and file one observation for the PO")
    prefix = a.prefix or "battle"
    if not NAME_RE.match(prefix):
        sys.exit("team-play: --prefix is a plain name (letters, digits, - _ .), e.g. T-0173")
    extra: list[str] = []
    for flag, v in (("--weapon", a.weapon), ("--ability", a.ability)):
        if v:
            if not SHOTS_KEY_RE.match(v):
                sys.exit(f"team-play: {flag} is a catalogue key in lower case (letters and _), e.g. railgun")
            extra += [flag, v]
    w, h = int(a.width or SHOTS_VIEWPORT[0]), int(a.height or SHOTS_VIEWPORT[1])
    if not (100 <= w <= MAX_VIEWPORT and 100 <= h <= MAX_VIEWPORT):
        sys.exit("team-play: viewport is 100..4000 px")
    out = out_dir()
    argv = [_env("TEAM_PLAY_NODE", "node"), str(script), *extra]
    env = dict(os.environ)
    env.update({"BASE_URL": base, "CI": "1", "SHOTS_OUT": str(out), "SHOTS_PREFIX": prefix, "SHOTS_WIDTH": str(w), "SHOTS_HEIGHT": str(h)})
    try:
        cp = subprocess.run(argv, cwd=str(cwd()), capture_output=True, text=True, timeout=SHOTS_TIMEOUT_S, env=env)
    except FileNotFoundError:
        sys.exit(f"team-play: {argv[0]} not found — Node is not installed on this machine (friction for the retro)")
    except subprocess.TimeoutExpired:
        sys.exit(f"team-play: the battle-frames script did not finish within {SHOTS_TIMEOUT_S}s")
    d = parse_load_output(cp.stdout)            # the same contract as the load script: the last JSON object of stdout
    frames = [f for f in (d or {}).get("frames") or [] if isinstance(f, dict) and NAME_RE.match(str(f.get("file") or ""))]
    present = [f for f in frames if (out / str(f["file"])).exists()]
    if d is None or not present:
        print(f"team-play: no battle frames (exit {cp.returncode}). {str((d or {}).get('error') or '')[:MAX_ERROR_CHARS]}\noutput tail:\n{(cp.stdout + cp.stderr)[-1500:]}")
        return 2
    where = f"a local server of this checkout ({base}{', ' + ' '.join(a.env) if getattr(a, 'env', None) else ''})" if getattr(a, "_base", None) else base
    print(f"battle frames {w}x{h} against {where}: {len(present)} of {len(frames)} written, car {json.dumps(d.get('car') or {})}, exit {cp.returncode}")
    for f in present:
        facts = json.dumps(f.get("facts") or {}, ensure_ascii=False)[:SHOTS_FACTS_CHARS]
        print(f"- {out / str(f['file'])} · {f.get('what', '')} · {facts}")
    errs = [str(e)[:160] for e in (d.get("page_errors") or [])][:5]
    if errs:
        print("page errors: " + " | ".join(errs))
    print("Read every frame before you judge: the facts line is what the page reports, the picture is what the player sees.")
    return 0 if cp.returncode == 0 else 1


def cmd_load(a: argparse.Namespace) -> int:
    """`team-play load`: run the repository's load script (slice 6) with bounded clients/seconds, print its JSON summary."""
    if not (1 <= a.clients <= MAX_LOAD_CLIENTS) or not (1 <= a.seconds <= MAX_LOAD_SECONDS):
        sys.exit(f"team-play: --clients 1..{MAX_LOAD_CLIENTS}, --seconds 1..{MAX_LOAD_SECONDS}")
    per_room = getattr(a, "clients_per_room", None)
    if per_room is not None and not (1 <= per_room <= MAX_LOAD_PER_ROOM):
        sys.exit(f"team-play: --clients-per-room 1..{MAX_LOAD_PER_ROOM}")
    script = Path(a.script or LOAD_SCRIPT)
    if ".." in script.parts or script.is_absolute():
        sys.exit("team-play: --script is a path inside the checkout")
    base = _env("TEAM_PLAY_BASE_URL")
    if not base:
        sys.exit("team-play: TEAM_PLAY_BASE_URL is not set: no server to load")
    if not (cwd() / script).exists():
        sys.exit(f"team-play: {script} not found in the checkout — the repository needs a load script (starter: team/templates/service-repo/tests/load/clients.ts)")
    runner = (_env("TEAM_PLAY_LOAD_RUNNER") or " ".join(LOAD_RUNNER)).split()
    argv = [*npx(), *runner, str(script), "--base-url", base, "--clients", str(a.clients), "--seconds", str(a.seconds), "--rate", str(a.rate)]
    if a.moves:
        argv.append("--moves")
    env = dict(os.environ)
    env.update({"BASE_URL": base, "CI": "1"})
    if per_room is not None:   # the profile of the task («5 rooms of 4»): the script reads the flag or the variable
        argv += ["--clients-per-room", str(per_room)]
        env["CLIENTS_PER_ROOM"] = str(per_room)
    v_before = server_version(base)
    t0 = time.time()
    try:
        cp = subprocess.run(argv, cwd=str(cwd()), capture_output=True, text=True, timeout=a.seconds + LOAD_EXTRA_TIMEOUT_S, env=env)
    except FileNotFoundError:
        sys.exit(f"team-play: {argv[0]} not found — Node is not installed on this machine (friction for the retro)")
    except subprocess.TimeoutExpired:
        sys.exit(f"team-play: load script did not finish within {a.seconds + LOAD_EXTRA_TIMEOUT_S}s")
    took = time.time() - t0
    out = out_dir()
    stamp = time.strftime("%Y%m%dT%H%M%S")
    raw_path = out / f"load-{stamp}.json"
    d = parse_load_output(cp.stdout)
    if d is None:
        print(f"team-play: the load script printed no JSON summary (exit {cp.returncode}); output tail:\n{(cp.stdout + cp.stderr)[-2000:]}")
        return 2
    v_after = server_version(base)
    redeployed = bool(v_before and v_after and v_before != v_after)
    d = {**d, "base_url": base, "script": str(script), "exit": cp.returncode, "took_s": round(took, 1),
         **({"clients_per_room": per_room} if per_room is not None else {}),
         **({"version_before": v_before, "version_after": v_after} if v_before or v_after else {})}
    raw_path.write_text(json.dumps(d, indent=1), encoding="utf-8")
    print(load_summary(d, base, took) + f" · exit {cp.returncode}")
    if redeployed:
        print(f"SERVER REDEPLOYED DURING THE RUN: version {v_before[:12]} -> {v_after[:12]}. A deploy restarts the server and drops every "
              "client, so the disconnects and the missing samples of this run are not the game's: verdict `inconclusive`, run it again.")
    if cp.stderr.strip():
        print("stderr: " + re.sub(r"\x1b\[[0-9;]*m", "", cp.stderr).strip()[-MAX_ERROR_CHARS:])
    print(f"report: {raw_path}")
    return 0 if cp.returncode == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="team-play", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("test"); s.add_argument("--grep", action="append"); s.add_argument("--spec"); s.add_argument("--project")
    s.add_argument("--local", action="store_true"); s.set_defaults(fn=cmd_test)
    s = sub.add_parser("screenshot"); s.add_argument("url"); s.add_argument("--out", required=True); s.add_argument("--width", type=int)
    s.add_argument("--height", type=int); s.add_argument("--full-page", action="store_true"); s.add_argument("--wait-ms", type=int, default=800)
    s.add_argument("--local", action="store_true"); s.add_argument("--env", action="append")
    s.set_defaults(fn=cmd_screenshot)
    s = sub.add_parser("shots"); s.add_argument("--weapon"); s.add_argument("--ability"); s.add_argument("--prefix"); s.add_argument("--script")
    s.add_argument("--width", type=int); s.add_argument("--height", type=int)
    s.add_argument("--local", action="store_true"); s.add_argument("--env", action="append"); s.set_defaults(fn=cmd_shots)
    s = sub.add_parser("unit"); s.add_argument("--grep"); s.add_argument("--tail", type=int, default=60); s.set_defaults(fn=cmd_unit)
    s = sub.add_parser("logs"); s.add_argument("--tail", type=int, default=3000); s.set_defaults(fn=cmd_logs)
    s = sub.add_parser("load"); s.add_argument("--clients", type=int, required=True); s.add_argument("--seconds", type=int, required=True)
    s.add_argument("--rate", type=float, default=2.0); s.add_argument("--script"); s.add_argument("--moves", action="store_true")
    s.add_argument("--clients-per-room", type=int, dest="clients_per_room"); s.set_defaults(fn=cmd_load)
    a = ap.parse_args(argv)
    return int(a.fn(a) or 0)


if __name__ == "__main__":
    sys.exit(main())
