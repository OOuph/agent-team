"""Run profile (owner 21.11, frictions F23/F30): where the time of a run went — categories, longest calls, waste flags,
redaction — from a synthetic events file; the receive timestamp the runner stamps on events; the report table."""
from __future__ import annotations

import json
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

from team import config as C
from team import report, runprofile
from team.runner import EVENT_TS_KEY, Runner, killed_kind

T0 = 1_800_000_000.0
SPEC = "npx playwright test tests/e2e/two-players.spec.ts --reporter=line"


class Events:
    """Builds stream-json events the way the CLI emits them: an assistant event with tool_use blocks, a user event
    with the tool_result, each stamped with the receive time."""

    def __init__(self, t0: float = T0, stamp: bool = True):
        self.t, self.stamp, self.n, self.events = t0, stamp, 0, []
        self._add({"type": "system", "subtype": "init", "session_id": "s1"})

    def _add(self, e: dict) -> None:
        if self.stamp:
            e[EVENT_TS_KEY] = round(self.t, 3)
        self.events.append(e)

    def call(self, tool: str, inp: dict, seconds: float, *, result: bool = True, think: float = 1.0) -> "Events":
        self.n += 1
        self.t += think
        self._add({"type": "assistant", "message": {"content": [{"type": "text", "text": "…"},
                                                               {"type": "tool_use", "id": f"tu{self.n}", "name": tool, "input": inp}]}})
        self.t += seconds
        if result:
            self._add({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": f"tu{self.n}", "content": "ok"}]}})
        return self

    def bash(self, cmd: str, seconds: float, **kw) -> "Events":
        return self.call("Bash", {"command": cmd}, seconds, **kw)


def _debug_run() -> Events:
    """The run of the owner's example in miniature: one e2e spec re-run again and again, git and reading take nothing."""
    ev = Events()
    ev.call("Read", {"file_path": "/repo/src/server.ts"}, 0.2)
    ev.call("Grep", {"pattern": "joinRoom", "path": "src"}, 0.1)
    ev.bash("cd /repo && git status", 0.3)
    ev.bash("npm ci", 40)
    ev.bash("npm run build", 20)
    ev.bash("npm test", 15)
    for i in range(6):
        ev.bash(f"cd /repo && {SPEC} 2>&1 | tail -{20 + i}", 60)
    ev.bash("npx playwright test", 120)
    ev.bash("npx playwright test --reporter=line", 130)
    ev.call("Edit", {"file_path": "/repo/src/server.ts", "old_string": "a", "new_string": "b"}, 0.1)
    ev.bash("cat PROTOCOL.md", 0.1)
    ev.bash("GITHUB_TOKEN=ghp_verysecret123 curl -H 'Authorization: Bearer abc.def' https://x-access-token:tok123@github.com/o/r.git && sleep 30", 30)
    ev.call("TodoRead", {}, 0.1)
    ev.call("StructuredOutput", {"status": "done"}, 0.1)
    return ev


def test_profile_from_a_synthetic_events_file(tmp_path: Path):
    ev = _debug_run()
    f = tmp_path / "events.jsonl"
    f.write_text("\n".join(json.dumps(e) for e in ev.events) + "\n", encoding="utf-8")
    events = [json.loads(ln) for ln in f.read_text(encoding="utf-8").splitlines()]
    wall = ev.t - T0
    prof = runprofile.build(events, wall_s=wall, started=T0)
    cats = {c["name"]: c for c in prof["categories"]}
    assert prof["timed"] and prof["calls"] == 18                       # StructuredOutput is the answer, not a call
    assert cats["e2e"]["calls"] == 8 and cats["e2e"]["s"] == 610.0     # 6 × 60 s on the spec + 120 + 130 full runs
    assert cats["install"] == {"name": "install", "calls": 1, "s": 40.0, "share": cats["install"]["share"]}
    assert cats["build"]["calls"] == 1 and cats["unit_tests"]["calls"] == 1 and cats["git"]["calls"] == 1
    assert cats["read_search"]["calls"] == 3                           # Read, Grep and `cat` in the shell
    assert cats["edit"]["calls"] == 1 and cats["shell"]["calls"] == 1 and cats["other"]["calls"] == 1
    assert prof["categories"][0]["name"] == "e2e" and 0.84 < cats["e2e"]["share"] < 0.86
    assert abs(prof["tool_s"] - sum(c["s"] for c in prof["categories"])) < 0.5 and prof["wall_s"] == round(wall, 1)
    assert abs(prof["model_s"] - (prof["wall_s"] - prof["tool_s"])) < 0.2

    longest = prof["longest"]
    assert len(longest) == 5 and [c["s"] for c in longest] == sorted((c["s"] for c in longest), reverse=True)
    assert longest[0] == {"s": 130.0, "category": "e2e", "tool": "Bash", "call": "npx playwright test --reporter=line"}
    assert all(len(c["call"]) <= 160 for c in longest)

    kinds = {f["kind"]: f for f in prof["flags"]}
    assert kinds["repeat"]["count"] == 6 and SPEC in kinds["repeat"]["text"] and "tail" not in kinds["repeat"]["text"]
    assert kinds["dominant"]["category"] == "e2e"
    assert kinds["full_e2e"]["count"] == 2                             # the spec runs are narrow, the two bare runs are full

    one = runprofile.line(prof, role="developer", task="T-0042")
    assert one.startswith(f"developer T-0042: {wall:.0f} s (tools ") and "e2e 85 % in 8 calls" in one and "2 full e2e runs" in one
    text = runprofile.render(prof, "run c1-dev-T-0042")
    assert "by category: e2e 610 s (85 %) in 8 calls" in text and "flag: same command 6×" in text


def test_secrets_are_redacted_in_the_longest_calls():
    ev = Events()
    ev.bash("GITHUB_TOKEN=ghp_verysecret123 API_KEY='k-1' curl -H 'Authorization: Bearer abc.def' https://x-access-token:tok123@github.com/o/r.git", 50)
    ev.bash("echo sk-ant-api03-zzz > /tmp/x", 40)
    prof = runprofile.build(ev.events)
    blob = json.dumps(prof)
    for secret in ("ghp_verysecret123", "abc.def", "tok123", "k-1", "sk-ant-api03-zzz"):
        assert secret not in blob
    assert "GITHUB_TOKEN=***" in prof["longest"][0]["call"] and "x-access-token:***" in prof["longest"][0]["call"]


def test_a_killed_run_counts_the_call_it_was_cut_in():
    ev = Events()
    ev.bash("git status", 1)
    ev.bash(SPEC, 0, result=False)                                     # the limit hit while Playwright was still running
    prof = runprofile.build(ev.events, wall_s=1500.0, started=T0, killed="timeout", limit_s=1500)
    top = prof["longest"][0]
    assert top["unfinished"] and top["category"] == "e2e" and top["s"] > 1490
    assert prof["killed"] == "timeout" and "killed by the time limit (1500 s)" in runprofile.line(prof, role="developer", task="T-0001")
    assert "cut off by the kill" in runprofile.render(prof)
    # not killed: a call without a result is not charged
    assert runprofile.build(ev.events, wall_s=1500.0, started=T0)["categories"][0]["name"] == "git"


def test_events_without_timestamps_give_counts_only():
    ev = Events(stamp=False)
    ev.bash(SPEC, 60).bash("git status", 1)
    prof = runprofile.build(ev.events, wall_s=100.0)
    assert not prof["timed"] and prof["tool_s"] == 0 and prof["calls"] == 2 and prof["longest"] == []
    assert "counts only" in runprofile.render(prof) and "e2e 1 calls" in runprofile.line(prof)
    # an ISO `timestamp` field (other writers of events.jsonl) is understood too
    iso = [{"type": "assistant", "timestamp": "2026-11-21T10:00:00Z", "message": {"content": [{"type": "tool_use", "id": "a", "name": "Bash", "input": {"command": "npm ci"}}]}},
           {"type": "user", "timestamp": "2026-11-21T10:00:30Z", "message": {"content": [{"type": "tool_result", "tool_use_id": "a", "content": ""}]}}]
    assert runprofile.build(iso)["categories"] == [{"name": "install", "calls": 1, "s": 30.0, "share": 1.0}]


def test_parallel_calls_are_not_counted_twice_in_tool_time():
    events = [{"type": "assistant", EVENT_TS_KEY: T0, "message": {"content": [
        {"type": "tool_use", "id": "a", "name": "Read", "input": {"file_path": "a"}}, {"type": "tool_use", "id": "b", "name": "Read", "input": {"file_path": "b"}}]}},
        {"type": "user", EVENT_TS_KEY: T0 + 10, "message": {"content": [{"type": "tool_result", "tool_use_id": "a"}]}},
        {"type": "user", EVENT_TS_KEY: T0 + 10, "message": {"content": [{"type": "tool_result", "tool_use_id": "b"}]}}]
    prof = runprofile.build(events)
    assert prof["tool_s"] == 10.0 and prof["categories"][0] == {"name": "read_search", "calls": 2, "s": 20.0, "share": 1.0}


def test_categories_and_thresholds_are_settings():
    ev = Events()
    for _ in range(3):
        ev.bash("make play-test", 100)
    base = runprofile.build(ev.events)
    assert base["categories"][0]["name"] == "build" and not [f for f in base["flags"] if f["kind"] == "repeat"]
    cfg = runprofile.settings({"extra_categories": [{"name": "e2e", "tools": ["Bash"], "pattern": r"\bmake play-test\b"}], "repeat_threshold": 2},
                              {"max_full_e2e": 5})                     # team.yaml, then product.yaml on top
    prof = runprofile.build(ev.events, cfg=cfg)
    assert prof["categories"][0]["name"] == "e2e" and {f["kind"] for f in prof["flags"]} == {"repeat", "dominant"}
    bad = runprofile.settings({"extra_categories": [{"name": "x", "pattern": "("}]})   # a broken regex is skipped, not fatal
    assert runprofile.build(ev.events, cfg=bad)["categories"][0]["name"] == "build"


def _fake_cli(tmp_path: Path, body: str) -> Path:
    fake = tmp_path / "fakeclaude"
    fake.write_text("#!/usr/bin/env python3\nimport sys, json, time\nsys.stdin.read()\n" + body, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return fake


def test_runner_stamps_the_receive_time_on_every_event(tmp_path: Path):
    fake = _fake_cli(tmp_path, (
        "def out(e):\n    print(json.dumps(e), flush=True)\n"
        "out({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {'command': 'npm ci'}}]}})\n"
        "time.sleep(1.2)\n"
        "out({'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1', 'content': 'ok'}]}})\n"
        "out({'type': 'result', 'subtype': 'success', 'is_error': False, 'result': '', 'structured_output': {'a': 1}, 'total_cost_usd': 0.0, 'num_turns': 1})\n"))
    rd = tmp_path / "run"
    res = Runner({"binary": str(fake)}, {"dev": {"description": "d", "prompt": "p"}}).run("dev", "task", {"type": "object"}, max_usd=0.1, cwd=tmp_path / "w", run_dir=rd)
    assert res.ok and res.started > 0 and res.killed is None and killed_kind(res) is None
    lines = [json.loads(ln) for ln in (rd / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 3 and all(isinstance(e.get(EVENT_TS_KEY), float) for e in lines)
    prof = runprofile.build(lines, wall_s=res.duration_s, started=res.started)
    assert prof["categories"][0]["name"] == "install" and 1.0 <= prof["categories"][0]["s"] < 5.0


def test_runner_reports_which_limit_killed_the_run(tmp_path: Path, caplog):
    fake = _fake_cli(tmp_path, (
        "print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {'command': 'npx playwright test'}}]}}), flush=True)\n"
        "time.sleep(30)\n"))
    r = Runner({"binary": str(fake)}, {"dev": {"description": "d", "prompt": "p"}})
    res = r.run("dev", "task", None, max_usd=0.1, cwd=tmp_path / "w", timeout_s=2, inactivity_s=60)
    assert not res.ok and res.killed == "timeout" and res.error == "timeout after 2s" and killed_kind(res) == "timeout"
    prof = runprofile.build(res.events, wall_s=res.duration_s, started=res.started, killed=res.killed, limit_s=2)
    assert prof["longest"][0]["unfinished"] and prof["longest"][0]["category"] == "e2e"
    res = r.run("dev", "task", None, max_usd=0.1, cwd=tmp_path / "w", timeout_s=60, inactivity_s=1)
    assert res.killed == "inactivity" and killed_kind(res) == "inactivity"
    with caplog.at_level("WARNING", logger="team.runner"):             # the hard ceiling holds even for a direct caller
        r.run("dev", "task", None, max_usd=0.1, cwd=tmp_path / "w", timeout_s=99_999, inactivity_s=1)
    assert "above the hard ceiling 3600" in caplog.text


# ------------------------------------------------------------------ report: «куда ушло время прогонов»

def _product(tmp_path: Path) -> C.Product:
    st = C.Settings(C._merge(C.DEFAULTS, {}), tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")
    d = st.products_dir / "game"
    d.mkdir(parents=True)
    (d / "product.yaml").write_text("slug: game\nname: Game\nhost: ''\nrepo:\n  url: ''\n", encoding="utf-8")
    (d / "VISION.md").write_text("# Game\n", encoding="utf-8")
    return C.load_product(st, d)


def _run_json(p: C.Product, cycle: str, label: str, role: str, prof: dict | None, *, finished: datetime, killed: str | None = None,
              duration: float = 0.0) -> None:
    rd = p.runs_dir / cycle / label
    rd.mkdir(parents=True)
    (rd / "run.json").write_text(json.dumps({"run_id": f"{cycle}-{label}", "role": role, "ok": killed is None, "error": "timeout after 1500s" if killed else None,
                                             "duration_s": duration or (prof or {}).get("wall_s"), "finished": finished.isoformat(timespec="seconds"),
                                             "limits": {"timeout_s": 1500}, "killed": killed, "profile": prof}), encoding="utf-8")


def test_report_has_the_run_time_table_and_task_show_lists_the_runs(tmp_path: Path):
    p = _product(tmp_path)
    now = datetime.now(timezone.utc)
    ev = _debug_run()
    prof = runprofile.build(ev.events, wall_s=1500.0, started=T0, killed="timeout", limit_s=1500)
    stamp = now.strftime("%Y%m%dT%H%M")
    _run_json(p, stamp, "dev-T-0042", "developer", prof, finished=now, killed="timeout")
    _run_json(p, stamp, "review-T-0042", "reviewer", runprofile.build(Events().call("Read", {"file_path": "a"}, 90).events, wall_s=120.0), finished=now)
    _run_json(p, stamp, "dev-T-00421", "developer", None, finished=now, duration=30.0)             # another task, an old run without a profile
    old = now - timedelta(days=3)
    _run_json(p, old.strftime("%Y%m%dT%H%M"), "dev-T-0042", "developer", prof, finished=old, killed="timeout")

    summ = runprofile.period_summary(p, now - timedelta(hours=12))
    assert summ["runs"] == 3 and summ["killed"] == 1
    dev = summ["roles"]["developer"]
    assert dev["runs"] == 2 and dev["killed"] == 1 and dev["categories"][0][0] == "e2e" and dev["flags"]["full_e2e"] == 1
    assert list(summ["roles"]) == ["developer", "reviewer"]             # the role that took the most time first

    led = report.ledger(p.st, [p], 12)
    md = report.ledger_md(led)
    assert "Куда ушло время прогонов" in md and "остановлено по лимиту 1" in md
    row = next(ln for ln in md.splitlines() if ln.startswith("| developer |"))
    assert "e2e 610" in row and row.split("|")[6].strip() == "1"
    assert report.compact_for_prompt(led)["products"][0]["run_time_seconds_by_role"]["developer"]["killed"] == 1
    assert report.run_time_md({"roles": {}}) == []

    runs = runprofile.task_runs(p, "T-0042")
    assert [r["role"] for r in runs] == ["developer", "developer", "reviewer"]      # both cycles, oldest first, not T-00421
    assert all("T-00421" not in r["run_id"] for r in runs)
    text = runprofile.task_runs_md(p, "T-0042")
    assert text.startswith("# RUN PROFILES of T-0042 (3 runs") and "killed by the time limit (1500 s)" in text and "limit 1500 s" in text
    assert "no profile recorded" in runprofile.task_runs_md(p, "T-00421") and runprofile.task_runs_md(p, "T-9999") == ""
