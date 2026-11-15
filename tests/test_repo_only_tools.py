"""Reviewer of repo_only tasks (owner 15.11): no shell, the dispatcher runs the tests and puts the output in the prompt.

Replaces the unittest allowlist tests (T-0109…T-0154): the allowlist never matched in Claude Code (patterns with `*` in
the middle), so the reviewer kept getting «required approval» and six retro PRs only added patterns."""
from __future__ import annotations

import subprocess
from pathlib import Path

from team import cycle, gitops

ROOT = Path(__file__).resolve().parent.parent


def test_reviewer_has_no_shell_beyond_team_fetch():
    bash = [t for t in cycle.REPO_ONLY_REVIEW_TOOLS if t.startswith("Bash(")]
    assert bash and all(t.startswith("Bash(team-fetch") for t in bash)
    assert {"Read", "Glob", "Grep"} <= set(cycle.REPO_ONLY_REVIEW_TOOLS)


def test_card_scheme_and_prompt_say_the_dispatcher_runs_tests():
    card = (ROOT / "team/roles/reviewer.md").read_text(encoding="utf-8")
    scheme = (ROOT / "SCHEME.md").read_text(encoding="utf-8")
    assert "has already run" in card and "no shell" in card and "-B" not in card
    assert "запускает диспетчер" in scheme
    assert "{tests}" in cycle.REPO_ONLY_PROMPT and "unittest" not in cycle.REPO_ONLY_PROMPT


def _repo(tmp_path: Path, test_body: str) -> tuple[Path, str]:
    repo = gitops.init_local(tmp_path / "repo")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text(test_body)
    (repo / "mcp").mkdir()
    (repo / "mcp" / "test_m.py").write_text("import unittest\nclass M(unittest.TestCase):\n    def test_m(self):\n        self.assertTrue(True)\n")
    gitops.git(repo, "add", "-A"); gitops.git(repo, "commit", "-q", "-m", "init")
    return repo, gitops.head(repo)


def test_run_tests_for_review_passes_and_writes(tmp_path: Path):
    body = ("import unittest, pathlib\nclass T(unittest.TestCase):\n    def test_writes(self):\n"
            "        pathlib.Path('scratch.txt').write_text('ok')\n        self.assertTrue(True)\n")
    repo, sha = _repo(tmp_path, body)
    out = cycle.run_tests_for_review(repo, sha, tmp_path / "run")
    assert "$ python3 -m unittest discover -s tests\n" in out and "$ python3 -m unittest discover -s mcp\n" in out
    assert out.count("exit code: 0") == 2 and "OK" in out                # a writable copy: tests may write (#163)


def test_run_tests_for_review_reports_failure_and_hides_secrets(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-value")
    body = ("import os, unittest\nclass T(unittest.TestCase):\n    def test_env(self):\n"
            "        self.assertNotIn('TELEGRAM_BOT_TOKEN', os.environ)\n    def test_fail(self):\n        self.assertEqual(1, 2)\n")
    repo, sha = _repo(tmp_path, body)
    out = cycle.run_tests_for_review(repo, sha, tmp_path / "run")
    assert "exit code: 1" in out and "FAIL: test_fail" in out and "FAIL: test_env" not in out


def test_run_tests_for_review_uses_test_cmd(tmp_path: Path):
    repo, sha = _repo(tmp_path, "import unittest\n")
    out = cycle.run_tests_for_review(repo, sha, tmp_path / "run", "echo custom-suite")
    assert out.startswith("$ echo custom-suite\nexit code: 0\ncustom-suite")


def test_tests_gate_ignores_appended_tests(tmp_path: Path):
    repo = gitops.init_local(tmp_path / "r")
    (repo / "tests").mkdir()
    for n in "abc":
        (repo / "tests" / f"test_{n}.py").write_text(f"def test_{n}():\n    assert 1\n")
    gitops.git(repo, "add", "-A"); gitops.git(repo, "commit", "-q", "-m", "base")
    base = gitops.git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    gitops.git(repo, "checkout", "-q", "-b", "task")
    with (repo / "tests" / "test_a.py").open("a") as f:                  # appended a test: not a change of tests
        f.write("\n\ndef test_a2():\n    assert 2\n")
    (repo / "tests" / "test_new.py").write_text("def test_n():\n    assert 1\n")
    (repo / "tests" / "test_b.py").write_text("def test_b():\n    assert 0\n")   # an assertion rewritten
    subprocess.run(["git", "rm", "-q", "tests/test_c.py"], cwd=repo, check=True)
    gitops.git(repo, "add", "-A"); gitops.git(repo, "commit", "-q", "-m", "task")
    assert gitops.tests_modified_or_deleted(repo, base) == ["tests/test_b.py", "tests/test_c.py"]
