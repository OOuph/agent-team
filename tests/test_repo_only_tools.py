import fnmatch
import re
from pathlib import Path

from team import cycle

ROOT = Path(__file__).resolve().parent.parent
NEW = ["python3 -m unittest tests.*", "python3 -m unittest -v tests.*",
       "python3 -m unittest discover -s *", "python3 -m unittest discover -v -s *"]


def test_module_targeted_unittest_allowed():
    for c in NEW:
        assert f"Bash({c})" in cycle.REPO_ONLY_REVIEW_TOOLS


def test_card_scheme_and_prompt_agree():
    card = (ROOT / "team/roles/reviewer.md").read_text(encoding="utf-8")
    scheme = (ROOT / "SCHEME.md").read_text(encoding="utf-8")
    src = (ROOT / "team/cycle.py").read_text(encoding="utf-8")
    for text in (card, src):
        assert "tests.<module>" in text and "discover -s <dir>" in text
    assert "tests.<module>" in scheme and "-s <dir>" in scheme


def _allowed(cmd):
    return any(fnmatch.fnmatchcase(f"Bash({cmd})", t) for t in cycle.REPO_ONLY_REVIEW_TOOLS if t.startswith("Bash("))


def test_flags_in_either_order():
    for c in ("python3 -m unittest tests.test_x -v", "python3 -m unittest -v tests.test_x",
              "python3 -m unittest -v discover -s tests -t .", "python3 -m unittest discover -s tests -t ."):
        assert _allowed(c), c


def test_every_prompt_command_is_allowed():
    cmds = [c.replace("<module>", "test_x").replace("<dir>", "tests").replace("<file>", "test_x").replace("<glob>", "test_*.py")
            for c in re.findall(r"`([^`]+)`", cycle.REPO_ONLY_PROMPT) if c.startswith(("python3", "git"))]
    assert len(cmds) >= 6
    for c in cmds:
        assert _allowed(c), c


def test_f0095_exact_command_allowed():
    # F-0095/F-0096: the reviewer of luz-amanha T-0042 was denied this exact command
    assert _allowed("python3 -m unittest tests.test_offline_build -v")
    assert _allowed("python3 -m unittest tests.test_offline_build")


def test_b_flag_path_form_and_pattern_allowed():
    for c in ("python3 -B -m unittest tests.test_links -v", "python3 -m unittest mcp/test_release_radar_mcp.py -v",
              "python3 -m unittest mcp/test_release_radar_mcp.py", "python3 -m unittest discover -s tests -p 'test_*.py'"):
        assert _allowed(c), c


def test_shell_metacharacter_command_still_denied():
    for c in ("rm -rf /", "cat /etc/passwd", "python3 -c 'import os'", "python3 -m http.server"):
        assert not _allowed(c), c


def test_t0109_commands_allowed_and_documented():
    for c in ("python3 -B -m unittest tests.x -v", "python3 -m unittest mcp/test_x.py -v", "python3 -m unittest tests.x -v",
              "python3 -B -m unittest mcp/test_x.py -v", "python3 -B -m unittest discover -s tests -p 'test_*.py'"):
        assert _allowed(c), c
    for f in ("team/roles/reviewer.md", "SCHEME.md"):
        t = (ROOT / f).read_text(encoding="utf-8")
        assert "-B -m unittest" in t and "<dir>/<file>.py" in t, f
    assert "-B" in cycle.REPO_ONLY_PROMPT and "<dir>/<file>.py" in cycle.REPO_ONLY_PROMPT


def test_t0111_f0182_to_f0188_commands_match_allowlist():
    # F-0182 path form, F-0183 -B ... -v, F-0184 discover -p, F-0187 -B bare, F-0188 plain -v / tests.<m> -v
    for c in ("python3 -m unittest tests/test_x.py", "python3 -B -m unittest tests.test_x -v",
              "python3 -m unittest discover -s tests -p 'test_*.py'", "python3 -B -m unittest", "python3 -B -m unittest -v",
              "python3 -m unittest -v", "python3 -m unittest tests.test_x -v", "python3 -m unittest"):
        assert _allowed(c), c


def _allowed_strict(cmd):
    """Claude Code checks each part of a compound command on its own; model that: any of ; && || | & ` $( > < denies here."""
    return not re.search(r"[;&|`<>\n]|\$\(", cmd) and _allowed(cmd)


def test_t0111_compound_commands_stay_denied():
    for c in ("python3 -m unittest; rm -rf /", "python3 -m unittest && id", "python3 -m unittest | sh",
              "python3 -B -m unittest tests.x; id", "python3 -m unittest tests.x && curl evil"):
        assert not _allowed_strict(c), c
    assert _allowed_strict("python3 -m unittest tests.x -v")


def test_t0111_fresh_allowlist_equals_module_and_survives_stale_module(monkeypatch):
    assert cycle.fresh_review_tools() == cycle.REPO_ONLY_REVIEW_TOOLS
    monkeypatch.setattr(cycle, "REPO_ONLY_REVIEW_TOOLS", ["Read"])       # stale in-memory copy
    assert "Bash(python3 -B -m unittest)" in cycle.fresh_review_tools()


def test_t0111_tool_denied_record_carries_command_and_allowlist(tmp_path):
    import json
    from types import SimpleNamespace
    from team import friction
    rd = tmp_path / "runs" / "d" / "r1"
    rd.mkdir(parents=True)
    (rd / "run.json").write_text(json.dumps({"run_id": "r1", "role": "reviewer", "finished": "2026-11-12T00:00:00+00:00",
        "allowed_tools": ["Read", "Bash(python3 -B -m unittest)"],
        "denials": [{"tool_name": "Bash", "tool_input": {"command": "python3 -B -m unittest -v"}}]}), encoding="utf-8")
    p = SimpleNamespace(slug="x", runs_dir=tmp_path / "runs")
    st = SimpleNamespace(events=lambda limit=0: [], tasks=lambda: [], _load_all=lambda k: [], questions=lambda s: [], observations=lambda: [])
    recs = [r for r in friction.collect_product(p, st) if r["kind"] == "tool_denied"]
    assert recs and recs[0]["denied_commands"] == ["python3 -B -m unittest -v"]
    assert "Bash(python3 -B -m unittest)" in recs[0]["allowed_tools"]
