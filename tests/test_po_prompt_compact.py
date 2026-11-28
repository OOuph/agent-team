"""PO prompt, rework slice 1 (owner 26.11): finished work travels brief, the unchanging part of the prompt comes first.

Measured on ai-battles 24–26.11: a PO run cost $3.70 because 88 % of `# BACKLOG` were the full texts of 119 tasks whose
work was done (accepted, released, measured). `cycle.po_prompt.compact_finished` gives those tasks as a short card;
`cycle.po_prompt.stable_first` puts VISION first and the cycle number and the spend last. Both default to off."""
from __future__ import annotations

from pathlib import Path

import yaml

from team import config as C
from team import prompts
from team.store import Store

LONG = "detail " * 120                     # a task text long enough to see whether it travelled


def _settings(tmp_path: Path, po_prompt: dict | None = None) -> C.Settings:
    raw = {**C.DEFAULTS, "framework": {"retro_every_h": 24, "repo": "", "issues": False, "product": ""}}
    if po_prompt is not None:
        raw["cycle"] = {**raw["cycle"], "po_prompt": po_prompt}
    return C.Settings(raw, tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")


def _product(st: C.Settings, cycle: dict | None = None) -> C.Product:
    d = st.products_dir / "p1"
    d.mkdir(parents=True, exist_ok=True)
    raw = {**C.PRODUCT_DEFAULTS, "slug": "p1", "name": "p1", "host": "p1.example"}
    if cycle is not None:
        raw["cycle"] = cycle
    (d / "product.yaml").write_text(yaml.safe_dump({"slug": "p1", "name": "p1", "host": "p1.example"}), encoding="utf-8")
    (d / "VISION.md").write_text("# p1\n\nVISION-MARKER\n", encoding="utf-8")
    return C.Product("p1", d, raw, st)


def _fill(s: Store) -> dict[str, str]:
    """One task per state the PO sees; returns {state: id}."""
    ids = {}
    for state in ("ready", "in_progress", "review", "blocked", "deferred", "released", "accepted", "measured", "done", "rejected"):
        t = s.create_task(title=f"task in {state}", type_="feature", problem=f"PROBLEM-{state} {LONG}", scope=f"SCOPE-{state}",
                          acceptance=[f"ACC-{state}"], goal="G1", created_by="po", state="ready", links=[f"https://x.example/{state}"])
        s.add_result(t["id"], {"run_id": "r0", "status": "partial", "summary": "an earlier attempt"})
        s.add_result(t["id"], {"run_id": "r1", "status": "done", "summary": f"OUTCOME-{state} first line\nsecond line " + "z" * 400})
        s.transition(t["id"], state, "dispatcher", force=True, **({"accepted_at": "2026-11-25T10:00:00+00:00"} if state in ("accepted", "measured") else {}))
        ids[state] = t["id"]
    return ids


def _kw() -> dict:
    return dict(cycle_id="20261126T0931", observations=[], questions=[], metrics_md="", spent_today=12.34, cap=350.0, max_ready=3, idle=False)


def _backlog(txt: str) -> str:
    return txt.split("# BACKLOG (open tasks)", 1)[1].split("\n# RECENTLY FINISHED", 1)[0]


def test_default_is_the_old_prompt(tmp_path: Path) -> None:
    p = _product(_settings(tmp_path))
    s = Store(p.dir)
    ids = _fill(s)
    txt = prompts.po_prompt(p, s, **_kw())
    assert txt.startswith("# Cycle 20261126T0931 — PO triage")
    for state in ("ready", "released", "accepted", "measured"):
        assert f"PROBLEM-{state}" in _backlog(txt), state      # every not-closed task with its full text, as before
    assert ids["done"] not in _backlog(txt) and ids["rejected"] not in _backlog(txt)
    assert not p.po_prompt_cfg["compact_finished"] and not p.po_prompt_cfg["stable_first"]
    s.close()


def test_compact_finished_keeps_every_task_and_cuts_the_text(tmp_path: Path) -> None:
    p = _product(_settings(tmp_path), cycle={"po_prompt": {"compact_finished": True}})
    s = Store(p.dir)
    ids = _fill(s)
    old = prompts.po_prompt(_product(_settings(tmp_path)), s, **_kw())
    txt = prompts.po_prompt(p, s, **_kw())
    back = _backlog(txt)
    for state in ("ready", "in_progress", "review", "blocked", "deferred", "released", "accepted", "measured"):
        assert ids[state] in back, f"{state} task is gone from the backlog section"
    for state in ("ready", "in_progress", "review", "blocked", "deferred"):      # unfinished work: the full text
        assert f"PROBLEM-{state}" in back and f"SCOPE-{state}" in back and f"ACC-{state}" in back
    for state in ("released", "accepted", "measured"):                           # finished work: the card only
        assert f"PROBLEM-{state}" not in back and f"SCOPE-{state}" not in back and f"ACC-{state}" not in back
        assert f"OUTCOME-{state} first line" in back and f"https://x.example/{state}" in back
    assert ids["done"] not in back and ids["rejected"] not in back
    assert len(txt) < len(old) - 3 * len(LONG) + 1200
    s.close()


def test_compact_card_fields_and_outcome_limit(tmp_path: Path) -> None:
    p = _product(_settings(tmp_path, po_prompt={"compact_finished": True}))      # team.yaml switches it on as well
    s = Store(p.dir)
    ids = _fill(s)
    card = prompts.finished_brief(s.task(ids["accepted"]))
    assert set(card) == {"id", "title", "state", "type", "role", "goal", "links", "accepted_at", "outcome"}
    assert card["state"] == "accepted" and card["accepted_at"] == "2026-11-25T10:00:00+00:00"
    assert len(card["outcome"]) <= prompts.FINISHED_OUTCOME_CHARS and "\n" not in card["outcome"]
    assert card["outcome"].startswith("OUTCOME-accepted first line second line")
    bare = prompts.finished_brief({"id": "T-0001", "title": "t", "state": "released", "results": []})
    assert bare == {"id": "T-0001", "title": "t", "state": "released"}           # nothing invented for a task without results
    note = prompts.finished_brief({"id": "T-0002", "title": "t", "state": "accepted", "results": [{"status": "done"}],
                                   "history": [{"to": "accepted", "note": "tester: all criteria observed"}]})
    assert note["outcome"] == "tester: all criteria observed"                    # no result summary → the note of the last transition
    s.close()


def test_stable_first_moves_cycle_and_spend_to_the_end(tmp_path: Path) -> None:
    p = _product(_settings(tmp_path), cycle={"po_prompt": {"stable_first": True, "compact_finished": True}})
    s = Store(p.dir)
    _fill(s)
    a = prompts.po_prompt(p, s, **_kw())
    b = prompts.po_prompt(p, s, **{**_kw(), "cycle_id": "20261126T1010", "spent_today": 99.5})
    assert a.index("VISION-MARKER") < a.index("# BACKLOG (open tasks)") < a.index("# THIS CYCLE")
    assert "20261126T0931" not in a.split("# THIS CYCLE")[0] and "$12.34" not in a.split("# THIS CYCLE")[0]
    assert "Cycle 20261126T0931" in a.split("# THIS CYCLE")[1] and "$12.34" in a.split("# THIS CYCLE")[1]
    assert "Propose at most 3 Ready tasks" in a.split("# THIS CYCLE")[1]
    assert a.split("# THIS CYCLE")[0] == b.split("# THIS CYCLE")[0]              # two cycles share everything before the tail
    idle = prompts.po_prompt(p, s, **{**_kw(), "idle": True})
    assert "The team is idle" in idle.split("# THIS CYCLE")[1]
    s.close()


def test_bad_setting_falls_back_to_off(tmp_path: Path) -> None:
    p = _product(_settings(tmp_path), cycle={"po_prompt": {"compact_finished": "yes please", "stable_first": 1}})
    assert p.po_prompt_cfg == {"compact_finished": False, "stable_first": False, "merged_since": False}
    p2 = _product(_settings(tmp_path), cycle={"po_prompt": "on"})
    assert p2.po_prompt_cfg == {"compact_finished": False, "stable_first": False, "merged_since": False}
