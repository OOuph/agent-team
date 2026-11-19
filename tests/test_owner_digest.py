from datetime import datetime, timezone
from types import SimpleNamespace

from team import billing, retro

NOW = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
ORDER = ["PRs merged in the last 24 h", "PRs waiting for a human", "Rollbacks to last-good", "Spend today"]


def _st(tmp_path):
    return SimpleNamespace(raw={"framework": {"protected_paths": ["team/store.py", "deploy/"]}}, data_dir=tmp_path, root=tmp_path)


def _digest(monkeypatch, st, prs, records, ledger):
    monkeypatch.setattr(billing, "load", lambda s: {"spend": ledger})
    return retro.owner_digest(st, prs, records, now=NOW, cap=10.0)


def test_digest_full(monkeypatch, tmp_path):
    prs = {"merged": [{"number": 7, "title": "Add thing"}],
           "open": [{"number": 8, "title": "Touch store", "body": "", "files": [{"name": "team/store.py", "status": "modified"}]},
                    {"number": 9, "title": "Edit test", "body": "", "files": [{"name": "tests/a.py", "status": "modified"}]},
                    {"number": 10, "title": "Edit test ok", "body": "tests-change-reason: F-0127 x",
                     "files": [{"name": "tests/a.py", "status": "modified"}]}]}
    recs = [{"kind": "rollback", "ts": "2026-11-08T05:00:00+00:00", "summary": "code rolled back"}]
    md = _digest(monkeypatch, _st(tmp_path), prs, recs, {"2026-11-08": 3.5})
    lines = md.strip().splitlines()
    assert lines[0] == "## Owner digest" and len(lines) <= 7
    pos = [next(i for i, l in enumerate(lines) if k in l) for k in ORDER]
    assert pos == sorted(pos)
    assert "#7 Add thing" in md and "#8" in md and "#9" in md and "#10" not in md
    assert "code rolled back" in md and "$3.50 of $10.00" in md


def test_digest_none_and_unknown(monkeypatch, tmp_path):
    md = _digest(monkeypatch, _st(tmp_path), {"merged": [], "open": []}, [], {})
    for k in ORDER[:3]:
        assert any(k in l and l.endswith("none") for l in md.splitlines())
    assert "Spend today: unknown" in md
    md = _digest(monkeypatch, _st(tmp_path), None, [], {"2026-11-08": 1})
    assert md.count("unknown") == 2


def test_digest_line_limit(monkeypatch, tmp_path):
    prs = {"merged": [{"number": i, "title": "t" * 200} for i in range(30)], "open": []}
    md = _digest(monkeypatch, _st(tmp_path), prs, [], {"2026-11-08": 1})
    assert len(md.strip().splitlines()) <= 7 and "+26 more" in md


def test_digest_five_lines_with_one_question(monkeypatch, tmp_path):
    md = _digest(monkeypatch, _st(tmp_path), {"merged": [], "open": []}, [], {"2026-11-08": 1})
    assert len([l for l in md.splitlines()[2:] if l]) == 4
    monkeypatch.setattr(billing, "load", lambda s: {"spend": {"2026-11-08": 1}})
    md = retro.owner_digest(_st(tmp_path), {"merged": [], "open": []}, [], now=NOW, cap=10.0, question="Raise cap?\nPlease")
    body = md.splitlines()[2:]
    assert len(body) == 5 and body[-1] == "- Question for the owner: Raise cap? Please"


def test_card_states_five_line_rule():
    from pathlib import Path
    card = (Path(__file__).resolve().parent.parent / "team/roles/framework-retro.md").read_text()
    assert "at most 5 lines" in card and "owner_question" in card
