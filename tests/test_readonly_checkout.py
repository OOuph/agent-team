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


def test_tests_can_copy_and_write_under_reviewer_sandbox(tmp_path):
    import sys
    repo = tmp_path / "r"
    (repo / "sub").mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    (repo / "sub" / "a.txt").write_text("x")
    (repo / "test_copy.py").write_text(
        "import shutil, tempfile, pathlib\n"
        "def test_copy():\n"
        "    d = pathlib.Path(tempfile.mkdtemp()) / 'c'\n"
        "    shutil.copytree('.', d, ignore=shutil.ignore_patterns('.git'))\n"
        "    (d / 'sub' / 'a.txt').unlink()\n"
        "    (d / 'sub' / 'b.txt').write_text('y')\n"
        "    (d / 'new.txt').write_text('z')\n")
    run("add", ".")
    run("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
    dest = gitops.readonly_checkout(repo, gitops.head(repo), tmp_path / "w" / "checkout")
    env = {**os.environ, **gitops.scratch_env(dest)}
    assert (tmp_path / "w" / "scratch").is_dir()
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "test_copy.py"], cwd=dest, env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    assert not os.access(dest / "sub" / "a.txt", os.W_OK) or os.geteuid() == 0
    assert not (dest / ".pytest_cache").exists()
