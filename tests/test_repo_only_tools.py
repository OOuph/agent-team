import fnmatch
import re
from pathlib import Path

from team import cycle

ROOT = Path(__file__).resolve().parent.parent
NEW = ["python3 -m unittest tests.*", "python3 -m unittest -v tests.*",
       "python3 -m unittest discover -s *", "python3 -m unittest discover -v -s *"]


def test_module_targeted_unittest_allowed():
    for c in NEW:
        assert f"Bash({c})" in cycle.REPO_ONLY_REVIEW_TOOLS


def test_card_scheme_and_prompt_agree():
    card = (ROOT / "team/roles/reviewer.md").read_text(encoding="utf-8")
    scheme = (ROOT / "SCHEME.md").read_text(encoding="utf-8")
    src = (ROOT / "team/cycle.py").read_text(encoding="utf-8")
    for text in (card, src):
        assert "tests.<module>" in text and "discover -s <dir>" in text
    assert "tests.<module>" in scheme and "-s <dir>" in scheme


def _allowed(cmd):
    return any(fnmatch.fnmatchcase(f"Bash({cmd})", t) for t in cycle.REPO_ONLY_REVIEW_TOOLS if t.startswith("Bash("))


def test_flags_in_either_order():
    for c in ("python3 -m unittest tests.test_x -v", "python3 -m unittest -v tests.test_x",
              "python3 -m unittest -v discover -s tests -t .", "python3 -m unittest discover -s tests -t ."):
        assert _allowed(c), c


def test_every_prompt_command_is_allowed():
    cmds = [c.replace("<module>", "test_x").replace("<dir>", "tests").replace("<file>", "test_x").replace("<glob>", "test_*.py")
            for c in re.findall(r"`([^`]+)`", cycle.REPO_ONLY_PROMPT) if c.startswith(("python3", "git"))]
    assert len(cmds) >= 6
    for c in cmds:
        assert _allowed(c), c


def test_f0095_exact_command_allowed():
    # F-0095/F-0096: the reviewer of luz-amanha T-0042 was denied this exact command
    assert _allowed("python3 -m unittest tests.test_offline_build -v")
    assert _allowed("python3 -m unittest tests.test_offline_build")


def test_b_flag_path_form_and_pattern_allowed():
    for c in ("python3 -B -m unittest tests.test_links -v", "python3 -m unittest mcp/test_release_radar_mcp.py -v",
              "python3 -m unittest mcp/test_release_radar_mcp.py", "python3 -m unittest discover -s tests -p 'test_*.py'"):
        assert _allowed(c), c


def test_shell_metacharacter_command_still_denied():
    for c in ("rm -rf /", "cat /etc/passwd", "python3 -c 'import os'", "python3 -m http.server"):
        assert not _allowed(c), c
