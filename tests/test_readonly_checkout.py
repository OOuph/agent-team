import os
import subprocess

from team import gitops


def test_readonly_checkout(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    (repo / "a.txt").write_text("x")
    run("add", ".")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
    sha = gitops.head(repo)
    dest = gitops.readonly_checkout(repo, sha, tmp_path / "w" / "checkout")
    assert (dest / "a.txt").read_text() == "x"
    assert not os.access(dest / "a.txt", os.W_OK) or os.geteuid() == 0
    assert gitops.git(dest, "remote") == ""
    gitops.readonly_checkout(repo, sha, dest)    # idempotent
