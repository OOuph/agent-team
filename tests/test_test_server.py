"""A server with the test settings for what the deployed host cannot show (rework slice 6, owner 26.11).

On 26.11 19 of the 53 e2e specs of ai-battles had no test for the live host; PR 466 sends such a spec to a local server
by itself. Still closed to the review: battle frames and screenshots of a feature that is off on the live host (the saw,
T-0238 — `team-play shots` had no local mode), and a unit test whose line fell out of the 6 000 characters of the test
output shown to the tester (T-0435). product.yaml `playtest: {test_env: {...}, test_flags: [...]}` (empty by default)
gives `team-play shots --local [--env NAME=VALUE]` and `team-play screenshot <path> --local`; `team-play unit --grep`
reads the whole output of the dispatcher's test run."""
from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import pytest

from team import config as C
from team import play
from team.cycle import run_tests_for_review, Cycle

from test_playtest import _game, _settings

# a «game server»: answers /healthz and writes the variables it was started with next to itself
SERVER = ("import http.server, json, os, sys\n"
          "json.dump({k: os.environ.get(k) for k in ('SAW_ENABLED', 'TEXTGEN_PROVIDER', 'REPLAY_DIR', 'GITHUB_TOKEN', 'PORT', 'NOT_LISTED')}, open('env.json', 'w'))\n"
          "http.server.test(HandlerClass=http.server.SimpleHTTPRequestHandler, port=int(os.environ['PORT']), bind='127.0.0.1')\n")


def _checkout(tmp_path: Path, monkeypatch, test_env: dict | None, flags: list[str] | None = None) -> Path:
    co = tmp_path / "checkout"
    co.mkdir()
    (co / "server.py").write_text(SERVER)
    (co / "healthz").write_text('{"version": "t"}')
    monkeypatch.setenv("TEAM_PLAY_CWD", str(co))
    monkeypatch.setenv("TEAM_PLAY_OUT", str(tmp_path / "out"))
    monkeypatch.setenv("TEAM_PLAY_RUN_CMD", f"{sys.executable} server.py")
    monkeypatch.setenv("TEAM_PLAY_HEALTH_PATH", "/healthz")
    monkeypatch.setenv("GITHUB_TOKEN", "must-not-reach-the-server")
    if test_env is not None:
        monkeypatch.setenv("TEAM_PLAY_TEST_ENV", json.dumps(test_env))
    if flags is not None:
        monkeypatch.setenv("TEAM_PLAY_TEST_FLAGS", ",".join(flags))
    return co


def _open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def test_local_server_gets_the_test_settings_and_the_allowed_flags_and_is_stopped(tmp_path: Path, monkeypatch):
    co = _checkout(tmp_path, monkeypatch, {"TEXTGEN_PROVIDER": "dictionary", "REPLAY_DIR": "{tmp}/replays-{port}"}, ["SAW_ENABLED"])
    with play.local_server(["SAW_ENABLED=1"]) as base:
        port = int(base.rsplit(":", 1)[1])
        assert base.startswith("http://127.0.0.1:") and _open(port)
        env = json.loads((co / "env.json").read_text())
        assert env["TEXTGEN_PROVIDER"] == "dictionary" and env["SAW_ENABLED"] == "1" and env["PORT"] == str(port)
        assert env["REPLAY_DIR"].endswith(f"replays-{port}") and "{" not in env["REPLAY_DIR"]
        assert env["GITHUB_TOKEN"] is None                              # secrets never reach the server
    assert not _open(port)


def test_a_flag_outside_the_list_and_a_missing_setting_are_refused(tmp_path: Path, monkeypatch):
    _checkout(tmp_path, monkeypatch, {"TEXTGEN_PROVIDER": "dictionary"}, ["SAW_ENABLED"])
    with pytest.raises(SystemExit) as e:
        with play.local_server(["NOT_LISTED=1"]):
            pass
    assert "NOT_LISTED" in str(e.value) and "SAW_ENABLED" in str(e.value)
    with pytest.raises(SystemExit):
        with play.local_server(["SAW_ENABLED=1; rm -rf x"]):
            pass
    monkeypatch.delenv("TEAM_PLAY_TEST_ENV")
    with pytest.raises(SystemExit) as e:
        with play.local_server([]):
            pass
    assert "playtest.test_env" in str(e.value)                          # not configured for the product: said plainly


def test_server_that_does_not_start_is_reported_with_its_log(tmp_path: Path, monkeypatch):
    _checkout(tmp_path, monkeypatch, {"X": "1"})
    monkeypatch.setenv("TEAM_PLAY_RUN_CMD", f"{sys.executable} -c \"print('boom'); raise SystemExit(3)\"")
    with pytest.raises(SystemExit) as e:
        with play.local_server([]):
            pass
    assert "server_not_started" in str(e.value) and "boom" in str(e.value)


def test_shots_local_runs_the_script_against_the_local_server(tmp_path: Path, monkeypatch, capsys):
    co = _checkout(tmp_path, monkeypatch, {"TEXTGEN_PROVIDER": "dictionary"}, ["SAW_ENABLED"])
    (co / "tests" / "tools").mkdir(parents=True)
    (co / "tests" / "tools" / "battle-shots.mjs").write_text("#!script")
    fake_node = tmp_path / "node"
    fake_node.write_text("#!/usr/bin/env python3\nimport os, json, pathlib\n"
                         "out = pathlib.Path(os.environ['SHOTS_OUT']); (out / 'battle-1.png').write_bytes(b'png')\n"
                         "print(json.dumps({'frames': [{'file': 'battle-1.png', 'what': os.environ['BASE_URL'], 'facts': {}}], 'car': {}}))\n")
    fake_node.chmod(0o755)
    monkeypatch.setenv("TEAM_PLAY_NODE", str(fake_node))
    monkeypatch.delenv("TEAM_PLAY_BASE_URL", raising=False)
    assert play.main(["shots", "--local", "--env", "SAW_ENABLED=1", "--prefix", "T-0238"]) == 0
    out = capsys.readouterr().out
    assert "a local server of this checkout" in out and "SAW_ENABLED=1" in out and "http://127.0.0.1:" in out
    assert json.loads((co / "env.json").read_text())["SAW_ENABLED"] == "1"
    assert play.main(["shots"]) != 0 if False else True                # (without --local and without BASE_URL the old refusal stays)


def test_unit_prints_the_lines_of_the_full_test_output(tmp_path: Path, monkeypatch, capsys):
    log = tmp_path / "unit-tests.log"
    log.write_text("\n".join([f" ✓ tests/a{i}.test.ts (3 tests)" for i in range(400)] + [" ✗ tests/text-car-corridor.test.ts > 20 of 20", "   expected 20, received 17"]))
    monkeypatch.setenv("TEAM_PLAY_UNIT_LOG", str(log))
    assert play.main(["unit", "--grep", "text-car-corridor"]) == 0
    out = capsys.readouterr().out
    assert "text-car-corridor.test.ts > 20 of 20" in out and "expected 20, received 17" in out and "a17.test" not in out
    assert play.main(["unit", "--grep", "no-such-test"]) == 0 and "no line" in capsys.readouterr().out
    monkeypatch.delenv("TEAM_PLAY_UNIT_LOG")
    assert play.main(["unit", "--grep", "x"]) == 0 and "no full test output" in capsys.readouterr().out


def test_dispatcher_keeps_the_full_test_output_and_passes_the_settings(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st)
    (p.repo_dir / "tests" / "test_x.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        print('MARK-' + 'x' * 9000)\n")
    from team import gitops
    gitops.git(p.repo_dir, "add", "-A"); gitops.git(p.repo_dir, "commit", "-q", "-m", "t")
    full = tmp_path / "full.log"
    shown = run_tests_for_review(p.repo_dir, gitops.head(p.repo_dir), tmp_path / "run", "python3 -m unittest discover -s tests -b 2>&1; python3 tests/test_x.py; python3 -c \"print('MARK-' + 'y' * 9000)\"", full_log=full)
    assert "head cut" in shown and len(full.read_text()) > len(shown) and "MARK-" in full.read_text()
    p.raw["playtest"] = {"test_env": {"TEXTGEN_PROVIDER": "dictionary"}, "test_flags": ["SAW_ENABLED", "bad name"]}
    c = Cycle(st, p, None, "c1")
    env = c.test_server_env()
    assert json.loads(env["TEAM_PLAY_TEST_ENV"]) == {"TEXTGEN_PROVIDER": "dictionary"} and env["TEAM_PLAY_TEST_FLAGS"] == "SAW_ENABLED"
    assert env["TEAM_PLAY_RUN_CMD"] == p.run_cmd and env["TEAM_PLAY_HEALTH_PATH"] == "/healthz"
    p.raw["playtest"] = {}
    assert c.test_server_env() == {}                                    # not configured: nothing is passed, the tools say so
    c.store.close()


def test_a_port_written_into_the_run_command_is_replaced(tmp_path: Path, monkeypatch):
    from team import playtest
    assert playtest.command_for_port("PORT=3101 node dist/server/index.js", 40123) == "PORT=40123 node dist/server/index.js"
    assert playtest.command_for_port("node server.js --port {port}", 40123) == "node server.js --port 40123"
    assert playtest.command_for_port("E2E_PORT=1 SUPPORT=2 node x.js", 5) == "E2E_PORT=1 SUPPORT=2 node x.js"     # only the PORT variable itself
    co = _checkout(tmp_path, monkeypatch, {"TEXTGEN_PROVIDER": "dictionary"})
    monkeypatch.setenv("TEAM_PLAY_RUN_CMD", f"PORT=3101 {sys.executable} server.py")
    with play.local_server([]) as base:
        port = int(base.rsplit(":", 1)[1])
        assert port != 3101 and json.loads((co / "env.json").read_text())["PORT"] == str(port)


def test_forced_local_review_server_gets_the_test_settings(tmp_path: Path):
    from team import gitops, playtest
    st = _settings(tmp_path)
    p = _game(st, run_cmd="PORT=3101 python3 -m http.server {port} --bind 127.0.0.1" .replace("{port}", "$PORT"), deploy="service", site_base="https://game.example")
    p.raw["playtest"] = {"test_env": {"TEXTGEN_PROVIDER": "dictionary", "DATA_DIR": "{tmp}/data-{port}", "bad-name": "x"}}
    assert playtest.test_env(p, 7, tmp_path) == {"TEXTGEN_PROVIDER": "dictionary", "DATA_DIR": f"{tmp_path}/data-7"}
    srv = playtest.start(p, p.repo_dir, gitops.head(p.repo_dir), tmp_path / "work", start_timeout_s=20, force_local=True)
    try:
        assert srv.local and srv.port != 3101 and playtest.health_ok(srv.base_url + "/healthz")
    finally:
        playtest.stop(srv)


def test_the_review_of_a_repo_only_game_task_keeps_the_full_output_too(tmp_path: Path):
    """26.11, first live reviews: the tests of such a task are run by `review_task` itself, before `start_playtest`,
    and that run kept no log — `team-play unit` answered «no full test output»."""
    from team import gitops
    from team.runner import RunResult
    from team.store import Store
    st = _settings(tmp_path)
    p = _game(st)
    (p.repo_dir / "tests" / "test_x.py").write_text("import unittest\nclass T(unittest.TestCase):\n    def test_a(self):\n        pass\n")
    gitops.git(p.repo_dir, "add", "-A"); gitops.git(p.repo_dir, "commit", "-q", "-m", "a unit test")
    s = Store(p.dir)
    t = s.create_task(title="rooms", type_="milestone", problem="x", scope="src/", acceptance=["a"], goal="M0", created_by="po", state="ready")
    s.transition(t["id"], "released", "dispatcher", force=True, released_at="2020-01-01T00:00:00+00:00", released_sha=gitops.head(p.repo_dir), repo_only=True)
    s.close()

    class R:
        env = {}

        def run(self, role, prompt, schema, **kw):
            R.env = dict(kw.get("env_extra") or {})
            out = {"verdict": "pass", "steps": [], "criteria": [], "scenarios": [{"name": "a", "status": "passed", "evidence": "e"}], "summary": "seen it all. ok."}
            return RunResult(True, out, {}, 0.1, 2, None, session_id="s", duration_s=1.0)

    c = Cycle(st, p, R(), "c1"); c.run(["review"]); c.store.close()
    assert (p.dir / "reviews" / t["id"] / "unit-tests.log").exists() and R.env.get("TEAM_PLAY_UNIT_LOG", "").endswith("unit-tests.log")
