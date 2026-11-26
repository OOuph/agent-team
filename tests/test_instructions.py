"""Instructions of the roles change only through the owner (rework slice 8). team/instructions.py."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from team import cli, editorial, gitops, instructions as ins
from team import config as C
from team.cycle import Cycle

from test_store_cycle import FakeRunner, _product, _settings


def _st(tmp_path: Path, on: bool = True, **extra) -> C.Settings:
    st = _settings(tmp_path)
    st.raw.setdefault("framework", {})["instructions"] = {"owner_only": on, **extra}
    return st


def test_off_by_default_and_bad_values_fall_back(tmp_path: Path):
    st = _settings(tmp_path)
    assert ins.settings(st) == {"owner_only": False, "paths": ["team/roles/", "DEPUTY.md"], "digest_every_days": 7}
    st.raw.setdefault("framework", {})["instructions"] = {"owner_only": "yes", "paths": "team/roles", "digest_every_days": 0}
    assert ins.settings(st) == {"owner_only": False, "paths": ["team/roles/", "DEPUTY.md"], "digest_every_days": 7}


def test_protected_paths():
    paths = ["team/roles/", "DEPUTY.md"]
    files = ["team/roles/po.md", "team/cycle.py", "DEPUTY.md", "docs/DEPUTY.md.txt", "team/roles_extra.py"]
    assert ins.protected(files, paths) == ["team/roles/po.md", "DEPUTY.md"]


def test_a_proposal_is_a_file_with_was_and_becomes_and_is_not_repeated(tmp_path: Path):
    st = _st(tmp_path)
    a = ins.propose(st, source="framework-retro", file="team/roles/reviewer.md", was="- old rule", becomes="- new rule", why="F-0127: denied chars",
                    evidence=["F-0127"])
    text = Path(a["path"]).read_text(encoding="utf-8")
    assert a["id"] == "P-0001" and "## Было\n- old rule" in text and "## Стало\n- new rule" in text and "status: pending" in text
    b = ins.propose(st, source="developer", file="team/roles/reviewer.md", was="x", becomes="-  new   rule", why="again")
    assert b["id"] == "P-0001" and len(ins.all_proposals(st)) == 1          # the same text for the same file: one proposal
    c = ins.propose(st, source="editor", file="products/p/editorial-lessons.md", was="", becomes="Lead with the number.", why="lesson", product="p")
    assert c["id"] == "P-0002" and "(не указано" in Path(c["path"]).read_text(encoding="utf-8")
    ins.decide(st, "P-0001", ins.REJECTED)
    assert [m["id"] for m in ins.pending(st)] == ["P-0002"]
    again = ins.propose(st, source="framework-retro", file="team/roles/reviewer.md", was="- old rule", becomes="- new rule", why="once more")
    assert again["id"] == "P-0001" and again["status"] == ins.REJECTED      # a rejected text does not come back as new
    with pytest.raises(KeyError):
        ins.decide(st, "P-0099", ins.ACCEPTED)


def test_the_owner_gets_one_list_a_week(tmp_path: Path):
    st = _st(tmp_path)
    sent: list[str] = []
    assert ins.send_digest(st, sent.append) is None                         # nothing pending: nothing sent
    ins.propose(st, source="framework-retro", file="team/roles/po.md", was="ask the owner", becomes="decide inside VISION", why="F-0001")
    ins.propose(st, source="editor", file="products/p/editorial-lessons.md", was="", becomes="Lead with the number.", why="lesson", product="p")
    t0 = datetime(2026, 11, 26, 12, tzinfo=timezone.utc)
    text = ins.send_digest(st, sent.append, now=t0)
    assert len(sent) == 1 and "2 предложений" in text and "P-0001" in text and "Было: ask the owner" in text and "Стало: decide inside VISION" in text
    assert ins.send_digest(st, sent.append, now=t0 + timedelta(days=6)) is None and len(sent) == 1
    assert ins.send_digest(st, sent.append, now=t0 + timedelta(days=7, minutes=1)) and len(sent) == 2
    assert ins.send_digest(st, sent.append, force=True, now=t0 + timedelta(days=7, minutes=2)) and len(sent) == 3
    off = _st(tmp_path / "off", on=False)
    ins.propose(off, source="x", file="team/roles/po.md", was="a", becomes="b", why="w")
    assert ins.send_digest(off, sent.append) is None                        # switch off: no weekly message


def test_a_failed_send_is_tried_again(tmp_path: Path):
    st = _st(tmp_path)
    ins.propose(st, source="x", file="team/roles/po.md", was="a", becomes="b", why="w")

    def boom(text):
        raise RuntimeError("telegram down")

    with pytest.raises(RuntimeError):
        ins.send_digest(st, boom)
    assert ins.digest_due(st)


def test_editor_lessons_become_proposals_when_on(tmp_path: Path):
    st = _st(tmp_path)
    p = _product(st)
    cy = Cycle(st, p, FakeRunner(), "c1")
    cy.keep_lessons(["Lead with the number.", "No hashtags."])
    assert editorial.read_lessons(p) == []                                  # nothing took effect by itself
    pend = ins.pending(st)
    assert [m["becomes"] for m in pend] == ["Lead with the number.", "No hashtags."] and pend[0]["file"].endswith("editorial-lessons.md")
    assert pend[0]["source"] == "editor" and pend[0]["product"] == p.slug
    cy.store.close()
    st2 = _st(tmp_path / "off", on=False)
    p2 = _product(st2)
    cy2 = Cycle(st2, p2, FakeRunner(), "c1")
    cy2.keep_lessons(["Lead with the number."])
    got = editorial.read_lessons(p2)
    assert len(got) == 1 and got[0].startswith("Lead with the number.") and not ins.pending(st2)      # off: appended, as before
    cy2.store.close()


def test_retro_role_card_proposal_is_kept_as_a_file_not_as_a_task(tmp_path: Path):
    from team import retro
    st = _st(tmp_path)
    st.raw["framework"].update({"product": "demo", "task_risks": ["low"], "issues": False, "repo": ""})
    p = _product(st)
    props = [{"title": "Reviewer: table instead of prose", "kind": "role_card", "risk": "low", "evidence": ["F-0127"], "files": ["team/roles/reviewer.md"],
              "change": "replace the prose with a table", "acceptance": ["a"], "was": "prose about denied chars", "becomes": "| bad | good |"},
             {"title": "team-fetch: add --head", "kind": "tooling", "risk": "low", "evidence": ["F-0200"], "files": ["team/fetch.py"],
              "change": "add --head N", "acceptance": ["a"]}]
    kept = [x for x in props if retro.to_instruction_proposal(st, x)]
    assert [x["title"] for x in kept] == ["Reviewer: table instead of prose"]
    m = ins.pending(st)[0]
    assert m["file"] == "team/roles/reviewer.md" and m["was"] == "prose about denied chars" and m["becomes"] == "| bad | good |" and m["evidence"] == ["F-0127"]
    off = _st(tmp_path / "off", on=False)
    assert retro.to_instruction_proposal(off, props[0]) is False            # off: it becomes a task, as before


def test_a_pull_request_touching_a_role_card_waits_for_the_owner_and_leaves_a_proposal(tmp_path: Path, monkeypatch):
    st = _st(tmp_path)
    st.raw["framework"].update({"auto_merge": True})
    p = _product(st)
    cy = Cycle(st, p, FakeRunner(), "c1")
    t = cy.store.create_task(title="reviewer card", type_="defect", problem="p", scope="team/roles/reviewer.md", acceptance=["a"], goal="G1", created_by="test")
    cy.store.transition(t["id"], "ready", "po")
    merged = []
    monkeypatch.setattr(gitops, "push_branch", lambda repo, tid: "abc")
    monkeypatch.setattr(gitops, "changed_files", lambda repo, base: ["team/roles/reviewer.md", "tests/test_x.py"])
    monkeypatch.setattr(gitops, "tests_modified_or_deleted", lambda repo, base: [])
    monkeypatch.setattr(gitops, "open_pull_request", lambda *a, **k: "https://github.com/o/r/pull/7")
    monkeypatch.setattr(gitops, "merge_pull_request", lambda url, **kw: merged.append(url) or "deadbeef00")
    monkeypatch.setattr(cy, "instruction_diff", lambda repo, path: ("- old line", "- new line"))
    monkeypatch.setattr(cy, "canary", lambda repo, product: (True, "ok"))
    cy.release_as_pull_request(cy.store.task(t["id"]), tmp_path, {"summary": "card changed"})
    task = cy.store.task(t["id"])
    assert task["state"] == "waiting_owner" and not merged and "instructions change only through the owner" in task["history"][-1]["note"]
    m = ins.pending(st)[0]
    assert m["file"] == "team/roles/reviewer.md" and m["was"] == "- old line" and m["becomes"] == "- new line" and m["ref"].endswith("/pull/7")
    cy.store.close()


def test_cli_lists_and_records_the_owners_word(tmp_path: Path, capsys):
    st = _st(tmp_path)
    ins.propose(st, source="x", file="team/roles/po.md", was="a", becomes="b", why="w")
    import argparse
    assert cli.cmd_instructions(st, argparse.Namespace(send=False, done=None, as_=None)) == 0
    assert "P-0001" in capsys.readouterr().out
    assert cli.cmd_instructions(st, argparse.Namespace(send=False, done="P-0001", as_="accepted")) == 0
    assert ins.pending(st) == [] and ins.all_proposals(st)[0]["status"] == "accepted"
    assert cli.cmd_instructions(st, argparse.Namespace(send=False, done="P-0404", as_="accepted")) == 1
