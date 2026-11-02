"""`team` — the dispatcher's command line.

  team init <slug> --name N --host H [--repo URL] [--channel telegram=@chan ...]   create products/<slug>/ from the template
  team cycle [--product SLUG|all] [--phase collect,review,...] [--dry-run] [--cycle ID]
  team status [SLUG]            task counts, spend, open questions, last cycles
  team tasks SLUG [--state s]   list tasks
  team task SLUG new|move|show  create / transition / print a task
  team ask SLUG --from R --to R --text Q [--task T]      · team answer SLUG Q-0001 --text A [--by owner]
  team obs SLUG --title T --text X [--class need_hypothesis]  add an observation by hand (owner's input)
  team stop SLUG|all / team resume SLUG|all   STOP file
  team digest SLUG              print today's digest text
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import config as C
from .cycle import PHASES, Cycle, daily_digest
from .runner import Runner, load_roles
from .store import TASK_STATES, Store, summary

log = logging.getLogger("team")
ROLES_DIR = Path(__file__).parent / "roles"


def _products(st: C.Settings, which: str | None) -> list[C.Product]:
    ps = C.load_products(st)
    if which and which != "all":
        ps = [p for p in ps if p.slug == which]
        if not ps:
            sys.exit(f"product {which!r} not found under {st.products_dir}")
    return ps


def cmd_init(st: C.Settings, a: argparse.Namespace) -> int:
    d = st.products_dir / a.slug
    if (d / "product.yaml").exists():
        sys.exit(f"{d} exists")
    d.mkdir(parents=True)
    channels = {}
    for ch in a.channel or []:
        k, _, v = ch.partition("=")
        channels[k] = v
    raw = {"slug": a.slug, "name": a.name or a.slug, "host": a.host, "site_base": f"https://{a.host}" if a.host else "",
           "repo": {"url": a.repo or "", "branch": "main", "public_dir": "public"}, "channels": channels,
           "targets": a.target or [], "budget_usd_per_day": a.budget, "paused": False}
    (d / "product.yaml").write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    shutil.copy(Path(__file__).parent / "templates" / "VISION.template.md", d / "VISION.md")
    (d / "decisions.md").write_text(f"# {raw['name']} — решения\n\n", encoding="utf-8")
    Store(d).close()
    print(f"created {d}: edit VISION.md and product.yaml, then `team cycle --product {a.slug} --dry-run`")
    return 0


def cmd_cycle(st: C.Settings, a: argparse.Namespace) -> int:
    roles = load_roles(ROLES_DIR)
    runner = Runner(st.claude, roles) if roles else None
    cycle_id = a.cycle or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")
    phases = [p.strip() for p in a.phase.split(",")] if a.phase else None
    if phases and any(p not in PHASES for p in phases):
        sys.exit(f"phases: {PHASES}")
    rc = 0
    for p in _products(st, a.product):
        (p.runs_dir / cycle_id).mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(p.runs_dir / cycle_id / "cycle.log", encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(fh)
        try:
            res = Cycle(st, p, runner, cycle_id, dry_run=a.dry_run).run(phases)
            print(f"{p.slug}: {res}")
        finally:
            logging.getLogger().removeHandler(fh)
    if a.product == "all" and not phases:
        from . import friction, retro
        try:
            friction.collect_all(st, _products(st, "all"))
            if retro.due(st) and not a.dry_run:
                print(f"retro: {retro.run_retro(st, runner)}")
        except Exception as exc:  # noqa: BLE001 — the framework's own loop must never break the products' one
            log.exception("friction/retro failed: %s", exc)
    return rc


def cmd_status(st: C.Settings, a: argparse.Namespace) -> int:
    for p in _products(st, a.product):
        s = Store(p.dir)
        sm = summary(s)
        print(f"== {p.slug} ({p.site_base}) stop={'yes' if p.stop_file.exists() else 'no'}")
        print(f"   tasks {sm['tasks']}: " + ", ".join(f"{k} {v}" for k, v in sorted(sm['by_state'].items())))
        print(f"   open questions {sm['open_questions']} · new observations {sm['new_observations']} · spent today ${sm['spent_today']:.2f} / cap ${p.daily_cap:.0f}")
        for c in s.cycles(5):
            print(f"   cycle {c['cycle_id']}: {c['phases']} ${c['spent_usd'] or 0:.2f} {c['finished'] or 'running'}")
        for q in s.questions(["open"]):
            print(f"   Q {q['id']} {q['from']}→{q['to']}: {q['question'][:120]}")
        s.close()
    return 0


def cmd_tasks(st: C.Settings, a: argparse.Namespace) -> int:
    p = _products(st, a.product)[0]
    s = Store(p.dir)
    for t in s.tasks([a.state] if a.state else None):
        print(f"{t['id']} {t['state']:13s} {t['priority']} {t.get('role', ''):9s} {t['title'][:80]}")
    s.close()
    return 0


def cmd_task(st: C.Settings, a: argparse.Namespace) -> int:
    p = _products(st, a.product)[0]
    s = Store(p.dir)
    if a.action == "new":
        t = s.create_task(title=a.title, type_=a.type, problem=a.problem or a.title, scope=a.scope or "", acceptance=a.acceptance or [],
                          goal=a.goal or "G1", created_by=a.by, priority=a.priority, role=a.role, state="ready" if a.ready else "proposed")
        print(t["id"])
    elif a.action == "move":
        t = s.transition(a.id, a.to, a.by, a.note or "", force=a.force)
        print(f"{t['id']} → {t['state']}")
    elif a.action == "show":
        t = s.task(a.id)
        print(yaml.safe_dump(t, sort_keys=False, allow_unicode=True, width=110) if t else "not found")
    s.close()
    return 0


def cmd_ask(st: C.Settings, a: argparse.Namespace) -> int:
    p = _products(st, a.product)[0]
    s = Store(p.dir)
    q = s.ask(from_=a.frm, to=a.to, question=a.text, task_id=a.task)
    print(q["id"])
    s.close()
    return 0


def cmd_answer(st: C.Settings, a: argparse.Namespace) -> int:
    p = _products(st, a.product)[0]
    s = Store(p.dir)
    q = s.answer(a.id, a.text, a.by)
    print(f"{q['id']} answered; unblocked: {q.get('blocks')}")
    s.close()
    return 0


def cmd_obs(st: C.Settings, a: argparse.Namespace) -> int:
    p = _products(st, a.product)[0]
    s = Store(p.dir)
    o = s.add_observation({"title": a.title, "classification": a.cls, "actual": a.text, "url": a.url or p.site_base, "source": a.by,
                           "expected": "", "evidence": []})
    print(o["id"])
    s.close()
    return 0


def cmd_stop(st: C.Settings, a: argparse.Namespace) -> int:
    targets = [st.root / "STOP"] if a.product == "all" else [p.stop_file for p in _products(st, a.product)]
    for f in targets:
        if a.cmd == "stop":
            f.write_text(f"stopped {datetime.now(timezone.utc).isoformat()} by {a.by}\n", encoding="utf-8")
            print(f"STOP written: {f}")
        elif f.exists():
            f.unlink()
            print(f"STOP removed: {f}")
    return 0


def cmd_digest(st: C.Settings, a: argparse.Namespace) -> int:
    p = _products(st, a.product)[0]
    s = Store(p.dir)
    print(daily_digest(p, s, datetime.now(timezone.utc).strftime("%Y-%m-%d")))
    s.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="team", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log-level", default=os.environ.get("TEAM_LOG_LEVEL", "INFO"))
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("init"); s.add_argument("slug"); s.add_argument("--name"); s.add_argument("--host", required=True)
    s.add_argument("--repo"); s.add_argument("--channel", action="append"); s.add_argument("--target", action="append")
    s.add_argument("--budget", type=float, default=None); s.set_defaults(fn=cmd_init)
    s = sub.add_parser("cycle"); s.add_argument("--product", default="all"); s.add_argument("--phase"); s.add_argument("--cycle")
    s.add_argument("--dry-run", action="store_true"); s.set_defaults(fn=cmd_cycle)
    s = sub.add_parser("status"); s.add_argument("product", nargs="?", default="all"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("tasks"); s.add_argument("product"); s.add_argument("--state", choices=TASK_STATES); s.set_defaults(fn=cmd_tasks)
    s = sub.add_parser("task"); s.add_argument("product"); s.add_argument("action", choices=["new", "move", "show"])
    s.add_argument("--id"); s.add_argument("--to", choices=TASK_STATES); s.add_argument("--note"); s.add_argument("--force", action="store_true")
    s.add_argument("--title"); s.add_argument("--type", default="feature"); s.add_argument("--problem"); s.add_argument("--scope")
    s.add_argument("--acceptance", action="append"); s.add_argument("--goal"); s.add_argument("--priority", default="P2")
    s.add_argument("--role", default="developer"); s.add_argument("--ready", action="store_true"); s.add_argument("--by", default="owner")
    s.set_defaults(fn=cmd_task)
    s = sub.add_parser("ask"); s.add_argument("product"); s.add_argument("--from", dest="frm", default="owner"); s.add_argument("--to", default="po")
    s.add_argument("--text", required=True); s.add_argument("--task"); s.set_defaults(fn=cmd_ask)
    s = sub.add_parser("answer"); s.add_argument("product"); s.add_argument("id"); s.add_argument("--text", required=True)
    s.add_argument("--by", default="owner"); s.set_defaults(fn=cmd_answer)
    s = sub.add_parser("obs"); s.add_argument("product"); s.add_argument("--title", required=True); s.add_argument("--text", required=True)
    s.add_argument("--class", dest="cls", default="need_hypothesis"); s.add_argument("--url"); s.add_argument("--by", default="owner"); s.set_defaults(fn=cmd_obs)
    for name in ("stop", "resume"):
        s = sub.add_parser(name); s.add_argument("product"); s.add_argument("--by", default="owner"); s.set_defaults(fn=cmd_stop)
    s = sub.add_parser("digest"); s.add_argument("product"); s.set_defaults(fn=cmd_digest)
    a = ap.parse_args(argv)
    logging.basicConfig(level=a.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    st = C.load()
    return int(a.fn(st, a) or 0)


if __name__ == "__main__":
    sys.exit(main())
