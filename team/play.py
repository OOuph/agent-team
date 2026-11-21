"""`team-play` — the tester's only shell command (game profile, slice 4; owner 20.11). Like `team-fetch` for the reviewer:
one allow-listed binary that runs Playwright in a fixed checkout against a fixed base URL and prints a compact result.

  team-play test [--grep TEXT] [--spec PATH] [--project NAME]    run the repository's Playwright tests (tests/e2e) against BASE_URL
  team-play screenshot URL --out NAME [--width W --height H] [--full-page]   a PNG of a page into the review folder
  team-play logs [--tail N]                                       the tail of the play-test server log
  team-play load --clients N --seconds S [--rate R] [--script tests/load/clients.ts] [--moves]   N WebSocket clients against BASE_URL

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
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

TEST_TIMEOUT_S = int(os.environ.get("TEAM_PLAY_TIMEOUT_S") or 600)
SCREENSHOT_TIMEOUT_S = 60
MAX_ERROR_CHARS = 400
MAX_TESTS_SHOWN = 60
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,60}$")
GREP_DENIED = set('|;&$`"<>()')
DEFAULT_VIEWPORT = (360, 740)        # the phone first (VISION: portrait, 360 px)
MAX_VIEWPORT = 4000
RESULTS_DIR = "test-results"         # Playwright's default output folder inside the checkout
LOAD_SCRIPT = "tests/load/clients.ts"   # the repository's load script (starter: team/templates/service-repo/tests/load/clients.ts)
LOAD_RUNNER = ["tsx"]                   # `npx tsx <script>`; TEAM_PLAY_LOAD_RUNNER overrides (e.g. "node" for a .js script)
MAX_LOAD_CLIENTS = 200
MAX_LOAD_SECONDS = 300
LOAD_EXTRA_TIMEOUT_S = 120
LOAD_KEYS = ("clients", "connected", "seconds", "messages", "samples", "p50_ms", "p95_ms", "max_ms", "errors", "disconnects", "rejected")


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


def cmd_test(a: argparse.Namespace) -> int:
    if a.grep and (set(a.grep) & GREP_DENIED or a.grep.startswith("-")):
        sys.exit("team-play: --grep takes plain text (no | ; & $ ` \" < > ( ) and no leading -)")
    if a.spec and (".." in Path(a.spec).parts or Path(a.spec).is_absolute()):
        sys.exit("team-play: --spec is a path inside the checkout")
    base = _env("TEAM_PLAY_BASE_URL")
    if not base:
        sys.exit("team-play: TEAM_PLAY_BASE_URL is not set: no server to test against")
    argv = [*npx(), "playwright", "test", "--reporter=json"]
    if a.project:
        argv += ["--project", a.project]
    if a.grep:
        argv += ["--grep", a.grep]
    if a.spec:
        argv.append(a.spec)
    env = dict(os.environ)
    env.update({"BASE_URL": base, "PLAYWRIGHT_BASE_URL": base, "PW_TEST_HTML_REPORT_OPEN": "never", "CI": "1"})
    if a.project and a.project.lower().startswith("mobile"):
        env["E2E_MOBILE"] = "1"  # the repository's playwright.config enables its mobile projects on this
    t0 = time.time()
    try:
        cp = subprocess.run(argv, cwd=str(cwd()), capture_output=True, text=True, timeout=TEST_TIMEOUT_S, env=env)
    except FileNotFoundError:
        sys.exit(f"team-play: {argv[0]} not found — Node and Playwright are not installed on this machine (friction for the retro)")
    except subprocess.TimeoutExpired:
        sys.exit(f"team-play: playwright test timed out after {TEST_TIMEOUT_S}s")
    out = out_dir()
    stamp = time.strftime("%Y%m%dT%H%M%S")
    raw_path = out / f"playwright-{stamp}.json"
    try:
        start = cp.stdout.index("{")
        report = json.loads(cp.stdout[start:])
        raw_path.write_text(cp.stdout[start:], encoding="utf-8")
    except (ValueError, json.JSONDecodeError):
        tail = cp.stdout + cp.stderr
        print(f"team-play: no JSON report (exit {cp.returncode}); output tail:\n{tail[-2000:]}")
        if a.project and re.search(r"project.*not found", tail, re.I):
            m = re.search(r"Available projects?:[^\n]*", tail, re.I)
            print("team-play: unknown --project " + repr(a.project) + ". " + (m.group(0) if m else
                  "List the names with `npx playwright test --list` (project names are in its output) or read playwright.config.ts; projects starting with 'mobile' get E2E_MOBILE=1."))
        return 2
    lines, totals = summarize(report)
    copied = copy_artifacts(cwd() / RESULTS_DIR, out)
    print(f"playwright test against {base}: " + ", ".join(f"{k} {v}" for k, v in totals.items() if v) + f" · {time.time() - t0:.0f}s · exit {cp.returncode}")
    for ln in lines[:MAX_TESTS_SHOWN]:
        print(ln)
    if len(lines) > MAX_TESTS_SHOWN:
        print(f"… {len(lines) - MAX_TESTS_SHOWN} more tests in {raw_path}")
    errs = [e for e in (report.get("errors") or []) if isinstance(e, dict) and e.get("message")]
    for e in errs[:5]:
        print("error: " + re.sub(r"\x1b\[[0-9;]*m", "", e["message"]).strip()[:MAX_ERROR_CHARS])
    if copied:
        print("artifacts: " + ", ".join(copied[:20]))
    print(f"report: {raw_path}")
    return 0 if cp.returncode == 0 else 1


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
    if not allowed_url(a.url):
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


def cmd_load(a: argparse.Namespace) -> int:
    """`team-play load`: run the repository's load script (slice 6) with bounded clients/seconds, print its JSON summary."""
    if not (1 <= a.clients <= MAX_LOAD_CLIENTS) or not (1 <= a.seconds <= MAX_LOAD_SECONDS):
        sys.exit(f"team-play: --clients 1..{MAX_LOAD_CLIENTS}, --seconds 1..{MAX_LOAD_SECONDS}")
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
    d = {**d, "base_url": base, "script": str(script), "exit": cp.returncode, "took_s": round(took, 1)}
    raw_path.write_text(json.dumps(d, indent=1), encoding="utf-8")
    print(load_summary(d, base, took) + f" · exit {cp.returncode}")
    if cp.stderr.strip():
        print("stderr: " + re.sub(r"\x1b\[[0-9;]*m", "", cp.stderr).strip()[-MAX_ERROR_CHARS:])
    print(f"report: {raw_path}")
    return 0 if cp.returncode == 0 else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="team-play", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("test"); s.add_argument("--grep"); s.add_argument("--spec"); s.add_argument("--project"); s.set_defaults(fn=cmd_test)
    s = sub.add_parser("screenshot"); s.add_argument("url"); s.add_argument("--out", required=True); s.add_argument("--width", type=int)
    s.add_argument("--height", type=int); s.add_argument("--full-page", action="store_true"); s.add_argument("--wait-ms", type=int, default=800)
    s.set_defaults(fn=cmd_screenshot)
    s = sub.add_parser("logs"); s.add_argument("--tail", type=int, default=3000); s.set_defaults(fn=cmd_logs)
    s = sub.add_parser("load"); s.add_argument("--clients", type=int, required=True); s.add_argument("--seconds", type=int, required=True)
    s.add_argument("--rate", type=float, default=2.0); s.add_argument("--script"); s.add_argument("--moves", action="store_true"); s.set_defaults(fn=cmd_load)
    a = ap.parse_args(argv)
    return int(a.fn(a) or 0)


if __name__ == "__main__":
    sys.exit(main())
