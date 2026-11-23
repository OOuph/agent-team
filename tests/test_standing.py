"""Standing directions (owner 21.11, team/standing.py): built-in defaults for the game profile, the due decision, the
task the dispatcher creates in the triage phase (once, with an event), the PO's `standing` task, the `team status`
line, the game designer's prompt for a standing task, onboarding. FakeRunner only: no model, no network."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from team import cli, config as C, gitops, onboard, prompts, standing
from team.cycle import Cycle
from team.runner import RunResult
from team.store import TASK_STATES, Store

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)
CATALOGUE = "# Mechanics catalogue\n\n## 2026-11-19 — ricochet\nessence: a shot bounces once\n"


def _settings(tmp_path: Path) -> C.Settings:
    raw = C._merge(C.DEFAULTS, {"cycle": {"review_after_min": 0, "persona_every_h": 0, "digest_hours_utc": [0]}})
    return C.Settings(raw, tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")


def _product(st: C.Settings, slug: str = "arena", profile: str = "game", stage: str = "M1", extra: dict | None = None, repo: bool = False) -> C.Product:
    d = st.products_dir / slug
    d.mkdir(parents=True)
    raw = {"slug": slug, "name": slug, "host": "", "profile": profile, "stage": stage,
           "repo": {"url": "", "branch": "main", "kind": "service" if profile == "game" else "static"}, **(extra or {})}
    if profile != "game":
        raw.pop("stage")
    (d / "product.yaml").write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    (d / "VISION.md").write_text(f"# {slug} — VISION\n", encoding="utf-8")
    p = C.load_product(st, d)
    if repo:
        r = gitops.init_local(p.repo_dir)
        (r / "README.md").write_text("game\n")
        (r / "design" / "ideas").mkdir(parents=True)
        (r / "design" / "ideas" / "mechanics.md").write_text(CATALOGUE)
        gitops.git(r, "add", "-A"); gitops.git(r, "commit", "-q", "-m", "init")
    return p


def _task(p: C.Product, sid: str | None, state: str, created_h_ago: float, by: str = "dispatcher") -> str:
    """A task of a direction as the store would hold it: given state, created `created_h_ago` hours before NOW."""
    s = Store(p.dir)
    t = s.create_task(title=f"task of {sid}", type_="design", problem="p", scope="s", acceptance=["a"], goal="g", created_by=by,
                      role="game-designer", state="ready")
    if sid:
        t["standing"] = sid
    t["state"] = state
    t["created"] = (NOW - timedelta(hours=created_h_ago)).isoformat(timespec="seconds")
    s.save_task(t)
    s.close()
    return t["id"]


def _due(p: C.Product, now: datetime = NOW) -> list[str]:
    s = Store(p.dir)
    try:
        return [d.id for d in standing.due(p, s, now)]
    finally:
        s.close()


class FakeRunner:
    """PO: no decisions (or the tasks given); game designer: returns the task's document."""

    def __init__(self, ready_tasks: list[dict] | None = None):
        self.calls: list[str] = []
        self.prompts: dict[str, str] = {}
        self.ready_tasks = ready_tasks or []

    def run(self, role, prompt, schema, *, max_usd, cwd, allowed_tools=None, env_extra=None, run_dir=None, run_id="", max_turns=None, **kw):
        self.calls.append(role)
        self.prompts[role] = prompt
        if role == "po":
            out = {"decisions": [], "answers": [], "owner_questions": [], "no_new_tasks": not self.ready_tasks, "digest_md": "ok",
                   "ready_tasks": self.ready_tasks}
        elif role == "game-designer":
            out = {"summary": "three new ideas, ricochet re-rated", "files": [{"path": "design/ideas/mechanics.md", "content": CATALOGUE + "\n## 2026-11-21 — magnet zone\n"}],
                   "proposed_tasks": [{"title": "Prototype: magnet zone behind a balance-table flag", "type": "feature", "role": "developer",
                                       "problem": "candidate", "acceptance": ["flag off by default; simulation report in the result"]}]}
        else:
            out = {"summary_md": "n/a", "observations": [], "task_effects": []}
        return RunResult(True, out, {"total_cost_usd": 0.1}, 0.1, 2, None, session_id="s", duration_s=1.0)


# ------------------------------------------------------------------ defaults

def test_game_profile_gets_two_built_in_directions_and_other_profiles_none(tmp_path: Path):
    st = _settings(tmp_path)
    game, site = _product(st, "arena"), _product(st, "site", profile="site")
    ds = {d["id"]: d for d in standing.defaults(game)}
    assert list(ds) == ["mechanics", "balance"]
    assert ds["mechanics"]["role"] == ds["balance"]["role"] == "game-designer" and ds["mechanics"]["type"] == ds["balance"]["type"] == "design"
    assert (ds["mechanics"]["every_h"], ds["balance"]["every_h"]) == (60, 24) and ds["mechanics"]["priority"] == ds["balance"]["priority"] == "P2"
    assert [d.id for d in standing.directions(game)] == ["mechanics", "balance"] and standing.load(game)[1] == []
    assert standing.defaults(site) == [] and standing.directions(site) == []
    s = Store(site.dir)
    assert standing.due(site, s, NOW) == [] and standing.status_lines(site, s, NOW) == [] and prompts.standing_block(site, s) == ""
    s.close()
    standing.defaults(game)[0]["title"] = "changed"                     # a copy: the built-in list is not mutated by a caller
    assert standing.GAME_DEFAULTS[0]["title"].startswith("Standing: mechanics catalogue")


def test_built_in_texts_are_generic_and_checkable_without_code_access():
    for d in standing.GAME_DEFAULTS:
        text = " ".join([d["title"], d["problem"], d["scope"], *d["acceptance"]]).lower()
        for word in ("car", "cars", "weapon", "weapons", "tank", "ai battles", "ship"):
            assert f" {word} " not in f" {text} ", (d["id"], word)          # units / options / levels, not one product's nouns
        assert not re.search(r"\.(ts|js|py|tsx)\b|\bsrc/|\bserver/|\bclient/", text), d["id"]   # the game designer has no code access
        assert "in the repository" in d["acceptance"][0]                    # the reviewer checks it in the checkout
    mech, bal = standing.GAME_DEFAULTS
    assert "design/ideas/mechanics.md" in mech["scope"] and mech["docs"] == ["design/ideas/mechanics.md"]
    assert all(k in " ".join(mech["acceptance"]) for k in ("counterplay", "S, M or L", "readability risk", "simulation", "at most 1 mechanic"))
    assert "No entry names a source-code file" in mech["acceptance"][-1] and "`proposed_tasks`" in mech["scope"]
    assert "design/balance/*.json" in bal["scope"] and "ONE lever" in bal["scope"] and bal["docs"] == ["design/reports/balance.md"]
    assert all(k in " ".join(bal["acceptance"]) for k in ("within thresholds", "exactly one changed lever", "before → after", "simulation check"))


# ------------------------------------------------------------------ the due decision

def test_never_created_means_due_now(tmp_path: Path):
    p = _product(_settings(tmp_path))
    assert _due(p) == ["mechanics", "balance"]


@pytest.mark.parametrize("state", [s for s in TASK_STATES if s not in standing.CLOSED_STATES])
def test_an_open_task_blocks_whatever_its_age(tmp_path: Path, state: str):
    p = _product(_settings(tmp_path))
    tid = _task(p, "balance", state, created_h_ago=500)
    assert _due(p) == ["mechanics"]
    s = Store(p.dir)
    st = {x.direction.id: x for x in standing.check(p, s, NOW)}["balance"]
    s.close()
    assert not st.due and st.open_task["id"] == tid and st.reason == f"open {tid} ({state})"


@pytest.mark.parametrize("state", standing.CLOSED_STATES)
def test_closed_and_elapsed_creates_closed_and_not_elapsed_waits(tmp_path: Path, state: str):
    p = _product(_settings(tmp_path))
    _task(p, "balance", state, created_h_ago=23.9)
    _task(p, "mechanics", state, created_h_ago=60)                      # exactly every_h → due
    assert _due(p) == ["mechanics"]
    assert _due(p, NOW + timedelta(minutes=5)) == ["mechanics"]         # 23 h 59 min: balance still waits
    assert _due(p, NOW + timedelta(minutes=6)) == ["mechanics", "balance"]
    s = Store(p.dir)
    st = {x.direction.id: x for x in standing.check(p, s, NOW)}["balance"]
    s.close()
    assert st.wait_h == pytest.approx(0.1) and st.next_due == NOW + timedelta(hours=0.1) and "next in 0.1 h" in st.reason


def test_the_newest_task_of_a_direction_sets_the_clock(tmp_path: Path):
    p = _product(_settings(tmp_path))
    _task(p, "balance", "done", created_h_ago=200)
    _task(p, "balance", "accepted", created_h_ago=3)                    # the latest one: 3 h < 24 h
    _task(p, None, "ready", created_h_ago=1)                            # a task without the field is nobody's
    assert _due(p) == ["mechanics"]


def test_a_hand_made_task_with_the_field_counts_as_the_directions_task(tmp_path: Path):
    p = _product(_settings(tmp_path))
    tid = _task(p, "mechanics", "in_progress", created_h_ago=2, by="owner")
    cyc = Cycle(p.st, p, None, "c1")
    assert cyc.create_standing_tasks() and [t["standing"] for t in cyc.store.tasks() if t["created_by"] == "dispatcher"] == ["balance"]
    assert [t["id"] for t in standing.tasks_of(cyc.store.tasks(), "mechanics")] == [tid]
    cyc.store.close()


def test_explicit_empty_list_switches_the_directions_off(tmp_path: Path):
    p = _product(_settings(tmp_path), extra={"standing": []})
    assert standing.directions(p) == [] and _due(p) == []
    cyc = Cycle(p.st, p, None, "c1")
    assert cyc.create_standing_tasks() == [] and cyc.store.tasks() == []
    cyc.store.close()


def test_explicit_list_overrides_and_inherits_built_in_fields(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st, extra={"standing": [{"id": "balance", "every_h": 12, "priority": "P1"},
                                         {"id": "rules-page", "role": "designer", "type": "quality", "every_h": 168, "title": "Rules page matches the game",
                                          "acceptance": ["the rules page names every unit of GAME-DESIGN.md"], "from_stage": "M2"}]})
    ds = {d.id: d for d in standing.directions(p)}
    assert list(ds) == ["balance", "rules-page"]                        # mechanics is not listed → off
    assert ds["balance"].every_h == 12 and ds["balance"].priority == "P1" and ds["balance"].role == "game-designer"
    assert ds["balance"].title == standing.builtin("balance")["title"] and len(ds["balance"].acceptance) == 5
    assert ds["rules-page"].cadence == "every 168 h" and ds["rules-page"].problem == "Rules page matches the game"
    _task(p, "balance", "done", created_h_ago=13)
    assert _due(p) == ["balance"]                                       # 13 h ≥ 12 h; rules-page waits for M2
    site = _product(st, "site", profile="site", extra={"standing": [{"id": "links", "role": "developer", "every_h": 24, "title": "Dead links",
                                                                      "acceptance": ["no 404 among internal links"]}]})
    assert [d.id for d in standing.directions(site)] == ["links"] and _due(site) == ["links"]   # the mechanism is not game-only


def test_bad_entries_are_skipped_and_reported_not_fatal(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st, extra={"standing": [{"id": "balance"}, {"role": "developer"}, {"id": "x", "role": "wizard", "every_h": 5, "title": "t", "acceptance": ["a"]},
                                         {"id": "y", "role": "developer", "every_h": 0, "title": "t", "acceptance": ["a"]},
                                         {"id": "z", "role": "developer", "every_h": 5, "title": "t"}, {"id": "balance", "every_h": 2}, "oops"]})
    ds, problems = standing.load(p)
    assert [d.id for d in ds] == ["balance"] and ds[0].every_h == 24 and len(problems) == 6
    assert any("has no `id`" in x for x in problems) and any("role 'wizard'" in x for x in problems) and any("every_h must be" in x for x in problems)
    assert any("non-empty `acceptance`" in x for x in problems) and any("listed twice" in x for x in problems)
    bad = _product(st, "bad", extra={"standing": "balance"})
    ds, problems = standing.load(bad)
    assert ds == [] and "must be a list" in problems[0]
    s = Store(bad.dir)
    assert standing.status_lines(bad, s, NOW)[0].startswith("standing config: ")
    s.close()


def test_stage_and_phase_gates_hold_a_direction_back(tmp_path: Path):
    st = _settings(tmp_path)
    m0 = _product(st, "m0", stage="M0")
    assert _due(m0) == []                                               # built-in directions start at M1: no playable core yet
    s = Store(m0.dir)
    assert standing.status_lines(m0, s, NOW) == ["standing mechanics: every 60 h, game-designer · waits for stage M1 (now M0)",
                                                 "standing balance: every 24 h, game-designer · waits for stage M1 (now M0)"]
    s.close()
    gated = _product(st, "gated", stage="M1", extra={"phase_gates": {"standing.mechanics": "M2"}})
    assert _due(gated) == ["balance"]
    allgated = _product(st, "allgated", stage="M1", extra={"phase_gates": {"standing": "M3"}})
    assert _due(allgated) == []
    early = _product(st, "early", stage="M0", extra={"standing": [{"id": "balance", "from_stage": "M0"}]})
    assert _due(early) == ["balance"]


# ------------------------------------------------------------------ the cycle creates the task

def test_cycle_creates_each_task_once_and_logs_the_event(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    r = FakeRunner()
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["triage"])
    tasks = cyc.store.tasks()
    assert [(t["standing"], t["state"], t["created_by"], t["role"], t["type"], t["priority"]) for t in tasks] == [
        ("mechanics", "ready", "dispatcher", "game-designer", "design", "P2"), ("balance", "ready", "dispatcher", "game-designer", "design", "P2")]
    assert tasks[0]["docs"] == ["design/ideas/mechanics.md"] and tasks[0]["history"][0]["note"] == "standing direction `mechanics`, every 60 h"
    ev = [e for e in cyc.store.events() if e["kind"] == "standing.created"]
    assert sorted((e["subject"], e["detail"]["direction"], e["actor"]) for e in ev) == [(tasks[0]["id"], "mechanics", "dispatcher"), (tasks[1]["id"], "balance", "dispatcher")]
    assert ev[0]["detail"]["cycle"] == "c1"
    # the PO ran in the same phase and saw the new tasks and the block
    po = r.prompts["po"]
    assert r.calls == ["po"] and "# STANDING DIRECTIONS (the dispatcher creates these tasks itself)" in po
    assert f"- standing balance: every 24 h, game-designer · open {tasks[1]['id']} (ready)" in po and "standing: mechanics" in po
    cyc.store.close()

    cyc = Cycle(st, p, FakeRunner(), "c2")                              # the next cycles: nothing new while the tasks are open
    cyc.run(["triage"])
    cyc.run(["triage"])
    assert len(cyc.store.tasks()) == 2 and len([e for e in cyc.store.events() if e["kind"] == "standing.created"]) == 2
    # the balance task is finished and a day passes → exactly one new balance task
    bal = cyc.store.task(tasks[1]["id"])
    bal["created"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat(timespec="seconds")
    cyc.store.save_task(bal)
    cyc.store.transition(bal["id"], "done", "dispatcher", "closed", force=True)
    assert cyc.create_standing_tasks() and cyc.create_standing_tasks() == []
    assert [t["standing"] for t in cyc.store.tasks()] == ["mechanics", "balance", "balance"]
    cyc.store.close()


def test_dry_run_stop_pause_and_onboarding_create_nothing(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st)
    cyc = Cycle(st, p, None, "c1", dry_run=True)
    cyc.run(["triage"])
    assert cyc.store.tasks() == [] and cyc.create_standing_tasks() == []
    cyc.store.close()

    p.stop_file.write_text("owner\n")
    cyc = Cycle(st, p, None, "c2")
    assert cyc.run(["triage"]).get("stopped") and cyc.create_standing_tasks() == [] and cyc.store.tasks() == []
    cyc.store.close()
    p.stop_file.unlink()

    paused = _product(st, "paused", extra={"paused": True})
    waiting = _product(st, "waiting", extra={"onboarding": {"state": "questions"}})
    for prod in (paused, waiting):
        cyc = Cycle(st, prod, None, "c1")
        assert cyc.create_standing_tasks() == [] and cyc.store.tasks() == []
        cyc.store.close()
    assert _due(paused) == ["mechanics", "balance"]                     # the decision itself is due; the cycle's guards hold it


def test_a_broken_config_does_not_cost_the_product_its_triage(tmp_path: Path, monkeypatch):
    st = _settings(tmp_path)
    p = _product(st)
    monkeypatch.setattr(standing, "due", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = FakeRunner()
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["triage"])
    assert r.calls == ["po"] and [e["detail"] for e in cyc.store.events() if e["kind"] == "standing.error"] == ["boom"]
    cyc.store.close()


def test_po_task_with_standing_is_the_directions_task_and_never_a_second_one(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st, extra={"standing": [{"id": "balance", "from_stage": "M3"}]})     # the dispatcher does not create it yet (stage M1)
    rt = {"title": "Balance: railgun over the band — one-lever patch", "type": "design", "problem": "OBS-0001", "scope": "design/balance/*.json",
          "acceptance": ["one lever"], "negative_cases": [], "goal": "M1", "priority": "P1", "size": "S", "role": "game-designer", "standing": "balance"}
    cyc = Cycle(st, p, None, "c1")
    cyc.apply_po({"ready_tasks": [rt, {**rt, "title": "Another balance task", "standing": "balance"},
                                  {**rt, "title": "Unknown direction", "standing": "nope"}], "digest_md": "d"}, [])
    tasks = cyc.store.tasks()
    assert [(t["title"], t.get("standing"), t["created_by"]) for t in tasks] == [
        ("Balance: railgun over the band — one-lever patch", "balance", "po"), ("Unknown direction", None, "po")]
    assert tasks[0]["docs"] == ["design/reports/balance.md"]
    assert [e["subject"] for e in cyc.store.events() if e["kind"] == "task.duplicate_skipped"] == [tasks[0]["id"]]
    assert "уже открыта" in (p.dir / "digests" / "c1.md").read_text(encoding="utf-8")
    cyc.store.close()


# ------------------------------------------------------------------ team status

def test_status_prints_one_line_per_direction(tmp_path: Path, monkeypatch, capsys):
    st = _settings(tmp_path)
    game = _product(st, "arena")
    _product(st, "site", profile="site")
    monkeypatch.setattr(cli.C, "load", lambda *a, **k: st)
    assert cli.main(["status"]) == 0
    game_block, site_block = capsys.readouterr().out.split("== site")
    assert "   standing mechanics: every 60 h, game-designer · due now (never created)" in game_block
    assert "   standing balance: every 24 h, game-designer · due now (never created)" in game_block
    assert "standing" not in site_block

    s = Store(game.dir)
    t = standing.create_task(game, s, standing.directions(game)[1])
    s.close()
    assert cli.main(["status", "arena"]) == 0
    assert f"   standing balance: every 24 h, game-designer · open {t['id']} (ready)" in capsys.readouterr().out
    s = Store(game.dir); s.transition(t["id"], "rejected", "po", "closed", force=True); s.close()
    assert cli.main(["status", "arena"]) == 0
    assert f"   standing balance: every 24 h, game-designer · next in 24 h (last {t['id']} created 0.0 h ago)" in capsys.readouterr().out


# ------------------------------------------------------------------ the game designer runs a standing task

def test_standing_task_gives_the_game_designer_its_document_whole(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st, repo=True, extra={"standing": [{"id": "mechanics"}]})
    r = FakeRunner()
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["triage", "execute"])
    assert r.calls == ["po", "game-designer"]
    gp = r.prompts["game-designer"]
    assert "standing: mechanics" in gp and "# DOCUMENTS OF THIS TASK (return each one COMPLETE in `files`)" in gp
    assert "--- design/ideas/mechanics.md ---\n# Mechanics catalogue" in gp and "## 2026-11-19 — ricochet" in gp
    assert "# SIMULATION REPORT\n(none: the repository has no `design/reports/simulation.md`" in gp
    t = cyc.store.tasks()[0]
    assert t["state"] == "released" and t["repo_only"] is True and t["results"][-1]["changes"] == ["design/ideas/mechanics.md"]
    assert "magnet zone" in (p.repo_dir / "design" / "ideas" / "mechanics.md").read_text()
    assert [o["title"] for o in cyc.store.observations("new")] == ["Prototype: magnet zone behind a balance-table flag"]
    assert cyc.create_standing_tasks() == []                             # released = still open: no second task
    cyc.store.close()


def test_simulation_report_and_missing_document_in_the_prompt(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st, repo=True, extra={"balance": {"sim_report": "sim/out.md"}})
    (p.repo_dir / "sim").mkdir(); (p.repo_dir / "sim" / "out.md").write_text("# sim\n10000 rounds: railgun 0.61\n")
    s = Store(p.dir)
    t = standing.create_task(p, s, next(d for d in standing.directions(p) if d.id == "balance"))
    gp = prompts.game_designer_prompt(p, t, p.repo_dir, s)
    assert "# SIMULATION REPORT (`sim/out.md`, written by the repository's own simulation — read it, do not return it)\n# sim\n10000 rounds" in gp
    assert "--- design/reports/balance.md ---\n(does not exist yet — this task creates it)" in gp
    plain = s.create_task(title="GDD", type_="design", problem="p", scope="s", acceptance=["a"], goal="g", created_by="po", role="game-designer")
    (p.repo_dir / "sim" / "out.md").unlink()
    assert "SIMULATION REPORT" not in prompts.game_designer_prompt(p, plain, p.repo_dir, s)      # an ordinary design task is unchanged
    assert prompts.task_docs({"docs": ["a.md", "../x", "/etc/passwd", ".git/config"]}) == ["a.md"]
    big = p.repo_dir / "design" / "ideas" / "mechanics.md"
    big.write_text("x" * (prompts.TASK_DOC_CHARS + 10))
    assert prompts.truncated_design_docs(p, p.repo_dir, {"docs": ["design/ideas/mechanics.md"]}) == {"design/ideas/mechanics.md"}
    assert prompts.truncated_design_docs(p, p.repo_dir) == set()
    s.close()


# ------------------------------------------------------------------ onboarding, cards, schema

def test_game_vision_gets_the_standing_section_and_the_backlog_does_not_duplicate(tmp_path: Path):
    template = (ROOT / "team" / "templates" / "VISION.game.template.md").read_text(encoding="utf-8")
    assert f"## {onboard.VISION_STANDING_HEADING}" in template
    assert f"раз в {standing.MECHANICS_EVERY_H} ч" in template and f"раз в {standing.BALANCE_EVERY_H} ч" in template   # the template follows the constants
    assert standing.MECHANICS_CATALOGUE in template and standing.BALANCE_REPORT in template and "флагом в таблицах баланса" in template
    prompt = onboard.plan_prompt("fleets", "brief", {"questions": [], "accounts": []}, {"slug": "fleets"})
    assert "## STANDING DIRECTIONS (the dispatcher creates these tasks itself — keep them OUT of the backlog)" in prompt
    assert "`mechanics` — роль game-designer, раз в 60 ч, с этапа M1" in prompt and "`balance` — роль game-designer, раз в 24 ч" in prompt
    assert "STANDING DIRECTIONS" not in onboard.questions_prompt("fleets", "brief", {"slug": "fleets"}).split("## VISION TEMPLATE")[0]

    st = _settings(tmp_path)
    brief = tmp_path / "brief.md"; brief.write_text("Пошаговая тактика 1 на 1.\n", encoding="utf-8")
    onboard.start(st, "fleets", brief, dry_run=True)
    res = onboard.apply_answers(st, "fleets", dry_run=True)
    d = st.products_dir / "fleets"
    vision = (d / "VISION.md").read_text(encoding="utf-8")
    assert vision.count(f"## {onboard.VISION_STANDING_HEADING}") == 1 and "| `balance`" in vision
    s = Store(d)
    assert not any(t.get("standing") for t in s.tasks()) and len(s.tasks()) == len(res["tasks"])
    assert not any(w in t["title"].lower() for t in s.tasks() for w in ("механик", "автобаланс", "mechanics catalogue", "balance reading"))
    p = C.load_product(st, d)
    assert standing.due(p, s, NOW) == []                                # onboarded at M0: the directions start at M1
    s.close()

    # a plan whose VISION left the section out gets the default one appended
    d2 = tmp_path / "products" / "bare"; d2.mkdir(parents=True)
    onboard.apply_plan(d2, {"slug": "bare", "onboarding": {}}, {"vision_md": "# Bare — VISION\n", "product": {"profile": "game", "stage": "M0"}, "backlog": [], "decisions": []},
                       {"accounts": []})
    v2 = (d2 / "VISION.md").read_text(encoding="utf-8")
    assert v2.startswith("# Bare — VISION\n\n## Постоянные направления\n") and "`mechanics` — роль game-designer, раз в 60 ч" in v2
    d3 = tmp_path / "products" / "plain"; d3.mkdir(parents=True)
    onboard.apply_plan(d3, {"slug": "plain", "onboarding": {}}, {"vision_md": "# Plain\n", "product": {"profile": "site", "stage": "M0"}, "backlog": [], "decisions": []},
                       {"accounts": []})
    assert (d3 / "VISION.md").read_text(encoding="utf-8") == "# Plain\n"


def test_cards_and_schema_name_the_directions_and_the_mechanic_test_path():
    import json
    roles = ROOT / "team" / "roles"
    gd = (roles / "game-designer.md").read_text()
    assert "## Standing directions" in gd and "design/ideas/mechanics.md" in gd and "ONE lever" in gd and "never name a source file" in gd
    po = (roles / "po.md").read_text()
    assert "**Standing directions**" in po and "never close a standing task as a duplicate" in po and "At most ONE new mechanic in work at a time" in po
    assert "A `balance` observation becomes a game-designer task at once" in po and "`standing: balance`" in po
    for card in ("game-designer.md", "po.md", "tester.md", "developer.md"):
        assert "flag in the balance tables" in (roles / card).read_text(), card           # the path: catalogue → flagged prototype → simulation → play-test → default
    assert "## A new mechanic" in (roles / "tester.md").read_text() and "simulation report" in (roles / "tester.md").read_text()
    assert "## Game products: the Balance table" in (roles / "analyst.md").read_text()
    assert "«Постоянные направления»" in (roles / "onboarding.md").read_text()
    schema = json.loads((ROOT / "team" / "schemas" / "po-decision.schema.json").read_text())
    assert schema["properties"]["ready_tasks"]["items"]["properties"]["standing"]["type"] == "string"
    scheme = (ROOT / "SCHEME.md").read_text(encoding="utf-8")
    assert "Постоянные направления" in scheme and "standing.created" in scheme and "win_rate_band" in scheme


def test_game_designer_gets_reports_named_by_the_task_and_standing_docs_always(tmp_path: Path):
    st = _settings(tmp_path)
    p = _product(st, repo=True)
    (p.repo_dir / "reports").mkdir()
    (p.repo_dir / "reports" / "stat-weights.md").write_text("# weights\nrailgun 1.7\n")
    s = Store(p.dir)
    t = s.create_task(title="Read reports/stat-weights.md", type_="design", problem="see reports/stat-weights.md and reports/missing.json",
                      scope="s", acceptance=["a"], goal="g", created_by="po", role="game-designer")
    gp = prompts.game_designer_prompt(p, t, p.repo_dir, s)
    assert "# REPORTS NAMED BY THE TASK" in gp and "--- reports/stat-weights.md ---\n# weights\nrailgun 1.7" in gp
    assert "--- reports/missing.json ---\n(does not exist in the repository)" in gp
    assert prompts.task_reports({"problem": "see ../reports/x.md and reports/../../etc.md"}) == []
    # a hand-made standing task without `docs` still carries the direction's documents
    m = s.create_task(title="Mechanics", type_="design", problem="p", scope="s", acceptance=["a"], goal="g", created_by="human", role="game-designer")
    m["standing"] = "mechanics"
    s.save_task(m)
    mp = prompts.game_designer_prompt(p, m, p.repo_dir, s)
    assert "# DOCUMENTS OF THIS TASK" in mp and "--- design/ideas/mechanics.md ---" in mp
