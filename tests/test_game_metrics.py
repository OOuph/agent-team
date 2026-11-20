"""Game metrics and load tasks (game profile, slice 6; owner 20.11): the /_team/metrics.json contract rendered for the PO
and the analyst, `team-play load` with a fake load script, and the tester's execute path for a `load` task (server =
python http.server, fake npx)."""
from __future__ import annotations

import json
import os
import stat
import textwrap
from pathlib import Path

import pytest
import yaml

from team import config as C, gitops, play, prompts
from team.cycle import Cycle, metrics_md
from team.runner import RunResult, load_roles
from team.store import TASK_TYPES, Store

ROOT = Path(__file__).resolve().parent.parent
RUN_OK = "python3 -m http.server {port} --bind 127.0.0.1"

GAME_METRICS = {
    "kind": "game", "host": "fleets.example.test", "version": "a1b2c3d4e5f6", "generated": "2026-11-21T06:00:12Z",
    "now": {"online": 3, "rooms_open": 2, "rooms_playing": 1},
    "days": {
        "2026-11-20": {"rounds_started": 6, "rounds_finished": 2, "players": 5, "move_latency_ms": {"p50": 40, "p95": 150, "samples": 100}},
        "2026-11-21": {"rounds_started": 14, "rounds_finished": 9, "rounds_abandoned": 5, "players": 11, "returning_players": 4, "online_peak": 6,
                       "round_length_s": {"median": 412, "p90": 900}, "move_latency_ms": {"p50": 38, "p95": 140, "samples": 620},
                       "moves": 610, "rejected_moves": 12, "disconnects": 7, "reconnects": 5, "errors": 1, "first_mover_wins": 5,
                       "by_result": {"win": 9, "timeout": 3, "resign": 2}}}}
SITE_METRICS = {"host": "x", "generated": "g", "days": {"2026-11-21": {"humans": {"sources": 3, "engaged": 1, "returns": 0}, "agents": {}, "crawlers": {}}}}


# ------------------------------------------------------------------ rendering

def test_game_metrics_are_detected_and_rendered_for_po_and_analyst(tmp_path: Path):
    assert prompts.is_game_metrics(GAME_METRICS) and not prompts.is_game_metrics(SITE_METRICS)
    assert prompts.is_game_metrics({"days": {"d": {"rounds_started": 1}}})          # no kind field, round fields are enough
    md = metrics_md(GAME_METRICS)
    assert md.startswith("# game metrics fleets.example.test") and "version a1b2c3d4e5f6" in md and "Now: online 3, rooms open 2" in md
    assert "| 2026-11-21 | 14 | 9 | 5 | 11 | 4 | 6 | 412 | 38/140 | 12 | 7 | 1 | 5 |" in md
    assert "| 2026-11-20 | 6 | 2 | — | 5 | — | — | — | 40/150 | — | — | — | — |" in md      # missing fields print —
    assert "humans" in metrics_md(SITE_METRICS) and "game metrics" not in metrics_md(SITE_METRICS)
    f = tmp_path / "2026-11-21.json"; f.write_text(json.dumps(GAME_METRICS))
    dig = prompts.metrics_digest([f])
    assert dig.startswith("Game metrics served by the game server") and dig.index("## 2026-11-21") < dig.index("## 2026-11-20")
    assert "rounds: started 14, finished 9, abandoned 5; by result: win 9, timeout 3, resign 2" in dig
    assert "move latency ms: p50 38, p95 140 (620 samples)" in dig and "first-mover wins 5" in dig and "abandoned —" in dig
    assert "HUMANS" in prompts.metrics_digest([_write(tmp_path / "s.json", SITE_METRICS)])


def _write(p: Path, data: dict) -> Path:
    p.write_text(json.dumps(data))
    return p


def test_fetch_metrics_writes_the_game_table(tmp_path: Path, monkeypatch):
    st = _settings(tmp_path)
    p = _game(st, site_base="https://fleets.example.test", deploy="service")
    import urllib.request

    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps(GAME_METRICS).encode()
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=20: R())
    cyc = Cycle(st, p, None, "c1")
    out = cyc.fetch_metrics()
    cyc.store.close()
    assert out and out.exists() and "| 2026-11-21 | 14 | 9 |" in (p.dir / "metrics" / "latest.md").read_text()
    po = prompts.po_prompt(p, Store(p.dir), cycle_id="c1", observations=[], questions=[], metrics_md=(p.dir / "metrics" / "latest.md").read_text(),
                           spent_today=0, cap=20, max_ready=3, idle=False)
    assert "# LATEST METRICS" in po and "game metrics fleets.example.test" in po


# ------------------------------------------------------------------ team-play load

def _fake_npx(tmp_path: Path, body: str) -> Path:
    b = tmp_path / "bin"; b.mkdir(exist_ok=True)
    f = b / "npx"
    f.write_text("#!/usr/bin/env python3\nimport sys, json, os\n" + textwrap.dedent(body))
    f.chmod(f.stat().st_mode | stat.S_IXUSR)
    return f


def test_team_play_load_runs_the_script_and_files_the_summary(tmp_path: Path, monkeypatch, capsys):
    co = tmp_path / "checkout"; (co / "tests" / "load").mkdir(parents=True)
    (co / "tests" / "load" / "clients.ts").write_text("// load\n")
    out = tmp_path / "out"
    npx = _fake_npx(tmp_path, '''
        args = sys.argv[1:]
        assert args[:2] == ["tsx", "tests/load/clients.ts"], args
        assert "--base-url" in args and os.environ["BASE_URL"] == "http://127.0.0.1:9"
        n = int(args[args.index("--clients") + 1]); s = int(args[args.index("--seconds") + 1])
        print("ramping")
        print(json.dumps({"clients": n, "connected": n, "seconds": s, "messages": n * s * 2, "samples": 40, "p50_ms": 12, "p95_ms": 41, "max_ms": 88, "errors": 0, "disconnects": 0, "rejected": 1}))
        sys.exit(0)
        ''')
    monkeypatch.setenv("TEAM_PLAY_CWD", str(co)); monkeypatch.setenv("TEAM_PLAY_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("TEAM_PLAY_OUT", str(out)); monkeypatch.setenv("TEAM_PLAY_NPX", str(npx))
    assert play.main(["load", "--clients", "20", "--seconds", "3"]) == 0
    text = capsys.readouterr().out
    assert "load against http://127.0.0.1:9: clients 20, connected 20, seconds 3, messages 120, samples 40, p50_ms 12, p95_ms 41, max_ms 88, errors 0, disconnects 0, rejected 1" in text
    raw = list(out.glob("load-*.json"))
    assert len(raw) == 1 and json.loads(raw[0].read_text())["p95_ms"] == 41 and json.loads(raw[0].read_text())["script"] == "tests/load/clients.ts"
    with pytest.raises(SystemExit, match="--clients 1..200"):
        play.main(["load", "--clients", "500", "--seconds", "3"])
    with pytest.raises(SystemExit, match="inside the checkout"):
        play.main(["load", "--clients", "2", "--seconds", "3", "--script", "../x.ts"])
    with pytest.raises(SystemExit, match="not found in the checkout"):
        play.main(["load", "--clients", "2", "--seconds", "3", "--script", "tests/load/other.ts"])
    _fake_npx(tmp_path, 'print("no json here"); sys.exit(1)\n')
    assert play.main(["load", "--clients", "2", "--seconds", "3"]) == 2
    assert "printed no JSON summary" in capsys.readouterr().out
    assert play.parse_load_output('x\n{"a": 1}\nnot json\n') == {"a": 1} and play.parse_load_output("") is None


# ------------------------------------------------------------------ the tester executes a load task

def _settings(tmp_path: Path) -> C.Settings:
    raw = C._merge(C.DEFAULTS, {"cycle": {"review_after_min": 0, "persona_every_n_cycles": 0, "digest_hours_utc": [0]}})
    return C.Settings(raw, tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")


def _game(st: C.Settings, run_cmd: str = RUN_OK, site_base: str = "", deploy: str = "pending", load_script: bool = True) -> C.Product:
    d = st.products_dir / "fleets"; d.mkdir(parents=True)
    (d / "product.yaml").write_text(yaml.safe_dump({
        "slug": "fleets", "name": "Fleets", "host": "", "site_base": site_base, "profile": "game", "stage": "M0",
        "repo": {"url": "", "branch": "main", "kind": "service", "run_cmd": run_cmd, "test_cmd": "", "health_path": "/healthz"},
        "deploy": {"mode": deploy}}, sort_keys=False), encoding="utf-8")
    (d / "VISION.md").write_text("# Fleets\n")
    p = C.load_product(st, d)
    r = gitops.init_local(p.repo_dir)
    (r / "healthz").write_text('{"version": "test"}\n')
    if load_script:
        (r / "tests" / "load").mkdir(parents=True); (r / "tests" / "load" / "clients.ts").write_text("// load\n")
    gitops.git(r, "add", "-A"); gitops.git(r, "commit", "-q", "-m", "init")
    return p


class TesterRunner:
    def __init__(self, verdict: str = "pass"):
        self.verdict = verdict
        self.calls: list[str] = []
        self.prompts: dict[str, str] = {}
        self.env: dict[str, dict] = {}
        self.tools: list = []

    def run(self, role, prompt, schema, *, max_usd, cwd, allowed_tools=None, env_extra=None, run_dir=None, run_id="", max_turns=None, **kw):
        self.calls.append(role); self.prompts[role] = prompt; self.env[role] = dict(env_extra or {}); self.tools = list(allowed_tools or [])
        assert role == "tester" and schema["title"] == "test-report"
        out = {"verdict": self.verdict, "steps": [{"url": env_extra["TEAM_PLAY_BASE_URL"] + "/healthz", "status": 200, "note": "ok"}],
               "criteria": [{"criterion": "50 clients p95 ≤ 300 ms", "met": self.verdict == "pass", "evidence": "p95 41 ms" if self.verdict == "pass" else "p95 910 ms"}],
               "scenarios": [{"name": "team-play load 50 clients 60 s", "status": "passed" if self.verdict == "pass" else "failed", "clients": 50, "evidence": "connected 50, errors 0"}],
               "latency_ms": {"p50": 12, "p95": 41 if self.verdict == "pass" else 910, "samples": 2400, "how": "team-play load 50 clients 60 s"},
               "first_failure": "" if self.verdict == "pass" else "p95 910 ms > 300 ms",
               "limits": "no load script" if self.verdict == "inconclusive" else "",
               "observations": ([] if self.verdict == "pass" else [{"title": "latency grows with 50 clients", "classification": "reproducible_defect",
                                                                     "url": env_extra["TEAM_PLAY_BASE_URL"], "actual": "p95 910 ms"}]),
               "summary": f"load {self.verdict}."}
        return RunResult(True, out, {"total_cost_usd": 0.1}, 0.1, 2, None, session_id="s", duration_s=1.0)


def _load_task(p: C.Product) -> str:
    s = Store(p.dir)
    t = s.create_task(title="50 clients", type_="load", problem="capacity unknown", scope="tests/load/", acceptance=["50 clients for 60 s: p95 ≤ 300 ms, 0 errors"],
                      goal="M1", created_by="po", role="tester", state="ready")
    s.close()
    return t["id"]


def test_load_task_runs_the_tester_against_a_started_server(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st)
    tid = _load_task(p)
    r = TesterRunner("pass")
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["execute"])
    cyc.store.close()
    assert r.calls == ["tester"]
    pr = r.prompts["tester"]
    assert pr.startswith(f"# LOAD TASK {tid}") and "Load script present: `tests/load/clients.ts`" in pr and "team-play load --clients N" in pr
    env = r.env["tester"]
    assert env["TEAM_PLAY_BASE_URL"].startswith("http://127.0.0.1:") and env["TEAM_PLAY_OUT"] == str(p.dir / "reviews" / tid)
    assert r.tools[0] == "Bash(team-play *)"
    s = Store(p.dir)
    t = s.task(tid)
    assert t["state"] == "accepted" and t["results"][-1]["latency_ms"]["p95"] == 41 and t["results"][-1]["verdict"] == "pass"
    kinds = [e["kind"] for e in s.events()]
    assert "playtest.started" in kinds and "task.role_not_available" not in kinds
    assert list((p.dir / "reviews" / tid).glob("load-report-*.yaml"))
    s.close()


def test_failed_load_is_done_with_observations_and_inconclusive_blocks(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st)
    tid = _load_task(p)
    cyc = Cycle(st, p, TesterRunner("fail"), "c1")
    cyc.run(["execute"])
    cyc.store.close()
    s = Store(p.dir)
    t = s.task(tid)
    assert t["state"] == "done" and "first failure: p95 910 ms" in t["history"][-1]["note"]
    obs = s.observations("new")
    assert len(obs) == 1 and obs[0]["source"] == "tester" and obs[0]["classification"] == "reproducible_defect" and obs[0]["task"] == tid
    s.close()
    p2 = _game(_settings(tmp_path / "b"), load_script=False)
    tid2 = _load_task(p2)
    r = TesterRunner("inconclusive")
    cyc = Cycle(p2.st, p2, r, "c1")
    cyc.run(["execute"])
    cyc.store.close()
    assert "No `tests/load/clients.ts` in the checkout" in r.prompts["tester"]
    s = Store(p2.dir)
    t = s.task(tid2)
    q = s.questions(["open"])
    assert t["state"] == "blocked" and len(q) == 1 and q[0]["to"] == "po" and "inconclusive" in q[0]["question"]
    s.close()


def test_load_task_without_a_server_blocks_with_a_po_question(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st, run_cmd='python3 -c "import sys; sys.exit(7)"')
    tid = _load_task(p)
    r = TesterRunner()
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["execute"])
    cyc.store.close()
    assert r.calls == []
    s = Store(p.dir)
    t = s.task(tid)
    assert t["state"] == "blocked" and t.get("assignee_run") is None
    assert [e["kind"] for e in s.events() if e["kind"] == "playtest.failed"] and s.observations("new")[0]["classification"] == "environment_error"
    assert "could not start the server" in s.questions(["open"])[0]["question"]
    s.close()


def test_schema_types_templates_and_docs():
    assert "load" in TASK_TYPES
    po = json.loads((ROOT / "team" / "schemas" / "po-decision.schema.json").read_text())
    txt = json.dumps(po)
    assert '"load"' in txt and '"tester"' in txt
    assert (ROOT / "team" / "templates" / "service-repo" / "tests" / "load" / "clients.ts").exists()
    assert (ROOT / "docs" / "game-metrics.md").read_text().startswith("# Game metrics contract")
    assert "tester" in load_roles(ROOT / "team" / "roles")
    card = (ROOT / "team" / "roles" / "tester.md").read_text()
    assert "team-play load --clients N --seconds S" in card and "## Load tasks" in card
    assert "type: load" in (ROOT / "team" / "roles" / "po.md").read_text()
    from team.cycle import GAME_ROLE_SLOTS, PENDING_ROLES
    assert GAME_ROLE_SLOTS["tester"] == 1 and "tester" not in PENDING_ROLES
