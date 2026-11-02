"""Framework friction register: where the framework itself got in the way of the roles.

Two sources, both without the owner:
1. deterministic — the dispatcher's own records: failed runs, tool/permission denials, phase errors, budget stops, tasks that
   went `blocked`, expired leases, "done but committed nothing", failed / inconclusive reviews, owner questions open for
   more than a day, observations classified `environment_error`;
2. reported — the optional `friction` field roles fill in their results (task-result, review-verdict): a missing tool, an
   unclear instruction, a schema that did not fit, data they could not reach.

The register is global across products: ``data/framework/friction.jsonl`` (append-only, one record per fingerprint) and
``data/framework/register.md`` (aggregate by kind). ``team friction`` prints the register; ``--out`` writes the public
export for the diary (counts and short summaries, secrets redacted). The retro role reads the register and proposes
changes to the framework (see retro.py).
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Product, Settings
from .store import Store, parse_ts

log = logging.getLogger("team.friction")

KINDS = ("run_failed", "tool_denied", "phase_error", "budget_stop", "task_blocked", "lease_expired", "no_commit",
         "review_fail", "review_inconclusive", "owner_wait", "env_error", "reported")
REPORTED_KINDS = ("missing_tool", "denied_permission", "unclear_instruction", "schema_mismatch", "missing_data", "environment", "other")
OWNER_WAIT_H = 24
SUMMARY_MAX = 300
_SECRET = re.compile(r"(x-access-token:|ghp_|sk-ant-|Bearer\s+)[^\s@\"']+", re.I)


def redact(s: str) -> str:
    return _SECRET.sub(r"\1***", str(s))


def _rec(product: str, kind: str, summary: str, *, role: str = "dispatcher", ts: str | None = None, task: str | None = None,
         run_id: str | None = None, fp: str) -> dict[str, Any]:
    return {"ts": ts or datetime.now(timezone.utc).isoformat(timespec="seconds"), "product": product, "role": role, "kind": kind,
            "summary": redact(summary)[:SUMMARY_MAX], "task": task, "run_id": run_id, "fingerprint": f"{product}:{kind}:{fp}"}


def collect_product(p: Product, store: Store) -> list[dict[str, Any]]:
    """Every friction signal in the product's records (idempotent: the register dedupes by fingerprint)."""
    out: list[dict[str, Any]] = []
    slug = p.slug
    for e in store.events(limit=5000):
        d = e.get("detail")
        if isinstance(d, str):
            try:
                d = json.loads(d)
            except (json.JSONDecodeError, TypeError):
                d = {"text": d}
        d = d or {}
        k, ts = e.get("kind"), e.get("ts")
        if k == "run" and d.get("ok") is False:
            out.append(_rec(slug, "run_failed", f"{e.get('actor')}: {d.get('error') or 'run not ok'}", role=str(e.get("actor")), ts=ts,
                            run_id=e.get("subject"), fp=str(e.get("subject"))))
        elif k == "phase.error":
            out.append(_rec(slug, "phase_error", f"phase {e.get('subject')}: {d.get('text') or d}", ts=ts, fp=f"{e.get('subject')}:{ts}"))
        elif k == "budget.stop":
            out.append(_rec(slug, "budget_stop", f"budget stop: {d}", ts=ts, fp=str(ts)))
    for rj in p.runs_dir.glob("*/*/run.json"):
        try:
            r = json.loads(rj.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        den = r.get("denials") or []
        if den:
            names = sorted({str((x or {}).get("tool_name") or x) for x in den})[:6]
            out.append(_rec(slug, "tool_denied", f"{r.get('role')}: denied {', '.join(names)}", role=str(r.get("role")), ts=r.get("finished"),
                            run_id=r.get("run_id"), fp=str(r.get("run_id"))))
    for t in store.tasks():
        for h in t.get("history") or []:
            note, to, ts = str(h.get("note") or ""), h.get("to"), h.get("ts")
            if to == "blocked":
                out.append(_rec(slug, "task_blocked", f"{t['id']} {t.get('title', '')[:60]}: {note}", ts=ts, task=t["id"], fp=f"{t['id']}:{ts}"))
            elif "lease of" in note and "expired" in note:
                out.append(_rec(slug, "lease_expired", f"{t['id']}: {note}", ts=ts, task=t["id"], fp=f"{t['id']}:{ts}"))
            elif "committed nothing" in note:
                out.append(_rec(slug, "no_commit", f"{t['id']}: {note}", role="developer", ts=ts, task=t["id"], fp=f"{t['id']}:{ts}"))
        for res in t.get("results") or []:
            for f in res.get("friction") or []:
                out.append(_rec(slug, "reported", f"{res.get('role') or t.get('role')}: [{f.get('kind')}] {f.get('what')}",
                                role=str(res.get("role") or t.get("role")), ts=res.get("finished") or t.get("updated"), task=t["id"],
                                run_id=res.get("run_id"), fp=f"{res.get('run_id')}:{f.get('what', '')[:60]}"))
    for r in store._load_all("reviews"):
        v = r.get("verdict")
        if v in ("fail", "inconclusive"):
            out.append(_rec(slug, f"review_{v}", f"{r.get('task')}: {r.get('summary', '')}", role="reviewer", ts=r.get("created"),
                            task=r.get("task"), run_id=r.get("run_id"), fp=str(r.get("id"))))
        for f in r.get("friction") or []:
            out.append(_rec(slug, "reported", f"reviewer: [{f.get('kind')}] {f.get('what')}", role="reviewer", ts=r.get("created"),
                            task=r.get("task"), run_id=r.get("run_id"), fp=f"{r.get('id')}:{f.get('what', '')[:60]}"))
    now = datetime.now(timezone.utc)
    for q in store.questions(["open"]):
        if q.get("to") == "owner":
            c = parse_ts(q.get("created"))
            if c and now - c >= timedelta(hours=OWNER_WAIT_H):
                out.append(_rec(slug, "owner_wait", f"{q['id']} open {int((now - c).total_seconds() // 3600)} h: {q.get('question', '')[:120]}",
                                role="po", ts=q.get("created"), task=q.get("task"), fp=str(q["id"])))
    for o in store.observations():
        if o.get("classification") == "environment_error":
            out.append(_rec(slug, "env_error", f"{o.get('source')}: {o.get('title', '')}", role=str(o.get("source")), ts=o.get("observed_at"),
                            fp=str(o.get("id") or o.get("fingerprint"))))
    return out


class Register:
    def __init__(self, dir_: Path):
        self.dir = dir_
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "friction.jsonl"
        self.records: list[dict[str, Any]] = []
        if self.path.exists():
            for ln in self.path.read_text(encoding="utf-8").splitlines():
                if ln.strip():
                    try:
                        self.records.append(json.loads(ln))
                    except json.JSONDecodeError:
                        continue
        self._fps = {r.get("fingerprint") for r in self.records}

    def add(self, recs: list[dict[str, Any]]) -> int:
        n = 0
        with self.path.open("a", encoding="utf-8") as fh:
            for r in recs:
                if r["fingerprint"] in self._fps:
                    continue
                r = {"id": f"F-{len(self.records) + 1:04d}", **r}
                fh.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
                self.records.append(r)
                self._fps.add(r["fingerprint"])
                n += 1
        if n:
            self.write_md()
        return n

    def since(self, days: int) -> list[dict[str, Any]]:
        cut = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        return [r for r in self.records if str(r.get("ts") or "") >= cut]

    def register_md(self, days: int = 7) -> str:
        recs = self.since(days)
        by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in recs:
            by_kind[r["kind"]].append(r)
        lines = [f"# Framework friction register — last {days} days", "",
                 f"{len(recs)} records ({len(self.records)} total) · products: " + ", ".join(sorted({r['product'] for r in recs}) or ["—"]), "",
                 "| kind | count | roles | last |", "|---|---|---|---|"]
        for k, rs in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"| {k} | {len(rs)} | {', '.join(sorted({str(r.get('role')) for r in rs}))} | {max(str(r.get('ts')) for r in rs)[:16]} |")
        for k, rs in sorted(by_kind.items(), key=lambda kv: -len(kv[1])):
            lines += ["", f"## {k} ({len(rs)})"]
            for r in sorted(rs, key=lambda r: str(r.get("ts")), reverse=True)[:8]:
                lines.append(f"- {r['id']} {str(r.get('ts'))[:16]} {r['product']} · {r['summary']}")
        return "\n".join(lines) + "\n"

    def write_md(self, days: int = 7) -> Path:
        p = self.dir / "register.md"
        p.write_text(self.register_md(days), encoding="utf-8")
        return p

    def export_public(self, out: Path, days: int = 30) -> Path:
        recs = self.since(days)
        by_day: dict[str, Counter] = defaultdict(Counter)
        for r in recs:
            by_day[str(r.get("ts"))[:10]][r["kind"]] += 1
        out.mkdir(parents=True, exist_ok=True)
        p = out / "friction.json"
        p.write_text(json.dumps({"generated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "days": days, "total": len(recs),
                                 "by_kind": dict(Counter(r["kind"] for r in recs)),
                                 "by_day": {d: dict(c) for d, c in sorted(by_day.items())},
                                 "recent": [{"ts": r.get("ts"), "product": r["product"], "role": r.get("role"), "kind": r["kind"],
                                             "summary": redact(r["summary"])[:200]} for r in sorted(recs, key=lambda r: str(r.get("ts")), reverse=True)[:50]]},
                                ensure_ascii=False, indent=1), encoding="utf-8")
        return p


def register_dir(st: Settings) -> Path:
    return st.data_dir / "framework"


def collect_all(st: Settings, products: list[Product]) -> int:
    reg = Register(register_dir(st))
    n = 0
    for p in products:
        s = Store(p.dir)
        try:
            n += reg.add(collect_product(p, s))
        finally:
            s.close()
    if n:
        log.info("friction: %d new records (%d total)", n, len(reg.records))
    return n
