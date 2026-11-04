"""Autonomy check: is the whole team able to run without the owner? (owner 04.11: "check again that everything works
without me, formalise the check protocol").

One command, one table: every component of the contour that must live for a week without a human, each with OK / WARN /
FAIL and the evidence it was judged on. Run by hand (`team autonomy-check`) or daily by cron with `--send`, which puts
the summary into the owner's ops chat. Exit code 1 when anything FAILs.

The protocol behind each check (what it proves, what to do on FAIL) is docs/autonomy-check.md.
"""
from __future__ import annotations

import json
import os
import socket
import ssl
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from . import billing
from .config import Product, Settings, load_products
from .store import Store

OK, WARN, FAIL = "OK", "WARN", "FAIL"

# Expected cron lines (substring match). Owner can add more; none of these may disappear while the owner is away.
CRON_REQUIRED = {
    "hourly cycle": "run-cycle.sh",
    "watchdog": "watchdog.sh >>",
    "state backup": "backup-state.sh",
    "diary build": "publish-diary.sh",
    "role report": "report-roles.sh",
    "agentsmd build": "build-site.sh agentsmd-tracker",
    "release-radar build": "build-site.sh release-radar",
    "luz build": "build-site.sh luz-amanha",
    "autonomy check": "autonomy-check.sh",
}
BUILD_SITES = ("agentsmd-tracker", "release-radar", "luz-amanha")
MAX_AGE_H = {"cycle_warn": 1.25, "cycle_fail": 2.5, "backup": 26, "build": 26, "diary": 26, "report": 13, "posts": 26,
             "owner_q": 2, "critic_report": 3, "critic_responses": 2, "billing": 2}


@dataclass
class Check:
    id: str
    name: str
    status: str
    evidence: str


def _age_h(ts: str | None, now: datetime) -> float | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (now - t).total_seconds() / 3600


def _mtime_age_h(p: Path, now: datetime) -> float | None:
    return (now.timestamp() - p.stat().st_mtime) / 3600 if p.exists() else None


def _fmt_h(h: float | None) -> str:
    return "never" if h is None else (f"{h * 60:.0f} min" if h < 1 else f"{h:.1f} h")


def _grade(h: float | None, limit: float, warn_at: float | None = None) -> str:
    if h is None or h > limit:
        return FAIL
    return WARN if warn_at is not None and h > warn_at else OK


def check_cron(st: Settings, crontab: str | None = None) -> Check:
    if crontab is None:
        try:
            crontab = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError) as exc:
            return Check("C1", "cron entries", FAIL, f"crontab unreadable: {exc}")
    live = [ln for ln in crontab.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    missing = [name for name, pat in CRON_REQUIRED.items() if not any(pat in ln for ln in live)]
    return Check("C1", "cron entries", FAIL if missing else OK,
                 f"missing: {', '.join(missing)}" if missing else f"all {len(CRON_REQUIRED)} present")


def check_stop(st: Settings) -> Check:
    stop = st.root / "STOP"
    return Check("C2", "no STOP file", FAIL if stop.exists() else OK, "STOP is set: nothing runs" if stop.exists() else "absent")


def check_cycles(products: list[Product], now: datetime) -> Check:
    rows, worst = [], OK
    for p in products:
        s = Store(p.dir)
        cyc = next((c for c in s.cycles(5) if c.get("finished")), None)
        s.close()
        h = _age_h(cyc["finished"] if cyc else None, now)
        g = _grade(h, MAX_AGE_H["cycle_fail"], MAX_AGE_H["cycle_warn"])
        worst = FAIL if FAIL in (worst, g) else (WARN if WARN in (worst, g) else OK)
        rows.append(f"{p.slug} {_fmt_h(h)}")
    return Check("C3", "last finished cycle per product", worst, "; ".join(rows))


def check_runs(products: list[Product], now: datetime) -> Check:
    since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    n = bad = 0
    for p in products:
        s = Store(p.dir)
        for e in s.events(since=since, limit=5000):
            if e["kind"] == "run":
                n += 1
                bad += 0 if (e.get("detail") or {}).get("ok") else 1
        s.close()
    if n == 0:
        return Check("C4", "role runs, last 24 h", FAIL, "no role runs in 24 h")
    share = bad / n
    return Check("C4", "role runs, last 24 h", OK if share < 0.10 else (WARN if share < 0.25 else FAIL),
                 f"{n} runs, {bad} failed ({share:.0%})")


def check_code(st: Settings, now: datetime) -> Check:
    good = (st.data_dir / "framework" / "last-good")
    good_sha = good.read_text(encoding="utf-8").strip() if good.exists() else ""
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=st.root, capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        head = ""
    wlog = st.data_dir / "watchdog.log"
    since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    rollbacks = [ln for ln in (wlog.read_text(encoding="utf-8").splitlines() if wlog.exists() else []) if ln[:19] >= since[:19]]
    status = FAIL if len(rollbacks) >= 3 else (WARN if rollbacks or (good_sha and head and good_sha != head) else OK)
    ev = f"HEAD {head[:9] or '?'}, last-good {good_sha[:9] or 'none'}" + (" (differ: the next clean cycle records HEAD)" if good_sha != head else "")
    return Check("C5", "code and watchdog", status, ev + f"; watchdog interventions 24 h: {len(rollbacks)}")


def check_tests(st: Settings) -> Check:
    py = st.root / ".venv" / "bin" / "python"
    try:
        r = subprocess.run([str(py if py.exists() else "python3"), "-m", "pytest", "-q", "-x", "tests"], cwd=st.root, capture_output=True,
                           text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("C6", "tests on HEAD", FAIL, f"could not run: {exc}")
    tail = (r.stdout.strip().splitlines() or ["?"])[-1]
    return Check("C6", "tests on HEAD", OK if r.returncode == 0 else FAIL, tail)


def check_backup(st: Settings, now: datetime) -> Check:
    bk = sorted((st.data_dir / "backups").glob("products-*.tgz"), key=lambda p: p.stat().st_mtime)
    h = _mtime_age_h(bk[-1], now) if bk else None
    return Check("C7", "state backup", _grade(h, MAX_AGE_H["backup"]), f"newest {bk[-1].name if bk else '—'} ({_fmt_h(h)} ago)")


def check_builds(st: Settings, now: datetime) -> Check:
    log = st.data_dir / "builds.log"
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    rows, worst = [], OK
    for site in BUILD_SITES:
        pushed = [ln for ln in lines if f"[{site}]" in ln and "pushed" in ln]
        h = _age_h(pushed[-1][:25] if pushed else None, now)
        g = _grade(h, MAX_AGE_H["build"])
        worst = FAIL if g == FAIL else worst
        rows.append(f"{site} {_fmt_h(h)}")
    dl = st.data_dir / "diary.log"
    dh = None
    if dl.exists() and "diary pushed" in dl.read_text(encoding="utf-8")[-4000:]:
        dh = _mtime_age_h(dl, now)
    g = _grade(dh, MAX_AGE_H["diary"])
    worst = FAIL if g == FAIL else worst
    rows.append(f"diary {_fmt_h(dh)}")
    return Check("C8", "site builds pushed (ago)", worst, "; ".join(rows))


def _https(url: str, timeout: float = 15.0) -> tuple[int | None, int | None, str]:
    host = url.split("/")[2]
    days = None
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=timeout) as sock, ctx.wrap_socket(sock, server_hostname=host) as ss:
            na = ss.getpeercert().get("notAfter")
            days = int((ssl.cert_time_to_seconds(na) - time.time()) // 86400) if na else None
        req = urllib.request.Request(url, headers={"User-Agent": "agent-team/autonomy-check"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, days, ""
    except Exception as exc:                                 # any network error is a finding, not a crash
        return None, days, str(exc)[:80]


def check_sites(products: list[Product], fetch: Callable[[str], tuple[int | None, int | None, str]] = _https) -> Check:
    rows, worst = [], OK
    for p in products:
        if not p.site_base:
            continue
        code, days, err = fetch(p.site_base + "/")
        g = OK if code == 200 and (days is None or days > 14) else (WARN if code == 200 else FAIL)
        worst = FAIL if g == FAIL else (WARN if g == WARN and worst == OK else worst)
        rows.append(f"{p.host} {code or err} cert {days if days is not None else '?'} d")
    return Check("C9", "sites answer, certificates", worst, "; ".join(rows))


def check_posts(products: list[Product], now: datetime) -> Check:
    rows, worst = [], OK
    for p in products:
        if not p.raw.get("channels"):
            continue
        s = Store(p.dir)
        posts = s.recent_posts(days=2)
        s.close()
        h = _age_h(posts[0]["ts"] if posts else None, now)
        g = _grade(h, MAX_AGE_H["posts"])
        worst = WARN if g != OK and worst == OK else worst          # a missed post is a quality issue, not an outage
        rows.append(f"{p.slug} {_fmt_h(h)}")
    return Check("C10", "a public post in the last 26 h", worst, "; ".join(rows) or "no products with channels")


def check_report(st: Settings, now: datetime) -> Check:
    reps = sorted((st.data_dir / "reports").glob("*.md"), key=lambda p: p.stat().st_mtime)
    h = _mtime_age_h(reps[-1], now) if reps else None
    return Check("C11", "12-hour role report", _grade(h, MAX_AGE_H["report"]), f"newest {reps[-1].name if reps else '—'} ({_fmt_h(h)} ago)")


def check_owner_questions(products: list[Product], now: datetime) -> Check:
    stale = []
    for p in products:
        s = Store(p.dir)
        for q in s.questions(states=["open"]):
            h = _age_h(q.get("created"), now)
            if q.get("to") == "owner" and h is not None and h > MAX_AGE_H["owner_q"]:
                stale.append(f"{p.slug}/{q.get('id')} {_fmt_h(h)}")
        s.close()
    return Check("C12", "owner questions answered by the deputy", WARN if stale else OK,
                 ("open > 2 h: " + ", ".join(stale[:6])) if stale else "none open longer than 2 h")


def check_critic(st: Settings, now: datetime) -> Check:
    cfg = st.raw.get("external_critic") or {}
    if not cfg.get("enabled"):
        return Check("C13", "external critic", OK, "disabled")
    d = Path(str(cfg.get("dir") or ""))
    reps = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime) if d.exists() else []
    rh = _mtime_age_h(reps[-1], now) if reps else None
    resp = Path(str(cfg.get("responses_dir") or "")) / "agent-team-responses.json"
    ph = _mtime_age_h(resp, now)
    g = OK if (rh is not None and rh <= MAX_AGE_H["critic_report"] and ph is not None and ph <= MAX_AGE_H["critic_responses"]) else WARN
    return Check("C13", "external critic in and answers out", g, f"newest report {_fmt_h(rh)} ago; responses written {_fmt_h(ph)} ago")


def check_billing(st: Settings, now: datetime) -> Check:
    s = billing.load(st)
    h = _age_h(s.get("updated"), now)
    stop = billing.paused(st)
    util = s.get("utilization")
    if stop:
        return Check("C14", "Claude plan and credits", FAIL, stop)
    if h is None or h > MAX_AGE_H["billing"]:
        return Check("C14", "Claude plan and credits", WARN, f"no limit info for {_fmt_h(h)}; " + billing.status_line(st))
    g = OK if s.get("overage_status") == "allowed" or (isinstance(util, (int, float)) and util < 0.9) else FAIL
    return Check("C14", "Claude plan and credits", g, billing.status_line(st))


def check_disk(st: Settings) -> Check:
    try:
        sv = os.statvfs(st.root)
        free = sv.f_bavail * sv.f_frsize / 1e9
    except OSError as exc:
        return Check("C15", "disk", WARN, str(exc))
    return Check("C15", "disk free", OK if free > 10 else (WARN if free > 3 else FAIL), f"{free:.0f} GB free")


SECRETS = ("CLAUDE_CODE_OAUTH_TOKEN", "GITHUB_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_OPS_CHAT_ID")


def check_secrets(st: Settings) -> Check:
    env = st.root / ".env"
    names = set()
    if env.exists():
        for ln in env.read_text(encoding="utf-8").splitlines():
            if "=" in ln and not ln.lstrip().startswith("#") and ln.split("=", 1)[1].strip():
                names.add(ln.split("=", 1)[0].strip().removeprefix("export "))
    missing = [n for n in SECRETS if n not in names and not os.environ.get(n)]
    return Check("C16", "required secrets present (names only)", FAIL if missing else OK,
                 f"missing: {', '.join(missing)}" if missing else f"{len(SECRETS)} of {len(SECRETS)}")


def check_budget(products: list[Product]) -> Check:
    rows, worst = [], OK
    for p in products:
        s = Store(p.dir)
        spent = s.spent_today()
        s.close()
        share = spent / p.daily_cap if p.daily_cap else 0
        if share >= 0.9:
            worst = WARN
        rows.append(f"{p.slug} ${spent:.2f}/{p.daily_cap:.0f}")
    return Check("C17", "daily budget per product (UTC day)", worst, "; ".join(rows))


def run_all(st: Settings, *, tests: bool = True, now: datetime | None = None) -> list[Check]:
    now = now or datetime.now(timezone.utc)
    products = load_products(st)
    checks: list[Callable[[], Check]] = [
        lambda: check_cron(st), lambda: check_stop(st), lambda: check_cycles(products, now), lambda: check_runs(products, now),
        lambda: check_code(st, now)] + ([lambda: check_tests(st)] if tests else []) + [
        lambda: check_backup(st, now), lambda: check_builds(st, now), lambda: check_sites(products),
        lambda: check_posts(products, now), lambda: check_report(st, now), lambda: check_owner_questions(products, now),
        lambda: check_critic(st, now), lambda: check_billing(st, now), lambda: check_disk(st), lambda: check_secrets(st),
        lambda: check_budget(products)]
    out = []
    for fn in checks:
        try:
            out.append(fn())
        except Exception as exc:                              # a broken check is itself a finding
            out.append(Check("C?", getattr(fn, "__name__", "check"), FAIL, f"check crashed: {exc}"[:200]))
    return out


def render(checks: list[Check], now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    n = {k: sum(1 for c in checks if c.status == k) for k in (OK, WARN, FAIL)}
    head = f"Проверка автономности {now.strftime('%Y-%m-%d %H:%M UTC')}: OK {n[OK]} · WARN {n[WARN]} · FAIL {n[FAIL]}"
    mark = {OK: "✅", WARN: "⚠️", FAIL: "❌"}
    return "\n".join([head, ""] + [f"{mark[c.status]} {c.id} {c.name} — {c.evidence}" for c in checks])


def summary_for_chat(checks: list[Check], now: datetime | None = None) -> str:
    """Short ops-chat version: the count line, then only WARN and FAIL lines (a clean day is one line)."""
    full = render(checks, now).splitlines()
    bad = [ln for ln in full[2:] if not ln.startswith("✅")]
    return "\n".join([full[0]] + ([""] + bad if bad else []))


def to_json(checks: list[Check]) -> str:
    return json.dumps([c.__dict__ for c in checks], ensure_ascii=False, indent=1)
