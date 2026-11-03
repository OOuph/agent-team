"""The marketer runs channels, not single posts (03.11): plan tasks accepted by the plan file without a post, memory of
what went out, visits by channel from the site's metrics, and the PO's view of the plan. No network, no models."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import jsonschema

from team import prompts
from team.cycle import Cycle, is_plan_task, metrics_md
from team.runner import RunResult
from team.store import Store

from test_store_cycle import FakeRunner, _product, _settings

PLAN_TEXT = """## Channels
- telegram: subscribers, daily card, 7/week, G1
- bluesky: open-web developers, one fact, 7/week, G1/G5

## Rubrics
- daily card · weekly digest

## Slots
| date (UTC) | channel | rubric | angle / page | goal |
|---|---|---|---|---|
| 2026-11-04 14:00 | telegram | daily card | 3 breaking releases, /today | G1 |
| 2026-11-04 14:05 | bluesky | daily card | vitest 4 drops node 18 | G1 |

## Drop
- devto until the first weekly digest has substance (0 posts, no data)

## Why
- bluesky brought 3 human sources by ?src= in two days; telegram none yet (posting since yesterday)
"""


class PlanRunner(FakeRunner):
    """The marketer writes marketing-plan.md in its work dir; the dispatcher must accept the task by the file."""

    def __init__(self, write_plan: bool = True):
        super().__init__()
        self.write_plan = write_plan
        self.prompts: list[str] = []

    def run(self, role, prompt, schema, *, max_usd, cwd, allowed_tools=None, env_extra=None, run_dir=None, run_id="", max_turns=None, **kw):
        if role == "marketer":
            self.calls.append(role)
            self.prompts.append(prompt)
            if self.write_plan:
                (Path(cwd) / prompts.MARKETING_PLAN).write_text(PLAN_TEXT, encoding="utf-8")
            out = {"status": "done", "summary": "plan written", "changes": [prompts.MARKETING_PLAN], "how_to_verify": ["sections"],
                   "self_check": [{"criterion": "plan", "met": True, "evidence": "file"}], "proposals": []}
            return RunResult(True, out, {"total_cost_usd": 0.1}, 0.1, 3, None, session_id="s", duration_s=1.0)
        return super().run(role, prompt, schema, max_usd=max_usd, cwd=cwd, allowed_tools=allowed_tools, env_extra=env_extra,
                           run_dir=run_dir, run_id=run_id, max_turns=max_turns, **kw)


def _with_channels(p):
    (p.dir / "product.yaml").write_text("slug: demo\nname: Demo\nhost: ''\nrepo:\n  url: ''\n  branch: main\n"
                                        "channels:\n  telegram: '@demo'\n  bluesky: demo.bsky.social\n  devto: demo\n")
    p.raw["channels"] = {"telegram": "@demo", "bluesky": "demo.bsky.social", "devto": "demo"}
    return p


def _metrics(p, by_src_days: dict[str, dict] | None = None, totals_by_src: dict | None = None):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    data = {"host": "demo", "generated": today, "window": {"since": today, "until": today},
            "days": {today: {"humans": {"sources": 5, "engaged": 2, "returns": 0}, "agents": {"requests": 1}, "crawlers": {"requests": 2},
                            "targets": {}, **({"by_src": by_src_days} if by_src_days is not None else {})}},
            "totals": {"top_pages": [{"path": "/", "requests": 4}], **({"by_src": totals_by_src} if totals_by_src is not None else {})}}
    (p.dir / "metrics").mkdir(exist_ok=True)
    (p.dir / "metrics" / f"{today}.json").write_text(json.dumps(data), encoding="utf-8")
    return data


def test_plan_task_accepted_without_post(tmp_path: Path):
    st = _settings(tmp_path)
    p = _with_channels(_product(st))
    s = Store(p.dir)
    t = s.create_task(title="Marketing plan, week 1", type_="marketing", problem="no plan", scope="7 days per channel",
                      acceptance=["marketing-plan.md has Channels, Rubrics, Slots, Drop, Why"], goal="G1", created_by="po",
                      state="ready", role="marketer", plan=True)
    assert is_plan_task(t) and t["plan"] is True
    r = PlanRunner()
    cy = Cycle(st, p, r, "c1")
    assert cy.content_without_post(t)                      # no plan file yet → nothing to accept
    cy.phase_execute()
    t = s.task(t["id"])
    assert t["state"] == "accepted", t["history"]
    assert t["results"][-1]["posted"] == 0 and r.calls.count("marketer") == 1 and r.calls.count("editor") == 0
    plan = prompts.plan_path(p)
    assert plan.exists() and "## Slots" in plan.read_text(encoding="utf-8")
    assert not cy.content_without_post(t)                  # accepted by the file, no post needed
    assert s.db.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
    assert not [q for q in s.questions(["open"]) if q.get("to") == "po"]
    assert "MARKETING PLAN TASK" in r.prompts[0] and "## Slots" in r.prompts[0] and "PUBLISHED IN THE LAST 7 DAYS" in r.prompts[0]
    s.close()


def test_plan_task_without_file_asks_po(tmp_path: Path):
    st = _settings(tmp_path)
    p = _with_channels(_product(st))
    s = Store(p.dir)
    t = s.create_task(title="plan", type_="marketing", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po",
                      state="ready", role="marketer", plan=True)
    Cycle(st, p, PlanRunner(write_plan=False), "c1").phase_execute()
    t = s.task(t["id"])
    assert t["state"] == "blocked" and not prompts.plan_path(p).exists()
    qs = [q for q in s.questions(["open"]) if q.get("to") == "po"]
    assert qs and "marketing-plan.md" in qs[0]["question"]
    s.close()


def test_marketer_prompt_has_memory_and_by_src(tmp_path: Path):
    st = _settings(tmp_path)
    p = _with_channels(_product(st))
    s = Store(p.dir)
    s.record_post("telegram", "1", "https://t.me/demo/1", "T-0001", text="Three packages shipped breaking changes today: vitest 4 drops Node 18.")
    s.record_post("bluesky", "at://x", "https://bsky.app/profile/demo/post/x", "T-0001", text="vitest 4 drops Node 18 — the quote and the link.")
    _metrics(p, by_src_days={"bluesky": {"requests": 9, "sources": 9, "human_sources": 3, "engaged_sources": 1}})
    s.add_observation({"title": "Reply on Bluesky: which packages?", "classification": "feedback", "source": "bluesky",
                       "url": "https://bsky.app/x", "actual": "A reader asked which packages were counted.", "fingerprint": "fb1"})
    t = s.create_task(title="Daily card 04.11", type_="marketing", problem="p", scope="s", acceptance=["a"], goal="G1",
                      created_by="po", state="ready", role="marketer", not_before="2026-11-04T14:00:00+00:00")
    mem = prompts.marketing_memory(p, s)
    assert [x["channel"] for x in mem["recent_posts"]] == ["bluesky", "telegram"]
    assert mem["recent_posts"][0]["text"].startswith("vitest 4 drops Node 18") and len(mem["recent_posts"][0]["text"]) <= 120
    rows = {r["channel"]: r for r in mem["channels"]}
    assert rows["bluesky"]["human_sources"] == 3 and rows["bluesky"]["posts"] == 1 and "keep or raise" in rows["bluesky"]["verdict"]
    assert rows["telegram"]["human_sources"] == 0 and "too early" in rows["telegram"]["verdict"]
    assert rows["devto"]["posts"] == 0 and "no posts yet" in rows["devto"]["verdict"]
    assert mem["feedback"][0]["title"].startswith("Reply on Bluesky")
    prompt = prompts.marketer_prompt(p, t, p.raw["channels"], [], lessons=["lead with the event"], memory=mem)
    for needle in ("PUBLISHED IN THE LAST 7 DAYS", "https://t.me/demo/1", "vitest 4 drops Node 18", "VISITS BY ?src=",
                   "| bluesky | 1 | 3 | 1 |", "WHAT PEOPLE SAID", "which packages", "no plan yet"):
        assert needle in prompt, needle
    # with a plan on disk the post task is told it is one slot of it
    prompts.plan_path(p).write_text(PLAN_TEXT, encoding="utf-8")
    prompt2 = prompts.marketer_prompt(p, t, p.raw["channels"], [], memory=prompts.marketing_memory(p, s))
    assert "CURRENT MARKETING PLAN" in prompt2 and "one slot of this plan" in prompt2 and "## Slots" in prompt2
    s.close()


def test_channel_rule_less_often_after_two_weeks(tmp_path: Path):
    st = _settings(tmp_path)
    p = _with_channels(_product(st))
    s = Store(p.dir)
    s.db.execute("INSERT INTO posts(day, channel, post_id, url, task_id, ts, text) VALUES('2026-01-01','devto','1','u','T-0001','2026-01-01T00:00:00+00:00','old')")
    s.db.commit()
    _metrics(p, totals_by_src={"telegram": {"requests": 2, "sources": 2, "human_sources": 1, "engaged_sources": 0}})
    rows = {r["channel"]: r for r in prompts.channel_report(p, s)}
    assert "post less often" in rows["devto"]["verdict"] and rows["devto"]["posts"] == 0
    assert rows["telegram"]["human_sources"] == 1          # totals fallback when days carry no by_src
    s.close()


def test_po_prompt_shows_plan_or_asks_for_one(tmp_path: Path):
    st = _settings(tmp_path)
    p = _with_channels(_product(st))
    s = Store(p.dir)
    kw = dict(cycle_id="c1", observations=[], questions=[], metrics_md="", spent_today=0.0, cap=5.0, max_ready=3, idle=True)
    pr = prompts.po_prompt(p, s, **kw)
    assert "MARKETING PLAN — NONE" in pr and "`plan: true`" in pr and "visits by ?src=" in pr
    prompts.plan_path(p).write_text(PLAN_TEXT, encoding="utf-8")
    pr2 = prompts.po_prompt(p, s, **kw)
    assert "cut its slots into post tasks" in pr2 and "2026-11-04 14:00 | telegram" in pr2
    # a product without channels (the framework itself) gets no marketing block
    p.raw["channels"] = {}
    assert "MARKETING PLAN" not in prompts.po_prompt(p, s, **kw)
    s.close()


def test_po_schema_accepts_plan_flag_and_apply_po_keeps_it(tmp_path: Path):
    schema = json.loads((Path(__file__).parent.parent / "team" / "schemas" / "po-decision.schema.json").read_text(encoding="utf-8"))
    task = {"title": "Marketing plan, week of 04.11", "type": "marketing", "problem": "no plan", "scope": "7 days",
            "acceptance": ["marketing-plan.md has Slots"], "negative_cases": [], "goal": "G1", "priority": "P2", "size": "S",
            "role": "marketer", "plan": True}
    decision = {"decisions": [], "answers": [], "ready_tasks": [task], "owner_questions": [], "no_new_tasks": False, "digest_md": "ok"}
    jsonschema.validate(decision, schema)
    st = _settings(tmp_path)
    p = _with_channels(_product(st))
    cy = Cycle(st, p, FakeRunner(), "c1")
    cy.apply_po(decision, [])
    s = Store(p.dir)
    t = s.tasks()[0]
    assert t["plan"] is True and is_plan_task(t)
    # the flag is ignored for other roles
    cy.apply_po({**decision, "ready_tasks": [{**task, "title": "code", "role": "developer"}]}, [])
    assert s.tasks()[1]["plan"] is False
    s.close()


def test_posts_table_migration_and_text(tmp_path: Path):
    import sqlite3
    d = tmp_path / "p"
    d.mkdir()
    db = sqlite3.connect(str(d / "team.db"))
    db.execute("CREATE TABLE posts(day TEXT, channel TEXT, post_id TEXT, url TEXT, task_id TEXT, ts TEXT)")
    db.execute("INSERT INTO posts VALUES('2026-11-02','telegram','3','https://t.me/x/3','T-0004','2026-11-02T14:00:00+00:00')")
    db.commit(); db.close()
    s = Store(d)                                           # an existing server store gets the text column
    s.record_post("bluesky", "at://y", "https://bsky.app/y", "T-0005", text="  new   text ")
    posts = s.recent_posts(days=400)
    assert posts[0]["channel"] == "bluesky" and posts[0]["text"] == "new text"
    assert posts[-1]["channel"] == "telegram" and posts[-1]["text"] == ""
    assert s.first_post_day("telegram") == "2026-11-02"
    s.close()


def test_metrics_md_lists_by_src():
    md = metrics_md({"host": "demo", "generated": "now", "days": {}, "totals": {"by_src": {"bluesky": {"human_sources": 3, "engaged_sources": 1, "requests": 9}}}})
    assert "Visits by ?src=" in md and "bluesky: 3/1/9" in md


def test_po_repo_only_is_kept(tmp_path: Path):
    schema = json.loads((Path(__file__).parent.parent / "team" / "schemas" / "po-decision.schema.json").read_text(encoding="utf-8"))
    task = {"title": "tests", "type": "feature", "problem": "p", "scope": "mcp/", "acceptance": ["tests under mcp/ pass"],
            "negative_cases": [], "goal": "G1", "priority": "P2", "size": "S", "role": "developer", "repo_only": True}
    decision = {"decisions": [], "answers": [], "ready_tasks": [task], "owner_questions": [], "no_new_tasks": False, "digest_md": "ok"}
    jsonschema.validate(decision, schema)
    p = _product(_settings(tmp_path))
    Cycle(_settings(tmp_path), p, FakeRunner(), "c1").apply_po(decision, [])
    s = Store(p.dir)
    assert s.tasks()[0]["repo_only"] is True
    s.close()


def test_raw_github_only_how_to_verify_sets_repo_only_and_uses_checkout(tmp_path: Path, monkeypatch):
    from team import gitops
    from team.cycle import only_raw_github_urls
    assert only_raw_github_urls(["https://raw.githubusercontent.com/o/r/abc/mcp/a.py"])
    assert not only_raw_github_urls(["https://raw.githubusercontent.com/o/r/abc/a.py", "https://demo.example/"])
    assert not only_raw_github_urls([])
    st = _settings(tmp_path)
    p = _product(st)
    s = Store(p.dir)
    t = s.create_task(title="mcp tests", type_="feature", problem="p", scope="mcp/", acceptance=["tests pass"], goal="G1",
                      created_by="po", state="ready", role="developer")
    s.add_result(t["id"], {"status": "done", "how_to_verify": ["https://raw.githubusercontent.com/o/r/abc/mcp/a.py"]})
    t = s.task(t["id"])
    assert not t.get("repo_only")
    seen = {}

    class R(FakeRunner):
        def run(self, role, prompt, schema, *, cwd, allowed_tools=None, **kw):
            seen.update(cwd=cwd, tools=allowed_tools, prompt=prompt)
            return RunResult(ok=False, structured=None, error="stop", raw="", cost_usd=0.0, num_turns=0)

    def fake_checkout(repo, sha, dest):
        Path(dest).mkdir(parents=True, exist_ok=True)
        return Path(dest)
    monkeypatch.setattr(gitops, "readonly_checkout", fake_checkout)
    t["released_sha"] = "abc123"
    Cycle(st, p, R(), "c1").review_task(t)
    assert s.task(t["id"])["repo_only"] is True
    assert str(seen["cwd"]).endswith("checkout") and seen["tools"] and "READ-ONLY CHECKOUT" in seen["prompt"]
    s.close()
