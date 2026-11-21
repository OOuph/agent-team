"""Time budget per role and product, and continuation instead of restart (owner 21.11, frictions F23/F30).

A developer run on the first game product hit `claude.timeout_s: 1500` six times in one day; the uncommitted work stayed
in the checkout and the next attempt's `git reset --hard` destroyed it. Here: limit resolution (global → team role →
repository kind → product role, hard ceiling), the budget block in the prompt, the wip commit and the continuation
prompt, `max_continuations` → a question to the PO with the run profiles, and the release by a normal run. FakeRunner only."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from team import cli
from team import config as C
from team import gitops, prompts
from team.cycle import INTERRUPTED, Cycle
from team.runner import EVENT_TS_KEY, RunResult
from team.store import Store

T0 = 1_800_000_000.0
SPEC = "npx playwright test tests/e2e/two-players.spec.ts"


def _settings(tmp_path: Path, over: dict | None = None) -> C.Settings:
    base = copy.deepcopy(C.DEFAULTS)                                   # the tests below change `claude` in place
    raw = C._merge(base, C._merge({"cycle": {"review_after_min": 0, "persona_every_n_cycles": 0, "digest_hour_utc": 99}}, over or {}))
    return C.Settings(raw, tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")


def _product(st: C.Settings, slug: str = "fleets", extra: str = "", kind: str = "service") -> C.Product:
    d = st.products_dir / slug
    d.mkdir(parents=True)
    (d / "product.yaml").write_text(f"slug: {slug}\nname: {slug}\nhost: ''\nrepo:\n  url: ''\n  branch: main\n  kind: {kind}\n{extra}", encoding="utf-8")
    (d / "VISION.md").write_text("# VISION\n\nGoals: G1.\n", encoding="utf-8")
    p = C.load_product(st, d)
    repo = gitops.init_local(p.repo_dir)
    (repo / "README.md").write_text("game server: npm run start\n", encoding="utf-8")
    gitops.git(repo, "add", "-A"); gitops.git(repo, "commit", "-q", "-m", "init")
    return p


def _task(p: C.Product, role: str = "developer") -> str:
    s = Store(p.dir)
    t = s.create_task(title="Two players see each other's move", type_="defect", problem="p", scope="src/", acceptance=["B sees A's move"],
                      goal="G1", created_by="po", role=role, state="ready")
    s.close()
    return t["id"]


def _events(n_spec: int = 5, spec_s: float = 290.0) -> list[dict]:
    """A run that spends its time re-running one Playwright spec; the last call has no result (the kill cut it)."""
    out, t = [], T0
    for i in range(n_spec):
        out.append({"type": "assistant", EVENT_TS_KEY: t, "message": {"content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {"command": SPEC}}]}})
        t += spec_s
        if i < n_spec - 1:
            out.append({"type": "user", EVENT_TS_KEY: t, "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "1 failed"}]}})
    return out


def _killed(limit: int = 2700, kind: str = "timeout") -> RunResult:
    err = f"timeout after {limit}s" if kind == "timeout" else f"inactive for {limit}s"
    return RunResult(False, None, None, None, 0, err, events=_events(), duration_s=float(limit), killed=kind, started=T0)


def _done(summary: str = "done") -> RunResult:
    out = {"status": "done", "summary": summary, "changes": ["src/server.ts"], "how_to_verify": ["npm test"],
           "self_check": [{"criterion": "B sees A's move", "met": True, "evidence": "spec passes"}]}
    return RunResult(True, out, {"total_cost_usd": 0.1}, 0.1, 5, None, session_id="s", duration_s=600.0)


class FakeRunner:
    """The developer/designer follows a script, one step per run: "kill" (writes files, commits nothing, the limit stops
    it), "inactive", "done" (commits), "partial" (leaves work uncommitted), "error" (a failed run that is not a kill)."""

    def __init__(self, script: list[str]):
        self.script, self.calls, self.prompts, self.kw = list(script), [], [], []

    def run(self, role, prompt, schema, *, max_usd, cwd, **kw):
        self.calls.append(role)
        if role == "po":
            self.prompts.append(("po", prompt))
            return RunResult(True, {"decisions": [], "answers": [], "owner_questions": [], "no_new_tasks": True, "digest_md": "ok", "ready_tasks": []},
                             {"total_cost_usd": 0.1}, 0.1, 1, None, duration_s=5.0)
        self.prompts.append((role, prompt))
        self.kw.append(kw)
        step, n, repo = self.script.pop(0), len([c for c in self.calls if c == role]), Path(cwd)
        (repo / "src").mkdir(exist_ok=True)
        if step in ("kill", "inactive"):
            (repo / "src" / "server.ts").write_text(f"// attempt {n}: half of the room logic\n", encoding="utf-8")
            (repo / "src" / f"draft{n}.ts").write_text("export const todo = 1\n", encoding="utf-8")
            return _killed(int(kw["timeout_s"]) if step == "kill" else int(kw["inactivity_s"]), "timeout" if step == "kill" else "inactivity")
        if step == "error":
            (repo / "src" / "lost.ts").write_text("x\n", encoding="utf-8")
            return RunResult(False, None, {"is_error": True}, 0.2, 60, "error_max_turns", duration_s=300.0)
        if step == "partial":
            (repo / "src" / "half.ts").write_text("export const half = true\n", encoding="utf-8")
            out = {"status": "partial", "summary": "server side done; left: the client and the e2e spec", "changes": ["src/half.ts"], "how_to_verify": [],
                   "self_check": [{"criterion": "B sees A's move", "met": False, "evidence": "client missing"}]}
            return RunResult(True, out, {"total_cost_usd": 0.1}, 0.1, 5, None, duration_s=900.0)
        (repo / "src" / "client.ts").write_text("export const client = true\n", encoding="utf-8")
        gitops.git(repo, "add", "-A"); gitops.git(repo, "commit", "-q", "-m", "T-0001: finish the room")
        return _done()


def _exec(st: C.Settings, p: C.Product, r: FakeRunner, cycle_id: str) -> None:
    cyc = Cycle(st, p, r, cycle_id)
    cyc.run(["execute"])
    cyc.store.close()


def _subject(repo: Path, ref: str) -> str:
    return gitops.git(repo, "log", "-1", "--format=%s", ref)


# ------------------------------------------------------------------ 1. the time budget per role and product

def test_limits_resolve_global_then_team_role_then_repo_kind_then_product(tmp_path: Path, caplog):
    st = _settings(tmp_path)
    game, site = _product(st, "fleets"), _product(st, "site", kind="static")
    # shipped defaults: only the developer of a service repository gets more than today
    assert game.run_limits("developer")["timeout_s"] == 2700 and "roles_by_repo_kind.service.developer" in game.run_limits("developer")["source"]["timeout_s"]
    for prod, role in ((game, "designer"), (game, "po"), (game, "reviewer"), (site, "developer"), (site, "marketer")):
        lim = prod.run_limits(role)
        assert (lim["timeout_s"], lim["inactivity_s"], lim["source"]["timeout_s"]) == (1500, 300, "claude"), (prod.slug, role)
    # team.yaml: the global number, then a role
    st.raw["claude"]["timeout_s"], st.raw["claude"]["roles"] = 1200, {"designer": {"timeout_s": 1800, "inactivity_s": 240}, "developer": {"timeout_s": 2000}}
    assert site.run_limits("po")["timeout_s"] == 1200
    assert (game.run_limits("designer")["timeout_s"], game.run_limits("designer")["inactivity_s"]) == (1800, 240)
    assert site.run_limits("developer")["timeout_s"] == 2000           # team role
    assert game.run_limits("developer")["timeout_s"] == 2700           # the repository-kind default is the more specific layer
    # product.yaml wins over everything
    game.raw["roles"] = {"developer": {"timeout_s": 3300, "inactivity_s": 600}, "designer": {"inactivity_s": 120}}
    lim = game.run_limits("developer")
    assert (lim["timeout_s"], lim["inactivity_s"], lim["source"]["timeout_s"]) == (3300, 600, "product.yaml roles.developer")
    assert (game.run_limits("designer")["timeout_s"], game.run_limits("designer")["inactivity_s"]) == (1800, 120)
    # the hard ceiling: a higher value is clamped with a WARNING; nonsense is ignored with a WARNING
    game.raw["roles"] = {"developer": {"timeout_s": 7200}, "designer": {"timeout_s": "long", "inactivity_s": -5}}
    with caplog.at_level("WARNING", logger="team.config"):
        lim = game.run_limits("developer")
        des = game.run_limits("designer")
    assert lim["timeout_s"] == C.RUN_TIMEOUT_CEILING_S == 3600 and lim["clamped_from"] == 7200 and "above the hard ceiling" in caplog.text
    assert (des["timeout_s"], des["inactivity_s"]) == (1800, 240) and "not a positive number" in caplog.text
    game.raw["roles"] = {"developer": {"timeout_s": 200}}              # the watchdog is never longer than the hard limit
    assert game.run_limits("developer")["inactivity_s"] == 200


def test_max_continuations_setting(tmp_path: Path):
    st = _settings(tmp_path)
    assert _product(st, "a").max_continuations == 3
    st.raw["cycle"]["max_continuations"] = 2
    assert _product(st, "b").max_continuations == 2
    assert _product(st, "c", "cycle:\n  max_continuations: 5\n  every_min: 10\n").max_continuations == 5
    assert _product(st, "d", "cycle:\n  max_continuations: 0\n").max_continuations == 0


def test_run_gets_its_limit_and_the_prompt_names_the_budget(tmp_path: Path):
    st = _settings(tmp_path)
    game, site = _product(st, "fleets"), _product(st, "site", kind="static")
    for p, minutes, wrap, secs in ((game, 45, 30, 2700), (site, 25, 17, 1500)):
        tid = _task(p)
        r = FakeRunner(["done"])
        _exec(st, p, r, "c1")
        role, prompt = r.prompts[0]
        assert "# TIME BUDGET OF THIS RUN" in prompt and f"stops this run {minutes} minutes after its start" in prompt
        assert f"≈ {wrap} minutes" in prompt and "`status: partial`" in prompt and "lists what is left" in prompt
        assert "# CONTINUATION" not in prompt
        assert r.kw[0]["timeout_s"] == secs and r.kw[0]["inactivity_s"] == 300
        rec = json.loads((p.runs_dir / "c1" / f"dev-{tid}" / "run.json").read_text(encoding="utf-8"))
        assert rec["limits"]["timeout_s"] == secs and rec["killed"] is None and rec["profile"]["calls"] == 0
    # a role outside a repository has no budget block and keeps the global limit
    s = Store(game.dir)
    assert "TIME BUDGET" not in prompts.po_prompt(game, s, cycle_id="c", observations=[], questions=[], metrics_md="", spent_today=0.0, cap=20.0, max_ready=3, idle=True)
    s.close()


# ------------------------------------------------------------------ 3. continuation instead of restart

def test_timeout_saves_a_wip_commit_and_the_next_run_continues_and_releases(tmp_path: Path, monkeypatch, capsys):
    st = _settings(tmp_path)
    p = _product(st)
    tid = _task(p)
    repo = p.repo_dir
    base0 = gitops.git(repo, "rev-parse", "main")
    r = FakeRunner(["kill", "done"])
    _exec(st, p, r, "c1")

    s = Store(p.dir)
    t = s.task(tid)
    assert t["state"] == "in_progress" and t["continuations"] == 1 and not t.get("attempts") and t["assignee_run"] is None
    res = t["results"][-1]
    assert res["status"] == INTERRUPTED and res["killed"] == "timeout" and res["wip_files"] == 2 and "2700 s" in res["summary"]
    wip = res["wip_commit"]
    # the work is a service commit on the task branch; the base branch is untouched and nothing was released
    assert _subject(repo, f"task/{tid}") == f"wip: {tid} — run stopped by the time limit, continues next cycle"
    assert gitops.git(repo, "rev-parse", f"task/{tid}") == wip and gitops.git(repo, "rev-parse", "main") == base0
    assert "src/server.ts" in gitops.git(repo, "show", "--stat", "--format=", wip) and not gitops.dirty(repo)
    assert not t.get("released_sha") and "src" not in gitops.git(repo, "ls-tree", "--name-only", "main")
    ev = {e["kind"]: e for e in s.events()}
    assert ev["run.wip_saved"]["detail"] == {"run": f"c1-exec-{tid}", "files": 2, "commit": wip, "branch": f"task/{tid}", "reason": "timeout"}
    assert ev["run.profile"]["detail"]["killed"] == "timeout" and ev["run.profile"]["detail"]["task"] == tid
    assert "e2e 100 % in 5 calls" in ev["run.profile"]["detail"]["line"] and "task.replan_asked" not in ev
    rec = json.loads((p.runs_dir / "c1" / f"dev-{tid}" / "run.json").read_text(encoding="utf-8"))
    assert rec["killed"] == "timeout" and rec["profile"]["longest"][0]["unfinished"] and rec["profile"]["flags"]

    # the PO sees one line about the stopped run; `team task show` prints the profile
    po = prompts.po_prompt(p, s, cycle_id="c2", observations=[], questions=[], metrics_md="", spent_today=0.0, cap=20.0, max_ready=3, idle=False)
    assert "# RUN TIME" in po and f"- developer {tid}: 2700 s (tools 2700 s), e2e 100 % in 5 calls, killed by the time limit (2700 s)" in po
    assert "same command 5×" in po and "up to 3 times per task" in po
    s.close()
    monkeypatch.setattr(cli.C, "load", lambda *a, **k: st)
    assert cli.main(["task", p.slug, "show", "--id", tid]) == 0
    shown = capsys.readouterr().out
    assert f"# RUN PROFILES of {tid} (1 runs" in shown and "killed by the time limit (2700 s)" in shown and "cut off by the kill" in shown

    # next cycle: the task is continued, not restarted — the prompt names the wip commit, its stat and the profile
    _exec(st, p, r, "c2")
    role, prompt = r.prompts[-1]
    assert "# CONTINUATION — DO NOT START OVER" in prompt and "was stopped by the time limit after 2700 s" in prompt
    assert wip[:10] in prompt and "src/server.ts" in prompt and "src/draft1.ts" in prompt and "Do not rewrite what is already written" in prompt
    assert "Where the stopped run spent its time" in prompt and "e2e 2700 s (100 %) in 5 calls" in prompt and "flag: same command 5×" in prompt
    assert "continuation 1 of 3" in prompt and prompt.count("by category:") == 1          # the profile is shown once
    # a normal run releases the task with ALL changes: the wip commit's files and its own
    s = Store(p.dir)
    t = s.task(tid)
    assert t["state"] == "released" and t["released_sha"] == gitops.git(repo, "rev-parse", "main")
    files = gitops.git(repo, "ls-tree", "-r", "--name-only", "main").splitlines()
    assert {"src/server.ts", "src/draft1.ts", "src/client.ts"} <= set(files)
    assert gitops.is_ancestor(repo, wip, "main") and r.calls.count("developer") == 2
    s.close()


def test_uncommitted_work_survives_into_the_continuation(tmp_path: Path):
    """The bug itself (F23): the next attempt's `reset --hard` + `clean -fd` used to destroy what the stopped run wrote."""
    st = _settings(tmp_path)
    p = _product(st)
    tid = _task(p)
    seen = {}

    class Looking(FakeRunner):
        def run(self, role, prompt, schema, *, max_usd, cwd, **kw):
            if role == "developer" and self.calls.count("developer") == 1:
                seen["server"] = (Path(cwd) / "src" / "server.ts").read_text(encoding="utf-8")
                seen["branch"] = gitops.git(Path(cwd), "rev-parse", "--abbrev-ref", "HEAD")
            return super().run(role, prompt, schema, max_usd=max_usd, cwd=cwd, **kw)

    r = Looking(["inactive", "done"])
    _exec(st, p, r, "c1")
    s = Store(p.dir)
    t = s.task(tid)
    assert t["results"][-1]["killed"] == "inactivity" and "inactivity watchdog" in _subject(p.repo_dir, f"task/{tid}")
    s.close()
    _exec(st, p, r, "c2")
    assert seen == {"server": "// attempt 1: half of the room logic\n", "branch": f"task/{tid}"}
    assert "stopped by the inactivity watchdog" in r.prompts[-1][1]


def test_after_max_continuations_the_task_goes_to_the_po_with_the_profiles(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st, extra="cycle:\n  max_continuations: 2\n")
    tid = _task(p)
    base0 = gitops.git(p.repo_dir, "rev-parse", "main")
    r = FakeRunner(["kill", "kill", "kill", "done"])
    for i in (1, 2):
        _exec(st, p, r, f"c{i}")
    s = Store(p.dir)
    t = s.task(tid)
    assert t["state"] == "in_progress" and t["continuations"] == 2 and not s.questions(["open"])
    assert "continuation 2 of 2: if this run is stopped too" not in r.prompts[-1][1] and "continuation 1 of 2" in r.prompts[-1][1]
    s.close()
    _exec(st, p, r, "c3")                                              # the third stopped run: no continuation is left
    assert "continuation 2 of 2: if this run is stopped too, the task is blocked" in r.prompts[-1][1]
    s = Store(p.dir)
    t = s.task(tid)
    assert t["state"] == "blocked" and t["continuations"] == 2 and not t.get("attempts")
    q = s.questions(["open"])
    assert len(q) == 1 and q[0]["to"] == "po" and q[0]["from"] == "dispatcher" and q[0]["blocks"] == [tid] and t["blocked_by"] == q[0]["id"]
    text = q[0]["question"]
    assert "stopped by the run time limit 3 times" in text and "Split it into smaller tasks" in text and f"git merge task/{tid}" in text
    assert text.count(f"- developer {tid}: 2700 s") == 3 and "e2e 100 % in 5 calls" in text and "same command 5×" in text
    assert f"team task {p.slug} show --id {tid}" in text and len(text) < 1500
    kinds = [e["kind"] for e in s.events()]
    assert kinds.count("run.wip_saved") == 3 and "task.continuations_exhausted" in kinds and "task.replan_asked" not in kinds
    # every stopped run's work is on the branch, none of it on the base branch
    assert gitops.git(p.repo_dir, "rev-parse", "main") == base0
    assert gitops.git(p.repo_dir, "rev-list", "--count", f"main..task/{tid}") == "3"
    s.close()
    _exec(st, p, r, "c4")                                              # blocked: not started again by itself
    assert r.calls.count("developer") == 3


def test_a_failed_run_that_is_not_a_kill_keeps_todays_behaviour(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    tid = _task(p)
    r = FakeRunner(["error"])
    _exec(st, p, r, "c1")
    s = Store(p.dir)
    t = s.task(tid)
    assert t["state"] == "ready" and t["attempts"] == 1 and not t.get("continuations") and t["results"][-1]["status"] == "error"
    assert "run.wip_saved" not in [e["kind"] for e in s.events()]
    assert gitops.git(p.repo_dir, "rev-list", "--count", f"main..task/{tid}") == "0"        # nothing was committed for it
    s.close()


def test_a_partial_result_keeps_its_uncommitted_work(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    tid = _task(p, role="designer")
    r = FakeRunner(["partial", "done"])
    _exec(st, p, r, "c1")
    s = Store(p.dir)
    t = s.task(tid)
    res = t["results"][-1]
    assert t["state"] == "in_progress" and res["status"] == "partial" and res["wip_files"] == 1 and not t.get("continuations")
    assert t["history"][-1]["note"] == "partial — continues next cycle"                    # the existing transition, unchanged
    assert _subject(p.repo_dir, f"task/{tid}") == f"wip: {tid} — partial result, continues next cycle"
    assert [e["detail"]["reason"] for e in s.events() if e["kind"] == "run.wip_saved"] == ["partial"]
    s.close()
    _exec(st, p, r, "c2")
    role, prompt = r.prompts[-1]
    assert role == "designer" and "The previous run of this task returned a partial result." in prompt and res["wip_commit"][:10] in prompt
    assert "src/half.ts" in prompt and "left: the client and the e2e spec" in prompt and "continuation 0 of" not in prompt
    s = Store(p.dir)
    assert s.task(tid)["state"] == "released" and "src/half.ts" in gitops.git(p.repo_dir, "ls-tree", "-r", "--name-only", "main")
    s.close()


def test_a_wip_commit_is_never_made_on_the_base_branch(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    tid = _task(p)
    repo = p.repo_dir
    (repo / "stray.txt").write_text("x\n", encoding="utf-8")
    head0 = gitops.head(repo)
    try:
        gitops.save_wip(repo, tid, "run stopped by the time limit, continues next cycle")
    except gitops.GitError as exc:
        assert "not on task/" in str(exc)
    else:
        raise AssertionError("a wip commit on the base branch must be refused")
    assert gitops.head(repo) == head0 and gitops.dirty(repo)
    # the cycle: the role left the checkout on the base branch → nothing is committed, the run is a plain failed run
    (repo / "stray.txt").unlink()

    class Switching(FakeRunner):
        def run(self, role, prompt, schema, *, max_usd, cwd, **kw):
            res = super().run(role, prompt, schema, max_usd=max_usd, cwd=cwd, **kw)
            if role == "developer":
                gitops.git(Path(cwd), "checkout", "-q", "main")     # against its card; the new files travel along, untracked
            return res

    _exec(st, p, Switching(["kill"]), "c1")
    s = Store(p.dir)
    t = s.task(tid)
    assert t["state"] == "ready" and t["attempts"] == 1 and gitops.git(repo, "rev-parse", "main") == head0
    assert "run.wip_failed" in [e["kind"] for e in s.events()]
    s.close()


def test_a_clean_tree_at_the_kill_still_continues(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    tid = _task(p)

    class Committing(FakeRunner):
        def run(self, role, prompt, schema, *, max_usd, cwd, **kw):
            res = super().run(role, prompt, schema, max_usd=max_usd, cwd=cwd, **kw)
            if role == "developer" and not res.ok:
                gitops.git(Path(cwd), "add", "-A"); gitops.git(Path(cwd), "commit", "-q", "-m", "T-0001: room, first half")
            return res

    r = Committing(["kill", "done"])
    _exec(st, p, r, "c1")
    s = Store(p.dir)
    t = s.task(tid)
    assert t["state"] == "in_progress" and t["continuations"] == 1 and "wip_commit" not in t["results"][-1]
    assert "nothing uncommitted to save" in t["results"][-1]["summary"] and "run.wip_saved" not in [e["kind"] for e in s.events()]
    s.close()
    _exec(st, p, r, "c2")
    prompt = r.prompts[-1][1]
    assert "# CONTINUATION — DO NOT START OVER" in prompt and "Nothing uncommitted was left; the branch is 1 commit(s) ahead of main" in prompt
