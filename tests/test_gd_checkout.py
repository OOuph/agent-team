"""The game designer sees the repository (rework slice 12, 26.11).

ai-battles: T-0483, T-0495 and T-0501 asked for rulings on `design/gdd/22-3-sources.md`, a file in `main`. The role ran
without tools in an empty working folder and was shown only GAME-DESIGN.md, the balance tables and the documents of a
standing direction — so it answered `needs_context` («the file is not visible») and the PO asked the owner for a
checkout. It was so from the first day of the role (20.11: «no shell, everything travels in the prompt»); the new
`needs_context` path only made it visible. `cycle.game_designer.checkout` (off by default): the run gets a read-only
checkout of the base branch as its working folder and Read / Glob / Grep."""
from __future__ import annotations

from pathlib import Path

from team import config as C
from team import gitops
from team.cycle import Cycle
from team.runner import RunResult
from team.store import Store

from test_playtest import _game, _settings as _base


def _setup(tmp_path: Path, on: bool) -> tuple[C.Settings, C.Product, str]:
    st = _base(tmp_path)
    st.raw["cycle"] = {**st.raw["cycle"], "game_designer": {"checkout": on}}
    p = _game(st)
    (p.repo_dir / "design" / "gdd").mkdir(parents=True)
    (p.repo_dir / "design" / "gdd" / "22-3-sources.md").write_text("# Sources\n\nRULE-LINE-ONE\n")
    (p.repo_dir / "GAME-DESIGN.md").write_text("# GDD\n")
    gitops.git(p.repo_dir, "add", "-A"); gitops.git(p.repo_dir, "commit", "-q", "-m", "docs")
    s = Store(p.dir)
    t = s.create_task(title="design/gdd/22-4-rulings.md §5: rulings on the five sections of 22-3-sources", type_="design", problem="p",
                      scope="Only design/gdd/22-4-rulings.md. Source: design/gdd/22-3-sources.md (in main).", acceptance=["five rulings"],
                      goal="M0", created_by="po", role="game-designer", state="ready")
    s.close()
    return st, p, t["id"]


class GD:
    def __init__(self):
        self.seen, self.tools, self.prompt, self.writable, self.turns = None, None, "", None, None

    def run(self, role, prompt, schema, *, cwd, allowed_tools=None, max_turns=None, **kw):
        f = Path(cwd) / "design" / "gdd" / "22-3-sources.md"
        self.seen = f.read_text() if f.exists() else None
        self.tools, self.prompt, self.turns = allowed_tools, prompt, max_turns
        try:
            (Path(cwd) / "x.txt").write_text("x")
            self.writable = True
        except OSError:
            self.writable = False
        out = {"summary": "rulings written", "files": [{"path": "design/gdd/22-4-rulings.md", "content": "# Rulings\n§5 …\n"}]}
        return RunResult(True, out, {}, 0.3, 4, None, session_id="s", duration_s=1.0)


def test_off_is_the_old_run_without_files(tmp_path: Path):
    st, p, tid = _setup(tmp_path, False)
    assert C.DEFAULTS["cycle"]["game_designer"] == {"checkout": False}
    r = GD()
    c = Cycle(st, p, r, "c1"); c.run(["execute"]); c.store.close()
    assert r.seen is None and r.tools == [] and "REPOSITORY CHECKOUT" not in r.prompt and r.turns == 8


def test_on_the_role_sees_the_file_the_task_names(tmp_path: Path):
    st, p, tid = _setup(tmp_path, True)
    r = GD()
    c = Cycle(st, p, r, "c1"); c.run(["execute"]); c.store.close()
    assert r.seen and "RULE-LINE-ONE" in r.seen                           # the file of the repository is in the run's folder
    assert r.tools == ["Read", "Glob", "Grep"] and r.writable is False and r.turns > 8
    assert "REPOSITORY CHECKOUT" in r.prompt and "design/gdd/22-3-sources.md" in r.prompt.split("REPOSITORY CHECKOUT")[1]
    s = Store(p.dir)
    assert s.task(tid)["state"] in ("released", "accepted")               # the documents are written and merged as before
    assert "design/gdd/22-4-rulings.md" in gitops.git(p.repo_dir, "ls-tree", "-r", "--name-only", p.repo_branch)
    assert not (p.work_dir / "c1" / f"gd-{tid}" / "checkout").exists()    # the copy does not stay on the disk
    s.close()


def test_card_says_where_the_files_are():
    text = " ".join((Path(C.__file__).parent / "roles" / "game-designer.md").read_text(encoding="utf-8").split())
    assert "REPOSITORY CHECKOUT" in text and "Read, Glob and Grep" in text
