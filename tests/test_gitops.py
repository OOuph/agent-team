from __future__ import annotations

from team import gitops
from team.gitops import git


def _repo(tmp_path):
    r = gitops.init_local(tmp_path / "r")
    (r / "a.txt").write_text("a\n")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    return r


def test_fresh_task_starts_at_base(tmp_path):
    r = _repo(tmp_path)
    assert gitops.start_task_branch(r, "T-1") == "task/T-1"
    assert git(r, "rev-parse", "HEAD") == git(r, "rev-parse", "main")


def test_retry_keeps_earlier_commit_and_merges_base(tmp_path):
    r = _repo(tmp_path)
    gitops.start_task_branch(r, "T-1")
    (r / "b.txt").write_text("b\n")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "T-1 attempt")
    git(r, "checkout", "-q", "main")
    (r / "c.txt").write_text("c\n")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "base moved")
    gitops.start_task_branch(r, "T-1")
    log = git(r, "log", "--format=%s")
    assert "T-1 attempt" in log and "base moved" in log


def test_retry_conflict_falls_back_to_fresh(tmp_path):
    r = _repo(tmp_path)
    gitops.start_task_branch(r, "T-1")
    (r / "a.txt").write_text("mine\n")
    git(r, "commit", "-qam", "T-1 attempt")
    git(r, "checkout", "-q", "main")
    (r / "a.txt").write_text("theirs\n")
    git(r, "commit", "-qam", "base moved")
    gitops.start_task_branch(r, "T-1")
    assert git(r, "rev-parse", "HEAD") == git(r, "rev-parse", "main")
    assert git(r, "status", "--porcelain") == ""
