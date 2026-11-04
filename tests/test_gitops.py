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


def test_retry_conflict_keeps_earlier_commit(tmp_path):
    r = _repo(tmp_path)
    gitops.start_task_branch(r, "T-1")
    (r / "a.txt").write_text("mine\n")
    git(r, "commit", "-qam", "T-1 attempt")
    attempt = git(r, "rev-parse", "HEAD")
    git(r, "checkout", "-q", "main")
    (r / "a.txt").write_text("theirs\n")
    git(r, "commit", "-qam", "base moved")
    gitops.start_task_branch(r, "T-1")
    assert git(r, "merge-base", "--is-ancestor", attempt, "HEAD", check=False) == ""
    assert "T-1 attempt" in git(r, "log", "--format=%s")
    assert git(r, "merge-base", "--is-ancestor", "main", "HEAD", check=False) == ""
    assert git(r, "status", "--porcelain") == ""


def _remote_setup(tmp_path):
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = _repo(tmp_path)
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-q", "origin", "main")
    return origin, seed


def test_remote_only_task_branch_is_continued(tmp_path):
    origin, seed = _remote_setup(tmp_path)
    gitops.start_task_branch(seed, "T-1")
    (seed / "b.txt").write_text("b\n")
    git(seed, "add", "-A")
    git(seed, "commit", "-qm", "T-1 remote attempt")
    git(seed, "push", "-q", "origin", "task/T-1")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    assert git(clone, "rev-parse", "--verify", "-q", "refs/heads/task/T-1", check=False) in ("", None)
    assert gitops.start_task_branch(clone, "T-1") == "task/T-1"
    assert git(clone, "rev-parse", "--abbrev-ref", "HEAD") == "task/T-1"
    assert "T-1 remote attempt" in git(clone, "log", "--format=%s")


def test_remote_without_task_branch_starts_at_base(tmp_path):
    origin, seed = _remote_setup(tmp_path)
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(origin), str(clone))
    assert gitops.start_task_branch(clone, "T-1") == "task/T-1"
    assert git(clone, "rev-parse", "HEAD") == git(clone, "rev-parse", "origin/main")
