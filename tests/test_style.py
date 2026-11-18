from __future__ import annotations

from pathlib import Path

from team import style


def test_catalogue_and_checklist_from_vendored_file() -> None:
    cat = style.catalogue()
    assert cat.startswith("Source: humanizer v4.0.0")
    assert "### 1. Not X but Y" in cat and "Project overrides" in cat
    assert not cat.lstrip().startswith("---")                      # front matter stripped after the source line
    lines = [ln for ln in style.checklist().splitlines() if ln.startswith("- §")]
    assert len(lines) == 26
    assert lines[0].startswith("- §1 Not X but Y: not X but Y")
    assert any(ln.startswith("- §8 Dashes as the universal connector:") for ln in lines)
    assert all(len(ln) < 260 for ln in lines)


def test_missing_catalogue_is_silent(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(style, "CATALOGUE_FILE", tmp_path / "nope.md")
    assert style.catalogue() == "" and style.checklist() == ""
