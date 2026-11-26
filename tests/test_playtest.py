"""Tester role and play-test server (game profile, slice 4; owner 20.11). The server under test is python's http.server
(no Node here); Playwright is replaced by a fake `npx` on PATH that prints a JSON report / writes a PNG."""
from __future__ import annotations

import json
import os
import socket
import stat
import textwrap
from pathlib import Path

import pytest

from team import config as C
from team import gitops, play, playtest
from team.cycle import Cycle
from team.runner import RunResult, load_roles
from team.store import Store

ROLES_DIR = Path(__file__).resolve().parent.parent / "team" / "roles"
RUN_OK = "python3 -m http.server {port} --bind 127.0.0.1"


def _settings(tmp_path: Path) -> C.Settings:
    raw = C._merge(C.DEFAULTS, {"cycle": {"review_after_min": 0, "persona_every_h": 0, "digest_hours_utc": [0]}})
    return C.Settings(raw, tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")


def _game(st: C.Settings, run_cmd: str = RUN_OK, deploy: str = "pending", site_base: str = "") -> C.Product:
    d = st.products_dir / "fleets"
    d.mkdir(parents=True)
    import yaml
    (d / "product.yaml").write_text(yaml.safe_dump({
        "slug": "fleets", "name": "Fleets", "host": "", "site_base": site_base, "profile": "game", "stage": "M0",
        "repo": {"url": "", "branch": "main", "kind": "service", "run_cmd": run_cmd, "test_cmd": "", "health_path": "/healthz"},
        "deploy": {"mode": deploy}}, sort_keys=False), encoding="utf-8")
    (d / "VISION.md").write_text("# Fleets\n")
    p = C.load_product(st, d)
    r = gitops.init_local(p.repo_dir)
    (r / "healthz").write_text('{"version": "test"}\n')
    (r / "README.md").write_text("game\n")
    (r / "tests" / "e2e").mkdir(parents=True)
    (r / "tests" / "e2e" / "rooms.spec.ts").write_text("// spec\n")
    gitops.git(r, "add", "-A"); gitops.git(r, "commit", "-q", "-m", "init")
    return p


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


# ------------------------------------------------------------------ playtest server

def test_start_health_and_stop(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st)
    sha = gitops.head(p.repo_dir)
    srv = playtest.start(p, p.repo_dir, sha, tmp_path / "work", start_timeout_s=20)
    try:
        assert srv.local and srv.port and srv.base_url == f"http://127.0.0.1:{srv.port}"
        assert playtest.health_ok(srv.base_url + "/healthz") and _port_open(srv.port)
        assert (srv.dest / "healthz").exists() and srv.dest != p.repo_dir
        assert any("answered after" in n for n in srv.notes)
        assert "PORT=" in srv.log_tail()
    finally:
        playtest.stop(srv)
    assert srv.proc is not None and srv.proc.poll() is not None and not _port_open(srv.port)


def test_start_fails_when_the_server_exits(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st, run_cmd='python3 -c "import sys; print(\'boom\'); sys.exit(3)"')
    with pytest.raises(playtest.PlaytestError) as ei:
        playtest.start(p, p.repo_dir, gitops.head(p.repo_dir), tmp_path / "work", start_timeout_s=20)
    assert "exited with code 3" in str(ei.value) and "boom" in str(ei.value)


def test_start_fails_without_run_cmd_or_health(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st, run_cmd="")
    with pytest.raises(playtest.PlaytestError, match="run_cmd"):
        playtest.start(p, p.repo_dir, gitops.head(p.repo_dir), tmp_path / "work")
    p2 = _game(_settings(tmp_path / "b"), run_cmd="python3 -c \"import time; time.sleep(30)\"")
    with pytest.raises(playtest.PlaytestError, match="did not answer"):
        playtest.start(p2, p2.repo_dir, gitops.head(p2.repo_dir), tmp_path / "b" / "work", start_timeout_s=2)


def test_live_host_is_used_without_a_local_server(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st, deploy="service", site_base="https://fleets.example.test")
    assert playtest.live_base_url(p) == "https://fleets.example.test"
    srv = playtest.start(p, p.repo_dir, gitops.head(p.repo_dir), tmp_path / "work")
    assert not srv.local and srv.proc is None and srv.base_url == "https://fleets.example.test" and (srv.dest / "tests").exists()
    playtest.stop(srv)                                                  # no-op
    p.raw["deploy"]["mode"] = "pending"
    assert playtest.live_base_url(p) is None


def test_live_host_checkout_is_built_for_the_tester(tmp_path: Path):
    """ai-battles 22.11: generated sources are not in git; the tester's Playwright runs from the checkout even against the deployed host."""
    st = _settings(tmp_path)
    p = _game(st, deploy="service", site_base="https://fleets.example.test")
    p.raw["repo"]["build_cmd"] = "echo generated > built.txt"
    srv = playtest.start(p, p.repo_dir, gitops.head(p.repo_dir), tmp_path / "work")
    assert not srv.local and (srv.dest / "built.txt").read_text().strip() == "generated"
    playtest.stop(srv)
    p.raw["repo"]["build_cmd"] = "exit 3"
    with pytest.raises(playtest.PlaytestError, match="build failed"):
        playtest.start(p, p.repo_dir, gitops.head(p.repo_dir), tmp_path / "work2")


def test_install_cmd_detection(tmp_path: Path):
    assert playtest.install_cmd_for(tmp_path) == ""
    (tmp_path / "package.json").write_text("{}")
    assert playtest.install_cmd_for(tmp_path).startswith("npm install")
    (tmp_path / "package-lock.json").write_text("{}")
    assert playtest.install_cmd_for(tmp_path).startswith("npm ci")
    (tmp_path / "node_modules").mkdir()
    assert playtest.install_cmd_for(tmp_path) == "" and playtest.install_cmd_for(tmp_path, "pnpm i") == "pnpm i"
    os.environ["TELEGRAM_BOT_TOKEN"] = "x"; os.environ["GITHUB_TOKEN"] = "y"      # conftest restores the environment
    env = playtest.clean_env()
    assert "TELEGRAM_BOT_TOKEN" not in env and "GITHUB_TOKEN" not in env and env["NODE_ENV"] == "test" and "PATH" in env


# ------------------------------------------------------------------ team-play

SAMPLE_REPORT = {
    "suites": [{"title": "rooms.spec.ts", "specs": [], "suites": [{"title": "rooms", "specs": [
        {"title": "two tabs see the move", "tests": [{"status": "expected", "projectName": "phone", "results": [{"status": "passed", "duration": 812}]}]},
        {"title": "illegal move rejected", "tests": [{"status": "unexpected", "projectName": "phone",
                                                     "results": [{"status": "failed", "duration": 5012, "error": {"message": "\x1b[31mexpect(locator).toHaveText\x1b[0m\nExpected: /not your turn/\nReceived: ''"}}]}]},
        {"title": "third player refused", "tests": [{"status": "skipped", "projectName": "phone", "results": [{"status": "skipped", "duration": 0}]}]}]}]}],
    "errors": []}


def test_summarize_playwright_report():
    lines, totals = play.summarize(SAMPLE_REPORT)
    assert totals == {"passed": 1, "failed": 1, "skipped": 1, "flaky": 0, "timedOut": 0}
    assert lines[0].startswith("✓ rooms.spec.ts › rooms › two tabs see the move [phone] 812 ms")
    assert lines[1].startswith("✗ rooms.spec.ts › rooms › illegal move rejected [phone] 5012 ms — expect(locator).toHaveText Expected")
    assert "\x1b" not in lines[1] and lines[2].startswith("- ")


def _fake_npx(tmp_path: Path, body: str) -> Path:
    b = tmp_path / "bin"; b.mkdir(exist_ok=True)
    f = b / "npx"
    f.write_text("#!/usr/bin/env python3\nimport sys, json, os, pathlib\n" + textwrap.dedent(body))
    f.chmod(f.stat().st_mode | stat.S_IXUSR)
    return f


def test_team_play_test_runs_fake_playwright_and_copies_artifacts(tmp_path: Path, monkeypatch, capsys):
    co = tmp_path / "checkout"; (co / "test-results" / "rooms-illegal").mkdir(parents=True)
    (co / "test-results" / "rooms-illegal" / "test-failed-1.png").write_bytes(b"png")
    out = tmp_path / "out"
    npx = _fake_npx(tmp_path, f"""
        a = sys.argv[1:]
        assert a[:3] == ["playwright", "test", "--reporter=line,json"], a
        assert os.environ["BASE_URL"] == "http://127.0.0.1:4321", os.environ.get("BASE_URL")
        assert "--grep=rooms" in a
        print("Running 3 tests")
        print(json.dumps({json.dumps(SAMPLE_REPORT)}))
        sys.exit(1)
        """)
    monkeypatch.setenv("TEAM_PLAY_CWD", str(co)); monkeypatch.setenv("TEAM_PLAY_BASE_URL", "http://127.0.0.1:4321")
    monkeypatch.setenv("TEAM_PLAY_OUT", str(out)); monkeypatch.setenv("TEAM_PLAY_NPX", str(npx))
    rc = play.main(["test", "--grep", "rooms"])
    cap = capsys.readouterr(); o = cap.out
    assert "playwright test started" in cap.err
    assert rc == 1 and "passed 1, failed 1, skipped 1" in o and "✗ rooms.spec.ts › rooms › illegal move rejected" in o
    assert "artifacts: rooms-illegal__test-failed-1.png" in o and (out / "rooms-illegal__test-failed-1.png").exists()
    assert list(out.glob("playwright-*.json"))
    with pytest.raises(SystemExit):
        play.main(["test", "--grep", "a|b"])
    with pytest.raises(SystemExit):
        play.main(["test", "--spec", "../x.spec.ts"])


def test_team_play_test_sets_e2e_mobile_only_for_mobile_projects(tmp_path: Path, monkeypatch, capsys):
    co = tmp_path / "checkout"; co.mkdir()
    npx = _fake_npx(tmp_path, f"""
        a = sys.argv[1:]
        proj = next((x.split("=", 1)[1] for x in a if x.startswith("--project=")), "")
        if proj == "nope":
            print('Error: Project(s) "nope" not found. Available projects: "phone", "mobile-360"')
            sys.exit(1)
        print("E2E_MOBILE=" + os.environ.get("E2E_MOBILE", "unset") + " project=" + proj)
        print(json.dumps({json.dumps(SAMPLE_REPORT)}))
        """)
    monkeypatch.setenv("TEAM_PLAY_CWD", str(co)); monkeypatch.setenv("TEAM_PLAY_BASE_URL", "http://127.0.0.1:4321")
    monkeypatch.setenv("TEAM_PLAY_OUT", str(tmp_path / "out")); monkeypatch.setenv("TEAM_PLAY_NPX", str(npx))
    monkeypatch.delenv("E2E_MOBILE", raising=False)
    seen = []
    real = play.subprocess.Popen
    def spy(argv, **kw):
        seen.append(kw["env"].get("E2E_MOBILE")); return real(argv, **kw)
    monkeypatch.setattr(play.subprocess, "Popen", spy)
    play.main(["test", "--project", "mobile-360"]); play.main(["test", "--project", "phone"]); play.main(["test"])
    assert seen == ["1", None, None]
    capsys.readouterr()
    assert play.main(["test", "--project", "nope"]) == 2
    assert 'Available projects: "phone", "mobile-360"' in capsys.readouterr().out


def test_team_play_screenshot_validates_and_writes(tmp_path: Path, monkeypatch, capsys):
    co = tmp_path / "checkout"; co.mkdir()
    out = tmp_path / "out"
    npx = _fake_npx(tmp_path, """
        a = sys.argv[1:]
        assert a[:2] == ["playwright", "screenshot"] and a[2] == "--viewport-size=360,740", a
        pathlib.Path(a[-1]).write_bytes(b"PNG")
        """)
    monkeypatch.setenv("TEAM_PLAY_CWD", str(co)); monkeypatch.setenv("TEAM_PLAY_BASE_URL", "http://127.0.0.1:4321")
    monkeypatch.setenv("TEAM_PLAY_OUT", str(out)); monkeypatch.setenv("TEAM_PLAY_NPX", str(npx))
    assert play.main(["screenshot", "http://127.0.0.1:4321/", "--out", "lobby-360"]) == 0
    assert (out / "lobby-360.png").read_bytes() == b"PNG" and "screenshot 360x740" in capsys.readouterr().out
    with pytest.raises(SystemExit):                                     # another host
        play.main(["screenshot", "https://example.com/", "--out", "x"])
    with pytest.raises(SystemExit):                                     # bad name
        play.main(["screenshot", "http://127.0.0.1:4321/", "--out", "../x"])
    monkeypatch.setenv("TEAM_ALLOWED_HOSTS", "fleets.example.test")
    assert play.allowed_url("https://www.fleets.example.test/rules") and not play.allowed_url("ftp://127.0.0.1/")


def test_team_play_logs(tmp_path: Path, monkeypatch, capsys):
    log = tmp_path / "server.log"; log.write_text("started\nrejected move: not_your_turn\n")
    monkeypatch.setenv("TEAM_PLAY_SERVER_LOG", str(log))
    assert play.main(["logs", "--tail", "500"]) == 0 and "rejected move" in capsys.readouterr().out
    monkeypatch.delenv("TEAM_PLAY_SERVER_LOG")
    assert play.main(["logs"]) == 0 and "no server log" in capsys.readouterr().out


def test_bin_wrapper_and_entry_point():
    root = Path(__file__).resolve().parent.parent
    w = root / "bin" / "team-play"
    assert w.exists() and os.access(w, os.X_OK) and "team.play" in w.read_text()
    assert 'team-play = "team.play:main"' in (root / "pyproject.toml").read_text()
    assert (root / "team" / "templates" / "service-repo" / "tests" / "e2e" / "two-players.spec.ts").exists()


# ------------------------------------------------------------------ the review phase uses the tester

class FakeRunner:
    def __init__(self):
        self.calls: list[str] = []
        self.prompts: dict[str, str] = {}
        self.env: dict[str, dict] = {}
        self.tools: dict[str, list] = {}
        self.port_open_during_run: bool | None = None

    def run(self, role, prompt, schema, *, max_usd, cwd, allowed_tools=None, env_extra=None, run_dir=None, run_id="", max_turns=None, **kw):
        self.calls.append(role); self.prompts[role] = prompt; self.env[role] = dict(env_extra or {}); self.tools[role] = list(allowed_tools or [])
        if role == "tester":
            base = (env_extra or {}).get("TEAM_PLAY_BASE_URL", "")
            self.port_open_during_run = _port_open(int(base.rsplit(":", 1)[1])) if base.startswith("http://127.0.0.1:") else None
            out = {"verdict": "pass", "steps": [{"url": base + "/healthz", "status": 200, "note": "ok"}],
                   "criteria": [{"criterion": "two tabs", "met": True, "evidence": "✓ rooms › two tabs see the move 812 ms"}],
                   "scenarios": [{"name": "two tabs see the move", "status": "passed", "clients": 2, "evidence": "✓ 812 ms", "screenshot": "lobby-360.png"}],
                   "screenshots": ["lobby-360.png"], "latency_ms": {"p50": 120, "p95": 300, "samples": 5, "how": "test assertion"},
                   "observations": [{"title": "status text unreadable on 360 px", "classification": "ux_friction", "url": base, "actual": "overlaps"}],
                   "summary": "played. fine."}
        elif role == "reviewer":
            out = {"verdict": "pass", "steps": [], "criteria": [{"criterion": "x", "met": True, "evidence": "read"}], "summary": "read. ok."}
        else:
            out = {}
        return RunResult(True, out, {"total_cost_usd": 0.1}, 0.1, 2, None, session_id="s", duration_s=1.0)


def _released(p: C.Product, role: str = "developer", **extra) -> str:
    s = Store(p.dir)
    t = s.create_task(title="rooms", type_="milestone", problem="x", scope="src/", acceptance=["two tabs see the move"], goal="M0",
                      created_by="po", role=role, state="ready")
    s.transition(t["id"], "released", "dispatcher", "merged", force=True, released_at="2020-01-01T00:00:00+00:00",
                 released_sha=gitops.head(p.repo_dir), **extra)
    s.add_result(t["id"], {"run_id": "r", "status": "done", "summary": "done", "changes": ["src/rooms.ts"], "how_to_verify": ["play"], "self_check": []})
    s.close()
    return t["id"]


def test_review_of_a_game_task_runs_the_tester_against_a_live_server(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st)
    tid = _released(p)
    r = FakeRunner()
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["review"])
    cyc.store.close()
    assert r.calls == ["tester"] and r.port_open_during_run is True        # the server answered while the tester ran …
    base = r.env["tester"]["TEAM_PLAY_BASE_URL"]
    assert not _port_open(int(base.rsplit(":", 1)[1]))                      # … and was stopped afterwards
    pr = r.prompts["tester"]
    assert "# PLAY-TEST SERVER" in pr and base in pr and "E2E specs present: tests/e2e/rooms.spec.ts" in pr and "# TEST RUN" in pr
    assert r.tools["tester"][0] == "Bash(team-play *)" and "Read" in r.tools["tester"]
    env = r.env["tester"]
    assert Path(env["TEAM_PLAY_CWD"]).is_dir() and env["TEAM_PLAY_OUT"] == str(p.dir / "reviews" / tid) and "127.0.0.1" in env["TEAM_ALLOWED_HOSTS"]
    assert Path(env["TEAM_PLAY_SERVER_LOG"]).exists()
    s = Store(p.dir)
    t = s.task(tid)
    assert t["state"] == "accepted" and t["repo_only"] is True
    rev = s.task(tid)["last_review"] if s.task(tid).get("last_review") else None
    kinds = [e["kind"] for e in s.events()]
    assert "playtest.started" in kinds
    obs = s.observations("new")
    assert len(obs) == 1 and obs[0]["classification"] == "ux_friction" and obs[0]["source"] == "reviewer"
    reviews = list((p.dir / "reviews").glob("*.yaml")) + list((p.dir / "reviews").glob("*.json"))
    assert reviews or (p.dir / "reviews" / tid).exists()
    s.close()


def test_review_falls_back_to_the_reviewer_when_the_server_does_not_start(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st, run_cmd='python3 -c "import sys; sys.exit(7)"')
    tid = _released(p)
    r = FakeRunner()
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["review"])
    cyc.store.close()
    assert r.calls == ["reviewer"]
    pr = r.prompts["reviewer"]
    assert "PLAY-TEST SERVER — NOT AVAILABLE" in pr and "exited with code 7" in pr and "READ-ONLY CHECKOUT" in pr
    s = Store(p.dir)
    assert s.task(tid)["state"] == "accepted"
    obs = [o for o in s.observations("new") if o["classification"] == "environment_error"]
    assert len(obs) == 1 and tid in obs[0]["title"]
    assert "playtest.failed" in [e["kind"] for e in s.events()]
    s.close()


def test_document_tasks_and_opt_out_stay_with_the_reviewer(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st)
    _released(p, role="game-designer")
    r = FakeRunner()
    cyc = Cycle(st, p, r, "c1"); cyc.run(["review"]); cyc.store.close()
    assert r.calls == ["reviewer"]
    p2 = _game(_settings(tmp_path / "b"))
    _released(p2, playtest=False)
    r2 = FakeRunner()
    cyc = Cycle(_settings(tmp_path / "b"), p2, r2, "c1"); cyc.run(["review"]); cyc.store.close()
    assert r2.calls == ["reviewer"]


def test_tester_card_and_schema():
    roles = load_roles(ROLES_DIR)
    assert roles["tester"]["_tools"][0] == "Bash(team-play *)" and "Write" not in roles["tester"]["_tools"]
    import jsonschema
    from team.cycle import schema
    ok = {"verdict": "pass", "steps": [], "criteria": [], "scenarios": [{"name": "a", "status": "passed", "evidence": "e"}], "summary": "s"}
    jsonschema.validate(ok, schema("test-report"))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"verdict": "pass", "steps": [], "criteria": [], "summary": "s"}, schema("test-report"))
    assert C.DEFAULTS["claude"]["models"]["tester"] == "sonnet"


# ------------------------------------------------------------------ owner 22.11 (ai-battles day review): no proof → no acceptance

class InconclusiveRunner(FakeRunner):
    """Every review comes back `inconclusive` (the tester could not run a single scenario)."""
    def run(self, role, prompt, schema, **kw):
        self.calls.append(role)
        out = {"verdict": "inconclusive", "steps": [], "criteria": [{"criterion": "two tabs", "met": False, "evidence": "No tests found"}],
               "scenarios": [], "screenshots": [], "summary": "could not run", "first_failure": "Cannot find module balance.data.js; No tests found"}
        return RunResult(True, out, {"total_cost_usd": 0.1}, 0.1, 2, None, session_id="s", duration_s=1.0)


def _three_reviews(st: C.Settings, p: C.Product, tid: str) -> Store:
    for i in range(3):
        cyc = Cycle(st, p, InconclusiveRunner(), f"c{i}")
        cyc.run(["review"])
        cyc.store.close()
    return Store(p.dir)


def test_game_task_is_not_accepted_after_three_inconclusive_reviews(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st)
    tid = _released(p)
    s = _three_reviews(st, p, tid)
    t = s.task(tid)
    assert t["state"] == "blocked" and t["blocked_by"] == "review" and t["review_inconclusive"] == 3
    assert "not accepted without proof" in t["history"][-1]["note"] and "balance.data.js" in t["history"][-1]["note"]
    assert t["history"][-1]["note"].startswith("inconclusive 3× [no_tests]")
    qs = [q for q in s.questions(["open"]) if q["to"] == "po" and tid in q["question"]]
    assert len(qs) == 1 and "could not be checked 3 times" in qs[0]["question"] and "do not treat it as accepted" in qs[0]["question"]
    s.close()


def test_two_inconclusive_reviews_leave_the_task_released(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st)
    tid = _released(p)
    for i in range(2):
        cyc = Cycle(st, p, InconclusiveRunner(), f"c{i}")
        cyc.run(["review"])
        cyc.store.close()
    s = Store(p.dir)
    assert s.task(tid)["state"] == "released" and s.task(tid)["review_inconclusive"] == 2 and not s.questions(["open"])
    s.close()


def test_inconclusive_rule_follows_the_product_setting_and_the_profile(tmp_path: Path):
    import yaml
    st = _settings(tmp_path)
    p = _game(st)
    f = p.dir / "product.yaml"
    raw = yaml.safe_load(f.read_text(encoding="utf-8"))
    raw["review"] = {"inconclusive": "accept"}                      # a game product may ask for the pilot's rule back
    f.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    p = C.load_product(st, p.dir)
    tid = _released(p)
    s = _three_reviews(st, p, tid)
    assert s.task(tid)["state"] == "accepted" and "accepted without external proof" in s.task(tid)["history"][-1]["note"]
    s.close()
    cyc = Cycle(st, p, InconclusiveRunner(), "cx")
    assert cyc.inconclusive_policy() == "accept"
    cyc.p.raw["review"] = {"inconclusive": "nonsense"}             # an unknown value falls back to the profile default
    assert cyc.inconclusive_policy() == "ask"
    cyc.p.raw.pop("review")
    cyc.p.raw["profile"] = "site"
    assert cyc.inconclusive_policy() == "accept"
    cyc.store.close()
def test_team_play_test_argv_spec_first_no_double_dash(monkeypatch):
    """26.11: the spec after `--` was dropped by Playwright 1.65 and every «single spec» run was the whole @live suite."""
    monkeypatch.setenv("TEAM_PLAY_NPX", "npx")
    ap = play.argparse.ArgumentParser(); sub = ap.add_subparsers()
    s = sub.add_parser("test"); s.add_argument("--grep", action="append"); s.add_argument("--spec"); s.add_argument("--project")
    a = ap.parse_args(["test", "--spec", "tests/e2e/x.spec.ts", "--project", "desktop"])
    argv = play.build_test_argv(a)
    assert "--" not in argv and argv[:4] == ["npx", "playwright", "test", "tests/e2e/x.spec.ts"]
    assert "--project=desktop" in argv and "--project" not in argv and "--reporter=line,json" in argv
    a = ap.parse_args(["test", "--grep", "one", "--grep", "two"])
    argv = play.build_test_argv(a)
    assert "--grep=one|two" in argv and sum(x.startswith("--grep") for x in argv) == 1


def _play_env(tmp_path: Path, monkeypatch, npx: Path, base: str = "http://127.0.0.1:4321") -> Path:
    co = tmp_path / "checkout"; co.mkdir(exist_ok=True)
    out = tmp_path / "out"
    monkeypatch.setenv("TEAM_PLAY_CWD", str(co)); monkeypatch.setenv("TEAM_PLAY_BASE_URL", base)
    monkeypatch.setenv("TEAM_PLAY_OUT", str(out)); monkeypatch.setenv("TEAM_PLAY_NPX", str(npx))
    monkeypatch.setattr(play, "POLL_S", 0.05); monkeypatch.setattr(play, "PROGRESS_EVERY_S", 0.3); monkeypatch.setattr(play, "KILL_GRACE_S", 2)
    return out


ONE_PASS = {"suites": [{"title": "a.spec.ts", "specs": [{"title": "works", "tests": [{"status": "expected", "projectName": "desktop",
            "results": [{"status": "passed", "duration": 5}]}]}]}]}


def test_team_play_lock_wait_is_not_counted_in_the_test_limit(tmp_path: Path, monkeypatch, capsys):
    npx = _fake_npx(tmp_path, f"""
        import time
        print("x INFO [e2e-host-lock] waiting for the e2e host lock: 0 s; another Playwright run on this host holds the lock (pid 7, /w/T-1). This is normal", file=sys.stderr, flush=True)
        time.sleep(3)
        print("x INFO [e2e-host-lock] e2e host lock acquired after 3 s (pid 8, /tmp/l)", file=sys.stderr, flush=True)
        print("[1/1] [desktop] › a.spec.ts:1:1 › works", flush=True)
        time.sleep(0.5)
        pathlib.Path(os.environ["PLAYWRIGHT_JSON_OUTPUT_NAME"]).write_text(json.dumps({json.dumps(ONE_PASS)}))
        """)
    out = _play_env(tmp_path, monkeypatch, npx)
    monkeypatch.setattr(play, "TEST_TIMEOUT_S", 2); monkeypatch.setattr(play, "LOCK_WAIT_S", 10)
    rc = play.main(["test", "--spec", "a.spec.ts", "--project", "desktop"])   # 3 s of lock wait + 0.5 s of tests, limit 2 s
    cap = capsys.readouterr()
    assert rc == 0, cap.out
    import re as _re
    m = _re.search(r"passed 1 · waited for the lock (\d+)s, ran (\d+)s", cap.out)
    assert m and int(m.group(1)) >= 3 and int(m.group(2)) <= 1, cap.out
    assert "team-play: result passed" in cap.out
    assert "waiting for the e2e host lock" in cap.err and "pid 7, /w/T-1" in cap.err
    rec = [json.loads(l) for l in (out / "team-play.jsonl").read_text().splitlines()]
    assert rec[-1]["reason"] == "passed" and rec[-1]["waited_s"] >= 3 and rec[-1]["ran_s"] <= 1 and rec[-1]["spec"] == "a.spec.ts"


def test_team_play_run_timeout_stops_the_whole_group_and_shows_the_started_tests(tmp_path: Path, monkeypatch, capsys):
    pidfile = tmp_path / "child.pid"
    npx = _fake_npx(tmp_path, f"""
        import subprocess, time
        c = subprocess.Popen(["sleep", "60"]); pathlib.Path({str(pidfile)!r}).write_text(str(c.pid))
        print("x INFO [e2e-host-lock] e2e host lock acquired after 0 s (pid 8, /tmp/l)", file=sys.stderr, flush=True)
        print("[1/80] [desktop] › a.spec.ts:1:1 › first", flush=True)
        print("[2/80] [desktop] › b.spec.ts:1:1 › second", flush=True)
        time.sleep(60)
        """)
    _play_env(tmp_path, monkeypatch, npx)
    monkeypatch.setattr(play, "TEST_TIMEOUT_S", 1)
    rc = play.main(["test", "--spec", "a.spec.ts"])
    cap = capsys.readouterr()
    assert rc == 124 and "ran 1s after the lock, limit 1s" in cap.out and "2 of 80 tests had started" in cap.out
    assert "b.spec.ts:1:1 › second" in cap.out and "team-play: result run_timeout" in cap.out and "inconclusive_reason: run_timeout" in cap.out
    child = int(pidfile.read_text())
    import time as _t
    for _ in range(40):
        try:
            os.kill(child, 0)
        except ProcessLookupError:
            break
        _t.sleep(0.05)
    else:
        pytest.fail("the child of the stopped run is still alive (the process group was not stopped)")


def test_team_play_lock_timeout(tmp_path: Path, monkeypatch, capsys):
    npx = _fake_npx(tmp_path, """
        import time
        for i in range(100):
            print(f"x INFO [e2e-host-lock] waiting for the e2e host lock: {i} s; another Playwright run on this host holds the lock (pid 7, /w/T-2).", file=sys.stderr, flush=True)
            time.sleep(0.2)
        """)
    _play_env(tmp_path, monkeypatch, npx)
    monkeypatch.setattr(play, "LOCK_WAIT_S", 1); monkeypatch.setattr(play, "TEST_TIMEOUT_S", 100)
    rc = play.main(["test", "--spec", "a.spec.ts"])
    o = capsys.readouterr().out
    assert rc == 124 and "no e2e host lock within 1s (holder: pid 7, /w/T-2)" in o and "team-play: result lock_timeout" in o


def test_team_play_falls_back_to_a_local_server_when_no_test_runs_on_the_deployed_host(tmp_path: Path, monkeypatch, capsys):
    npx = _fake_npx(tmp_path, f"""
        rep = os.environ["PLAYWRIGHT_JSON_OUTPUT_NAME"]
        if os.environ.get("BASE_URL"):
            print("Error: No tests found", flush=True); sys.exit(1)
        assert "PLAYWRIGHT_BASE_URL" not in os.environ and os.environ["E2E_PORT"].isdigit()
        pathlib.Path(rep).write_text(json.dumps({json.dumps(ONE_PASS)}))
        """)
    out = _play_env(tmp_path, monkeypatch, npx)
    rc = play.main(["test", "--spec", "tests/e2e/cups.spec.ts", "--project", "desktop"])
    o = capsys.readouterr().out
    assert rc == 0 and "no test for the deployed host" in o and "against a local server of this checkout: passed 1" in o
    rec = [json.loads(l) for l in (out / "team-play.jsonl").read_text().splitlines()]
    assert [(r["local"], r["reason"]) for r in rec] == [(False, "no_tests_live"), (True, "passed")]
    monkeypatch.setenv("TEAM_PLAY_BASE_URL", "")
    assert play.main(["test", "--spec", "tests/e2e/cups.spec.ts", "--local"]) == 0


def test_inconclusive_reason_words():
    from team.cycle import inconclusive_reason
    assert inconclusive_reason({"inconclusive_reason": "lock_timeout", "summary": "timed out after 600s"}) == "lock_timeout"
    assert inconclusive_reason({"inconclusive_reason": "made-up", "first_failure": "playwright test timed out after 600s, no per-test line"}) == "run_timeout"
    assert inconclusive_reason({"limits": "team-play: playwright test stopped: no e2e host lock within 600s"}) == "lock_timeout"
    assert inconclusive_reason({"first_failure": "Error: No tests found"}) == "no_tests"
    assert inconclusive_reason({"first_failure": "not observable: the toast"}) == "not_observable"
    assert inconclusive_reason({"summary": "hmm"}) == "other"
    schema = json.loads((Path(play.__file__).parent / "schemas" / "review-verdict.schema.json").read_text())
    assert tuple(schema["properties"]["inconclusive_reason"]["enum"]) == play.INCONCLUSIVE_REASONS
    assert set(play.INCONCLUSIVE_FOR.values()) <= set(play.INCONCLUSIVE_REASONS)


def test_tester_bash_timeouts_cover_lock_wait_and_run():
    from team.cycle import tester_bash_timeouts
    ms = int(tester_bash_timeouts()["BASH_DEFAULT_TIMEOUT_MS"])
    assert ms >= (play.LOCK_WAIT_S + play.TEST_TIMEOUT_S) * 1000


def test_team_play_test_progress_line_on_stderr_at_start(tmp_path: Path, monkeypatch, capsys):
    npx = _fake_npx(tmp_path, 'print(json.dumps({"suites": []}))\n')
    monkeypatch.setenv("TEAM_PLAY_CWD", str(tmp_path)); monkeypatch.setenv("TEAM_PLAY_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("TEAM_PLAY_OUT", str(tmp_path / "out")); monkeypatch.setenv("TEAM_PLAY_NPX", str(npx))
    play.main(["test", "--spec", "a.spec.ts", "--project", "desktop", "--grep", "x", "--grep", "y"])
    cap = capsys.readouterr()
    assert "playwright test started" in cap.err and "playwright test started" not in cap.out
