"""Framework retro: a role reads the friction register and proposes changes to the framework itself.

Runs once per ``framework.retro_every_h`` (team.yaml) after the product cycles, not per product. Output:
- ``data/framework/retro/<date>.md`` + ``.yaml`` — the report and its proposals (always);
- GitHub Issues in ``framework.repo`` labelled ``framework.issue_label`` (when ``framework.issues`` is true and
  GITHUB_TOKEN is set), one per proposal, deduped by title against open issues — this is the register the owner sees
  without reading the server;
- tasks in the framework product (``framework.product``, a product whose repo is the framework in ``repo.mode: pr``) for
  proposals of ``risk: low`` and kind role_card / docs / config — the developer implements them in a branch, the tests
  must pass, a pull request is opened, the owner merges (L2). Dispatcher code changes stay proposals (an issue) — a bad
  change there would break the loop that is supposed to fix it.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from . import friction
from .config import Settings
from .store import Store, parse_ts, utcnow

log = logging.getLogger("team.retro")
ROLES_DIR = Path(__file__).parent / "roles"

AUTO_TASK_KINDS = ("role_card", "docs", "config")
GITHUB_API = "https://api.github.com"


def _cfg(st: Settings) -> dict[str, Any]:
    return dict(st.raw.get("framework") or {})


def due(st: Settings) -> bool:
    cfg = _cfg(st)
    every = float(cfg.get("retro_every_h", 24))
    if every <= 0:
        return False
    stamp = friction.register_dir(st) / "last-retro"
    if not stamp.exists():
        return True
    last = parse_ts(stamp.read_text(encoding="utf-8").strip())
    return not last or datetime.now(timezone.utc) - last >= timedelta(hours=every)


def _gh(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    tok = os.environ.get("GITHUB_TOKEN")
    if not tok:
        raise RuntimeError("GITHUB_TOKEN not set")
    req = urllib.request.Request(f"{GITHUB_API}{path}", method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                                          "Content-Type": "application/json", "User-Agent": "agent-team"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "null")


def open_issues(repo: str, label: str) -> list[dict[str, Any]]:
    try:
        return list(_gh("GET", f"/repos/{repo}/issues?state=open&labels={label}&per_page=100") or [])
    except (urllib.error.URLError, RuntimeError, OSError) as exc:
        log.warning("retro: cannot list issues of %s: %s", repo, exc)
        return []


def file_issue(repo: str, label: str, title: str, body: str) -> str | None:
    try:
        r = _gh("POST", f"/repos/{repo}/issues", {"title": title[:200], "body": body[:60000], "labels": [label]})
        return str(r.get("html_url"))
    except (urllib.error.URLError, RuntimeError, OSError) as exc:
        log.warning("retro: cannot file issue in %s: %s", repo, exc)
        return None


def retro_prompt(st: Settings, reg: friction.Register, previous: list[str], existing_titles: list[str]) -> str:
    cards = []
    for p in sorted(ROLES_DIR.glob("*.md")):
        cards.append(f"### {p.name}\n{p.read_text(encoding='utf-8')[:3500]}")
    scheme = (st.root / "SCHEME.md")
    return "\n\n".join([
        "# Framework retro",
        "You are the retro of an autonomous product team run by a dispatcher. Below: the friction register (where the framework got in the "
        "way of the roles in the last 7 days), the scheme of the loop, the role cards, previous proposals and open issues. Propose changes "
        "to the FRAMEWORK (role cards, schemas, docs, config, dispatcher, tooling) that would remove recurring friction. Not product work.",
        "## Friction register (last 7 days)\n" + reg.register_md(7)[:14000],
        "## SCHEME.md\n" + (scheme.read_text(encoding="utf-8")[:9000] if scheme.exists() else "(missing)"),
        "## Role cards\n" + "\n\n".join(cards)[:16000],
        "## Previous proposals (do not repeat)\n" + "\n".join(f"- {t}" for t in (previous + existing_titles)[:60]),
        "## Rules\n- Each proposal cites friction ids as evidence and names the file(s) to change and acceptance criteria that a developer "
        "can verify by running the tests or reading the file.\n- risk: low = role card text, docs, config values; medium = schema or tool "
        "behaviour; high = dispatcher code or state machine.\n- No proposal without at least 2 friction records or one blocking record. If the "
        "register is empty or noise, say so in no_action and propose nothing.\n- English, terse.",
    ])


def run_retro(st: Settings, runner: Any, dry_run: bool = False) -> dict[str, Any] | None:
    cfg = _cfg(st)
    reg = friction.Register(friction.register_dir(st))
    rdir = friction.register_dir(st) / "retro"
    rdir.mkdir(parents=True, exist_ok=True)
    stamp = friction.register_dir(st) / "last-retro"
    if not reg.since(7):
        log.info("retro: register empty for 7 days, nothing to do")
        stamp.write_text(utcnow(), encoding="utf-8")
        return None
    previous: list[str] = []
    for y in sorted(rdir.glob("*.yaml"))[-5:]:
        try:
            previous += [str(p.get("title")) for p in (yaml.safe_load(y.read_text(encoding="utf-8")) or {}).get("proposals") or []]
        except yaml.YAMLError:
            continue
    repo, label = str(cfg.get("repo") or ""), str(cfg.get("issue_label") or "friction")
    issues = open_issues(repo, label) if repo and cfg.get("issues") and not dry_run else []
    titles = [str(i.get("title")) for i in issues]
    prompt = retro_prompt(st, reg, previous, titles)
    if dry_run or runner is None:
        (rdir / "prompt-dry-run.md").write_text(prompt, encoding="utf-8")
        return None
    from .cycle import schema
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    rd = st.data_dir / "runs" / "_framework" / f"retro-{day}"
    rd.mkdir(parents=True, exist_ok=True)
    rr = runner.run("framework-retro", prompt, schema("retro-report"), max_usd=float(cfg.get("max_usd_per_run", 2.0)), cwd=st.root,
                    allowed_tools=["Read", "Glob", "Grep"], run_dir=rd, run_id=f"retro-{day}", max_turns=40)
    stamp.write_text(utcnow(), encoding="utf-8")
    if not (rr.ok and rr.structured):
        log.warning("retro: run failed: %s", rr.error)
        return None
    rep = rr.structured
    props = rep.get("proposals") or []
    (rdir / f"{day}.md").write_text(f"# Framework retro {day}\n\n{rep.get('summary_md', '')}\n\n" +
                                    "\n".join(f"## {p.get('title')}\n- kind: {p.get('kind')} · risk: {p.get('risk')} · evidence: {', '.join(p.get('evidence') or [])}\n"
                                              f"- files: {', '.join(p.get('files') or [])}\n- change: {p.get('change')}\n- acceptance:\n" +
                                              "\n".join(f"  - {a}" for a in p.get("acceptance") or []) for p in props) +
                                    ("\n\n## No action\n" + "\n".join(f"- {x}" for x in rep.get("no_action") or []) if rep.get("no_action") else ""),
                                    encoding="utf-8")
    (rdir / f"{day}.yaml").write_text(yaml.safe_dump({"date": day, "proposals": props, "cost_usd": rr.cost_usd}, allow_unicode=True, sort_keys=False), encoding="utf-8")
    filed, tasks = [], []
    for p in props:
        title = f"[retro] {p.get('title')}"
        url = None
        if repo and cfg.get("issues") and title not in titles:
            body = (f"**kind:** {p.get('kind')} · **risk:** {p.get('risk')} · **evidence:** {', '.join(p.get('evidence') or [])}\n\n"
                    f"**files:** {', '.join(p.get('files') or [])}\n\n**change:**\n{p.get('change')}\n\n**acceptance:**\n" +
                    "\n".join(f"- {a}" for a in p.get("acceptance") or []) + f"\n\n_Proposed by the framework retro role on {day} from the friction register._")
            url = file_issue(repo, label, title, body)
            if url:
                filed.append(url)
        slug = str(cfg.get("product") or "")
        if slug and p.get("risk") == "low" and p.get("kind") in AUTO_TASK_KINDS:
            pdir = st.products_dir / slug
            if (pdir / "product.yaml").exists():
                s = Store(pdir)
                try:
                    if not any(t.get("title") == title for t in s.tasks()):
                        t = s.create_task(title=title, type_="fix", created_by="framework-retro", role="developer", state="ready",
                                          problem=f"Friction: {', '.join(p.get('evidence') or [])}. {p.get('change')}",
                                          scope=", ".join(p.get("files") or []), acceptance=list(p.get("acceptance") or []),
                                          goal="framework", links=[url] if url else [], note="low-risk retro proposal")
                        tasks.append(t["id"])
                finally:
                    s.close()
    log.info("retro: %d proposals, %d issues filed, %d tasks", len(props), len(filed), len(tasks))
    return {"proposals": len(props), "issues": filed, "tasks": tasks, "cost_usd": rr.cost_usd}
