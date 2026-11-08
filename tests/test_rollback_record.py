import subprocess
from pathlib import Path
from types import SimpleNamespace

from team import friction


def _g(repo, *a):
    return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                                "GIT_COMMITTER_EMAIL": "t@t", "PATH": "/usr/bin:/bin"}).stdout.strip()


def _repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _g(r, "init", "-q", "-b", "main")
    shas = []
    for i in range(3):
        (r / "f").write_text(str(i))
        _g(r, "add", "f")
        _g(r, "commit", "-q", "-m", f"commit {i}")
        shas.append(_g(r, "rev-parse", "HEAD"))
    return r, shas


def _st(tmp_path):
    return SimpleNamespace(data_dir=tmp_path / "data")


def _records(st):
    return friction.Register(friction.register_dir(st)).records


def test_rollback_writes_one_record_with_dropped_commits(tmp_path):
    r, shas = _repo(tmp_path)
    st = _st(tmp_path)
    assert friction.check_rollback(st, r) is None          # first run: only records HEAD
    _g(r, "reset", "-q", "--hard", shas[0])                 # watchdog rolls back to the first commit
    rec = friction.check_rollback(st, r)
    recs = _records(st)
    assert len(recs) == 1 and recs[0]["kind"] == "rollback"
    assert [d.split(" ", 1)[1] for d in rec["dropped"]] == ["commit 2", "commit 1"]
    assert "commit 1" in recs[0]["summary"] and "commit 2" in recs[0]["summary"]
    friction.check_rollback(st, r)                          # same HEAD again: nothing new
    assert len(_records(st)) == 1


def test_fast_forward_and_unchanged_write_no_record(tmp_path):
    r, shas = _repo(tmp_path)
    st = _st(tmp_path)
    _g(r, "checkout", "-q", shas[0])
    friction.check_rollback(st, r)
    friction.check_rollback(st, r)                          # unchanged
    _g(r, "checkout", "-q", "main")                         # fast-forward to descendant
    assert friction.check_rollback(st, r) is None
    assert _records(st) == []
