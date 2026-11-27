"""A ready node_modules per lock file instead of an install in every run and check (27.11). team/nodecache.py."""
from __future__ import annotations

from pathlib import Path

from team import config as C
from team import cycle as cycle_mod
from team import gitops, nodecache
from team.cycle import Cycle, run_tests_for_review

from test_playtest import _game, _settings as _base

INSTALL = "mkdir -p node_modules/dep && cat package-lock.json > node_modules/dep/from-lock && echo x >> ../installs.log"


def _checkout(tmp_path: Path, name: str, lock: str = "lock-1") -> Path:
    d = tmp_path / name
    d.mkdir(parents=True)
    (d / "package.json").write_text("{}")
    (d / "package-lock.json").write_text(lock)
    (d / "secret-code.ts").write_text("task code")
    return d


def _installs(cache: Path, slug: str = "g") -> int:
    f = cache / slug / "installs.log"
    return len(f.read_text().splitlines()) if f.exists() else 0


def test_strip_install():
    assert nodecache.strip_install("npm ci && npm run build && npm test") == "npm run build && npm test"
    assert nodecache.strip_install("npm ci --no-audit && npm run build") == "npm run build"
    assert nodecache.strip_install("npm install") == "true"
    assert nodecache.strip_install("npm run build && npm test") == "npm run build && npm test"
    assert nodecache.strip_install("npm ci; npm test") == "npm ci; npm test"          # not a plain && chain: left alone
    assert nodecache.strip_install("npm cite && x") == "npm cite && x"
    assert nodecache.strip_install("python3 -m pytest -q") == "python3 -m pytest -q"


def test_one_install_per_lock_file_and_a_copy_per_checkout(tmp_path: Path):
    cache = tmp_path / "cache"
    a, b = _checkout(tmp_path, "a"), _checkout(tmp_path, "b")
    assert nodecache.seed(cache, "g", a, INSTALL) and nodecache.seed(cache, "g", b, INSTALL)
    assert _installs(cache) == 1                                             # installed once, copied twice
    assert (a / "node_modules/dep/from-lock").read_text() == "lock-1" and nodecache.seeded(a) and nodecache.seeded(b)
    (a / "node_modules/dep/from-lock").write_text("a test wrote here")       # a test that writes into node_modules …
    c = _checkout(tmp_path, "c")
    assert nodecache.seed(cache, "g", c, INSTALL)
    assert (c / "node_modules/dep/from-lock").read_text() == "lock-1"        # … changes only its own checkout
    assert (b / "node_modules/dep/from-lock").read_text() == "lock-1"
    assert not (cache / "g" / nodecache.key_of(a) / "secret-code.ts").exists()   # the install saw the manifests only


def test_a_changed_lock_file_builds_another_cache(tmp_path: Path):
    cache = tmp_path / "cache"
    a = _checkout(tmp_path, "a", "lock-1")
    assert nodecache.seed(cache, "g", a, INSTALL)
    b = _checkout(tmp_path, "b", "lock-2")
    assert nodecache.seed(cache, "g", b, INSTALL) and _installs(cache) == 2
    assert (b / "node_modules/dep/from-lock").read_text() == "lock-2"
    (a / "package-lock.json").write_text("lock-2")                            # the role changed the lock in its checkout
    assert not nodecache.seeded(a)                                           # its node_modules is of the old lock: commands install as before
    assert nodecache.seed(cache, "g", a, INSTALL) is False and (a / "node_modules/dep/from-lock").read_text() == "lock-1"   # left alone


def test_no_lock_or_a_failed_install_changes_nothing(tmp_path: Path):
    cache = tmp_path / "cache"
    d = tmp_path / "plain"
    d.mkdir()
    assert nodecache.seed(cache, "g", d, INSTALL) is False and not (d / "node_modules").exists()
    e = _checkout(tmp_path, "e")
    assert nodecache.seed(cache, "g", e, "exit 3") is False and not (e / "node_modules").exists()
    assert nodecache.seed(cache, "g", e, INSTALL) is False                   # not tried again right away after a failed install
    assert _installs(cache) == 0


def test_only_the_newest_caches_are_kept(tmp_path: Path):
    cache = tmp_path / "cache"
    for i in range(nodecache.KEEP + 2):
        assert nodecache.seed(cache, "g", _checkout(tmp_path, f"c{i}", f"lock-{i}"), INSTALL)
    kept = [d for d in (cache / "g").iterdir() if d.is_dir()]
    assert len(kept) == nodecache.KEEP


# ------------------------------------------------------------------ the dispatcher uses it

def _product(tmp_path: Path, on: bool):
    st = _base(tmp_path)
    st.raw["cycle"] = {**st.raw["cycle"], "node_cache": {"enabled": on}}
    p = _game(st)
    p.raw["repo"] = {**p.raw["repo"], "install_cmd": INSTALL, "test_cmd": "npm ci && test -f node_modules/dep/from-lock && echo TESTS-OK"}
    (p.repo_dir / "package.json").write_text("{}")
    (p.repo_dir / "package-lock.json").write_text("lock-1")
    (p.repo_dir / ".gitignore").write_text("node_modules/\n")
    gitops.git(p.repo_dir, "add", "-A"); gitops.git(p.repo_dir, "commit", "-q", "-m", "node")
    return st, p


def test_review_tests_run_without_the_install_when_on(tmp_path: Path):
    st, p = _product(tmp_path, True)
    assert C.DEFAULTS["cycle"]["node_cache"] == {"enabled": False}
    c = Cycle(st, p, None, "c1")
    out = run_tests_for_review(p.repo_dir, gitops.head(p.repo_dir), tmp_path / "run", p.test_cmd, prepare=c.seed_node_modules)
    assert "$ test -f node_modules/dep/from-lock && echo TESTS-OK" in out and "exit code: 0" in out and "TESTS-OK" in out
    out2 = run_tests_for_review(p.repo_dir, gitops.head(p.repo_dir), tmp_path / "run2", p.test_cmd, prepare=c.seed_node_modules)
    assert "exit code: 0" in out2 and _installs(st.data_dir / "cache" / "node_modules", p.slug) == 1     # the second check installs nothing
    c.store.close()


def test_off_the_command_is_run_as_written(tmp_path: Path):
    st, p = _product(tmp_path, False)
    c = Cycle(st, p, None, "c1")
    assert c.seed_node_modules(tmp_path) is False
    out = run_tests_for_review(p.repo_dir, gitops.head(p.repo_dir), tmp_path / "run", "echo AS-WRITTEN", prepare=c.seed_node_modules)
    assert "$ echo AS-WRITTEN" in out and not (st.data_dir / "cache").exists()
    c.store.close()


def test_a_task_checkout_is_seeded_and_the_developer_is_told(tmp_path: Path):
    from team import prompts
    st, p = _product(tmp_path, True)
    from team.store import Store
    s = Store(p.dir)
    t = s.create_task(title="x", type_="milestone", problem="p", scope="s", acceptance=["a"], goal="M0", created_by="po", state="ready")
    s.close()
    c = Cycle(st, p, None, "c1")
    wt, branch = c.task_checkout(t)
    assert (wt / "node_modules/dep/from-lock").exists() and nodecache.seeded(wt)
    assert "node_modules" not in gitops.git(wt, "status", "--porcelain")
    pr = prompts.developer_prompt(p, t, wt, branch, [], None)
    assert "already installed" in pr and "run `test -f node_modules/dep/from-lock && echo TESTS-OK`" in pr
    c.store.close()


def test_a_repository_that_does_not_ignore_node_modules_is_not_seeded(tmp_path: Path):
    repo = gitops.init_local(tmp_path / "repo")
    (repo / "package.json").write_text("{}"); (repo / "package-lock.json").write_text("lock-1")
    gitops.git(repo, "add", "-A"); gitops.git(repo, "commit", "-q", "-m", "init")
    assert nodecache.seed(tmp_path / "cache", "g", repo, INSTALL) is False and not (repo / "node_modules").exists()
