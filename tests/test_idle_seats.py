"""Seats that stood idle with ready tasks (ai-battles 26.11, 19:13–22:00 UTC; owner: «почему не тратятся токены»).

Three causes, each silent:
  * the PO scheduled its tasks for 00:05 UTC (spend was above 80 % of the cap), and the open-task check — written for
    marketing slots, «same role, same start moment = one slot» — dropped two of three developer tasks as duplicates of
    the first, because they shared that moment;
  * a lane that found its task not startable (scheduled for later) ended without a line in the log;
  * a task the owner created for the role `artist` — the product's word for the `designer` role — had no seat and was
    never picked, without a word either."""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from team import cli
from team.cycle import Cycle, canonical_role, duplicate_task
from team.store import Store

from test_parallel_lanes import LaneRunner, _commit, _product, _settings, _state, _task


def test_two_developer_tasks_scheduled_for_the_same_moment_are_two_tasks():
    nb = "2026-11-27T00:05:00Z"
    open_ = [{"id": "T-0529", "title": "Tests: builder hash pins", "role": "developer", "not_before": nb}]
    assert duplicate_task(open_, {"title": "Composite weapons step 2: weapon_recipes.json", "role": "developer", "not_before": nb}) is None
    assert duplicate_task(open_, {"title": "Entry queue thresholds", "role": "developer", "not_before": nb}) is None
    assert duplicate_task(open_, {"title": "tests builder hash pins", "role": "developer", "not_before": nb})["id"] == "T-0529"   # the title rule stays
    posts = [{"id": "T-0006", "title": "Daily post 03.11 for bluesky", "role": "marketer", "not_before": nb}]
    assert duplicate_task(posts, {"title": "Post about the release, bluesky", "role": "marketer", "not_before": nb})["id"] == "T-0006"   # a slot is a slot


def test_a_lane_says_why_it_did_not_start_its_task(tmp_path: Path, caplog):
    st = _settings(tmp_path)
    p = _product(st)
    tid = _task(p, "developer", "scheduled task")
    s = Store(p.dir)
    t = s.task(tid); t["not_before"] = "2099-01-01T00:05:00+00:00"; s.save_task(t); s.close()
    r = LaneRunner()
    with caplog.at_level(logging.INFO):
        c = Cycle(st, p, r, "c1"); c.run(["execute"]); c.store.close()
    assert r.calls == []
    line = next(m for m in caplog.messages if tid in m and "not started" in m)
    assert "scheduled for 2099-01-01" in line and "not_before" in line
    s = Store(p.dir)
    ev = [e for e in s.events() if e["kind"] == "execute.idle"]
    assert len(ev) == 1 and ev[0]["detail"]["not_started"][0]["task"] == tid       # one record per cycle: why the seats had nothing to do
    s.close()


def test_artist_is_the_designer_and_an_unknown_role_is_said_aloud(tmp_path: Path, caplog):
    assert canonical_role("artist") == "designer" and canonical_role("developer") == "developer" and canonical_role("wizard") == "wizard"
    st = _settings(tmp_path)
    p = _product(st)
    a, w = _task(p, "artist", "look of composite weapons"), _task(p, "wizard", "cast a spell")
    r = LaneRunner()
    r.work["designer"] = lambda cwd, n: _commit(cwd, "public/look.css", "x\n")
    with caplog.at_level(logging.WARNING):
        c = Cycle(st, p, r, "c1"); c.run(["execute"]); c.store.close()
    assert _state(p, a)["role"] == "designer" and "designer" in r.calls            # the owner's task got its seat and ran
    assert any("role `artist` is the `designer` role" in h["note"] for h in _state(p, a)["history"])
    assert _state(p, w)["state"] == "ready" and any("wizard" in m and "no seat" in m for m in caplog.messages)
    s = Store(p.dir)
    assert any("wizard" in str(o.get("title")) for o in s.observations())          # the PO hears about it once
    s.close()


def test_task_new_takes_the_alias_and_refuses_an_unknown_role(tmp_path: Path, capsys):
    st = _settings(tmp_path)
    p = _product(st)
    ns = dict(product=p.slug, action="new", title="look", type="quality", problem=None, scope=None, acceptance=None, goal=None, priority="P1",
              ready=True, by="owner", id=None, to=None, note=None, force=False, wait_s=0)
    assert cli.cmd_task(st, argparse.Namespace(role="artist", **ns)) == 0
    tid = capsys.readouterr().out.strip().splitlines()[-1]
    assert _state(p, tid)["role"] == "designer"
    assert cli.cmd_task(st, argparse.Namespace(role="wizard", **ns)) == 2
    assert "wizard" in capsys.readouterr().out
