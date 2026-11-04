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
