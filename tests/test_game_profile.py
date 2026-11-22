"""Game profile, slices 2–3 (owner 20.11): phase gates by stage, service-repository prompt, roles designer and
game-designer in the cycle, `milestone` → `feature` from M2, pending roles wait without a lease. FakeRunner only."""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from team import config as C
from team import gitops, hypothesis, prompts
from team.cycle import Cycle, schema
from team.runner import RunResult, load_roles
from team.store import Store

ROLES_DIR = Path(__file__).resolve().parent.parent / "team" / "roles"
GDD = "# Два флота — GAME-DESIGN\n\n## Core loop\nход = перемещение + выстрел\n\n## Rules\n…\n"
SHIPS = json.dumps({"ships": [{"name": "scout", "move": 3, "range": 1, "hp": 1}]})


def _settings(tmp_path: Path) -> C.Settings:
    raw = C._merge(C.DEFAULTS, {"cycle": {"review_after_min": 0, "persona_every_h": 2, "digest_hours_utc": [0],
                                          "execute_slots": {"developer": 1, "marketer": 1, "analyst": 1}}})
    return C.Settings(raw, tmp_path, tmp_path / "products", tmp_path / "data", tmp_path / "data" / "repos")


def _game(st: C.Settings, slug: str = "fleets", stage: str = "M0", repo: bool = True, gdd: bool = False) -> C.Product:
    d = st.products_dir / slug
    d.mkdir(parents=True)
    (d / "product.yaml").write_text(
        f"slug: {slug}\nname: Fleets\nhost: ''\nprofile: game\nstage: {stage}\n"
        "repo:\n  url: ''\n  branch: main\n  kind: service\n  run_cmd: npm run start\n  test_cmd: ''\n  health_path: /healthz\n  port: 8080\n"
        "deploy:\n  mode: pending\nphase_gates:\n  marketing: M2\n  seo: M1\n  persona: M1\n  geo: M2\n  voice: M2\n"
        "channels:\n  telegram: '@fleets'\n", encoding="utf-8")
    (d / "VISION.md").write_text("# Fleets — VISION\n\n| M0 | двое играют | … |\n", encoding="utf-8")
    (d / "scenarios").mkdir(); (d / "scenarios" / "s1.md").write_text("---\nid: s1\npersona: p1\n---\nplay\n")
    (d / "personas").mkdir(); (d / "personas" / "p1.md").write_text("---\nid: p1\n---\nplayer\n")
    p = C.load_product(st, d)
    if repo:
        r = gitops.init_local(p.repo_dir)
        (r / "README.md").write_text("game server: npm run start\n")
        (r / "PROTOCOL.md").write_text("join · move · state · error\n")
        if gdd:
            (r / "GAME-DESIGN.md").write_text(GDD)
        gitops.git(r, "add", "-A"); gitops.git(r, "commit", "-q", "-m", "init")
    return p


class FakeRunner:
    def __init__(self, gd_files: bool = True):
        self.calls: list[str] = []
        self.prompts: dict[str, str] = {}
        self.gd_files = gd_files

    def run(self, role, prompt, schema, *, max_usd, cwd, allowed_tools=None, env_extra=None, run_dir=None, run_id="", max_turns=None, **kw):
        self.calls.append(role)
        self.prompts[role] = prompt
        if role in ("developer", "designer"):
            f = Path(cwd) / ("DESIGN.md" if role == "designer" else "src.ts")
            f.write_text(f"{role} work\n")
            gitops.git(Path(cwd), "add", "-A"); gitops.git(Path(cwd), "commit", "-q", "-m", f"{role}: work")
            out = {"status": "done", "summary": f"{role} done", "changes": [f.name], "how_to_verify": [f.name],
                   "self_check": [{"criterion": "file", "met": True, "evidence": "exists"}]}
        elif role == "game-designer":
            out = {"summary": "v1 of the rules", "files": ([{"path": "GAME-DESIGN.md", "content": GDD}, {"path": "design/balance/ships.json", "content": SHIPS}]
                                                          if self.gd_files else []),
                   "balance_changes": [{"what": "scout range 1", "why": "brief", "expected_effect": "positional play", "read_in": "finished rounds share after 20 rounds"}],
                   "proposed_tasks": [{"title": "Implement the move+shoot turn", "type": "milestone", "role": "developer", "problem": "core", "acceptance": ["two tabs see the move"]}],
                   "open_questions": [{"question": "Allow 3-player rounds?", "why_owner": "changes the genre"}]}
        elif role == "reviewer":
            out = {"verdict": "pass", "steps": [], "criteria": [{"criterion": "file", "met": True, "evidence": "read"}], "summary": "ok. fine."}
        elif role == "po":
            out = {"decisions": [], "answers": [], "owner_questions": [], "no_new_tasks": True, "digest_md": "ok", "ready_tasks": []}
        else:
            out = {"summary_md": "n/a", "observations": [], "task_effects": []}
        return RunResult(True, out, {"total_cost_usd": 0.1}, 0.1, 2, None, session_id="s", duration_s=1.0)


# ------------------------------------------------------------------ config: stages and gates

def test_stage_order_and_gates(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st, stage="M0", repo=False)
    assert p.profile == "game" and p.repo_kind == "service" and p.port == 8080 and p.health_path == "/healthz"
    assert not p.gate_open("marketing") and not p.gate_open("seo") and not p.gate_open("persona") and p.gate_open("review")
    p.raw["stage"] = "M1"
    assert p.gate_open("seo") and p.gate_open("persona") and not p.gate_open("marketing")
    p.raw["stage"] = "M2"
    assert p.gate_open("marketing") and p.gate_open("voice")
    p.raw["stage"] = "M9"                                            # unknown stage: sorts after the known ones, opens everything
    assert p.gate_open("marketing")
    assert C.stage_index("") == -1 and C.stage_index("m1") == 1


def test_site_product_has_no_gates(tmp_path: Path):
    st = _settings(tmp_path)
    d = st.products_dir / "site"; d.mkdir(parents=True)
    (d / "product.yaml").write_text("slug: site\nhost: ''\nrepo:\n  url: ''\n")
    p = C.load_product(st, d)
    assert p.profile == "site" and p.repo_kind == "static" and p.gate_open("marketing") and p.stage == "M0"


# ------------------------------------------------------------------ cycle: gated phases, PO prompt

def test_gated_phases_do_not_run_before_their_stage(tmp_path: Path, monkeypatch):
    st = _settings(tmp_path)
    p = _game(st, repo=False)
    (p.dir / "metrics").mkdir()
    p.raw["geo_queries"] = ["who plays fleets"]
    r = FakeRunner()
    import team.seocheck as seocheck

    def _no_network(*a, **k):
        raise AssertionError("seocheck.report must not run before its stage")
    monkeypatch.setattr(seocheck, "report", _no_network)
    p.raw["site_base"] = "https://fleets.example.test"            # a site exists, yet the gates are closed at M0
    cyc = Cycle(st, p, r, "c1")                                     # no persona run yet → personas would fire without the gate
    cyc.run(["collect", "geo", "voice"])
    assert "persona" not in r.calls and "geo-probe" not in r.calls and "voice" not in r.calls
    assert not [e for e in cyc.store.events() if e["kind"] == "phase.error"]
    cyc.store.close()
    # at M2 (seo kept closed so no network) the same cycle runs personas and the geo probe
    p.raw["stage"] = "M2"
    p.raw["phase_gates"]["seo"] = "M3"
    Cycle(st, p, None, "c2").run(["digest"])                        # one more cycle: the schedule is by the clock, not by the count
    r2 = FakeRunner()
    cyc = Cycle(st, p, r2, "c3")
    cyc.run(["collect", "geo"])
    assert "persona" in r2.calls and "geo-probe" in r2.calls
    cyc.store.close()


def test_po_prompt_has_game_block_and_marketing_off(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st, gdd=True)
    s = Store(p.dir)
    txt = prompts.po_prompt(p, s, cycle_id="c", observations=[], questions=[], metrics_md="", spent_today=0.0, cap=20.0, max_ready=3, idle=True)
    s.close()
    assert "# GAME PROFILE" in txt and "Stage `M0`" in txt and "marketing (from M2)" in txt
    assert "## GAME-DESIGN.md outline" in txt and "## Core loop" in txt
    assert "MARKETING — OFF until stage M2" in txt and "MARKETING PLAN" not in txt
    p.raw["stage"] = "M2"
    s = Store(p.dir)
    txt2 = prompts.po_prompt(p, s, cycle_id="c", observations=[], questions=[], metrics_md="", spent_today=0.0, cap=20.0, max_ready=3, idle=True)
    s.close()
    assert "MARKETING PLAN — NONE" in txt2


def test_developer_prompt_service_block(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st, gdd=True)
    t = {"id": "T-0001", "title": "rooms", "type": "milestone", "state": "ready", "problem": "x", "scope": "src/", "acceptance": ["a"]}
    txt = prompts.developer_prompt(p, t, p.repo_dir, "task/T-0001", [], None)
    assert "# SERVICE REPOSITORY" in txt and "npm run start" in txt and "/healthz" in txt and "--- PROTOCOL.md ---" in txt and "--- GAME-DESIGN.md ---" in txt
    assert "You are the `developer` role" in txt and "no live site yet" in txt
    d = prompts.developer_prompt(p, t, p.repo_dir, "task/T-0001", [], None, role="designer")
    assert "You are the designer" in d and "You are the `designer` role" in d
    # a static site product keeps the old prompt
    sd = st.products_dir / "site"; sd.mkdir()
    (sd / "product.yaml").write_text("slug: site\nhost: s.example.test\nrepo:\n  url: ''\n")
    sp = C.load_product(st, sd)
    assert "SERVICE REPOSITORY" not in prompts.developer_prompt(sp, t, tmp_path, "task/T-0001", [], None)


# ------------------------------------------------------------------ cycle: new roles in execute

def test_designer_task_runs_like_a_developer_and_is_reviewed_in_the_checkout(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st)
    s = Store(p.dir)
    t = s.create_task(title="DESIGN.md v1", type_="design", problem="no design", scope="DESIGN.md", acceptance=["DESIGN.md exists"],
                      goal="M0", created_by="po", role="designer", state="ready")
    s.close()
    r = FakeRunner()
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["execute"])
    cyc.store.close()
    assert r.calls == ["designer"]
    s = Store(p.dir)
    t = s.task(t["id"])
    assert t["state"] == "released" and t.get("repo_only") is not True   # repo_only is set at review time
    assert (p.repo_dir / "DESIGN.md").exists()                            # merged into main of the local repo
    s.close()
    cyc = Cycle(st, p, r, "c2")
    cyc.run(["review"])
    cyc.store.close()
    s = Store(p.dir)
    t = s.task(t["id"])
    assert t["repo_only"] is True and t["state"] == "accepted"            # no site_base → read-only checkout review → pass
    s.close()
    assert "READ-ONLY CHECKOUT" in r.prompts["reviewer"]
    runs = list((st.data_dir / "runs" / "fleets" / "c1").glob("designer-*"))
    assert len(runs) == 1


def test_game_designer_writes_gdd_into_the_repo_and_proposes(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st)
    s = Store(p.dir)
    t = s.create_task(title="GAME-DESIGN.md v1", type_="design", problem="no rules", scope="GAME-DESIGN.md, design/balance/ships.json",
                      acceptance=["GAME-DESIGN.md has Core loop"], goal="M0", created_by="po", role="game-designer", state="ready")
    s.close()
    r = FakeRunner()
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["execute"])
    cyc.store.close()
    assert r.calls == ["game-designer"]
    assert "# CURRENT GAME-DESIGN.md" in r.prompts["game-designer"] and "(none yet — write v1)" in r.prompts["game-designer"]
    assert (p.repo_dir / "GAME-DESIGN.md").read_text() == GDD and json.loads((p.repo_dir / "design" / "balance" / "ships.json").read_text())["ships"]
    assert gitops.git(p.repo_dir, "branch", "--show-current").strip() == "main"
    s = Store(p.dir)
    t = s.task(t["id"])
    assert t["state"] == "released" and t["repo_only"] is True and t["results"][-1]["changes"] == ["GAME-DESIGN.md", "design/balance/ships.json"]
    assert t["results"][-1]["balance_changes"][0]["read_in"].startswith("finished rounds")
    obs = s.observations("new")
    assert len(obs) == 1 and obs[0]["source"] == "game-designer" and obs[0]["title"] == "Implement the move+shoot turn"
    qs = s.questions(["open"])
    assert len(qs) == 1 and qs[0]["from"] == "game-designer" and qs[0]["to"] == "owner" and "3-player" in qs[0]["question"]
    s.close()
    # the second run sees the current document
    s = Store(p.dir)
    t2 = s.create_task(title="balance pass", type_="design", problem="x", scope="design/balance/ships.json", acceptance=["a"], goal="M0",
                       created_by="po", role="game-designer", state="ready")
    s.close()
    r2 = FakeRunner()
    cyc = Cycle(st, p, r2, "c2")
    cyc.run(["execute"])
    cyc.store.close()
    gp = r2.prompts["game-designer"]
    assert "## Core loop" in gp and "--- design/balance/ships.json ---" in gp


def test_game_designer_without_repo_url_starts_the_local_repository(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st, repo=False)
    s = Store(p.dir)
    t = s.create_task(title="GDD", type_="design", problem="x", scope="GAME-DESIGN.md", acceptance=["a"], goal="M0", created_by="po", role="game-designer", state="ready")
    s.close()
    cyc = Cycle(st, p, FakeRunner(), "c1")
    cyc.run(["execute"])
    cyc.store.close()
    assert (p.repo_dir / "GAME-DESIGN.md").read_text() == GDD and not (p.dir / "GAME-DESIGN.md").exists()
    s = Store(p.dir)
    assert s.task(t["id"])["state"] == "released"
    assert [e["kind"] for e in s.events() if e["kind"] == "repo.local_init"] == ["repo.local_init"]
    s.close()
    assert "## Core loop" in prompts.game_block(p)                       # the PO sees the outline from the repository


def test_game_block_falls_back_to_the_product_folder(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st, repo=False)
    (p.dir / "GAME-DESIGN.md").write_text(GDD)
    assert "## Core loop" in prompts.game_block(p)


def test_game_designer_without_files_blocks_with_a_po_question(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st)
    s = Store(p.dir)
    t = s.create_task(title="GDD", type_="design", problem="x", scope="GAME-DESIGN.md", acceptance=["a"], goal="M0", created_by="po", role="game-designer", state="ready")
    s.close()
    cyc = Cycle(st, p, FakeRunner(gd_files=False), "c1")
    cyc.run(["execute"])
    cyc.store.close()
    s = Store(p.dir)
    t = s.task(t["id"])
    assert t["state"] == "blocked"
    assert any(q["from"] == "game-designer" and q["to"] == "po" for q in s.questions(["open"]))
    s.close()


def test_unsafe_paths_from_the_game_designer_are_dropped(tmp_path: Path):
    assert prompts.safe_rel_path("GAME-DESIGN.md") and prompts.safe_rel_path("design/balance/x.json")
    assert not prompts.safe_rel_path("../x.md") and not prompts.safe_rel_path("/etc/passwd") and not prompts.safe_rel_path(".git/config") and not prompts.safe_rel_path("")


def test_pending_role_waits_without_a_lease_and_slots_include_design_roles(tmp_path: Path, monkeypatch):
    """A role without an execute path waits in `ready` with one event. Since slice 6 every game role executes, so the
    mechanism is checked with PENDING_ROLES patched back to the slice-3 state."""
    import team.cycle as cycle_mod
    monkeypatch.setattr(cycle_mod, "PENDING_ROLES", ("tester",))
    st = _settings(tmp_path)
    p = _game(st)
    s = Store(p.dir)
    t = s.create_task(title="load test", type_="quality", problem="x", scope="tests/load", acceptance=["a"], goal="M1", created_by="po", role="tester", state="ready")
    s.close()
    r = FakeRunner()
    cyc = Cycle(st, p, r, "c1")
    assert cyc.execute_slots() == {"developer": 1, "marketer": 1, "analyst": 1, "designer": 1, "game-designer": 1, "tester": 1}
    cyc.run(["execute"])
    cyc.store.close()
    assert r.calls == []
    s = Store(p.dir)
    t = s.task(t["id"])
    assert t["state"] == "ready" and t["role_pending_noted"] is True and t.get("assignee_run") is None
    assert [e["kind"] for e in s.events() if e["kind"] == "task.role_not_available"] == ["task.role_not_available"]
    s.close()


def test_milestone_becomes_feature_from_m2(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st, stage="M0", repo=False)
    cyc = Cycle(st, p, None, "c1")
    assert cyc.task_type_for_stage("milestone") == "milestone" and cyc.task_type_for_stage("defect") == "defect"
    p.raw["stage"] = "M2"
    assert cyc.task_type_for_stage("milestone") == "feature"
    cyc.store.close()
    assert not hypothesis.applies({"type": "milestone", "created_by": "po"}) and hypothesis.applies({"type": "feature", "created_by": "po"})


# ------------------------------------------------------------------ definitions

def test_cards_schemas_and_models():
    roles = load_roles(ROLES_DIR)
    assert roles["designer"]["_tools"] == ["Bash", "Read", "Edit", "Write", "Glob", "Grep"] and roles["game-designer"]["_tools"] == []
    assert roles["game-designer"]["model"] == "opus"
    rt = schema("po-decision")["properties"]["ready_tasks"]["items"]["properties"]
    assert {"designer", "game-designer"} <= set(rt["role"]["enum"]) and {"milestone", "design"} <= set(rt["type"]["enum"])
    ds = schema("design-spec")
    jsonschema.validate({"summary": "s", "files": [{"path": "GAME-DESIGN.md", "content": GDD}]}, ds)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"summary": "s"}, ds)
    assert C.DEFAULTS["claude"]["models"]["game-designer"] == "opus" and C.DEFAULTS["claude"]["models"]["designer"] == "sonnet"
    assert "Game profile" in (ROLES_DIR / "po.md").read_text(encoding="utf-8") and "SERVICE REPOSITORY" in (ROLES_DIR / "developer.md").read_text(encoding="utf-8")


def test_game_without_repo_url_starts_a_local_repository(tmp_path: Path):
    st = _settings(tmp_path)
    p = _game(st, repo=False)
    s = Store(p.dir)
    t = s.create_task(title="scaffold", type_="ops", problem="x", scope="src/", acceptance=["a"], goal="M0", created_by="po", role="developer", state="ready")
    s.close()
    r = FakeRunner()
    cyc = Cycle(st, p, r, "c1")
    cyc.run(["execute"])
    cyc.store.close()
    assert r.calls == ["developer"] and (p.repo_dir / ".git").exists() and (p.repo_dir / "src.ts").exists()
    s = Store(p.dir)
    assert s.task(t["id"])["state"] == "released"
    obs = [o for o in s.observations("new") if o["source"] == "dispatcher"]
    assert len(obs) == 1 and "repo.url" in obs[0]["title"]
    assert [e["kind"] for e in s.events() if e["kind"] == "repo.local_init"] == ["repo.local_init"]
    s.close()
    # a site product without a repository is still blocked, as before
    sd = st.products_dir / "site"; sd.mkdir()
    (sd / "product.yaml").write_text("slug: site\nhost: ''\nrepo:\n  url: ''\n")
    (sd / "VISION.md").write_text("# site\n")
    sp = C.load_product(st, sd)
    s = Store(sp.dir)
    t2 = s.create_task(title="x", type_="defect", problem="x", scope="public/", acceptance=["a"], goal="G1", created_by="po", role="developer", state="ready")
    s.close()
    cyc = Cycle(st, sp, FakeRunner(), "c1")
    cyc.run(["execute"])
    cyc.store.close()
    s = Store(sp.dir)
    assert s.task(t2["id"])["state"] == "blocked"
    s.close()


def test_game_designer_prompt_carries_the_whole_long_document(tmp_path: Path):
    """F18 (ai-battles 20.11): GAME-DESIGN.md of 20 000 chars and a 4 500-char balance.json travel whole — the role returns
    complete files, so a tail would make it delete the head. The PO's outline lists the headings of the whole document."""
    st = _settings(tmp_path)
    p = _game(st)
    repo = p.repo_dir
    repo.mkdir(parents=True, exist_ok=True)
    head = "# AI Battles — GAME-DESIGN\n\n## 0. Research\nHEAD-MARKER\n"
    body = "".join(f"## {i}. Section {i}\n" + "текст раздела " * 120 + "\n" for i in range(1, 13))
    (repo / prompts.GDD_FILE).write_text(head + body + "## 13. Open questions\nTAIL-MARKER\n", encoding="utf-8")
    assert len((repo / prompts.GDD_FILE).read_text(encoding="utf-8")) > 12000
    (repo / prompts.BALANCE_DIR).mkdir(parents=True)
    table = json.dumps({"stats": {f"s{i}": {"cost_per_level": i, "note": "x" * 60} for i in range(60)}, "last": "TABLE-TAIL"})
    assert len(table) > 3000
    (repo / prompts.BALANCE_DIR / "balance.json").write_text(table, encoding="utf-8")
    store = Store(p.dir)
    try:
        prompt = prompts.game_designer_prompt(p, {"id": "T-0001", "title": "t", "role": "game-designer"}, repo, store)
    finally:
        store.close()
    assert "HEAD-MARKER" in prompt and "TAIL-MARKER" in prompt and "TABLE-TAIL" in prompt and '"s0"' in prompt
    assert "TRUNCATED" not in prompt and prompts.truncated_design_docs(p, repo) == set()
    block = prompts.game_block(p, repo)
    assert "0. Research" in block and "13. Open questions" in block


def test_truncated_design_document_is_marked_and_never_overwritten(tmp_path: Path):
    """A document over the limit (`design.gdd_chars` in product.yaml) is shown as a marked tail and reported as partial,
    so exec_game_designer drops a rewrite of it instead of committing a document without its head."""
    st = _settings(tmp_path)
    p = _game(st)
    p.raw["design"] = {"gdd_chars": 500, "table_chars": 200}
    repo = p.repo_dir
    repo.mkdir(parents=True, exist_ok=True)
    (repo / prompts.GDD_FILE).write_text("# HEAD-MARKER\n" + "x" * 2000 + "\nTAIL-MARKER\n", encoding="utf-8")
    (repo / prompts.BALANCE_DIR).mkdir(parents=True)
    (repo / prompts.BALANCE_DIR / "small.json").write_text("{}", encoding="utf-8")
    store = Store(p.dir)
    try:
        prompt = prompts.game_designer_prompt(p, {"id": "T-0002", "title": "t", "role": "game-designer"}, repo, store)
    finally:
        store.close()
    assert "TRUNCATED: only the last 500" in prompt and "TAIL-MARKER" in prompt and "HEAD-MARKER" not in prompt
    assert prompts.truncated_design_docs(p, repo) == {prompts.GDD_FILE}
