"""`team-play` — the tester's only shell command (game profile, slice 4; owner 20.11). Like `team-fetch` for the reviewer:
one allow-listed binary that runs Playwright in a fixed checkout against a fixed base URL and prints a compact result.

  team-play test [--grep TEXT] [--spec PATH] [--project NAME]    run the repository's Playwright tests (tests/e2e) against BASE_URL
  team-play screenshot URL --out NAME [--width W --height H] [--full-page]   a PNG of a page into the review folder
  team-play logs [--tail N]                                       the tail of the play-test server log

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
        print(f"team-play: no JSON report (exit {cp.returncode}); output tail:\n{(cp.stdout + cp.stderr)[-2000:]}")
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="team-play", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("test"); s.add_argument("--grep"); s.add_argument("--spec"); s.add_argument("--project"); s.set_defaults(fn=cmd_test)
    s = sub.add_parser("screenshot"); s.add_argument("url"); s.add_argument("--out", required=True); s.add_argument("--width", type=int)
    s.add_argument("--height", type=int); s.add_argument("--full-page", action="store_true"); s.add_argument("--wait-ms", type=int, default=800)
    s.set_defaults(fn=cmd_screenshot)
    s = sub.add_parser("logs"); s.add_argument("--tail", type=int, default=3000); s.set_defaults(fn=cmd_logs)
    a = ap.parse_args(argv)
    return int(a.fn(a) or 0)


if __name__ == "__main__":
    sys.exit(main())
