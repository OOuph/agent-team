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


def test_t0109_commands_allowed_and_documented():
    for c in ("python3 -B -m unittest tests.x -v", "python3 -m unittest mcp/test_x.py -v", "python3 -m unittest tests.x -v",
              "python3 -B -m unittest mcp/test_x.py -v", "python3 -B -m unittest discover -s tests -p 'test_*.py'"):
        assert _allowed(c), c
    for f in ("team/roles/reviewer.md", "SCHEME.md"):
        t = (ROOT / f).read_text(encoding="utf-8")
        assert "-B -m unittest" in t and "<dir>/<file>.py" in t, f
    assert "-B" in cycle.REPO_ONLY_PROMPT and "<dir>/<file>.py" in cycle.REPO_ONLY_PROMPT


def test_t0120_forms_allowed_and_escape_hatches_denied():
    for c in ("python3 -B -m unittest tests.test_links -v", "python3 -m unittest mcp/test_x.py",
              "python3 -m unittest mcp/test_x.py -v", "python3 -B -m unittest -v", "python3 -B -m unittest discover -v -s tests"):
        assert _allowed(c), c
    for c in ("python3 -c 'print(1)'", "python3 -m pip install x", "python3 -B -c 'x'"):
        assert not _allowed(c), c


def _promised_commands():
    """Every `python3 -m unittest ...` command in backticks in the reviewer card, SCHEME.md and the repo_only prompt,
    except those the card names as denied (preceded by 'denied example')."""
    out = set()
    texts = [(ROOT / "team/roles/reviewer.md").read_text(encoding="utf-8"), cycle.REPO_ONLY_PROMPT]
    for text in texts:
        for m in re.finditer(r"`(python3 -m unittest[^`]*)`", text):
            before = text[max(0, m.start() - 40):m.start()]
            if "denied" in before.lower() or "No `-B`" in before:
                continue
            if "[" in m.group(1):
                c = m.group(1)
                out.add(re.sub(r" ?\[-v\]", "", c))
                out.add(re.sub(r"\[-v\]", "-v", c))
                continue
            out.add(m.group(1).replace("<module>", "test_x").replace("<dir>", "tests").replace("<glob>", "test_*.py"))
    return out


def test_t0126_every_promised_unittest_command_matches_an_entry():
    cmds = _promised_commands()
    assert len(cmds) >= 5, cmds
    for c in cmds:
        assert _allowed(c), c


def test_t0126_machine_links_reviewer_command_allowed():
    # F-0188: `python3 -m unittest tests.test_machine_links -v` was denied
    for c in ("python3 -m unittest tests.test_machine_links -v", "python3 -m unittest -v tests.test_machine_links",
              "python3 -m unittest tests.test_machine_links"):
        assert _allowed(c), c


def test_t0126_removing_an_entry_denies_its_command(monkeypatch):
    cmd = "python3 -m unittest tests.test_machine_links -v"
    matching = [t for t in cycle.REPO_ONLY_REVIEW_TOOLS if fnmatch.fnmatchcase(f"Bash({cmd})", t)]
    assert matching
    monkeypatch.setattr(cycle, "REPO_ONLY_REVIEW_TOOLS", [t for t in cycle.REPO_ONLY_REVIEW_TOOLS if t not in matching])
    assert not _allowed(cmd)


def test_t0136_argv_allows_flag_orders():
    for c in ("python3 -B -m unittest tests.test_go_pages -v", "python3 -m unittest tests.x", "python3 -B -m unittest -v tests.x",
              "python3 -m unittest tests.x -v", "python3 -B -m unittest tests.x", "python3 -m unittest discover -v -s tests -t .",
              "python3 -m unittest -v discover -s tests", "python3 -B -m unittest discover -s tests -p 'test_*.py' -v"):
        assert cycle.unittest_argv_allowed(c), c


def test_t0136_argv_denies_injection_and_other_interpreters():
    for c in ("python3 -m unittest tests.x; ls", "python3 -c 'print(1)'", "python3 -m unittest tests.x && id",
              "python3 -m unittest tests.x $(id)", "python3 -m unittest tests.x | sh", "python3 -m unittest -v -v tests.x",
              "python3 -m unittest ../x/test.py", "python3 -m pip install x", "python3 -m unittest tests.x -k foo",
              "python3 -X -m unittest tests.x"):
        assert not cycle.unittest_argv_allowed(c), c


def test_t0136_patterns_cover_every_accepted_form():
    for c in ("python3 -B -m unittest tests.test_go_pages -v", "python3 -B -m unittest -v tests.x", "python3 -m unittest tests.x -v",
              "python3 -B -m unittest discover -v -s tests -t ."):
        assert _allowed(c), c
