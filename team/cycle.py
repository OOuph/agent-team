"""One dispatcher cycle for one product: collect → review → triage (PO) → execute → measure → digest.

Each phase is idempotent and checkpointed through the store (task states, events); a cycle that dies mid-way is
continued by the next one. Every model run is capped (per run, per product per day) and booked; a run without a
reported cost is booked at the reserve. A STOP file (product or root) ends the cycle before any model run.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from . import geo, gitops, prompts, publish
from .config import Product, Settings
from .runner import Runner, RunResult, front_matter
from .store import Store, parse_ts, utcnow

log = logging.getLogger("team.cycle")

PHASES = ("collect", "review", "triage", "execute", "measure", "geo", "digest")
PRIO = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
SCHEMAS = Path(__file__).parent / "schemas"


def schema(name: str) -> dict[str, Any]:
    return json.loads((SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Cycle:
    def __init__(self, st: Settings, product: Product, runner: Runner | None, cycle_id: str, *, dry_run: bool = False):
        self.st, self.p, self.runner, self.cycle_id, self.dry_run = st, product, runner, cycle_id, dry_run
        self.store = Store(product.dir)
        self.run_dir = product.runs_dir / cycle_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.phases_done: list[str] = []
        self.n_runs = 0

    # ------------------------------------------------------------ guards

    def stopped(self) -> str | None:
        for f in (self.p.stop_file, self.st.root / "STOP"):
            if f.exists():
                return str(f)
        if self.p.raw.get("paused"):
            return "product.yaml: paused"
        return None

    def budget_ok(self, cap_run: float) -> bool:
        spent, cap = self.store.spent_today(), self.p.daily_cap
        if spent + cap_run > cap + 1e-9:
            self.store.event("budget.stop", self.p.slug, "dispatcher", {"spent": round(spent, 3), "cap": cap, "run_cap": cap_run})
            log.warning("%s: budget stop — spent $%.2f + $%.2f > cap $%.2f", self.p.slug, spent, cap_run, cap)
            return False
        if spent > cap * float(self.st.budget.get("warn_share", 0.8)):
            log.warning("%s: spend $%.2f above %.0f%% of cap $%.2f", self.p.slug, spent, 100 * float(self.st.budget.get("warn_share", 0.8)), cap)
        return True

    # ------------------------------------------------------------ one role run

    def role_run(self, role: str, prompt: str, schema_name: str | None, *, cwd: Path, label: str, cap: float | None = None,
                 allowed_tools: list[str] | None = None, env_extra: dict[str, str] | None = None,
                 max_turns: int | None = None) -> RunResult | None:
        if self.runner is None:
            log.error("no runner: %s skipped", label)
            return None
        cap = float(cap or self.st.budget["max_usd_per_run"])
        remaining = self.p.daily_cap - self.store.spent_today()
        cap = min(cap, remaining)                                   # a run may use what is left of the day
        if cap < float(self.st.budget.get("min_usd_per_run", 0.3)) or not self.budget_ok(cap):
            if cap < float(self.st.budget.get("min_usd_per_run", 0.3)):
                self.store.event("budget.stop", self.p.slug, "dispatcher", {"remaining": round(remaining, 3), "cap": self.p.daily_cap})
            return None
        if self.dry_run:
            log.info("[dry-run] would run %s (%s): prompt %d chars", role, label, len(prompt))
            return None
        self.n_runs += 1
        run_id = f"{self.cycle_id}-{label}"
        rd = self.run_dir / label
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "prompt.md").write_text(prompt, encoding="utf-8")
        env = {"TEAM_USER_AGENT": f"agent-team/{run_id} ({role}; {self.p.slug})", "TEAM_ALLOWED_HOSTS": self._allowed_hosts(role),
               "TEAM_TRACE_FILE": str(rd / "fetch.jsonl"), "TEAM_PRODUCT": self.p.slug, "TEAM_RUN_ID": run_id,
               "PATH": f"{self._bin_dir()}{os.pathsep}{os.environ.get('PATH', '')}", **(env_extra or {})}
        model = (self.p.raw.get("models") or {}).get(role)      # product.yaml may pick a cheaper model per role
        rr = self.runner.run(role, prompt, schema(schema_name) if schema_name else None, max_usd=cap, cwd=cwd, model=str(model) if model else None,
                             allowed_tools=allowed_tools, env_extra=env, run_dir=rd, run_id=run_id, max_turns=max_turns)
        cost = rr.cost_usd if rr.cost_usd is not None else float(self.st.budget["unknown_cost_reserve_usd"])
        self.store.book_spend(run_id, role, cost, rr.cost_usd is not None)
        rec = {"run_id": run_id, "role": role, "ok": rr.ok, "error": rr.error, "cost_usd": cost, "cost_reported": rr.cost_usd is not None,
               "num_turns": rr.num_turns, "duration_s": round(rr.duration_s, 1), "session_id": rr.session_id,
               "denials": rr.denials, "structured": rr.structured, "finished": utcnow()}
        (rd / "run.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        self.store.event("run", run_id, role, {"ok": rr.ok, "cost": round(cost, 3), "turns": rr.num_turns, "error": (rr.error or "")[:200]})
        return rr

    def _allowed_hosts(self, role: str | None = None) -> str:
        """Hosts team-fetch may open for a run: the product site, product extra_hosts, the publishing channels, and the
        per-role list from team.yaml `fetch.extra_hosts_by_role` (03.11: the reviewer reads repo-only deliverables on
        raw.githubusercontent.com; without this every such review was denied_permission and stayed inconclusive)."""
        hosts = [self.p.host] if self.p.host else []
        hosts += [str(h) for h in self.p.raw.get("extra_hosts") or []]
        hosts += ["bsky.app", "t.me", "dev.to"]
        by_role = (self.st.raw.get("fetch") or {}).get("extra_hosts_by_role") or {}
        if role:
            hosts += [str(h) for h in by_role.get(role) or []]
        return ",".join(dict.fromkeys(h for h in hosts if h))

    def _bin_dir(self) -> Path:
        return self.st.root / "bin"

    # ------------------------------------------------------------ phases

    def run(self, phases: list[str] | None = None) -> dict[str, Any]:
        self.store.cycle_start(self.cycle_id)
        reason = self.stopped()
        if reason:
            log.warning("%s: stopped (%s)", self.p.slug, reason)
            self.store.event("cycle.stopped", self.cycle_id, "dispatcher", reason)
            return {"stopped": reason}
        t0 = time.time()
        spent0 = self.store.spent_today()
        for ph in phases or PHASES:
            try:
                getattr(self, f"phase_{ph}")()
                self.phases_done.append(ph)
            except Exception as exc:  # noqa: BLE001 — one phase failing must not kill the cycle
                log.exception("%s: phase %s failed: %s", self.p.slug, ph, exc)
                self.store.event("phase.error", ph, "dispatcher", str(exc)[:500])
            if self.stopped():
                break
        spent = self.store.spent_today() - spent0
        self.store.cycle_finish(self.cycle_id, self.phases_done, round(spent, 4))
        log.info("%s cycle %s: phases %s, runs %d, spent $%.2f, %.0fs", self.p.slug, self.cycle_id, self.phases_done, self.n_runs, spent, time.time() - t0)
        return {"phases": self.phases_done, "runs": self.n_runs, "spent_usd": round(spent, 4)}

    # collect: stale claims, metrics snapshot, personas ------------------------------------------------

    def phase_collect(self) -> None:
        for c in self.store.stale_claims():
            t = self.store.task(c["task_id"])
            if t and t.get("state") in ("claimed", "in_progress"):
                self.store.transition(t["id"], "ready", "dispatcher", f"lease of {c['run_id']} expired", force=True)
        self.fetch_metrics()
        self.collect_feedback()
        self.support_replies()
        self.seo_check()
        n = len(self.store.cycles())
        every = int(self.st.cycle.get("persona_every_n_cycles", 6))
        if every and n % every == 1:
            self.run_personas()

    def collect_feedback(self) -> int:
        """External feedback -> observation cards: GitHub issues of the site repository, replies and mentions on Bluesky.
        Real people's words weigh above anything synthetic; each item becomes one card, deduped by its id."""
        if self.dry_run:
            return 0
        from . import feedback
        n = 0
        try:
            for card in feedback.collect(self.p):
                self.store.add_observation(card)
                n += 1
        except Exception as exc:  # noqa: BLE001 - a feedback source failing must not stop the cycle
            log.warning("%s: feedback collection failed: %s", self.p.slug, exc)
            self.store.event("feedback.error", self.p.slug, "dispatcher", str(exc)[:300])
        if n:
            self.store.event("feedback.collected", self.p.slug, "dispatcher", {"items": n})
        return n

    def support_replies(self) -> int:
        """People who wrote to the product get an answer within VISION (role support); outside VISION → question to the PO."""
        if self.dry_run or self.runner is None:
            return 0
        from . import support
        cfg = dict(self.st.raw.get("support") or {})
        if not cfg.get("enabled", True):
            return 0
        cap_day = int(cfg.get("max_replies_per_day", support.DEFAULT_MAX_PER_DAY))
        todo = support.pending(self.p, self.store)[: int(cfg.get("max_per_cycle", support.DEFAULT_MAX_PER_CYCLE))]
        n = 0
        for o in todo:
            if support.replies_today(self.store) >= cap_day:
                log.info("%s: support daily cap %d reached", self.p.slug, cap_day)
                break
            try:
                channel, how = support.channel_for(o)
            except ValueError as exc:
                self.store.event("support.skip", str(o.get("fingerprint")), "dispatcher", str(exc))
                continue
            work = self.p.work_dir / self.cycle_id / f"support-{o['id']}"
            work.mkdir(parents=True, exist_ok=True)
            rr = self.role_run("support", support.support_prompt(self.p, o, self.p.vision()), "support-reply", cwd=work,
                               label=f"support-{o['id']}", cap=0.5, max_turns=12)
            if rr is None or not (rr.ok and rr.structured):
                continue
            res = rr.structured
            if res.get("action") == "escalate" and res.get("question"):
                self.store.ask(from_="support", to="po", question=f"{res['question']} (from {o.get('author')}: {o.get('url')})")
            if res.get("action") == "skip" or not (res.get("text") or "").strip():
                self.store.event("support.skip", str(o.get("fingerprint")), "support", str(res.get("reason"))[:200])
                continue
            disc = self.st.publish.get("disclosure", "")
            disc_s = (disc.get(channel) or disc.get("default")) if isinstance(disc, dict) else str(disc)
            r = publish.post(channel, str(res["text"]), disclosure=str(disc_s or ""), dry_run=bool(self.st.publish.get("dry_run")), **how)
            if r.ok:
                self.store.event("support.reply", str(o.get("fingerprint")), "support", {"channel": channel, "url": r.url, "action": res.get("action")})
                self.store.record_post(channel, str(r.post_id or ""), str(r.url or ""), None)
                n += 1
            else:
                self.store.event("support.error", str(o.get("fingerprint")), "support", str(r.error)[:300])
        return n

    def seo_check(self) -> None:
        """Programmatic SEO/GEO page checks every `seo_every_h` hours: defects become observation cards, the full table
        goes to metrics/seo-<date>.json for the analyst."""
        if self.dry_run or not self.p.site_base:
            return
        from . import seocheck
        every = int(self.st.cycle.get("seo_every_h", 12))
        last = sorted((self.p.dir / "metrics").glob("seo-*.json"))
        if last:
            prev = parse_ts(json.loads(last[-1].read_text(encoding="utf-8")).get("generated") or "")
            if prev and _now() - prev < timedelta(hours=every):
                return
        langs = [str(x) for x in (self.p.raw.get("langs") or [])] or None
        try:
            rep = seocheck.report(self.p.site_base, seocheck.default_fetch, max_pages=8, langs=langs)
        except Exception as exc:  # noqa: BLE001
            log.warning("%s: seocheck failed: %s", self.p.slug, exc)
            return
        rep["generated"] = utcnow()
        (self.p.dir / "metrics" / f"seo-{_now().strftime('%Y-%m-%d')}.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        for card in rep.get("cards") or []:
            self.store.add_observation({**card, "run_id": f"{self.cycle_id}-seo"})
        self.store.event("seo.checked", self.p.slug, "dispatcher", {"passed": rep.get("passed"), "failed": rep.get("failed")})

    def fetch_metrics(self) -> Path | None:
        if not self.p.site_base or self.dry_run:
            return None
        url = self.p.site_base.rstrip("/") + "/_team/metrics.json"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"agent-team/{self.cycle_id} (dispatcher; {self.p.slug})"})
            with urllib.request.urlopen(req, timeout=20) as r:
                data = json.loads(r.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            log.info("%s: metrics not available at %s: %s", self.p.slug, url, exc)
            return None
        day = _now().strftime("%Y-%m-%d")
        out = self.p.dir / "metrics" / f"{day}.json"
        txt = json.dumps(data, ensure_ascii=False, indent=1)
        if not out.exists() or out.read_text(encoding="utf-8") != txt:
            out.write_text(txt, encoding="utf-8")
            md = self.p.dir / "metrics" / "latest.md"
            md.write_text(metrics_md(data), encoding="utf-8")
        return out

    def run_personas(self) -> None:
        sdir, pdir = self.p.dir / "scenarios", self.p.dir / "personas"
        if not sdir.exists():
            return
        for sp in sorted(sdir.glob("*.md"))[:6]:
            smeta, sbody = front_matter(sp.read_text(encoding="utf-8"))
            pp = pdir / f"{smeta.get('persona', '')}.md"
            if not smeta.get("id") or not pp.exists():
                continue
            pmeta, pbody = front_matter(pp.read_text(encoding="utf-8"))
            shown = {k: v for k, v in smeta.items() if k not in ("success", "expected_gaps", "title")}
            prompt = prompts.persona_prompt(pmeta, pbody.strip(), shown, sbody.strip())
            work = self.p.work_dir / self.cycle_id / f"persona-{smeta['id']}"
            rr = self.role_run("persona", prompt, "persona-result", cwd=work, label=f"persona-{smeta['id']}", cap=0.6, max_turns=25)
            if rr and rr.ok and rr.structured:
                for o in rr.structured.get("observations") or []:
                    self.store.add_observation({**o, "source": f"persona:{smeta.get('persona')}", "scenario": smeta["id"],
                                                "run_id": f"{self.cycle_id}-persona-{smeta['id']}",
                                                "fingerprint": fingerprint(smeta["id"], o.get("classification", ""), o.get("url", ""), o.get("title", "")),
                                                "scenario_success": str(smeta.get("success", ""))[:400]})

    # review: released tasks → reviewer --------------------------------------------------------------

    def phase_review(self) -> None:
        if self.p.repo_mode == "pr":
            self.check_pull_requests()
        after = timedelta(minutes=int(self.st.cycle.get("review_after_min", 6)))
        for t in self.store.tasks(["released"]):
            rel = parse_ts(t.get("released_at"))
            if rel and _now() - rel < after:
                continue
            self.review_task(t)

    def review_task(self, t: dict[str, Any]) -> None:
        result = (t.get("results") or [None])[-1]
        prompt = prompts.reviewer_prompt(self.p, t, result, t.get("released_sha"))
        work = self.p.work_dir / self.cycle_id / f"review-{t['id']}"
        rr = self.role_run("reviewer", prompt, "review-verdict", cwd=work, label=f"review-{t['id']}", cap=1.0, max_turns=30)
        if rr is None:
            return
        if not (rr.ok and rr.structured):
            t["review_inconclusive"] = int(t.get("review_inconclusive") or 0) + 1
            self.store.save_task(t)
            if t["review_inconclusive"] >= 3:
                self.store.transition(t["id"], "blocked", "reviewer", f"review failed to run 3×: {rr.error}", force=True)
            return
        v = rr.structured
        self.store.add_review(t["id"], v, f"{self.cycle_id}-review-{t['id']}")
        for o in v.get("observations") or []:
            self.store.add_observation({**o, "source": "reviewer", "task": t["id"], "run_id": f"{self.cycle_id}-review-{t['id']}",
                                        "fingerprint": fingerprint("review", o.get("classification", ""), o.get("url", ""), o.get("title", ""))})
        verdict = v.get("verdict")
        if verdict == "pass":
            self.store.transition(t["id"], "accepted", "reviewer", v.get("summary", "")[:300], accepted_at=utcnow())
        elif verdict == "fail":
            fails = int(t.get("review_fails") or 0) + 1
            if fails >= int(self.st.cycle.get("max_review_fails", 2)):
                self.store.transition(t["id"], "blocked", "reviewer", f"failed review {fails}×: {v.get('first_failure', '')[:200]}",
                                      review_fails=fails, blocked_by="review")
                self.store.ask(from_="reviewer", to="po", question=f"Task {t['id']} failed review {fails} times: {v.get('first_failure', '')}. "
                               f"Narrow the task, drop it, or re-specify?", task_id=None)
            else:
                self.store.transition(t["id"], "in_progress", "reviewer", f"failed: {v.get('first_failure', '')[:200]}",
                                      review_fails=fails, last_review=v)
        else:
            t["review_inconclusive"] = int(t.get("review_inconclusive") or 0) + 1
            self.store.save_task(t)
            if t["review_inconclusive"] >= 3:
                self.store.transition(t["id"], "accepted", "reviewer", "inconclusive 3×: accepted without external proof", accepted_at=utcnow())

    # triage: PO ------------------------------------------------------------------------------------

    def phase_triage(self) -> None:
        new_obs = self.store.observations("new")
        qs = [q for q in self.store.questions(["open"]) if q.get("to") == "po"]
        active = self.store.tasks(["ready", "claimed", "in_progress", "review", "released"])
        idle = not active
        if not new_obs and not qs and not idle:
            log.info("%s: triage skipped (nothing new, team busy)", self.p.slug)
            return
        latest = self.p.dir / "metrics" / "latest.md"
        prompt = prompts.po_prompt(self.p, self.store, cycle_id=self.cycle_id, observations=new_obs[:25], questions=qs,
                                   metrics_md=latest.read_text(encoding="utf-8") if latest.exists() else "",
                                   spent_today=self.store.spent_today(), cap=self.p.daily_cap,
                                   max_ready=int(self.st.cycle.get("max_ready", 3)), idle=idle)
        work = self.p.work_dir / self.cycle_id / "po"
        rr = self.role_run("po", prompt, "po-decision", cwd=work, label="po", cap=2.0, allowed_tools=[], max_turns=8)
        if rr is None or not (rr.ok and rr.structured):
            return
        self.apply_po(rr.structured, new_obs)

    def apply_po(self, res: dict[str, Any], new_obs: list[dict[str, Any]]) -> None:
        ids = {o["id"] for o in new_obs}
        for d in res.get("decisions") or []:
            oid = str(d.get("observation_id"))
            if oid in ids:
                self.store.update_observation(oid, status="triaged", decision={**d, "cycle": self.cycle_id})
        for a in res.get("answers") or []:
            try:
                self.store.answer(str(a["question_id"]), str(a["answer"]), "po", not_before=a.get("not_before"))
            except FileNotFoundError:
                pass
        for rp in res.get("reprioritize") or []:
            t = self.store.task(str(rp.get("task_id")))
            if t and rp.get("priority") in PRIO:
                t["priority"] = rp["priority"]
                t.setdefault("history", []).append({"ts": utcnow(), "actor": "po", "from": t["state"], "to": t["state"], "note": f"priority → {rp['priority']}: {rp.get('reason', '')[:200]}"})
                self.store.save_task(t)
        for c in res.get("close_tasks") or []:
            t = self.store.task(str(c.get("task_id")))
            if t and t.get("state") in CLOSABLE_BY_PO:
                self.store.transition(t["id"], "rejected", "po", f"closed by PO: {c.get('reason', '')[:300]}", force=True)
        skipped = []
        for rt in res.get("ready_tasks") or []:
            dup = duplicate_task(self.store.tasks(OPEN_TASK_STATES), rt)
            if dup:
                # the PO re-proposed a task that is already open (same title, or the same role and start moment): one slot, one task
                self.store.event("task.duplicate_skipped", dup["id"], "po", {"title": rt["title"][:140], "not_before": rt.get("not_before")})
                skipped.append(f"{rt['title'][:80]} → already open as {dup['id']}")
                continue
            self.store.create_task(title=rt["title"], type_=rt["type"], problem=rt["problem"], scope=rt["scope"], acceptance=rt["acceptance"],
                                   negative_cases=rt.get("negative_cases"), goal=rt["goal"], priority=rt["priority"], size=rt["size"],
                                   role=rt["role"], observations=rt.get("observations"), links=rt.get("links"), created_by="po", state="ready",
                                   not_before=rt.get("not_before"))
        for q in res.get("owner_questions") or []:
            self.store.ask(from_="po", to="owner", question=f"{q['question']} (why the owner: {q.get('why_owner', '')})", task_id=q.get("task_id"))
        digest = res.get("digest_md", "")
        if skipped:
            digest += "\n\n_Диспетчер не создал дубли (задача уже открыта): " + "; ".join(skipped) + "._"
        (self.p.dir / "digests" / f"{self.cycle_id}.md").write_text(f"# PO digest {self.cycle_id}\n\n{digest}\n", encoding="utf-8")

    # execute: one ready task → developer / marketer / analyst ------------------------------------

    def phase_execute(self) -> None:
        n_max = int(self.st.cycle.get("max_execute_per_cycle", 1))
        done = 0
        # finish tasks that are in_progress (a failed review or a partial result) before starting new ones
        for t in sorted(self.store.tasks(["in_progress", "ready"]), key=lambda x: (x["state"] != "in_progress", PRIO.get(x.get("priority"), 9), x.get("created", ""))):
            if done >= n_max:
                break
            if t.get("state") == "in_progress" and t.get("assignee_run") and not t.get("last_review") and not (t.get("results") or [{}])[-1].get("status") == "partial":
                continue        # a run is on it (lease alive); stale leases are handled in collect
            nb = parse_ts(t.get("not_before"))
            if nb and _now() < nb:
                continue        # scheduled for later (daily posts): the PO set not_before
            if t.get("state") == "ready" and not t.get("not_before") and future_dated_title(str(t.get("title", "")), _now()):
                self.store.add_observation({
                    "title": f"Task {t['id']} names a future date in its title but has no not_before; not started: {t.get('title', '')}"[:140],
                    "classification": "need_hypothesis", "source": "dispatcher", "task": t["id"],
                    "fingerprint": f"future-dated-{t['id']}", "run_id": f"{self.cycle_id}-execute",
                    "detail": "PO: set not_before on this task (or fix the title)."})
                continue
            if self.execute_task(t):
                done += 1

    def execute_task(self, t: dict[str, Any]) -> bool:
        run_id = f"{self.cycle_id}-exec-{t['id']}"
        if not self.store.claim(t["id"], str(t.get("role")), run_id):
            return False
        if t["state"] == "ready":
            self.store.transition(t["id"], "claimed", "dispatcher", run_id, assignee_run=run_id)
            self.store.transition(t["id"], "in_progress", "dispatcher", "started", assignee_run=run_id)
        else:
            t["assignee_run"] = run_id
            self.store.save_task(t)
        role = str(t.get("role") or "developer")
        try:
            if role == "developer":
                return self.exec_developer(t, run_id)
            if role == "marketer":
                return self.exec_marketer(t, run_id)
            if role == "analyst":
                return self.exec_analyst_task(t, run_id)
            self.store.transition(t["id"], "blocked", "dispatcher", f"unknown role {role}", force=True)
            return True
        finally:
            self.store.release(t["id"])

    def exec_developer(self, t: dict[str, Any], run_id: str) -> bool:
        repo = self.p.repo_dir
        if self.p.repo_url:
            gitops.ensure_clone(repo, self.p.repo_url, self.p.repo_branch)
        elif not (repo / ".git").exists():
            self.store.transition(t["id"], "blocked", "dispatcher", "no repository configured", force=True)
            return True
        branch = gitops.start_task_branch(repo, t["id"], self.p.repo_branch)
        prompt = prompts.developer_prompt(self.p, t, repo, branch, t.get("results") or [], t.get("last_review"))
        cap = float(t.get("budget_usd") or self.st.budget["max_usd_per_run"])
        rr = self.role_run("developer", prompt, "task-result", cwd=repo, label=f"dev-{t['id']}", cap=cap,
                           allowed_tools=["Bash", "Read", "Edit", "Write", "Glob", "Grep"])
        if rr is None:
            self.store.transition(t["id"], "ready", "dispatcher", "no run (budget or dry-run)", force=True, assignee_run=None)
            return False
        if not (rr.ok and rr.structured):
            attempts = int(t.get("attempts") or 0) + 1
            note = f"run failed: {rr.error}"
            self.store.add_result(t["id"], {"run_id": run_id, "status": "error", "summary": note[:400]})
            if attempts >= 3:
                self.store.transition(t["id"], "blocked", "dispatcher", note[:300], force=True, attempts=attempts)
            else:
                self.store.transition(t["id"], "ready", "dispatcher", note[:300], force=True, attempts=attempts, assignee_run=None)
            return True
        res = rr.structured
        res["run_id"] = run_id
        res["commit"] = gitops.head(repo)
        self.store.add_result(t["id"], res)
        for pr in res.get("proposals") or []:
            self.store.add_observation({"title": str(pr)[:140], "classification": "need_hypothesis", "source": "developer", "task": t["id"],
                                        "actual": str(pr)[:400], "url": self.p.site_base, "fingerprint": fingerprint("dev", "proposal", "", str(pr))})
        if res.get("status") == "blocked":
            self.store.ask(from_="developer", to="po", question=str(res.get("question") or res.get("summary"))[:1200], task_id=t["id"])
            return True
        ahead = gitops.commits_ahead(repo, self.p.repo_branch) or (1 if gitops.dirty(repo) else 0)
        if ahead == 0:
            self.store.transition(t["id"], "ready", "dispatcher", "developer reported done but committed nothing", force=True,
                                  attempts=int(t.get("attempts") or 0) + 1, assignee_run=None)
            return True
        if res.get("status") == "partial":
            self.store.transition(t["id"], "in_progress", "dispatcher", "partial — continues next cycle", force=True, last_review=None)
            return True
        if self.p.repo_mode == "pr":
            return self.release_as_pull_request(t, repo, res)
        try:
            sha = gitops.merge_and_push(repo, t["id"], self.p.repo_branch)
        except gitops.GitError as exc:
            self.store.transition(t["id"], "blocked", "dispatcher", f"merge/push failed: {gitops.redact(str(exc))[:300]}", force=True)
            return True
        self.store.transition(t["id"], "released", "dispatcher", f"merged {sha[:10]}", force=True, released_at=utcnow(), released_sha=sha, last_review=None)
        return True

    def release_as_pull_request(self, t: dict[str, Any], repo: Path, res: dict[str, Any]) -> bool:
        """repo.mode: pr — the tests of the repository are the reviewer; a green branch becomes a pull request the owner merges
        (waiting_owner → accepted once merged). Used for the framework's own repository: nothing lands there without a human."""
        if self.p.test_cmd:
            # the test suite never gets the dispatcher's secrets: a test that posts would post for real (Demo $25, 02.11)
            test_env = {k: v for k, v in os.environ.items() if not k.startswith(("TELEGRAM_", "BLUESKY_", "DEVTO_", "MASTODON_", "GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY"))}
            test_env["TEAM_PUBLISH_DRY_RUN"] = "1"
            try:
                cp = subprocess.run(self.p.test_cmd, shell=True, cwd=str(repo), capture_output=True, text=True, timeout=900, env=test_env)
            except subprocess.TimeoutExpired:
                cp = None
            if cp is None or cp.returncode != 0:
                tail = (cp.stdout + cp.stderr)[-600:] if cp else "tests timed out"
                attempts = int(t.get("attempts") or 0) + 1
                self.store.add_result(t["id"], {"run_id": res.get("run_id"), "status": "tests_failed", "summary": tail})
                self.store.transition(t["id"], "blocked" if attempts >= 2 else "ready", "dispatcher", f"tests failed: {tail[:300]}", force=True,
                                      attempts=attempts, assignee_run=None)
                return True
        fw = dict(self.st.raw.get("framework") or {})
        touched = gitops.changed_files(repo, self.p.repo_branch)
        protected = [f for f in touched if any(f == pp or f.startswith(pp.rstrip("/") + "/") for pp in fw.get("protected_paths") or [])]
        try:
            gitops.push_branch(repo, t["id"])
            body = (f"{res.get('summary', '')}\n\n**Changes:** " + "; ".join(res.get("changes") or []) +
                    "\n\n**Self-check:**\n" + "\n".join(f"- {'✓' if c.get('met') else '✗'} {c.get('criterion')} — {c.get('evidence')}" for c in res.get("self_check") or []) +
                    "\n\n" + "\n".join(f"Evidence: {l}" for l in t.get("links") or []) +
                    "\n\n_Opened by the agent-team dispatcher for task " + t["id"] + "; tests passed on the branch. A human merges._")
            url = gitops.open_pull_request(self.p.repo_url, t["id"], self.p.repo_branch, f"{t['id']}: {t.get('title', '')}", body)
        except gitops.GitError as exc:
            self.store.transition(t["id"], "blocked", "dispatcher", f"push/PR failed: {gitops.redact(str(exc))[:300]}", force=True)
            return True
        if not fw.get("auto_merge") or protected:
            why = f"protected files {', '.join(protected[:5])}" if protected else "auto_merge off"
            self.store.transition(t["id"], "waiting_owner", "dispatcher", f"pull request {url} ({why})", force=True, pr_url=url, assignee_run=None)
            return True
        ok, note = self.canary(repo, str(fw.get("canary_product") or ""))
        if not ok:
            self.store.transition(t["id"], "waiting_owner", "dispatcher", f"pull request {url}; canary failed: {note[:200]}", force=True, pr_url=url, assignee_run=None)
            gitops.comment_pull_request(url, f"Canary cycle failed, left for a human:\n\n```\n{note[:1500]}\n```")
            return True
        try:
            sha = gitops.merge_pull_request(url)
            gitops.update_running_checkout(self.st.root, self.p.repo_branch)
        except gitops.GitError as exc:
            self.store.transition(t["id"], "waiting_owner", "dispatcher", f"pull request {url}; merge failed: {gitops.redact(str(exc))[:200]}", force=True, pr_url=url, assignee_run=None)
            return True
        self.store.transition(t["id"], "accepted", "dispatcher", f"canary passed ({note[:120]}); merged {sha[:10]}", force=True, pr_url=url, accepted_at=utcnow(), released_sha=sha)
        self.store.transition(t["id"], "done", "dispatcher", "framework change live; the watchdog rolls back if the next cycles fail", force=True)
        self.store.event("framework.merged", t["id"], "dispatcher", {"sha": sha, "pr": url, "files": touched[:20]})
        return True

    def canary(self, repo: Path, product: str) -> tuple[bool, str]:
        """One real cycle of one product on the candidate code (PYTHONPATH=candidate, TEAM_ROOT=this root): all phases
        must finish and no phase may error. Returns (passed, note)."""
        if not product:
            return True, "no canary product configured"
        env = {**os.environ, "PYTHONPATH": str(repo), "TEAM_ROOT": str(self.st.root)}
        cyc = f"{self.cycle_id}-canary"
        try:
            cp = subprocess.run([sys.executable, "-m", "team.cli", "cycle", "--product", product, "--cycle", cyc], cwd=str(repo),
                                env=env, capture_output=True, text=True, timeout=int(self.st.claude.get("timeout_s", 1500)) + 300)
        except subprocess.TimeoutExpired:
            return False, "canary timed out"
        out = (cp.stdout + cp.stderr)
        if cp.returncode != 0 or "Traceback" in out:
            return False, f"rc={cp.returncode} " + out[-800:]
        m = re.search(r"phases \[([^\]]*)\]", out)
        phases = [x.strip(" '") for x in m.group(1).split(",")] if m else []
        missing = [ph for ph in PHASES if ph not in phases]
        if missing:
            return False, f"phases missing: {missing}; " + out[-500:]
        if "phase.error" in out or "phase %s failed" in out or " failed: " in out and "phase" in out:
            return False, "phase error: " + out[-800:]
        return True, f"cycle {cyc} of {product}: all phases"

    def check_pull_requests(self) -> None:
        for t in self.store.tasks(["waiting_owner"]):
            url = t.get("pr_url")
            if not url or self.dry_run:
                continue
            try:
                state = gitops.pull_request_state(url)
            except gitops.GitError as exc:
                log.warning("%s: PR state of %s: %s", self.p.slug, url, exc)
                continue
            if state == "merged":
                self.store.transition(t["id"], "accepted", "owner", f"pull request merged {url}", force=True, accepted_at=utcnow())
                self.store.transition(t["id"], "done", "dispatcher", "merged by the owner; framework tasks are not measured", force=True)
            elif state == "closed":
                self.store.transition(t["id"], "rejected", "owner", f"pull request closed without merge {url}", force=True)

    def exec_marketer(self, t: dict[str, Any], run_id: str) -> bool:
        channels = self.p.raw.get("channels") or {}
        if not channels:
            self.store.transition(t["id"], "blocked", "dispatcher", "no channels configured in product.yaml", force=True)
            return True
        work = self.p.work_dir / self.cycle_id / f"mkt-{t['id']}"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        ledger = self.run_dir / f"mkt-{t['id']}" / "ledger.jsonl"
        from . import editorial
        lessons = editorial.read_lessons(self.p)
        prompt = prompts.marketer_prompt(self.p, t, channels, t.get("results") or [], lessons=lessons)
        disc = self.st.publish.get("disclosure", "")
        disc_map = disc if isinstance(disc, dict) else {"default": str(disc)}
        drafts_dir = work / "drafts"
        env = {"TEAM_PUBLISH_LEDGER": str(ledger), "TEAM_PUBLISH_DISCLOSURES": json.dumps(disc_map, ensure_ascii=False),
               "TEAM_PUBLISH_MAX_PER_DAY": str(self.st.publish.get("max_posts_per_channel_per_day", 2)),
               "TEAM_PUBLISH_PRODUCT": self.p.slug, "TEAM_PUBLISH_COUNTS": json.dumps({c: self.store.posts_today(c) for c in channels}),
               "TEAM_PUBLISH_DRAFT_DIR": str(drafts_dir),
               **({"TEAM_PUBLISH_DRY_RUN": "1"} if self.st.publish.get("dry_run") else {})}
        mkt_tools = ["Bash(team-publish *)", "Bash(team-publish:*)", "Bash(team-fetch *)", "Bash(team-fetch:*)", "Read", "Write"]
        rr = self.role_run("marketer", prompt, "task-result", cwd=work, label=f"mkt-{t['id']}", cap=1.0, env_extra=env, allowed_tools=mkt_tools)
        posted, editor_summary = 0, ""
        if rr is not None and rr.ok and rr.structured and self.runner is not None:
            posted, editor_summary = self.editorial_loop(t, work, drafts_dir, channels, env, mkt_tools, lessons)
        if rr is None or not (rr.ok and rr.structured):
            self.store.transition(t["id"], "ready" if posted == 0 else "accepted", "dispatcher", f"run failed: {rr.error if rr else 'no run'}; posted {posted}", force=True, assignee_run=None)
            return True
        res = rr.structured
        res["run_id"] = run_id
        res["posted"] = posted
        if editor_summary:
            res["editor"] = editor_summary[:600]
        self.store.add_result(t["id"], res)
        if res.get("status") == "blocked" or posted == 0:
            why = str(res.get("question") or f"nothing was posted: {res.get('summary', '')}")
            if editor_summary:
                why = f"editor: {editor_summary} / marketer: {res.get('summary', '')}"
            self.store.ask(from_="marketer", to="po", question=why[:1200], task_id=t["id"])
            return True
        self.store.transition(t["id"], "accepted", "dispatcher", f"posted {posted} (editor-approved)", force=True, accepted_at=utcnow())
        return True

    def editorial_loop(self, t: dict[str, Any], work: Path, drafts_dir: Path, channels: dict[str, Any], env: dict[str, str],
                       mkt_tools: list[str], lessons: list[str]) -> tuple[int, str]:
        """draft → editor → (one revision by the marketer) → editor → publish approved. Returns (posted, editor summary)."""
        from . import editorial
        drafts = self._load_drafts(drafts_dir)
        if not drafts:
            return 0, ""
        _, _, recent_issues = editorial.rolling(self.st, self.p.slug)
        verdicts: dict[str, dict[str, Any]] = {}
        summary = ""
        prev_issues: list[str] = []
        for round_no in (1, 2):
            prompt = editorial.editor_prompt(self.p, t, drafts, lessons, recent_issues, round_no, prev_issues or None)
            ework = work / f"editor-{round_no}"
            ework.mkdir(parents=True, exist_ok=True)
            rr = self.role_run("editor", prompt, "editor-review", cwd=ework, label=f"editor-{t['id']}-{round_no}", cap=0.5, max_turns=16)
            if rr is None or not (rr.ok and rr.structured):
                log.warning("%s: editor run failed in round %d; drafts not published", self.p.slug, round_no)
                return 0, f"editor run failed in round {round_no}"
            rev = rr.structured
            summary = str(rev.get("summary") or "")
            verdicts = {str(r.get("draft")): r for r in rev.get("reviews") or []}
            new_lessons = [ls for r in verdicts.values() for ls in (r.get("lessons") or [])][: editorial.MAX_LESSONS_PER_REVIEW]
            if new_lessons:
                editorial.add_lessons(self.p, new_lessons, "editor")
            revise = [(n, r) for n, r in verdicts.items() if r.get("verdict") == "revise"]
            final = round_no == 2 or not revise
            for name, d in drafts:
                r = verdicts.get(name) or {"verdict": "reject", "scores": {}, "issues": ["no verdict from the editor"]}
                editorial.record_score(self.st, {"product": self.p.slug, "task": t["id"], "channel": d.get("channel"), "round": round_no,
                                                 "verdict": r.get("verdict"), "scores": r.get("scores") or {}, "mean": editorial.mean_score(r.get("scores") or {}),
                                                 "issues": r.get("issues") or [], "final": final or r.get("verdict") != "revise"})
            self.store.event("editorial", t["id"], "editor", {"round": round_no, "verdicts": {n: r.get("verdict") for n, r in verdicts.items()}, "summary": summary[:200]})
            if final:
                break
            prev_issues = [i for _, r in revise for i in (r.get("issues") or [])]
            notes = [{"draft": n, "issues": r.get("issues"), "scores": r.get("scores")} for n, r in revise]
            drafts_dir2 = work / "drafts-2"
            prompt2 = prompts.marketer_prompt(self.p, t, channels, [], lessons=editorial.read_lessons(self.p), editor_notes=notes)
            rr2 = self.role_run("marketer", prompt2, "task-result", cwd=work, label=f"mkt-{t['id']}-rev", cap=1.0,
                                env_extra={**env, "TEAM_PUBLISH_DRAFT_DIR": str(drafts_dir2)}, allowed_tools=mkt_tools)
            drafts2 = self._load_drafts(drafts_dir2) if rr2 is not None else []
            if not drafts2:
                return 0, summary or "marketer produced no revised drafts"
            approved_r1 = [(n, d) for n, d in drafts if (verdicts.get(n) or {}).get("verdict") == "approve"]
            drafts = approved_r1 + drafts2 if approved_r1 else drafts2
        posted = 0
        for name, d in drafts:
            r = verdicts.get(name) or {}
            if r.get("verdict") != "approve":
                continue
            text = str(r.get("final_text") or "").strip() or None
            res = publish.publish_draft(Path(d["_path"]), text=text, dry_run=bool(self.st.publish.get("dry_run")))
            if res.ok and not res.dry_run:
                self.store.record_post(str(res.channel), str(res.post_id or ""), str(res.url or ""), t["id"])
                posted += 1
            elif not res.ok:
                self.store.event("post.error", t["id"], "dispatcher", {"channel": d.get("channel"), "error": str(res.error)[:200]})
        if editorial.degrading(self.st, self.p.slug):
            m, n, top = editorial.rolling(self.st, self.p.slug)
            self.store.add_observation({"title": f"Editorial quality falling: mean {m}/5 over the last {n} posts", "classification": "need_hypothesis",
                                        "source": "editor", "url": self.p.site_base, "actual": "; ".join(top)[:400], "expected": f"≥ {editorial.DEGRADE_BELOW}",
                                        "fingerprint": fingerprint("editor", "degrading", self.p.slug, _now().strftime("%Y-%m-%d"))})
        return posted, summary

    @staticmethod
    def _load_drafts(dir_: Path) -> list[tuple[str, dict[str, Any]]]:
        out = []
        for p in sorted(dir_.glob("*.json")) if dir_.exists() else []:
            try:
                d = publish.load_draft(p)
            except (json.JSONDecodeError, OSError):
                continue
            d["_path"] = str(p)
            out.append((p.name, d))
        return out

    def exec_analyst_task(self, t: dict[str, Any], run_id: str) -> bool:
        rr = self.analyst_run(label=f"an-{t['id']}", question=f"Task {t['id']}: {t.get('problem')}\nScope: {t.get('scope')}")
        if rr is None or not (rr.ok and rr.structured):
            self.store.transition(t["id"], "ready", "dispatcher", "analyst run failed", force=True, assignee_run=None)
            return True
        self.store.add_result(t["id"], {"run_id": run_id, "status": "done", "summary": rr.structured.get("summary_md", "")[:1200]})
        self.store.transition(t["id"], "done", "dispatcher", "measurement delivered", force=True)
        return True

    # measure: daily analyst ---------------------------------------------------------------------------

    def phase_measure(self) -> None:
        day = _now().strftime("%Y-%m-%d")
        every = int(self.st.cycle.get("measure_every_h", 24))
        slot = _now().hour // max(1, every)
        marker = self.p.dir / "digests" / (f"analyst-{day}.md" if every >= 24 else f"analyst-{day}-{slot:02d}.md")
        if marker.exists():
            return
        if not list((self.p.dir / "metrics").glob("*.json")):
            return
        rr = self.analyst_run(label="analyst")
        if rr is None or not (rr.ok and rr.structured):
            return
        res = rr.structured
        marker.write_text(f"# Analyst {day}\n\n{res.get('summary_md', '')}\n\n```yaml\n{yaml.safe_dump({k: res.get(k) for k in ('task_effects', 'goal_status')}, allow_unicode=True, sort_keys=False)}```\n", encoding="utf-8")
        for o in res.get("observations") or []:
            self.store.add_observation({**o, "source": "analyst", "run_id": f"{self.cycle_id}-analyst",
                                        "fingerprint": fingerprint("analyst", o.get("classification", ""), o.get("url", ""), o.get("title", ""))})
        effects = {e.get("task_id"): e for e in res.get("task_effects") or []}
        after = timedelta(hours=int(self.st.cycle.get("measure_after_h", 24)))
        for t in self.store.tasks(["accepted"]):
            acc = parse_ts(t.get("accepted_at"))
            if acc and _now() - acc >= after:
                e = effects.get(t["id"], {})
                self.store.transition(t["id"], "measured", "analyst", f"effect: {e.get('effect', 'insufficient_data')} — {e.get('evidence', '')[:200]}", effect=e.get("effect", "insufficient_data"))
                self.store.transition(t["id"], "done", "dispatcher", "measured")

    # geo: weekly control questions to AI search -------------------------------------------------

    def phase_geo(self) -> None:
        every_h = int(self.st.cycle.get("geo_every_h", 168))
        last = sorted((self.p.dir / "metrics").glob("geo-*.json"))
        if last:
            prev = parse_ts(json.loads(last[-1].read_text(encoding="utf-8")).get("generated") or "")
            if prev and _now() - prev < timedelta(hours=every_h):
                return
        qs = geo.queries_for(self.p.raw)
        if not qs:
            return
        prompt = geo.build_prompt(str(self.p.raw.get("name") or self.p.slug), self.p.site_base, qs, geo.audience_section(self.p.vision()))
        rr = self.role_run("geo-probe", prompt, "geo-report", cwd=self.p.work_dir / self.cycle_id / "geo", label="geo", cap=1.5,
                           allowed_tools=["WebSearch", "WebFetch"], max_turns=40)
        if rr is None or not (rr.ok and rr.structured):
            return
        day = _now().strftime("%Y-%m-%d")
        m = geo.to_metrics(rr.structured, day)
        m["generated"] = utcnow()
        (self.p.dir / "metrics" / f"geo-{day}.json").write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
        (self.p.dir / "digests" / f"geo-{day}.md").write_text(
            f"# GEO probe {day}\n\n{rr.structured.get('summary_md', '')}\n\n```yaml\n{yaml.safe_dump(m, allow_unicode=True, sort_keys=False)}```\n", encoding="utf-8")
        for card in geo.to_observations(rr.structured, self.p.slug):
            self.store.add_observation({**card, "run_id": f"{self.cycle_id}-geo"})

    def analyst_run(self, *, label: str, question: str | None = None) -> RunResult | None:
        files = sorted((self.p.dir / "metrics").glob("????-??-??.json"), reverse=True)[:4]
        metrics = [(f.stem, f.read_text(encoding="utf-8")) for f in files]
        since = (_now() - timedelta(hours=72)).isoformat()
        accepted = [t for t in self.store.tasks(["accepted", "measured", "done", "released"]) if (t.get("accepted_at") or t.get("released_at") or "") >= since]
        posts = [{"day": r[0], "channel": r[1], "url": r[3], "ts": r[5]} for r in self.store.db.execute("SELECT * FROM posts ORDER BY ts DESC LIMIT 20")]
        prompt = prompts.analyst_prompt(self.p, self.store, metrics=metrics, accepted=accepted, posts=posts)
        if question:
            prompt += f"\n\n# QUESTION TO ANSWER\n{question}\n"
        return self.role_run("analyst", prompt, "analyst-report", cwd=self.p.work_dir / self.cycle_id / label, label=label, cap=1.0, allowed_tools=[], max_turns=6)

    # digest: once a day to the owner's channel ----------------------------------------------------

    def phase_digest(self) -> None:
        day = _now().strftime("%Y-%m-%d")
        hours = sorted(int(h) for h in (self.st.cycle.get("digest_hours_utc") or [self.st.cycle.get("digest_hour_utc", 7)]))
        due = [h for h in hours if _now().hour >= h]
        if not due:
            return
        out = self.p.dir / "digests" / f"daily-{day}-{due[-1]:02d}.md"
        if out.exists():
            return
        text = daily_digest(self.p, self.store, day)
        out.write_text(text, encoding="utf-8")
        chat = os.environ.get("TELEGRAM_OPS_CHAT_ID")
        if chat and os.environ.get("TELEGRAM_BOT_TOKEN") and not self.dry_run and os.environ.get("TEAM_PUBLISH_DRY_RUN") != "1":
            r = publish.post("telegram", text[:3500], target=chat, disclosure=" ", dry_run=False)
            self.store.event("digest.sent", day, "dispatcher", {"ok": r.ok, "error": r.error})


# ------------------------------------------------------------------ helpers

OPEN_TASK_STATES = ("ready", "claimed", "in_progress", "review", "released", "blocked", "waiting_owner")
CLOSABLE_BY_PO = ("proposed", "ready", "blocked", "waiting_owner", "deferred")


def _norm_title(s: str) -> str:
    return re.sub(r"[^\w]+", " ", str(s or "").lower()).strip()


def _norm_ts(s: Any) -> str:
    d = parse_ts(str(s)) if s else None
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M") if d else ""


def duplicate_task(open_tasks: list[dict[str, Any]], rt: dict[str, Any]) -> dict[str, Any] | None:
    """The open task a proposed ready task duplicates, or None.

    Same normalized title → duplicate. Same role and the same `not_before` moment → duplicate too: a scheduled post is one
    slot, and the PO wrote the same slot under a slightly different title in every cycle (luz-amanha T-0006/T-0011,
    release-radar T-0009/T-0012/T-0015 on 03.11)."""
    title = _norm_title(rt.get("title"))
    nb = _norm_ts(rt.get("not_before"))
    for t in open_tasks:
        if title and _norm_title(t.get("title")) == title:
            return t
        if nb and t.get("role") == rt.get("role") and _norm_ts(t.get("not_before")) == nb:
            return t
    return None


DATE_IN_TITLE_RE = re.compile(r"(?<![\w.])(\d{2})\.(\d{2})(?:\.(\d{4}))?(?!\w|\.\d)")


def future_dated_title(title: str, now: datetime) -> bool:
    """True if the title contains a standalone DD.MM or DD.MM.YYYY date later than today (UTC).

    Only two-digit day and month count (``03.11``), so version numbers (``3.12``, ``v1.12``, ``2.1.4``) and decimals
    are not dates. Without a year, December → January is read as the next year."""
    for d, m, y in DATE_IN_TITLE_RE.findall(title):
        d, m = int(d), int(m)
        if not (1 <= m <= 12 and 1 <= d <= 31):
            continue
        if y:
            if (int(y), m, d) > (now.year, now.month, now.day):
                return True
        elif (m, d) > (now.month, now.day) or (now.month == 12 and m == 1):
            return True
    return False


def fingerprint(scope: str, cls: str, url: str, title: str) -> str:
    import hashlib
    import re
    path = re.sub(r"^https?://[^/]+", "", url or "")
    t = re.sub(r"[^a-z0-9а-я]+", " ", (title or "").lower()).strip()[:60]
    return hashlib.sha1(f"{scope}|{cls}|{path}|{t}".encode()).hexdigest()[:12]


def metrics_md(data: dict[str, Any]) -> str:
    days = data.get("days") or {}
    lines = [f"# metrics {data.get('host', '')} — generated {data.get('generated', '')}", "",
             "| day | humans | engaged | returns | agents | crawlers | targets |", "|---|---|---|---|---|---|---|"]
    for day in sorted(days)[-10:]:
        d = days[day]
        h = d.get("humans") or {}
        tg = d.get("targets") or {}
        tg_s = ", ".join(f"{k}:{(v.get('requests') if isinstance(v, dict) else v)}" for k, v in list(tg.items())[:4])
        lines.append(f"| {day} | {h.get('sources', 0)} | {h.get('engaged', 0)} | {h.get('returns', 0)} | "
                     f"{(d.get('agents') or {}).get('requests', d.get('agents', 0)) if isinstance(d.get('agents'), dict) else d.get('agents', 0)} | "
                     f"{(d.get('crawlers') or {}).get('requests', 0) if isinstance(d.get('crawlers'), dict) else d.get('crawlers', 0)} | {tg_s} |")
    top = (data.get("totals") or {}).get("top_pages") or (days.get(max(days), {}) if days else {}).get("top_pages") or []
    if top:
        lines += ["", "Top pages (humans): " + ", ".join(f"{p.get('path') if isinstance(p, dict) else p[0]} ({p.get('requests') if isinstance(p, dict) else p[1]})" for p in top[:8])]
    return "\n".join(lines) + "\n"


def daily_digest(p: Product, store: Store, day: str) -> str:
    from .store import summary
    s = summary(store)
    po = sorted((p.dir / "digests").glob("20*.md"))
    po_txt = po[-1].read_text(encoding="utf-8").split("\n", 2)[-1].strip() if po else "(PO не запускался)"
    ans = sorted((p.dir / "digests").glob(f"analyst-{day}*.md"))
    an_txt = ans[-1].read_text(encoding="utf-8").split("\n", 2)[-1].split("```")[0].strip() if ans else "(аналитик сегодня не запускался)"
    qs = store.questions(["open"])
    owner_q = [q for q in qs if q.get("to") == "owner"]
    from . import editorial
    em, en, _ = editorial.rolling(p.st, p.slug)
    ed_line = f"Тексты: средний балл редактора {em}/5 за последние {en} постов" + (" ⚠ ниже планки" if em is not None and en >= 5 and em < editorial.DEGRADE_BELOW else "") if em is not None else "Тексты: редактор ещё не оценивал"
    lines = [f"📦 {p.raw.get('name') or p.slug} — {day}", "", ed_line,
             f"Задачи: {s['tasks']} всего · " + ", ".join(f"{k} {v}" for k, v in sorted(s["by_state"].items())),
             f"Расход сегодня: ${s['spent_today']:.2f} (кап ${p.daily_cap:.0f}) · открытых вопросов: {len(qs)}, владельцу: {len(owner_q)}", "",
             "PO: " + po_txt[:1200], "", "Аналитик: " + an_txt[:1200]]
    if owner_q:
        lines += ["", "❓ Владельцу:"] + [f"- {q['id']}: {q['question'][:300]}" for q in owner_q[:5]]
    return "\n".join(lines) + "\n"
