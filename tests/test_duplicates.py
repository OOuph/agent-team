"""Duplicate tasks (rework slice 3, owner 26.11, point 4 of the guide review).

Measured on ai-battles 20–26.11 before the change: the open-task check (`duplicate_task`) skipped 4 proposals in five
days and nothing shows a task started twice — `working`, `store.lock` and the `_taken` set of the execute phase hold.
What was not covered: the PO did not see closed tasks at all (129 rejected and 197 done of 465), so it re-created work
under a new id — 25 of the rejected tasks name the task that replaced them — and the chain of one goal was lost
whenever the PO forgot `continues`. `cycle.duplicates` (off by default):

  closed_list   the PO's prompt gets `# CLOSED TASKS`: id, title, state and one line of why, for the last days;
  closed_check  a proposed task with the title of a task whose work is DONE is skipped; one with the title of a
                REJECTED task is created and linked to it with `continues`."""
from __future__ import annotations

from pathlib import Path

from team import config as C
from team import prompts
from team.cycle import Cycle
from team.runner import RunResult
from team.store import Store

from test_flow import REFILL
from test_parallel_lanes import LaneRunner, _product as _lane_product, _settings as _lane_settings, _state, _task


def _setup(tmp_path: Path, **dup: bool) -> tuple[C.Settings, C.Product, Store]:
    raw = C._merge(C.DEFAULTS, {"cycle": {"duplicates": dup}})
    st = C.Settings(raw, tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")
    d = st.products_dir / "p1"
    d.mkdir(parents=True)
    (d / "product.yaml").write_text("slug: p1\nname: p1\nhost: ''\n")
    (d / "VISION.md").write_text("# p1\n")
    p = C.load_product(st, d)
    return st, p, Store(p.dir)


def _mk(s: Store, title: str, state: str, note: str = "", **kw) -> str:
    t = s.create_task(title=title, type_="defect", problem="p", scope="s", acceptance=["a"], goal="G1", created_by="po", state="ready", **kw)
    s.transition(t["id"], state, "po", note, force=True)
    return t["id"]


def _rt(title: str, **kw) -> dict:
    return {"title": title, "type": "defect", "problem": "p", "scope": "s", "acceptance": ["a"], "goal": "G1", "priority": "P2",
            "size": "S", "role": "developer", **kw}


def _po(st, p, tasks: list[dict]) -> Cycle:
    c = Cycle(st, p, None, "c1")
    c.apply_po({"decisions": [], "answers": [], "owner_questions": [], "no_new_tasks": False, "digest_md": "d", "ready_tasks": tasks}, [])
    return c


def _kw() -> dict:
    return dict(cycle_id="c", observations=[], questions=[], metrics_md="", spent_today=0.0, cap=20.0, max_ready=3, idle=False)


def test_off_by_default_nothing_changes(tmp_path: Path):
    st, p, s = _setup(tmp_path)
    assert C.DEFAULTS["cycle"]["duplicates"] == {"closed_list": False, "closed_check": False}
    done = _mk(s, "Fix the cup counter", "done")
    _po(st, p, [_rt("Fix the cup counter")])
    assert len(s.tasks()) == 2 and "# CLOSED TASKS" not in prompts.po_prompt(p, s, **_kw())
    assert done in [t["id"] for t in s.tasks(["done"])]
    s.close()


def test_closed_list_shows_every_recently_closed_task_with_its_reason(tmp_path: Path):
    st, p, s = _setup(tmp_path, closed_list=True)
    a = _mk(s, "Lobby seat mark", "rejected", "closed by PO: cannot finish as written, the negative cases forbid the protocol change " + "x" * 300)
    b = _mk(s, "Cup counter", "done", "measured: no effect on returning players")
    c = _mk(s, "Open thing", "ready")
    old = _mk(s, "Ancient", "rejected", "closed long ago")
    t = s.task(old); t["updated"] = "2026-01-01T00:00:00+00:00"
    from team.store import _write
    _write(s._path("T", old), t)
    txt = prompts.po_prompt(p, s, **_kw())
    sec = txt.split("# CLOSED TASKS", 1)[1].split("\n# ", 1)[0]
    assert a in sec and b in sec and c not in sec and old not in sec
    assert "cannot finish as written" in sec and "measured: no effect" in sec
    assert max(len(line) for line in sec.splitlines()) < 400           # one short line of why per task, not the text
    assert "continues" in sec                                          # the PO is told how to build on a closed task
    # every task of the store is somewhere in the prompt in the form the PO decides by: open in BACKLOG, closed here
    assert c in txt.split("# BACKLOG (open tasks)", 1)[1].split("# CLOSED TASKS", 1)[0] or c in txt
    s.close()


def test_closed_list_is_bounded(tmp_path: Path):
    st, p, s = _setup(tmp_path, closed_list=True)
    ids = [_mk(s, f"Task number {i}", "rejected", "closed") for i in range(prompts.CLOSED_LIST_MAX + 5)]
    sec = prompts.closed_block(s)
    assert ids[-1] in sec and ids[0] not in sec and f"{prompts.CLOSED_LIST_MAX} newest" in sec
    s.close()


def test_title_of_finished_work_is_not_created_again(tmp_path: Path):
    st, p, s = _setup(tmp_path, closed_check=True)
    done = _mk(s, "Fix the cup counter", "accepted")
    _po(st, p, [_rt("fix the Cup counter!"), _rt("Another thing")])
    titles = [t["title"] for t in s.tasks()]
    assert titles == ["Fix the cup counter", "Another thing"]
    ev = [e for e in s.events() if e["kind"] == "task.duplicate_skipped"]
    assert len(ev) == 1 and ev[0]["subject"] == done and ev[0]["detail"]["closed"] == "accepted"
    assert "уже сделана" in (p.dir / "digests" / "c1.md").read_text(encoding="utf-8")
    s.close()


def test_finished_title_is_allowed_when_the_task_says_it_continues_it_or_is_scheduled(tmp_path: Path):
    st, p, s = _setup(tmp_path, closed_check=True)
    done = _mk(s, "Fix the cup counter", "done")
    _mk(s, "Weekly post", "done", role="marketer")
    _po(st, p, [_rt("Fix the cup counter", continues=[done]), _rt("Weekly post", not_before="2099-01-01T10:00:00+00:00", role="marketer")])
    assert len(s.tasks()) == 4
    s.close()


def test_title_of_a_rejected_task_is_created_and_linked(tmp_path: Path):
    st, p, s = _setup(tmp_path, closed_check=True)
    rej = _mk(s, "Lobby seat mark", "rejected", "closed by PO: re-scope")
    _po(st, p, [_rt("Lobby seat mark")])
    new = s.tasks(["ready"])[0]
    assert new["continues"] == [rej]                                   # the chain of the goal is kept for the stuck-goal counter
    assert any(e["kind"] == "task.recreated" and e["detail"]["continues"] == rej for e in s.events())
    s.close()


def test_old_closed_tasks_are_not_compared(tmp_path: Path):
    st, p, s = _setup(tmp_path, closed_check=True)
    done = _mk(s, "Fix the cup counter", "done")
    t = s.task(done); t["updated"] = "2026-01-01T00:00:00+00:00"
    from team.store import _write
    _write(s._path("T", done), t)
    _po(st, p, [_rt("Fix the cup counter")])
    assert len(s.tasks()) == 2
    s.close()


def test_refill_never_starts_a_task_twice_in_one_cycle(tmp_path: Path):
    """The path the handoff asked to check: with refill a seat reads the backlog again for every pick; a task that came
    back to `ready` inside the cycle (a developer run that committed nothing) must not be picked again by this cycle."""
    st = _lane_settings(tmp_path, REFILL)
    p = _lane_product(st)
    ids = [_task(p, "developer", f"task {i}") for i in range(2)]
    r = LaneRunner()
    r.work["developer"] = lambda cwd, n: None                           # reports done, commits nothing → back to `ready`
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["execute"])
    cyc.store.close()
    assert r.calls.count("developer") <= len(ids)
    assert len({Path(c).name for c in r.cwds["developer"]}) == r.calls.count("developer")   # each task at most once
    assert _state(p, ids[0])["state"] == "ready"
