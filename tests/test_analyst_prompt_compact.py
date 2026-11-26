"""The analyst's prompt carries accepted tasks as cards (rework slice 10, 26.11).

ai-battles 26.11: the analyst's prompt was 816 000 bytes — the full texts of every task accepted in 72 hours — and three
runs in a row ended at the role's $1 cap (`error_max_budget_usd`), so the daily measure did not pass and 149 tasks stood
in `accepted`. `cycle.analyst_prompt.compact_tasks` (off by default) gives each task as what the effect is judged by:
id, title, type, goal, accepted_at and one line of its last result. Also here: a task the owner accepted by hand has no
`accepted_at`; the measure phase now reads the moment from its history instead of skipping it for ever."""
from __future__ import annotations

from pathlib import Path

from team import config as C
from team import prompts
from team.cycle import Cycle, accepted_moment
from team.runner import RunResult
from team.store import Store

from test_po_prompt_compact import LONG, _product, _settings


def _tasks(s: Store, n: int = 3) -> list[dict]:
    out = []
    for i in range(n):
        t = s.create_task(title=f"task {i}", type_="feature", problem=f"PROBLEM-{i} {LONG}", scope=f"SCOPE-{i}", acceptance=[f"ACC-{i}"], goal="G1",
                          created_by="po", state="ready")
        s.add_result(t["id"], {"run_id": "r", "status": "done", "summary": f"OUTCOME-{i} line one\nline two " + "z" * 400})
        s.transition(t["id"], "accepted", "reviewer", force=True, accepted_at="2026-11-25T10:00:00+00:00", released_sha="abc")
        out.append(s.task(t["id"]))
    return out


def test_default_is_the_old_prompt(tmp_path: Path):
    p = _product(_settings(tmp_path))
    s = Store(p.dir)
    ts = _tasks(s)
    txt = prompts.analyst_prompt(p, s, metrics=[], accepted=ts, posts=[])
    assert all(f"PROBLEM-{i}" in txt and f"ACC-{i}" in txt for i in range(3)) and p.analyst_prompt_cfg == {"compact_tasks": False}
    s.close()


def test_compact_keeps_every_task_and_what_the_effect_is_judged_by(tmp_path: Path):
    p = _product(_settings(tmp_path), cycle={"analyst_prompt": {"compact_tasks": True}})
    s = Store(p.dir)
    ts = _tasks(s)
    old = prompts.analyst_prompt(_product(_settings(tmp_path)), s, metrics=[], accepted=ts, posts=[])
    txt = prompts.analyst_prompt(p, s, metrics=[], accepted=ts, posts=[])
    sec = txt.split("# TASKS ACCEPTED RECENTLY", 1)[1].split("# POSTS PUBLISHED", 1)[0]
    for i, t in enumerate(ts):
        assert t["id"] in sec and f"OUTCOME-{i} line one line two" in sec and "2026-11-25T10:00:00+00:00" in sec
        assert f"PROBLEM-{i}" not in sec and f"SCOPE-{i}" not in sec and f"ACC-{i}" not in sec
    assert set(prompts.analyst_brief(ts[0])) == {"id", "title", "type", "goal", "accepted_at", "outcome"}
    assert len(prompts.analyst_brief(ts[0])["outcome"]) <= prompts.FINISHED_OUTCOME_CHARS
    assert len(txt) < len(old) - 3 * len(LONG) + 600
    s.close()


def test_a_task_accepted_by_hand_is_measured_by_the_moment_in_its_history(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    s = Store(p.dir)
    t = s.create_task(title="menu", type_="feature", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po", state="ready")
    s.transition(t["id"], "accepted", "owner", "looked at it", force=True)                 # `team task move --force`: no accepted_at
    t = s.task(t["id"])
    assert not t.get("accepted_at") and accepted_moment(t) is not None
    assert accepted_moment({"state": "accepted", "accepted_at": "2026-11-21T14:58:00+00:00"}).day == 21
    assert accepted_moment({"state": "accepted", "history": []}) is None
    old = [dict(h) for h in t["history"]]
    old[-1]["ts"] = "2026-11-21T14:58:00+00:00"
    t["history"] = old
    s.save_task(t)
    (p.dir / "metrics").mkdir(exist_ok=True)
    (p.dir / "metrics" / "2026-11-26.json").write_text("{}")
    (p.dir / "digests").mkdir(exist_ok=True)

    class Analyst:
        def run(self, role, prompt, schema, **kw):
            return RunResult(True, {"summary_md": "s", "observations": [], "task_effects": [], "goal_status": []}, {}, 0.1, 2, None, session_id="s", duration_s=1.0)

    c = Cycle(st, p, Analyst(), "c1")
    c.phase_measure()
    c.store.close()
    assert s.task(t["id"])["state"] == "done"                                             # measured and closed, not left in `accepted` for ever
    s.close()
