"""`team hold` run as a real command (27.11 18:08: `team hold ai-battles --by … --why … -- bash x.sh` placed the request,
drained a running cycle of the game — and then tried to run a program called «--by»)."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


def _root(tmp_path: Path) -> Path:
    (tmp_path / "team.yaml").write_text("paths:\n  products_dir: products\n  data_dir: data\n  repos_dir: data/repos\n", encoding="utf-8")
    d = tmp_path / "products" / "fleets"
    d.mkdir(parents=True)
    (d / "product.yaml").write_text("slug: fleets\nname: fleets\nhost: ''\nrepo:\n  url: ''\n  branch: main\n  kind: service\n", encoding="utf-8")
    (d / "VISION.md").write_text("# VISION\n", encoding="utf-8")
    return tmp_path


def _hold(root: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(REPO), "TEAM_ROOT": str(root)}
    return subprocess.run([sys.executable, "-m", "team.cli", "hold", *args], cwd=root, env=env, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize("args", [
    ["fleets", "--by", "operator", "--why", "an edit", "--wait-s", "5", "--", sys.executable, "-c", "open('ran.txt','w').write('yes')"],   # as typed on 27.11
    ["--by", "operator", "--why", "an edit", "--wait-s", "5", "fleets", sys.executable, "-c", "open('ran.txt','w').write('yes')"],
    ["fleets", "--why=an edit", "--", sys.executable, "-c", "open('ran.txt','w').write('yes')"],
    ["fleets", sys.executable, "-c", "open('ran.txt','w').write('yes')"],
])
def test_the_command_runs_whatever_the_order_of_the_options(tmp_path: Path, args: list[str]):
    root = _root(tmp_path)
    cp = _hold(root, *args)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert (root / "ran.txt").read_text() == "yes" and "lock taken" in cp.stdout
    assert not (root / "products" / "fleets" / "DRAIN").exists()           # the request is removed


def test_an_unknown_option_or_no_command_is_refused_before_anything_is_drained(tmp_path: Path):
    root = _root(tmp_path)
    cp = _hold(root, "fleets", "--force", "--", "true")
    assert cp.returncode == 2 and "unknown option --force" in cp.stdout
    cp = _hold(root, "fleets", "--why", "x", "--")
    assert cp.returncode == 2 and "no command" in cp.stdout
    assert not (root / "products" / "fleets" / "DRAIN").exists()


def test_the_exit_code_of_the_command_comes_back(tmp_path: Path):
    root = _root(tmp_path)
    cp = _hold(root, "fleets", "--", sys.executable, "-c", "raise SystemExit(7)")
    assert cp.returncode == 7 and not (root / "products" / "fleets" / "DRAIN").exists()
