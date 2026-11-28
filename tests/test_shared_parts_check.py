"""Measure 2 of 28.11: the shared parts of the repository change only with the label `core-change` on the pull request.
The check is a shell script the GitHub workflow calls; here it runs on a small repository."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / ".github" / "shared-parts.sh"
ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _git(cwd: Path, *args: str) -> str:
    cp = subprocess.run(["git", *args], cwd=cwd, env=ENV, capture_output=True, text=True, timeout=60)
    assert cp.returncode == 0, cp.stderr
    return cp.stdout.strip()


def _change(repo: Path, files: dict[str, str]) -> tuple[str, str]:
    base = _git(repo, "rev-parse", "HEAD")
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    _git(repo, "add", "-A"); _git(repo, "commit", "-q", "-m", "change")
    return base, _git(repo, "rev-parse", "HEAD")


def _check(repo: Path, base: str, head: str, labels: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), base, head, labels], cwd=repo, env=ENV, capture_output=True, text=True, timeout=60)


def test_a_product_change_needs_no_label_and_a_shared_change_does(tmp_path: Path):
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "start")
    _change(tmp_path, {"README.md": "x\n"})
    own = _change(tmp_path, {"products/hittheline/product.yaml": "a: 1\n", "docs/notes/hittheline.md": "n\n", "products-retired/old/x.yaml": "o\n"})
    cp = _check(tmp_path, *own)
    assert cp.returncode == 0 and "no label needed" in cp.stdout
    # 28.11: #584 changed team/cycle.py, team/runner.py, role cards and schemas from a product session
    core = _change(tmp_path, {"team/cycle.py": "x = 1\n", "team/roles/po.md": "card\n", "products/hittheline/product.yaml": "a: 2\n"})
    cp = _check(tmp_path, *core)
    assert cp.returncode == 1 and "team/cycle.py" in cp.stdout and "team/roles/po.md" in cp.stdout and "products/hittheline" not in cp.stdout
    assert "2 shared file(s)" in cp.stdout
    assert _check(tmp_path, *core, "bug core-change").returncode == 0
    assert _check(tmp_path, *core, "not-core-change").returncode == 1            # the label, not a word that contains it
    for name in ("update-code.sh", ".github/workflows/tests.yml", "tests/test_x.py", "pyproject.toml", "products/README.md"):
        assert _check(tmp_path, *_change(tmp_path, {name: "y\n"})).returncode == 1, name


def test_the_workflow_runs_the_suite_and_the_check_on_every_pull_request_into_main():
    text = (REPO / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
    assert "branches: [main]" in text and "labeled, unlabeled" in text
    assert "python -m pytest tests" in text and 'TEAM_PUBLISH_DRY_RUN: "1"' in text
    assert "bash .github/shared-parts.sh" in text and "SHARED_LABEL: core-change" in text
