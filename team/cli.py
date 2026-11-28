"""`team` — the dispatcher's command line.

  team init <slug> --name N --host H [--repo URL] [--channel telegram=@chan ...]   create products/<slug>/ from the template
  team onboard <slug> --brief F [--host H --repo URL --profile game|site] | --answers [F] | --status [--dry-run]   brief → questions/accounts → VISION + backlog
  team deploy <slug> [--sha S | --rollback] [--force] [--show]   service deploy by hand (deploy.mode: service; the cycle does it after a merge)
  team cycle [--product SLUG|all] [--phase collect,review,...] [--dry-run] [--cycle ID]
  team cycle --due              only products with product.yaml `cycle.every_min` whose last finished cycle is that old (run-cycle-fast.sh)
                                one lock per product (team/locks.py): a product whose cycle is running is skipped; exit 75 = nothing started
  team status [SLUG]            cadence, task counts, spend, open questions, last cycles
  team tasks SLUG [--state s]   list tasks
  team task SLUG new|move|show  create / transition / print a task
  team ask SLUG --from R --to R --text Q [--task T]      · team answer SLUG Q-0001 --text A [--by owner]
  team obs SLUG --title T --text X [--class need_hypothesis]  add an observation by hand (owner's input)
                                obs, task new|move, ask and answer do NOT need the product lock: they write under the
                                store's own short lock (team/store.py StoreLock) and may be typed while the cycle runs;
                                `task move` of a task a lane is working on is refused (exit 75) or waits (--wait-s N)
  team stop SLUG|all / team resume SLUG|all   STOP file
  team digest SLUG              print today's digest text
  team report [--hours 12] [--send] [--no-llm] [--style bonus|plain]   12-hour role report (ledger + reporter run) → Telegram ops chat
  team autonomy-check [--send] [--json] [--no-tests]   can the contour run without the owner? (docs/autonomy-check.md)
"""
from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
from contextlib import ExitStack, nullcontext
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import config as C
from . import lanes
from .cycle import PHASES, Cycle, daily_digest
from .runner import Runner, load_roles
from .store import TASK_STATES, Store, summary

log = logging.getLogger("team")
ROLES_DIR = Path(__file__).parent / "roles"
# watchdog.sh reads the first 19 characters (the time). `%(lane)s` (team/lanes.py `install_log_lane`) is empty on the cycle's
# main thread and " <lane label>" inside a parallel lane: `INFO [12345 review-T-0042] team.cycle: …` — the pid tells the
# processes apart in data/cron.log, the lane label the interleaved lines of one cycle's parallel runs.
LOG_FORMAT = "%(asctime)s %(levelname)s [%(process)d%(lane)s] %(name)s: %(message)s"
CYCLE_LOG_FORMAT = "%(asctime)s %(levelname)s%(lane_tag)s %(name)s: %(message)s"   # data/runs/<slug>/<cycle>/cycle.log: `INFO [review-T-0042] team.cycle: …`
lanes.install_log_lane()                   # the formats above need `lane` / `lane_tag` on every record, whoever configures logging


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


def cmd_onboard(st: C.Settings, a: argparse.Namespace) -> int:
    """`team onboard <slug> --brief F` → questions.md for the owner; `--answers [F]` → VISION, GDD, backlog; `--status`.
    `--dry-run` uses the fixture instead of a model run (owner 20.11: onboarding of a game product, team/onboard.py)."""
    from . import onboard
    if a.status:
        print(onboard.status(st, a.slug))
        return 0
    roles = load_roles(ROLES_DIR)
    runner = Runner(st.claude, roles) if roles and not a.dry_run else None
    try:
        if a.brief:
            res = onboard.start(st, a.slug, Path(a.brief), name=a.name, host=a.host or "", repo=a.repo or "", budget=a.budget,
                                runner=runner, dry_run=a.dry_run, profile=a.profile)
            print(f"{a.slug}: {res['n_questions']} questions and {res['n_accounts']} accounts written ({res['source']}) → {res['questions_md']}\n"
                  f"owner question {res['question_id']}; fill the file, then: team onboard {a.slug} --answers {res['questions_md']}")
        elif a.answers is not None:
            res = onboard.apply_answers(st, a.slug, Path(a.answers) if a.answers else None, runner=runner, dry_run=a.dry_run)
            print(f"{a.slug}: onboarding done ({res['source']}): VISION {res['vision']}" + (f", GDD {res['game_design']}" if res["game_design"] else "")
                  + f"; tasks {', '.join(res['tasks'])}" + (f" (deferred: {', '.join(res['deferred'])})" if res["deferred"] else ""))
            print("\n" + res["summary_md"].strip() + f"\n\nnext: team cycle --product {a.slug} --dry-run")
        else:
            sys.exit("team onboard: one of --brief FILE, --answers [FILE], --status is required")
    except onboard.OnboardingError as exc:
        sys.exit(f"onboard: {exc}")
    return 0


def cmd_deploy(st: C.Settings, a: argparse.Namespace) -> int:
    """`team deploy <slug> [--sha S | --rollback] [--force]` — run the service deploy by hand (slice 5, owner 20.11):
    the same deploy-service.sh call the dispatcher makes after a merge; `--show` prints the command without running it."""
    from . import servicedeploy
    p = C.load_product(st, a.slug)
    cmd = servicedeploy.command(p, sha=a.sha or "", rollback=a.rollback, force=a.force)
    print(f"{p.slug}: {servicedeploy.describe(p)}")
    if a.show:
        print(" ".join(cmd))
        return 0
    if not servicedeploy.enabled(p):
        sys.exit(f"deploy: {p.slug} has deploy.mode={p.deploy_mode}; set deploy: {{mode: service, host: local}} in product.yaml once the host is registered (add-service.sh)")
    res = servicedeploy.run(p, sha=a.sha or "", rollback=a.rollback, force=a.force, log_path=p.work_dir / "deploy" / f"manual-{a.sha or ('rollback' if a.rollback else 'head')}.log")
    print(res.summary())
    if not (res.ok or res.skipped):
        print(res.output[-1500:])
        return 1
    return 0


def cmd_cycle(st: C.Settings, a: argparse.Namespace) -> int:
    """One cycle per selected product, each under its own lock (team/locks.py, friction F17): a product whose cycle is
    already running in another process is skipped, the others are not held up by it. Exit 75 when nothing was started
    because the dispatcher's code is being updated or another hourly pass is running."""
    from . import cadence, locks
    products = _products(st, a.product)
    phases = [p.strip() for p in a.phase.split(",")] if a.phase else None
    if phases and any(p not in PHASES for p in phases):
        sys.exit(f"phases: {PHASES}")
    if a.due:
        # Fast entry (run-cycle-fast.sh, cron every 5 min): only the products on a cadence that are due now. The look is
        # read-only and needs no lock. The framework's own hourly work (rollback check, friction, retro, critic) stays with `all`.
        n_all = len(products)
        products = cadence.due_products(st, products)
        log.log(logging.INFO if products else logging.DEBUG, "cycle --due: %d of %d product(s) due: %s", len(products), n_all,
                ", ".join(p.slug for p in products) or "none")
        if not products:
            return 0
    full = a.product == "all" and not phases and not a.due      # the hourly pass over every product
    again: list[str] = []                                       # products whose next cycle follows at once (`cycle.flow.chain`)
    with ExitStack() as held:
        if not held.enter_context(locks.cycles(st)):
            log.warning("cycle: nothing started — the dispatcher code is being updated (%s or %s is held)", locks.gate_path(st), locks.code_lock_path(st))
            return locks.EX_TEMPFAIL
        if full and not held.enter_context(locks.hourly_pass(st)):
            log.warning("cycle: nothing started — another hourly pass is running (%s)", locks.hourly_pass_path(st))
            return locks.EX_TEMPFAIL
        rc = _run_cycles(st, a, products, phases, full, again=again if a.due and not phases and not a.cycle else None)
    if again:
        _chain(st, a, again)
    return rc


def _chain_sleep(seconds: float) -> None:
    """The pause between two chained cycles (its own name: the tests put the operator's script here)."""
    time.sleep(seconds)


def _chain(st: C.Settings, a: argparse.Namespace, slugs: list[str]) -> None:
    """`cycle.flow.chain` (owner 25.11, team/flow.py): the next cycle of a product right after the previous one, in this
    process, while its cycle did work and startable work waits — instead of 5–10 minutes of nothing until cron looks
    again. Between two cycles EVERY lock is given up for `chain_pause_s`: an operator script waiting for the product lock
    (`flock data/locks/product-<slug>.lock …`) gets in there, a code update gets its turn at the gate. The chain ends
    when the checkout changed (this process holds the old code), an update is waiting, the product is no longer due
    (STOP, paused), nothing is left to chain, or after `chain_max` cycles — then cron and `cycle.every_min` decide."""
    from . import cadence, flow, locks
    head = flow.code_version(st.root)
    done = {s: 1 for s in slugs}                                # cycles of the product in this process, the first included
    while slugs:
        products = []
        for slug in slugs:
            try:
                products.append(C.load_product(st, slug))       # read again: the operator may have changed product.yaml meanwhile
            except Exception as exc:  # noqa: BLE001 — a product.yaml broken meanwhile ends that product's chain, not the others'
                log.warning("chain %s: ended — product.yaml cannot be loaded: %s", slug, exc)
        products = [p for p in products if done[p.slug] < p.flow.chain_max and p.flow.chain]
        ended = sorted(set(slugs) - {p.slug for p in products})
        if ended:
            log.info("chain: ended for %s (cycle.flow.chain_max reached, chain switched off, or product.yaml unreadable)", ", ".join(ended))
        if not products:
            return
        pause = max(p.flow.chain_pause_s for p in products)
        log.info("chain: %s — next cycle in %.0fs; no lock is held meanwhile", ", ".join(f"{p.slug} (cycle {done[p.slug] + 1} of up to {p.flow.chain_max})" for p in products), pause)
        _chain_sleep(pause)
        now_head = flow.code_version(st.root)
        if now_head != head or not head:
            log.info("chain: ended — the dispatcher checkout %s; the next cron entry starts on the new code",
                     f"changed {head[:8]} → {now_head[:8]}" if head and now_head else "cannot be read")
            return
        if locks.update_waiting(st):
            log.info("chain: ended — a code update is waiting at %s", locks.gate_path(st))
            return
        from . import drain as _drain
        products = [p for p in products if not _drain.requested(st, p.slug)]
        if not products:
            log.info("chain: ended — a drain request stands")
            return
        products = [p for p in products if cadence.check(st, p, chained=True).due]
        if not products:
            log.info("chain: ended — no product is due any more (STOP, paused)")
            return
        again: list[str] = []
        with locks.cycles(st) as ok:
            if not ok:
                log.info("chain: ended — the dispatcher code is being updated")
                return
            _run_cycles(st, a, products, None, False, again=again, chained=True)
        for p in products:
            done[p.slug] += 1
        slugs = again


def _run_cycles(st: C.Settings, a: argparse.Namespace, products: list[C.Product], phases: list[str] | None, full: bool,
                again: list[str] | None = None, chained: bool = False) -> int:
    """The body of `team cycle`, with the code lock held shared (and the hourly-pass lock for the full pass). `again`
    (only `--due`): filled with the products whose next cycle should follow at once (`Cycle.chain_reason`); `chained`:
    this call is such a follow-up — the pause of `cycle.every_min` is not asked for, and the product lock is waited for
    `chain_lock_wait_s` (an operator script may have taken it in the window between the two cycles)."""
    from . import cadence, drain, locks
    roles = load_roles(ROLES_DIR)
    runner = Runner(st.claude, roles) if roles else None
    cycle_id = a.cycle or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M")
    if full and not a.dry_run:
        from . import friction as _fr
        _fr.check_rollback(st, Path(__file__).resolve().parent.parent)
    ran: list[str] = []
    skipped: list[str] = []
    for p in products:
        with locks.product_cycle(st, p.slug, note=f"cycle={cycle_id} entry={'chain' if chained else 'due' if a.due else a.product}",
                                 wait_s=p.flow.chain_lock_wait_s if chained else 0.0) as free:
            why = None
            if not free:
                why = f"its cycle is already running (lock {locks.product_lock_path(st, p.slug)})"
            elif not a.dry_run and drain.requested(st, p.slug):
                why = f"a drain request stands ({drain.requested(st, p.slug)}): an update or an edit is waiting for this product"
            elif a.due and not cadence.check(st, p, chained=chained).due:
                why = "no longer due: another entry finished its cycle while this one was busy"     # the list was made before the locks
            elif not a.cycle and cadence.has_cycle(p, cycle_id):
                why = f"cycle {cycle_id} of this product was already started by another entry in the same minute"
            if why:
                log.info("cycle %s: skipped — %s", p.slug, why)
                print(f"{p.slug}: skipped ({why})")
                skipped.append(p.slug)
                continue
            (p.runs_dir / cycle_id).mkdir(parents=True, exist_ok=True)
            fh = logging.FileHandler(p.runs_dir / cycle_id / "cycle.log", encoding="utf-8")
            fh.setFormatter(logging.Formatter(CYCLE_LOG_FORMAT))
            logging.getLogger().addHandler(fh)
            cyc = None
            try:
                cyc = Cycle(st, p, runner, cycle_id, dry_run=a.dry_run)
                res = cyc.run(phases)
                print(f"{p.slug}: {res}")
                ran.append(p.slug)
                if again is not None and p.flow.chain:
                    go, reason = cyc.chain_reason(res)
                    log.info("cycle %s: next cycle %s — %s", p.slug, "right away" if go else "by the cadence", reason)
                    if go:
                        again.append(p.slug)
            finally:
                if cyc is not None and again is not None and p.flow.chain:
                    cyc.store.close()             # a chain makes many cycles in one process: no connection is left behind
                logging.getLogger().removeHandler(fh)
                fh.close()
    log.log(logging.INFO if skipped else logging.DEBUG, "cycle %s: ran %s; skipped %s", cycle_id, ", ".join(ran) or "none", ", ".join(skipped) or "none")
    if full:
        from . import friction, retro
        try:
            friction.collect_all(st, _products(st, "all"))
            if retro.due(st) and not a.dry_run:
                # the retro writes tasks into the framework product's store: not while that product's own cycle runs there
                fw = str((st.raw.get("framework") or {}).get("product") or "")
                with locks.product_cycle(st, fw, note=f"retro cycle={cycle_id}") if fw else nullcontext(True) as free:
                    if free:
                        print(f"retro: {retro.run_retro(st, runner)}")
                    else:
                        log.info("retro: put off to the next pass — the cycle of %s is running", fw)
        except Exception as exc:  # noqa: BLE001 — the framework's own loop must never break the products' one
            log.exception("friction/retro failed: %s", exc)
        if not a.dry_run:
            try:                                  # rework slice 8: the owner's weekly list of pending instruction proposals
                from . import billing, instructions
                if instructions.send_digest(st, billing.owner_notify()):
                    log.info("instructions: the owner's list of pending proposals was sent")
            except Exception as exc:  # noqa: BLE001
                log.exception("instructions digest failed: %s", exc)
        if not a.dry_run:
            try:
                from . import critic
                critic.write_responses(st, _products(st, "all"))     # what we did with the external critic's findings
            except Exception as exc:  # noqa: BLE001
                log.warning("critic responses failed: %s", exc)
    return 0


def _cadence_line(st: C.Settings, p: C.Product) -> str:
    """`hourly`, or `cycle every N min` with what `team cycle --due` would do right now."""
    if not p.cycle_every_min:
        return p.cadence
    from . import cadence
    from . import locks
    if locks.product_busy(st, p.slug):
        return f"{p.cadence} · cycle running now"
    d = cadence.check(st, p)
    return f"{p.cadence} · " + ("due now" if d.due else (f"next in {d.wait_min:.0f} min" if d.wait_min else d.reason))


def cmd_status(st: C.Settings, a: argparse.Namespace) -> int:
    for p in _products(st, a.product):
        s = Store(p.dir)
        sm = summary(s)
        print(f"== {p.slug} ({p.site_base}) stop={'yes' if p.stop_file.exists() else 'no'}")
        print(f"   cadence: {_cadence_line(st, p)}")
        print(f"   lanes: {p.parallel.describe()}")      # owner 21.11: what may run at once inside this product's cycle
        from . import flow as _flow
        _why = _flow.allowed(st, p.flow) if (p.flow.refill or p.flow.chain) else None
        print(f"   flow: {p.flow.describe()}" + (f" — off right now: {_why}" if _why else ""))   # owner 25.11: team/flow.py
        from . import standing
        for ln in standing.status_lines(p, s):        # owner 21.11: one line per standing direction — cadence, next due or the open task
            print(f"   {ln}")
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


MOVE_POLL_S = 2.0                          # `team task move --wait-s N`: how often the lane mark of the task is looked at


def _move_task(st: C.Settings, p: C.Product, s: Store, a: argparse.Namespace) -> int:
    """`team task move` beside a running cycle (owner 25.11): no product lock — the move is one short section under the
    store lock. The one case that must not pass: a lane of the running cycle works on this very task (it holds its own
    copy and writes it back — team/lanes.py invariant 2). Then: exit 75 with the lane's name, or wait with --wait-s."""
    from . import locks
    deadline = time.monotonic() + max(0.0, float(a.wait_s or 0.0))
    t0 = time.monotonic()
    while True:
        with s.lock:                           # look and move in one piece: the lane marks the task under the same lock
            lane = s.working_lane(a.id) if locks.product_busy(st, p.slug) else None      # no cycle running: a mark is stale
            if lane is None:
                t = s.transition(a.id, a.to, a.by, a.note or "", force=a.force)
                waited = time.monotonic() - t0
                log.info("task move %s %s → %s by %s%s", p.slug, a.id, t["state"], a.by, f" after waiting {waited:.0f}s for its lane" if waited >= MOVE_POLL_S else "")
                print(f"{t['id']} → {t['state']}")
                return 0
        if time.monotonic() >= deadline:
            from .locks import EX_TEMPFAIL
            log.warning("task move %s %s → %s: not done — lane %s of the running cycle works on it", p.slug, a.id, a.to, lane)
            print(f"{a.id}: not moved — lane {lane} of the running cycle is working on it; try again when it ends, or add --wait-s N", file=sys.stderr)
            return EX_TEMPFAIL
        time.sleep(min(MOVE_POLL_S, max(0.05, deadline - time.monotonic())))


def _prio_task(st: C.Settings, p: C.Product, s: Store, a: argparse.Namespace) -> int:
    """`team task <product> prio --id T-… --priority P1 [--by …] [--note …] [--wait-s N]`: change the priority of a task
    beside a running cycle. 27.11: the operator raised ten tasks by editing their files — a lane that holds its own copy
    of a task writes it back and the edit is gone, and a half-written file can be read in between. Here: one short section
    under the store lock, refused (exit 75) while a lane of the running cycle works on this very task, with a line in the
    task's history and an event."""
    from . import locks
    if not a.id or not a.priority:
        print("team task prio: --id and --priority (P0..P3) are needed", file=sys.stderr)
        return 2
    deadline = time.monotonic() + max(0.0, float(a.wait_s or 0.0))
    while True:
        with s.lock:
            t = s.task(a.id)
            if t is None:
                print(f"{a.id}: not found", file=sys.stderr)
                return 2
            lane = s.working_lane(a.id) if locks.product_busy(st, p.slug) else None
            if lane is None:
                old = t.get("priority")
                if old == a.priority:
                    print(f"{a.id} is {old} already")
                    return 0
                t["priority"] = a.priority
                t.setdefault("history", []).append({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "actor": a.by, "from": t.get("state"),
                                                    "to": t.get("state"), "note": f"priority {old} → {a.priority}" + (f": {a.note}" if a.note else "")})
                s.save_task(t)
                s.event("task.priority", a.id, a.by, {"from": old, "to": a.priority, "note": (a.note or "")[:200]})
                log.info("task prio %s %s: %s → %s by %s", p.slug, a.id, old, a.priority, a.by)
                print(f"{a.id}: {old} → {a.priority}")
                return 0
        if time.monotonic() >= deadline:
            print(f"{a.id}: not changed — lane {lane} of the running cycle is working on it; try again when it ends, or add --wait-s N", file=sys.stderr)
            return locks.EX_TEMPFAIL
        time.sleep(min(MOVE_POLL_S, max(0.05, deadline - time.monotonic())))


def cmd_task(st: C.Settings, a: argparse.Namespace) -> int:
    p = _products(st, a.product)[0]
    s = Store(p.dir)
    rc = 0
    if a.action == "new":
        from .cycle import EXEC_ROLES, canonical_role
        role = canonical_role(a.role)                  # `artist` is the designer's seat; an unknown role would never start
        if role not in EXEC_ROLES:
            print(f"team task new: no role `{a.role}`; roles that can take a task: {', '.join(EXEC_ROLES)} (artist = designer)")
            s.close()
            return 2
        a.role = role
        t = s.create_task(title=a.title, type_=a.type, problem=a.problem or a.title, scope=a.scope or "", acceptance=a.acceptance or [],
                          goal=a.goal or "G1", created_by=a.by, priority=a.priority or "P2", role=a.role, state="ready" if a.ready else "proposed")
        print(t["id"])
    elif a.action == "move":
        rc = _move_task(st, p, s, a)
    elif a.action == "prio":
        rc = _prio_task(st, p, s, a)
    elif a.action == "show":
        t = s.task(a.id)
        print(yaml.safe_dump(t, sort_keys=False, allow_unicode=True, width=110) if t else "not found")
        if t:                                          # owner 21.11: where the time of the task's runs went (team/runprofile.py)
            from . import runprofile
            profiles = runprofile.task_runs_md(p, a.id)
            if profiles:
                print(profiles)
    s.close()
    return rc


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
    q = s.answer(a.id, a.text, a.by, not_before=getattr(a, "not_before", None))
    print(f"{q['id']} answered; unblocked: {q.get('blocks')}" + (f" (not before {a.not_before})" if getattr(a, "not_before", None) else ""))
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


def cmd_window(st: C.Settings, a: argparse.Namespace) -> int:
    """`team window <product> --from ISO --to ISO [--vs-from ISO --vs-to ISO]`, or `--night YYYY-MM-DD` (the night that
    ended on that day, 21:00–07:00 UTC, against the night before): the same measures for two windows (team/windowreport.py)."""
    from datetime import timedelta
    from . import windowreport as W
    p = _products(st, a.product)[0]
    if a.night:
        day = W.parse_when(a.night + "T00:00:00")
        start, end = day - timedelta(hours=3), day + timedelta(hours=7)
        vs = (start - timedelta(days=1), end - timedelta(days=1))
    elif a.frm and a.to:
        start, end = W.parse_when(a.frm), W.parse_when(a.to)
        vs = (W.parse_when(a.vs_from), W.parse_when(a.vs_to)) if a.vs_from and a.vs_to else None
    else:
        print("team window: give --from and --to, or --night YYYY-MM-DD", file=sys.stderr)
        return 2
    seats = int(a.seats or (p.raw.get("execute_slots") or {}).get(W.IDLE_ROLE) or 1)
    name = lambda x, y: f"{x:%d.%m %H:%M}–{y:%d.%m %H:%M} UTC"      # noqa: E731
    now = W.measure(p.dir, st.data_dir, p.slug, start, end, seats)
    before = W.measure(p.dir, st.data_dir, p.slug, vs[0], vs[1], seats) if vs else None
    print(f"# {p.slug}: {name(start, end)}" + (f" против {name(*vs)}" if vs else "") + f" (мест {W.IDLE_ROLE}: {seats})\n")
    print(W.table(now, before, name(start, end), name(*vs) if vs else ""))
    return 0


def cmd_hold(st: C.Settings, a: argparse.Namespace) -> int:
    """`team hold <product> [--wait-s N] [--by X] [--why …] -- <command …>`: the product's running cycle drains, the
    command runs under the product's lock, the request is removed (team/drain.py)."""
    from . import drain
    p = _products(st, a.product)[0]
    argv = list(a.command or [])
    # argparse hands over everything after the product as the command — the options of `hold` written there included
    # (27.11 18:08: `team hold ai-battles --by … --why … -- bash x.sh` drained a cycle and then tried to run «--by»)
    own = {"--by": "by", "--why": "why", "--wait-s": "wait_s"}
    while argv and argv[0].startswith("--"):
        head = argv.pop(0)
        if head == "--":
            break
        name, eq, val = head.partition("=")
        if name not in own:
            print(f"team hold: unknown option {name} before the command (the command starts after `--`)")
            return 2
        if not eq:
            if not argv:
                print(f"team hold: {name} needs a value")
                return 2
            val = argv.pop(0)
        setattr(a, own[name], val)
    if not argv:
        print("team hold: no command after `--`")
        return 2
    return drain.hold(st, p.slug, argv, a.by, a.why or "", float(a.wait_s))


def cmd_drain(st: C.Settings, a: argparse.Namespace) -> int:
    """`team drain <product|all> [--clear]`: place or remove the request by hand (`all` = data/locks/update.pending)."""
    from . import drain
    f = drain.global_file(st) if a.product == "all" else drain.product_file(st, _products(st, a.product)[0].slug)
    if a.clear:
        print(f"drain request {'removed' if drain.clear(f) else 'was not there'}: {f}")
    else:
        print(f"drain requested: {drain.place(f, a.by, a.why or '')}")
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


def cmd_lesson(st: C.Settings, a: argparse.Namespace) -> int:
    from . import editorial
    p = _products(st, a.product)[0]
    n = editorial.add_lessons(p, [a.text], a.by)
    print(f"{p.slug}: {n} lesson(s) added → {editorial.lessons_path(p)}")
    return 0


def cmd_export(st: C.Settings, a: argparse.Namespace) -> int:
    from .export import export_product
    out = Path(a.out) if a.out else st.data_dir / "public"
    n = 0
    for p in _products(st, a.product):
        n += len(export_product(p, out, days=a.days))
    print(f"exported {n} day files to {out}")
    return 0


def cmd_gsc(st: C.Settings, a: argparse.Namespace) -> int:
    from . import gsc
    return gsc.run_all(st, [p for p in _products(st, a.product) if p.host or p.site_base])


def cmd_friction(st: C.Settings, a: argparse.Namespace) -> int:
    from . import friction
    friction.collect_all(st, _products(st, "all"))
    reg = friction.Register(friction.register_dir(st))
    print(reg.register_md(a.days))
    if a.out:
        print(f"written {reg.export_public(Path(a.out), a.days)}")
    return 0


def cmd_retro(st: C.Settings, a: argparse.Namespace) -> int:
    from . import friction, retro
    friction.collect_all(st, _products(st, "all"))
    roles = load_roles(ROLES_DIR)
    res = retro.run_retro(st, Runner(st.claude, roles) if roles and not a.dry_run else None, dry_run=a.dry_run)
    print(res or "retro: nothing done (dry-run, empty register, or run failed)")
    return 0


def cmd_report(st: C.Settings, a: argparse.Namespace) -> int:
    """12-hour role report: ledger from the stores → one reporter run → Telegram ops chat (owner 03.11)."""
    from . import report
    roles = load_roles(ROLES_DIR)
    runner = Runner(st.claude, roles) if roles and not a.no_llm else None
    res = report.run_report(st, _products(st, a.product), a.hours, runner=runner, send=a.send, dry_run=a.dry_run,
                            out=Path(a.out) if a.out else None, style=a.style)
    print(res["text"])
    print(f"\n[report {res['stamp']}: narrative={res['narrative']} sent={res['sent']} file={res['path']}]", file=sys.stderr)
    return 0 if (not a.send or all(x.get("ok") for x in res["sent"])) else 1


def cmd_instructions(st: C.Settings, a: argparse.Namespace) -> int:
    """Proposals to change the roles' instructions (rework slice 8): list the pending ones, send the owner's list now
    (`--send`), or record the owner's word (`--done P-0003 --as accepted|rejected`)."""
    from . import billing, instructions
    if a.done:
        try:
            m = instructions.decide(st, a.done, a.as_ or "")
        except (KeyError, ValueError) as exc:
            print(f"team instructions: {a.done}: {exc if isinstance(exc, ValueError) else 'no such proposal'}")
            return 1
        print(f"{m['id']} {m['status']}: {m.get('file')}")
        return 0
    if a.send:
        text = instructions.send_digest(st, billing.owner_notify(), force=True)
        print(text or "nothing pending: nothing sent")
        return 0
    cfg = instructions.settings(st)
    items = instructions.pending(st)
    print(f"owner_only: {cfg['owner_only']} · paths: {', '.join(cfg['paths'])} · list every {cfg['digest_every_days']} days · pending: {len(items)}")
    print(instructions.digest(st) or "(no pending proposals)")
    return 0


def cmd_cost(st: C.Settings, a: argparse.Namespace) -> int:
    """Spend per accepted task and prompt-cache share for a window, from the run folders and the backlog (read-only)."""
    import json
    from . import cost
    r = cost.collect(st, _products(st, a.product), days=a.days)
    text = json.dumps(r, ensure_ascii=False, indent=2) if a.json else cost.render(r)
    if a.out:
        Path(a.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


def cmd_autonomy(st: C.Settings, a: argparse.Namespace) -> int:
    """Autonomy check (owner 04.11): every component that must live a week without the owner, OK / WARN / FAIL with
    evidence; protocol in docs/autonomy-check.md. --send puts the short version into the ops chat."""
    from . import autonomy
    checks = autonomy.run_all(st, tests=not a.no_tests)
    print(autonomy.to_json(checks) if a.json else autonomy.render(checks))
    out = st.data_dir / "autonomy"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M")
    (out / f"{stamp}.md").write_text(autonomy.render(checks) + "\n", encoding="utf-8")
    if a.send:
        from .report import send_telegram
        send_telegram(autonomy.summary_for_chat(checks), a.dry_run)
    return 1 if any(c.status == autonomy.FAIL for c in checks) else 0


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
    s = sub.add_parser("onboard"); s.add_argument("slug"); s.add_argument("--brief"); s.add_argument("--answers", nargs="?", const="")
    s.add_argument("--status", action="store_true"); s.add_argument("--name"); s.add_argument("--host"); s.add_argument("--repo")
    s.add_argument("--budget", type=float, default=None); s.add_argument("--dry-run", action="store_true")
    s.add_argument("--profile", choices=["game", "site"], default=None, help="new product: game (default) or site"); s.set_defaults(fn=cmd_onboard)
    s = sub.add_parser("deploy"); s.add_argument("slug"); s.add_argument("--sha"); s.add_argument("--rollback", action="store_true")
    s.add_argument("--force", action="store_true"); s.add_argument("--show", action="store_true"); s.set_defaults(fn=cmd_deploy)
    s = sub.add_parser("cycle"); s.add_argument("--product", default="all"); s.add_argument("--phase"); s.add_argument("--cycle")
    s.add_argument("--dry-run", action="store_true"); s.add_argument("--due", action="store_true"); s.set_defaults(fn=cmd_cycle)
    s = sub.add_parser("status"); s.add_argument("product", nargs="?", default="all"); s.set_defaults(fn=cmd_status)
    s = sub.add_parser("tasks"); s.add_argument("product"); s.add_argument("--state", choices=TASK_STATES); s.set_defaults(fn=cmd_tasks)
    s = sub.add_parser("task"); s.add_argument("product"); s.add_argument("action", choices=["new", "move", "show", "prio"])
    s.add_argument("--id"); s.add_argument("--to", choices=TASK_STATES); s.add_argument("--note"); s.add_argument("--force", action="store_true")
    s.add_argument("--title"); s.add_argument("--type", default="feature"); s.add_argument("--problem"); s.add_argument("--scope")
    s.add_argument("--acceptance", action="append"); s.add_argument("--goal"); s.add_argument("--priority", choices=["P0", "P1", "P2", "P3"])
    s.add_argument("--role", default="developer"); s.add_argument("--ready", action="store_true"); s.add_argument("--by", default="owner")
    s.add_argument("--wait-s", dest="wait_s", type=float, default=0.0)      # move: wait this long for a lane of the running cycle to let go of the task
    s.set_defaults(fn=cmd_task)
    s = sub.add_parser("ask"); s.add_argument("product"); s.add_argument("--from", dest="frm", default="owner"); s.add_argument("--to", default="po")
    s.add_argument("--text", required=True); s.add_argument("--task"); s.set_defaults(fn=cmd_ask)
    s = sub.add_parser("answer"); s.add_argument("product"); s.add_argument("id"); s.add_argument("--text", required=True)
    s.add_argument("--by", default="owner"); s.add_argument("--not-before", dest="not_before"); s.set_defaults(fn=cmd_answer)
    s = sub.add_parser("obs"); s.add_argument("product"); s.add_argument("--title", required=True); s.add_argument("--text", required=True)
    s.add_argument("--class", dest="cls", default="need_hypothesis"); s.add_argument("--url"); s.add_argument("--by", default="owner"); s.set_defaults(fn=cmd_obs)
    s = sub.add_parser("window"); s.add_argument("product"); s.add_argument("--from", dest="frm"); s.add_argument("--to")
    s.add_argument("--vs-from", dest="vs_from"); s.add_argument("--vs-to", dest="vs_to"); s.add_argument("--night"); s.add_argument("--seats", type=int)
    s.set_defaults(fn=cmd_window)
    s = sub.add_parser("hold"); s.add_argument("product"); s.add_argument("--wait-s", type=float, default=2700.0)
    s.add_argument("--by", default="operator"); s.add_argument("--why", default=""); s.add_argument("command", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_hold)
    s = sub.add_parser("drain"); s.add_argument("product"); s.add_argument("--clear", action="store_true")
    s.add_argument("--by", default="operator"); s.add_argument("--why", default=""); s.set_defaults(fn=cmd_drain)
    for name in ("stop", "resume"):
        s = sub.add_parser(name); s.add_argument("product"); s.add_argument("--by", default="owner"); s.set_defaults(fn=cmd_stop)
    s = sub.add_parser("digest"); s.add_argument("product"); s.set_defaults(fn=cmd_digest)
    s = sub.add_parser("export-public"); s.add_argument("--product", default="all"); s.add_argument("--out"); s.add_argument("--days", type=int, default=14)
    s.set_defaults(fn=cmd_export)
    s = sub.add_parser("lesson"); s.add_argument("product"); s.add_argument("--text", required=True); s.add_argument("--by", default="owner"); s.set_defaults(fn=cmd_lesson)
    s = sub.add_parser("gsc"); s.add_argument("--product", default="all"); s.set_defaults(fn=cmd_gsc)
    s = sub.add_parser("friction"); s.add_argument("--days", type=int, default=7); s.add_argument("--out"); s.set_defaults(fn=cmd_friction)
    s = sub.add_parser("cost"); s.add_argument("--product", default="all"); s.add_argument("--days", type=float, default=7.0)
    s.add_argument("--json", action="store_true"); s.add_argument("--out"); s.set_defaults(fn=cmd_cost)
    s = sub.add_parser("instructions"); s.add_argument("--send", action="store_true"); s.add_argument("--done"); s.add_argument("--as", dest="as_", choices=["accepted", "rejected"])
    s.set_defaults(fn=cmd_instructions)
    s = sub.add_parser("retro"); s.add_argument("--dry-run", action="store_true"); s.set_defaults(fn=cmd_retro)
    s = sub.add_parser("report"); s.add_argument("--product", default="all"); s.add_argument("--hours", type=float, default=12.0)
    s.add_argument("--send", action="store_true"); s.add_argument("--no-llm", action="store_true"); s.add_argument("--dry-run", action="store_true")
    s.add_argument("--out"); s.add_argument("--style", choices=["bonus", "plain"], default=os.environ.get("REPORT_STYLE", "bonus"))
    s.set_defaults(fn=cmd_report)
    s = sub.add_parser("autonomy-check"); s.add_argument("--send", action="store_true"); s.add_argument("--json", action="store_true")
    s.add_argument("--no-tests", action="store_true"); s.add_argument("--dry-run", action="store_true"); s.set_defaults(fn=cmd_autonomy)
    a = ap.parse_args(argv)
    # the pid tells the lines of parallel cycles apart in data/cron.log (two cron entries append to it at once)
    logging.basicConfig(level=a.log_level.upper(), format=LOG_FORMAT, stream=sys.stderr)
    st = C.load()
    return int(a.fn(st, a) or 0)


if __name__ == "__main__":
    sys.exit(main())
