"""backup-state.sh (owner 22.11, ai-battles: «копия записей боёв и журнала»): the daily snapshot also covers the data of
service products. The script runs in a temp dispatcher root against a local bare repository as the state repo and a temp
services tree. No network, no root."""
from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "backup-state.sh"
SLUG = "game"


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def tree(tmp_path: Path) -> dict[str, Path]:
    if not shutil.which("rsync"):
        pytest.skip("rsync is not installed")
    disp = tmp_path / "dispatcher"
    (disp / "products" / "p1").mkdir(parents=True)
    (disp / "products" / "p1" / "VISION.md").write_text("vision\n", encoding="utf-8")
    (disp / "team.yaml").write_text("roles: {}\n", encoding="utf-8")
    (disp / ".env").write_text("GITHUB_TOKEN=test-token-never-used\n", encoding="utf-8")
    shutil.copy2(SCRIPT, disp / "backup-state.sh")
    remote = tmp_path / "remote.git"
    _git("init", "-q", "--bare", "--initial-branch=main", str(remote))
    state = disp / "data" / "state-repo"
    state.parent.mkdir(parents=True)
    _git("clone", "-q", str(remote), str(state))
    _git("config", "user.name", "t", cwd=state)
    _git("config", "user.email", "t@example.org", cwd=state)
    _git("checkout", "-q", "-B", "main", cwd=state)
    data = tmp_path / "services" / SLUG / "data"
    (data / "replays").mkdir(parents=True)
    (data / "gen-requests").mkdir()
    (data / "replays" / "a1.summary.json").write_text('{"id":"a1"}', encoding="utf-8")
    (data / "replays" / "a1.replay.json.gz").write_bytes(b"\x1f\x8b" + b"0" * 2000)
    (data / "gen-requests" / "gen-requests-2026-11-22.jsonl").write_text('{"text":"fast buggy"}\n', encoding="utf-8")
    (data / "balance-stats.json").write_text("{}", encoding="utf-8")
    (tmp_path / "services" / "no-data-yet" / "releases").mkdir(parents=True)     # a service without a data directory
    return {"disp": disp, "remote": remote, "state": state, "services": tmp_path / "services", "data": data}


def _run(t: dict[str, Path], **env: str) -> subprocess.CompletedProcess[str]:
    e = {**os.environ, "SERVICES_DIR": str(t["services"]), **env}
    return subprocess.run(["bash", str(t["disp"] / "backup-state.sh")], capture_output=True, text=True, env=e, timeout=120)


def _pushed(t: dict[str, Path]) -> list[str]:
    return _git("--git-dir", str(t["remote"]), "ls-tree", "-r", "--name-only", "main").split()


def test_service_data_goes_to_the_tarball_and_the_small_part_to_the_state_repo(tree):
    r = _run(tree)
    assert r.returncode == 0, r.stdout + r.stderr
    pushed = _pushed(tree)
    assert "products/p1/VISION.md" in pushed and "team.yaml" in pushed
    assert f"services/{SLUG}/data/gen-requests/gen-requests-2026-11-22.jsonl" in pushed
    assert f"services/{SLUG}/data/replays/a1.summary.json" in pushed
    assert f"services/{SLUG}/data/balance-stats.json" in pushed
    assert not [p for p in pushed if p.endswith(".replay.json.gz")], "battle bodies must not reach git"
    assert not [p for p in pushed if ".env" in p], "secrets must not reach git"
    backups = tree["disp"] / "data" / "backups"
    assert len(list(backups.glob("products-*.tgz"))) == 1
    svc = list(backups.glob("services-*.tgz"))
    assert len(svc) == 1
    with tarfile.open(svc[0]) as tf:
        names = tf.getnames()
    assert f"{SLUG}/data/replays/a1.replay.json.gz" in names, "the local tarball keeps the battle bodies"
    assert f"{SLUG}/data/gen-requests/gen-requests-2026-11-22.jsonl" in names
    assert "test-token-never-used" not in r.stdout + r.stderr


def test_records_the_game_pruned_stay_in_the_state_repo_and_new_ones_are_added(tree):
    assert _run(tree).returncode == 0
    (tree["data"] / "replays" / "a1.summary.json").unlink()                      # the game's own retention removed it
    (tree["data"] / "replays" / "b2.summary.json").write_text('{"id":"b2"}', encoding="utf-8")
    with (tree["data"] / "gen-requests" / "gen-requests-2026-11-22.jsonl").open("a", encoding="utf-8") as f:
        f.write('{"text":"sniper"}\n')
    r = _run(tree, BACKUP_KEEP="1")
    assert r.returncode == 0, r.stdout + r.stderr
    pushed = _pushed(tree)
    assert f"services/{SLUG}/data/replays/a1.summary.json" in pushed
    assert f"services/{SLUG}/data/replays/b2.summary.json" in pushed
    journal = _git("--git-dir", str(tree["remote"]), "show", f"main:services/{SLUG}/data/gen-requests/gen-requests-2026-11-22.jsonl")
    assert "sniper" in journal and "fast buggy" in journal
    assert len(list((tree["disp"] / "data" / "backups").glob("services-*.tgz"))) <= 1, "BACKUP_KEEP prunes old tarballs"


def test_without_service_data_the_product_state_is_still_saved(tree):
    r = _run(tree, SERVICES_DIR=str(tree["disp"] / "no-such-dir"))
    assert r.returncode == 0, r.stdout + r.stderr
    pushed = _pushed(tree)
    assert "products/p1/VISION.md" in pushed
    assert not [p for p in pushed if p.startswith("services/")]
    assert not list((tree["disp"] / "data" / "backups").glob("services-*.tgz"))
    assert "no service data" in r.stdout
