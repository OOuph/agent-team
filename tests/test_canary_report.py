"""The canary's report must hold before a framework change is merged, and a change of existing tests or of the canary
code waits for the owner (rework slice 7, owner 26.11, points 2 and 3 of the guide review). team/canary.py."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from team import canary, gitops
from team.cycle import Cycle

from test_store_cycle import FakeRunner, _product, _settings

T0 = datetime(2026, 11, 26, 10, 0, tzinfo=timezone.utc)
iso = lambda d: d.isoformat(timespec="seconds")   # noqa: E731


def _report(tmp_path: Path, **over) -> Path:
    p = tmp_path / "rep.json"
    rep = canary.write_report(p, cycle="c1-canary", product="rr", sha="a" * 40, started=iso(T0), ok=True, note="all phases", artefacts=["digests/c1.md"])
    if over:
        rep.update(over)
        p.write_text(json.dumps(rep), encoding="utf-8")
    return p


def test_a_good_report_of_this_revision_and_this_run_passes(tmp_path: Path):
    ok, why = canary.check_report(_report(tmp_path), "a" * 40, iso(T0))
    assert ok and "1 artefact" in why


def test_no_report_means_no_merge(tmp_path: Path):
    assert canary.check_report(None, "a" * 40, iso(T0)) == (False, "no canary report")
    assert canary.check_report(tmp_path / "missing.json", "a" * 40, iso(T0))[0] is False


def test_a_report_that_does_not_parse_means_no_merge(tmp_path: Path):
    p = tmp_path / "rep.json"
    for text in ("", "{", "[]", json.dumps({"ok": True}), json.dumps({"version": 1, "cycle": "c", "product": "p", "sha": "a" * 40, "started": "x",
                                                                    "finished": "not a time", "ok": True, "note": "", "artefacts": ["a"]})):
        p.write_text(text, encoding="utf-8")
        ok, why = canary.check_report(p, "a" * 40, iso(T0))
        assert not ok and "does not parse" in why, text


def test_a_report_of_another_revision_means_no_merge(tmp_path: Path):
    ok, why = canary.check_report(_report(tmp_path), "b" * 40, iso(T0))
    assert not ok and "another revision" in why
    assert canary.check_report(_report(tmp_path), "", iso(T0))[0] is False


def test_a_report_older_than_the_run_means_no_merge(tmp_path: Path):
    p = _report(tmp_path, finished=iso(T0 - timedelta(minutes=5)))
    ok, why = canary.check_report(p, "a" * 40, iso(T0))
    assert not ok and "older than this run" in why


def test_a_failed_or_empty_report_means_no_merge(tmp_path: Path):
    assert "the canary failed" in canary.check_report(_report(tmp_path, ok=False, note="phase error"), "a" * 40, iso(T0))[1]
    assert "the canary failed" in canary.check_report(_report(tmp_path, ok="true"), "a" * 40, iso(T0))[1]      # only a real true passes
    assert "no result artefact" in canary.check_report(_report(tmp_path, artefacts=[]), "a" * 40, iso(T0))[1]


BASE = "class Cycle:\n    def canary(self):\n        return True\n\n    def other(self):\n        return 1\n"


def test_protected_functions_changed_sees_the_function_not_the_file():
    prot = {"team/cycle.py": ["canary"], "team/canary.py": ["*"]}
    same = canary.protected_functions_changed({"team/cycle.py": BASE}, {"team/cycle.py": BASE.replace("return 1", "return 2")}, prot)
    assert same == []                                                  # another function of the hot file: not the canary's business
    moved = canary.protected_functions_changed({"team/cycle.py": BASE}, {"team/cycle.py": "import os\n\n" + BASE}, prot)
    assert moved == []                                                 # the same text on other lines is not a change
    changed = canary.protected_functions_changed({"team/cycle.py": BASE}, {"team/cycle.py": BASE.replace("return True", "return bool(1)")}, prot)
    assert changed == ["team/cycle.py:canary"]
    gone = canary.protected_functions_changed({"team/cycle.py": BASE}, {"team/cycle.py": "class Cycle:\n    pass\n"}, prot)
    assert gone == ["team/cycle.py:canary"]
    whole = canary.protected_functions_changed({"team/canary.py": "x = 1\n"}, {"team/canary.py": "x = 2\n"}, prot)
    assert whole == ["team/canary.py:*"]
    broken = canary.protected_functions_changed({"team/cycle.py": BASE}, {"team/cycle.py": "def (:\n"}, prot)
    assert broken == ["team/cycle.py:(does not parse)"]


def test_the_real_canary_and_gate_are_on_the_protected_list():
    import inspect
    from team import cycle
    names = canary.PROTECTED_FUNCTIONS["team/cycle.py"]
    assert {"canary", "release_as_pull_request", "tests_guard"} <= set(names)
    for n in names:
        assert n in canary._functions(inspect.getsource(cycle)), n      # a renamed function must not fall off the list silently


# ------------------------------------------------------------------ the merge gate of a framework pull request

def _gate(tmp_path, monkeypatch, *, guard: dict, tests_changed=(), summary="s", report=True, funcs=(), canary_ok=True):
    st = _settings(tmp_path)
    st.raw.setdefault("framework", {}).update({"auto_merge": True, "guard": guard, "canary_product": "rr"})
    p = _product(st)
    cy = Cycle(st, p, FakeRunner(), "c1")
    t = cy.store.create_task(title="t", type_="defect", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="test")
    cy.store.transition(t["id"], "ready", "po")
    merged, told = [], []
    monkeypatch.setattr(gitops, "push_branch", lambda repo, tid: "abc")
    monkeypatch.setattr(gitops, "changed_files", lambda repo, base: ["team/x.py"])
    monkeypatch.setattr(gitops, "tests_modified_or_deleted", lambda repo, base: list(tests_changed))
    monkeypatch.setattr(gitops, "open_pull_request", lambda *a, **k: "https://github.com/o/r/pull/1")
    monkeypatch.setattr(gitops, "merge_pull_request", lambda url, **kw: merged.append(url) or "deadbeef00")
    monkeypatch.setattr(gitops, "update_running_checkout", lambda *a, **k: None)
    monkeypatch.setattr(gitops, "update_pull_request_body", lambda *a, **k: None)
    monkeypatch.setattr(gitops, "comment_pull_request", lambda *a, **k: None)
    monkeypatch.setattr(gitops, "head", lambda repo: "c" * 40)
    monkeypatch.setattr(cy, "protected_functions_touched", lambda repo: list(funcs))
    monkeypatch.setattr(cy, "tell_owner", lambda text: told.append(text))

    def fake_canary(repo, product):
        if report:
            cy.canary_report = canary.report_path(st.data_dir, "c1-canary")
            canary.write_report(cy.canary_report, cycle="c1-canary", product="rr", sha="c" * 40, started=cy.canary_started or iso(T0),
                                ok=canary_ok, note="all phases", artefacts=["digests/c1.md"])
        return canary_ok, "ok"

    monkeypatch.setattr(cy, "canary", fake_canary)
    cy.release_as_pull_request(cy.store.task(t["id"]), tmp_path, {"summary": summary})
    task = cy.store.task(t["id"])
    return task["state"], bool(merged), task["history"][-1]["note"] if task["state"] == "waiting_owner" else "", told


def test_gate_off_is_the_old_behaviour(tmp_path: Path, monkeypatch):
    state, merged, _, told = _gate(tmp_path, monkeypatch, guard={}, report=False)
    assert (state, merged, told) == ("done", True, [])                  # the canary's return value alone decides, as before
    state, merged, _, _ = _gate(tmp_path / "b", monkeypatch, guard={}, tests_changed=["tests/test_a.py"],
                                summary="x\ntests-change-reason: F-0127 obsolete assert")
    assert (state, merged) == ("done", True)


def test_canary_report_gate_merges_on_a_valid_report_only(tmp_path: Path, monkeypatch):
    assert _gate(tmp_path, monkeypatch, guard={"canary_report": True})[:2] == ("done", True)
    state, merged, note, told = _gate(tmp_path / "b", monkeypatch, guard={"canary_report": True}, report=False)
    assert (state, merged) == ("waiting_owner", False) and "no canary report" in note and len(told) == 1


def test_strict_tests_new_tests_merge_changed_tests_wait_for_the_owner(tmp_path: Path, monkeypatch):
    assert _gate(tmp_path, monkeypatch, guard={"tests_strict": True})[:2] == ("done", True)       # nothing existing was changed
    state, merged, note, told = _gate(tmp_path / "b", monkeypatch, guard={"tests_strict": True}, tests_changed=["tests/test_a.py"],
                                      summary="x\ntests-change-reason: F-0127 obsolete assert")
    assert (state, merged) == ("waiting_owner", False) and "existing tests changed" in note    # a reason line no longer merges it
    assert len(told) == 1 and "\n" not in told[0] and "tests/test_a.py" in told[0] and "pull/1" in told[0]


def test_strict_tests_a_change_of_the_canary_code_waits_for_the_owner(tmp_path: Path, monkeypatch):
    state, merged, note, told = _gate(tmp_path, monkeypatch, guard={"tests_strict": True}, funcs=["team/cycle.py:canary"])
    assert (state, merged) == ("waiting_owner", False) and "canary code changed" in note and "team/cycle.py:canary" in told[0]
