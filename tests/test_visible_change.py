"""A visible change is reviewed before it reaches the players (rework slice 5, owner 26.11).

Before: `in_progress → released` (merged AND deployed) `→ review`, so players saw a change before anybody had judged it;
on the night of 25–26.11 the team re-laid the first screen of ai-battles with no new function, the review passed it
«as in the mock-up», and the owner wrote the principle «редизайн делаем только при новом функционале».

`cycle.visible` (off by default):
  review_before_merge   a task the PO marked `visible: true` is not merged when its author reports done: its commit
                        stays on `task/<id>`, the review plays it on a server started locally from that commit, and only
                        a pass merges it (then the usual deploy). A fail returns it to the author, nothing was live.
  redesign_gate         a task marked `redesign: true` (it changes the layout or look of an existing screen) without a
                        `new_function` is not made Ready: it waits in `proposed` for the owner's word."""
from __future__ import annotations

import json
from pathlib import Path

from team import config as C
from team import gitops, playtest
from team.cycle import Cycle
from team.runner import RunResult
from team.store import Store

from test_playtest import _game, _settings as _base_settings

ROLES = Path(C.__file__).parent / "roles"
SCHEMAS = Path(C.__file__).parent / "schemas"


def _settings(tmp_path: Path, **visible: bool) -> C.Settings:
    st = _base_settings(tmp_path)
    st.raw["cycle"] = {**st.raw["cycle"], "visible": visible}
    return st


class Runner:
    """developer: commits one file and reports done; tester / reviewer: the scripted verdict."""

    def __init__(self, verdict: str = "pass"):
        self.verdict, self.calls, self.prompts, self.env = verdict, [], {}, {}

    def run(self, role, prompt, schema, *, cwd, **kw):
        self.calls.append(role)
        self.prompts[role] = prompt
        self.env[role] = dict(kw.get("env_extra") or {})
        if role in ("developer", "designer"):
            (Path(cwd) / "src").mkdir(exist_ok=True)
            (Path(cwd) / "src" / "screen.ts").write_text("new layout\n")
            gitops.git(Path(cwd), "add", "-A"); gitops.git(Path(cwd), "commit", "-q", "-m", "T-0001: screen")
            out = {"status": "done", "summary": "screen changed", "changes": ["src/screen.ts"], "how_to_verify": ["open the screen"], "self_check": []}
        else:
            out = {"verdict": self.verdict, "steps": [], "criteria": [{"criterion": "c", "met": self.verdict == "pass", "evidence": "seen"}],
                   "scenarios": [{"name": "a", "status": "passed", "evidence": "e"}], "screenshots": [], "summary": "checked it. really.",
                   "first_failure": "" if self.verdict == "pass" else "the button is not there"}
        return RunResult(True, out, {"total_cost_usd": 0.1}, 0.1, 3, None, session_id="s", duration_s=1.0)


def _task(p: C.Product, **fields) -> str:
    s = Store(p.dir)
    t = s.create_task(title="Cups button on the first screen", type_="milestone", problem="p", scope="src/", acceptance=["the button opens cups"],
                      goal="M0", created_by="po", state="ready")
    t.update(fields)
    s.save_task(t)
    s.close()
    return t["id"]


def _on_base(p: C.Product, path: str) -> bool:
    return path in gitops.git(p.repo_dir, "ls-tree", "-r", "--name-only", p.repo_branch)


def _rt(**kw) -> dict:
    return {"title": "First screen by the mock-up", "type": "milestone", "problem": "p", "scope": "s", "acceptance": ["a"], "negative_cases": [],
            "goal": "M0", "priority": "P2", "size": "S", "role": "developer", **kw}


def _po(st, p, tasks: list[dict]) -> None:
    c = Cycle(st, p, None, "c1")
    c.apply_po({"decisions": [], "answers": [], "owner_questions": [], "no_new_tasks": False, "digest_md": "d", "ready_tasks": tasks}, [])
    c.store.close()


# ------------------------------------------------------------------ the mark

def test_schema_and_defaults():
    props = json.loads((SCHEMAS / "po-decision.schema.json").read_text())["properties"]["ready_tasks"]["items"]["properties"]
    assert props["visible"]["type"] == "boolean" and props["redesign"]["type"] == "boolean" and props["new_function"]["type"] == "string"
    assert C.DEFAULTS["cycle"]["visible"] == {"review_before_merge": False, "redesign_gate": False}


def test_the_marks_are_kept_on_the_task_whatever_the_switches(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st)
    _po(st, p, [_rt(visible=True, redesign=True, new_function="")])
    s = Store(p.dir)
    t = s.tasks()[0]
    assert t["state"] == "ready" and t["visible"] is True and t["redesign"] is True      # gate off: created as before
    s.close()


# ------------------------------------------------------------------ redesign only with a new function

def test_redesign_without_a_new_function_waits_for_the_owner(tmp_path: Path):
    st = _settings(tmp_path, redesign_gate=True)
    p = _game(st)
    _po(st, p, [_rt(visible=True, redesign=True), _rt(title="Cups button", visible=True, redesign=True, new_function="the player opens the cups table"),
                _rt(title="Server metric")])
    s = Store(p.dir)
    held, ok, plain = s.tasks()
    assert held["state"] == "proposed" and "redesign without a new function" in held["history"][-1]["note"]
    assert ok["state"] == "ready" and ok["new_function"] == "the player opens the cups table" and plain["state"] == "ready"
    q = [q for q in s.questions(["open"]) if q["to"] == "owner"]
    assert len(q) == 1 and held["id"] in q[0]["question"] and "team task move" in q[0]["question"] and not q[0]["blocks"]
    assert any(e["kind"] == "task.redesign_held" and e["subject"] == held["id"] for e in s.events())
    assert held["id"] in (p.dir / "digests" / "c1.md").read_text(encoding="utf-8")
    s.close()


def test_cards_carry_the_owners_principle():
    for name in ("po", "reviewer", "tester"):
        text = " ".join((ROLES / f"{name}.md").read_text(encoding="utf-8").split())
        assert "редизайн делаем только при новом функционале" in text, name
    po = (ROLES / "po.md").read_text(encoding="utf-8")
    assert "`visible: true`" in po and "`redesign: true`" in po and "`new_function`" in po
    assert "redesign without a new function" in " ".join((ROLES / "reviewer.md").read_text(encoding="utf-8").split())


# ------------------------------------------------------------------ review before the merge

def test_off_a_visible_task_is_merged_at_once_as_before(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st)
    tid = _task(p, visible=True)
    cyc = Cycle(st, p, Runner(), "c1"); cyc.run(["execute"]); cyc.store.close()
    s = Store(p.dir)
    assert s.task(tid)["state"] == "released" and _on_base(p, "src/screen.ts") and not s.task(tid).get("pre_merge")
    s.close()


def test_a_visible_task_is_not_merged_until_the_review_passes(tmp_path: Path):
    st = _settings(tmp_path, review_before_merge=True)
    p = _game(st)
    tid, other = _task(p, visible=True), None
    r = Runner("pass")
    cyc = Cycle(st, p, r, "c1"); cyc.run(["execute"]); cyc.store.close()
    s = Store(p.dir)
    t = s.task(tid)
    assert t["state"] == "review" and t["pre_merge"] is True and t["released_sha"]
    assert not _on_base(p, "src/screen.ts")                             # nothing of it is on the base branch, so nothing can be deployed
    assert "src/screen.ts" in gitops.git(p.repo_dir, "ls-tree", "-r", "--name-only", t["released_sha"])
    assert any(e["kind"] == "review.before_merge" for e in s.events())
    s.close()
    cyc = Cycle(st, p, r, "c2"); cyc.run(["review"]); cyc.store.close()
    assert r.calls == ["developer", "tester"]
    assert r.env["tester"]["TEAM_PLAY_BASE_URL"].startswith("http://127.0.0.1:")          # a server started from the branch commit
    assert "REVIEW BEFORE THE MERGE" in r.prompts["tester"] and "not on the live host" in r.prompts["tester"]
    s = Store(p.dir)
    t = s.task(tid)
    assert t["state"] == "accepted" and t["accepted_at"] and not t.get("pre_merge") and _on_base(p, "src/screen.ts")
    assert t["released_sha"] == gitops.head(p.repo_dir)                 # the merge commit: what the deploy takes
    assert [h["to"] for h in t["history"]][-2:] == ["review", "accepted"] and "merged" in t["history"][-1]["note"]
    s.close()


def test_a_failed_review_returns_the_task_and_nothing_was_merged(tmp_path: Path):
    st = _settings(tmp_path, review_before_merge=True)
    p = _game(st)
    tid = _task(p, visible=True)
    r = Runner("fail")
    cyc = Cycle(st, p, r, "c1"); cyc.run(["execute"]); cyc.store.close()
    cyc = Cycle(st, p, r, "c2"); cyc.run(["review"]); cyc.store.close()
    s = Store(p.dir)
    t = s.task(tid)
    assert t["state"] == "in_progress" and t["review_fails"] == 1 and "the button is not there" in t["last_review"]["first_failure"]
    assert not _on_base(p, "src/screen.ts")
    assert gitops.git(p.repo_dir, "rev-parse", "--verify", f"task/{tid}").strip()         # the branch is there for the next run
    s.close()


def test_a_task_without_the_mark_goes_the_old_way_when_the_switch_is_on(tmp_path: Path):
    st = _settings(tmp_path, review_before_merge=True)
    p = _game(st)
    tid = _task(p)
    cyc = Cycle(st, p, Runner(), "c1"); cyc.run(["execute"]); cyc.store.close()
    s = Store(p.dir)
    assert s.task(tid)["state"] == "released" and _on_base(p, "src/screen.ts")
    s.close()


def test_review_before_merge_uses_a_local_server_even_when_a_host_is_deployed(tmp_path: Path, monkeypatch):
    st = _settings(tmp_path, review_before_merge=True)
    p = _game(st, deploy="service", site_base="https://game.example")
    seen = {}
    real = playtest.start

    def spy(product, repo_dir, sha, work, **kw):
        seen.update(kw)
        product.raw["deploy"] = {"mode": "pending"}                     # the test has no host: start it locally either way
        return real(product, repo_dir, sha, work, **{k: v for k, v in kw.items() if k != "force_local"})

    monkeypatch.setattr(playtest, "start", spy)
    tid = _task(p, visible=True)
    s = Store(p.dir)
    s.transition(tid, "review", "dispatcher", force=True, released_at="2020-01-01T00:00:00+00:00", released_sha=gitops.head(p.repo_dir), pre_merge=True)
    s.add_result(tid, {"run_id": "r", "status": "done", "summary": "done", "changes": [], "how_to_verify": ["play"], "self_check": []})
    s.close()
    cyc = Cycle(st, p, Runner(), "c1"); cyc.review_task(Store(p.dir).task(tid)); cyc.store.close()
    assert seen.get("force_local") is True
    assert playtest.live_base_url(_game(_settings(tmp_path / "b"), deploy="service", site_base="https://g.example")) == "https://g.example"
