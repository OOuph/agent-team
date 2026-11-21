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
    raw = C._merge(C.DEFAULTS, {"cycle": {"review_after_min": 0, "persona_every_n_cycles": 0, "digest_hours_utc": [0]}})
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
        assert a[:3] == ["playwright", "test", "--reporter=json"], a
        assert os.environ["BASE_URL"] == "http://127.0.0.1:4321", os.environ.get("BASE_URL")
        assert "--grep" in a and a[a.index("--grep") + 1] == "rooms"
        print("Running 3 tests")
        print(json.dumps({json.dumps(SAMPLE_REPORT)}))
        sys.exit(1)
        """)
    monkeypatch.setenv("TEAM_PLAY_CWD", str(co)); monkeypatch.setenv("TEAM_PLAY_BASE_URL", "http://127.0.0.1:4321")
    monkeypatch.setenv("TEAM_PLAY_OUT", str(out)); monkeypatch.setenv("TEAM_PLAY_NPX", str(npx))
    rc = play.main(["test", "--grep", "rooms"])
    o = capsys.readouterr().out
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
        proj = a[a.index("--project") + 1] if "--project" in a else ""
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
    real = play.subprocess.run
    def spy(argv, **kw):
        seen.append(kw["env"].get("E2E_MOBILE")); return real(argv, **kw)
    monkeypatch.setattr(play.subprocess, "run", spy)
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
